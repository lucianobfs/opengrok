#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1"]
# ///
"""Offline unit tests for tools/claude-shim.py.

Run: uv run --with 'anthropic>=1' python3 tools/test-claude-shim.py
     python3 tools/test-claude-shim.py   (if anthropic already installed)

No network. No real credentials. The module is imported by path (importlib);
importing it must never start a server or make a network call.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path

# The shim's get_client() resolves ANTHROPIC_API_KEY via the SDK's own chain.
# A visibly-fake value keeps any accidental client construction inert offline.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-a-real-key")

SHIM_PATH = Path(__file__).resolve().parent / "claude-shim.py"


def _load_shim() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("claude_shim_under_test", SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass() introspects sys.modules[cls.__module__] at class-creation
    # time, so the module must be registered before exec_module() runs it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shim = _load_shim()


# --- small fakes mirroring the Anthropic SDK's object shapes ----------------

class FakeBlock:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class FakeMessage:
    def __init__(self, content, stop_reason="end_turn", id="msg_123", usage=None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.id = id
        self.usage = usage or FakeUsage()

    def to_dict(self) -> dict:
        out_content = []
        for block in self.content:
            d = dict(block.__dict__)
            out_content.append(d)
        return {"content": out_content}


# --- (a) system + developer hoisting ----------------------------------------

class SystemHoistingTests(unittest.TestCase):
    def test_system_and_developer_merge_into_one_cached_block(self) -> None:
        body = {
            "model": "claude-sonnet-5",
            "messages": [
                {"role": "system", "content": "sys-part"},
                {"role": "developer", "content": "dev-part"},
                {"role": "user", "content": "hi"},
            ],
        }
        kwargs, ctx = shim.to_anthropic(body)
        self.assertEqual(len(kwargs["system"]), 1)
        block = kwargs["system"][0]
        self.assertEqual(block["type"], "text")
        self.assertIn("sys-part", block["text"])
        self.assertIn("dev-part", block["text"])
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})
        self.assertEqual(ctx.anthropic_model, "claude-sonnet-5")


# --- (b) effort suffix -> output_config.effort, model id stripped ----------

class EffortSlugTests(unittest.TestCase):
    def _body(self, model: str) -> dict:
        return {"model": model, "messages": [{"role": "user", "content": "hi"}]}

    def test_each_effort_suffix_maps_and_strips(self) -> None:
        for eff in shim.EFFORTS:
            with self.subTest(effort=eff):
                model = "claude-sonnet-5-%s" % eff
                kwargs, ctx = shim.to_anthropic(self._body(model))
                self.assertEqual(kwargs["model"], "claude-sonnet-5")
                self.assertEqual(kwargs["output_config"], {"effort": eff})
                self.assertEqual(ctx.anthropic_model, "claude-sonnet-5")
                self.assertEqual(ctx.effort, eff)

    def test_no_suffix_omits_output_config(self) -> None:
        kwargs, ctx = shim.to_anthropic(self._body("claude-sonnet-5"))
        self.assertNotIn("output_config", kwargs)
        self.assertIsNone(ctx.effort)


# --- (c) body reasoning_effort wins, minimal->low, unknown fails closed -----

class ReasoningEffortBodyTests(unittest.TestCase):
    def test_body_reasoning_effort_overrides_slug_suffix(self) -> None:
        body = {"model": "claude-sonnet-5-low", "reasoning_effort": "high",
                 "messages": [{"role": "user", "content": "hi"}]}
        kwargs, ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["output_config"], {"effort": "high"})
        self.assertEqual(ctx.effort, "high")

    def test_minimal_alias_folds_to_low(self) -> None:
        body = {"model": "claude-sonnet-5", "reasoning_effort": "minimal",
                 "messages": [{"role": "user", "content": "hi"}]}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["output_config"], {"effort": "low"})

    def test_unknown_reasoning_effort_is_400_class(self) -> None:
        body = {"model": "claude-sonnet-5", "reasoning_effort": "bogus",
                 "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_anthropic(body)
        self.assertEqual(cm.exception.status, 400)

    def test_non_string_reasoning_effort_is_400_class(self) -> None:
        body = {"model": "claude-sonnet-5", "reasoning_effort": 3,
                 "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_anthropic(body)
        self.assertEqual(cm.exception.status, 400)


# --- (d) thinking always adaptive/summarized, budget_tokens never present --

class ThinkingPinTests(unittest.TestCase):
    def test_thinking_is_always_adaptive_summarized(self) -> None:
        for model in ("claude-sonnet-5", "claude-sonnet-5-max", "claude-opus-5-low"):
            body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
            kwargs, _ctx = shim.to_anthropic(body)
            self.assertEqual(kwargs["thinking"], {"type": "adaptive", "display": "summarized"})
            self.assertNotIn("budget_tokens", kwargs["thinking"])


# --- (e) temperature/top_p/n dropped and recorded ---------------------------

class DroppedKeyTests(unittest.TestCase):
    def test_unhonorable_knobs_are_dropped_and_recorded(self) -> None:
        body = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "n": 2,
        }
        kwargs, ctx = shim.to_anthropic(body)
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("top_p", kwargs)
        self.assertIn("temperature", ctx.dropped)
        self.assertIn("top_p", ctx.dropped)
        self.assertIn("n", ctx.dropped)

    def test_default_valued_knobs_are_not_reported(self) -> None:
        body = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 1,
            "logprobs": False,
        }
        _kwargs, ctx = shim.to_anthropic(body)
        self.assertNotIn("n", ctx.dropped)
        self.assertNotIn("logprobs", ctx.dropped)


# --- (f) tools -> input_schema, tool_choice variants, parallel disable -----

class ToolMappingTests(unittest.TestCase):
    def _tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "get it",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }]

    def test_tools_map_to_input_schema(self) -> None:
        body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}],
                 "tools": self._tools()}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertEqual(len(kwargs["tools"]), 1)
        tool = kwargs["tools"][0]
        self.assertEqual(tool["name"], "get_weather")
        self.assertEqual(tool["input_schema"]["properties"]["city"]["type"], "string")

    def test_tool_choice_auto_none_required_named(self) -> None:
        for choice, expected in (
            ("auto", {"type": "auto"}),
            ("none", {"type": "none"}),
            ("required", {"type": "any"}),
            ({"type": "function", "function": {"name": "get_weather"}},
             {"type": "tool", "name": "get_weather"}),
        ):
            with self.subTest(choice=choice):
                body = {"model": "claude-sonnet-5",
                         "messages": [{"role": "user", "content": "hi"}],
                         "tools": self._tools(), "tool_choice": choice}
                kwargs, _ctx = shim.to_anthropic(body)
                self.assertEqual(kwargs["tool_choice"], expected)

    def test_parallel_tool_calls_false_disables_parallel_use(self) -> None:
        body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}],
                 "tools": self._tools(), "parallel_tool_calls": False}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertTrue(kwargs["tool_choice"]["disable_parallel_tool_use"])


# --- (g) assistant tool_calls -> tool_use; tool role -> tool_result group --

class ToolCallAndResultTests(unittest.TestCase):
    def test_assistant_tool_calls_become_tool_use_with_parsed_input(self) -> None:
        body = {
            "model": "claude-sonnet-5",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
            ],
        }
        kwargs, _ctx = shim.to_anthropic(body)
        messages = kwargs["messages"]
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        tool_use = [b for b in assistant_msg["content"] if b["type"] == "tool_use"][0]
        self.assertEqual(tool_use["input"], {"city": "NYC"})
        self.assertEqual(tool_use["id"], "call_1")
        # The tool-role message groups into one user message of tool_result blocks.
        last_user = messages[-1]
        self.assertEqual(last_user["role"], "user")
        result_blocks = [b for b in last_user["content"] if b["type"] == "tool_result"]
        self.assertEqual(len(result_blocks), 1)
        self.assertEqual(result_blocks[0]["tool_use_id"], "call_1")
        self.assertEqual(result_blocks[0]["content"], "72F")

    def test_multiple_consecutive_tool_messages_group_into_one_user_message(self) -> None:
        body = {
            "model": "claude-sonnet-5",
            "messages": [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "a", "arguments": "{}"}},
                    {"id": "call_2", "type": "function",
                     "function": {"name": "b", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
                {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
            ],
        }
        kwargs, _ctx = shim.to_anthropic(body)
        last = kwargs["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(len(last["content"]), 2)

    def test_tool_message_without_tool_call_id_is_400(self) -> None:
        with self.assertRaises(shim.ShimError):
            shim.tool_result_block({"role": "tool", "content": "x"})


# --- (h) tool-name sanitization: deterministic, matches pattern, round-trips

class ToolNameSanitizationTests(unittest.TestCase):
    def test_already_valid_name_is_unchanged(self) -> None:
        self.assertEqual(shim.sanitize_tool_name("get_weather"), "get_weather")

    def test_invalid_name_is_sanitized_deterministically(self) -> None:
        name = "weather.lookup!v2"
        sanitized_1 = shim.sanitize_tool_name(name)
        sanitized_2 = shim.sanitize_tool_name(name)
        self.assertEqual(sanitized_1, sanitized_2)
        self.assertRegex(sanitized_1, r"^[a-zA-Z0-9_-]{1,128}$")

    def test_round_trip_via_convert_tools_restores_original_name(self) -> None:
        tools = [{
            "type": "function",
            "function": {"name": "weather.lookup!v2", "parameters": {}},
        }]
        ctx_names: dict = {}
        dropped: list = []
        converted = shim.convert_tools(tools, ctx_names, dropped)
        sanitized = converted[0]["name"]
        self.assertNotEqual(sanitized, "weather.lookup!v2")
        self.assertEqual(ctx_names[sanitized], "weather.lookup!v2")

        ctx = shim.RequestContext(model="m", anthropic_model="m", effort=None,
                                   stream=False, max_tokens=10, tool_names=ctx_names)
        self.assertEqual(ctx.original_tool_name(sanitized), "weather.lookup!v2")


# --- (i) image_url data: URL -> base64 image source -------------------------

class ImageBlockTests(unittest.TestCase):
    def test_data_url_maps_to_base64_source(self) -> None:
        b64 = "aGVsbG8="  # "hello"
        url = "data:image/png;base64,%s" % b64
        dropped: list = []
        block = shim._image_block(url, dropped)
        self.assertEqual(block, {"type": "image",
                                  "source": {"type": "base64", "media_type": "image/png",
                                             "data": b64}})
        self.assertEqual(dropped, [])

    def test_bad_base64_is_dropped_not_raised(self) -> None:
        url = "data:image/png;base64,***not-base64***"
        dropped: list = []
        block = shim._image_block(url, dropped)
        self.assertIsNone(block)
        self.assertIn("image_url:bad-base64", dropped)

    def test_http_url_maps_to_url_source(self) -> None:
        dropped: list = []
        block = shim._image_block("https://example.com/x.png", dropped)
        self.assertEqual(block, {"type": "image",
                                  "source": {"type": "url", "url": "https://example.com/x.png"}})


# --- (j) max_tokens defaults (stream vs non-stream) and clamp --------------

class MaxTokensTests(unittest.TestCase):
    def test_default_max_tokens_non_stream(self) -> None:
        body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        kwargs, ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["max_tokens"], shim.DEFAULT_MAX_TOKENS_BLOCKING)
        self.assertEqual(ctx.max_tokens, shim.DEFAULT_MAX_TOKENS_BLOCKING)

    def test_default_max_tokens_stream(self) -> None:
        body = {"model": "claude-sonnet-5", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["max_tokens"], shim.DEFAULT_MAX_TOKENS_STREAM)

    def test_max_tokens_is_clamped_to_cap(self) -> None:
        body = {"model": "claude-sonnet-5", "max_tokens": shim.MAX_TOKENS_CAP * 10,
                 "messages": [{"role": "user", "content": "hi"}]}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["max_tokens"], shim.MAX_TOKENS_CAP)

    def test_max_completion_tokens_takes_priority_over_max_tokens(self) -> None:
        body = {"model": "claude-sonnet-5", "max_tokens": 100, "max_completion_tokens": 50,
                 "messages": [{"role": "user", "content": "hi"}]}
        kwargs, _ctx = shim.to_anthropic(body)
        self.assertEqual(kwargs["max_tokens"], 50)

    def test_invalid_max_tokens_is_400(self) -> None:
        body = {"model": "claude-sonnet-5", "max_tokens": "not-a-number",
                 "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_anthropic(body)
        self.assertEqual(cm.exception.status, 400)


# --- (k) finish_reason mapping -----------------------------------------------

class FinishReasonTests(unittest.TestCase):
    def test_mapping_table(self) -> None:
        cases = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "refusal": "content_filter",
            "stop_sequence": "stop",
            None: None,
        }
        for stop_reason, expected in cases.items():
            with self.subTest(stop_reason=stop_reason):
                self.assertEqual(shim.map_finish_reason(stop_reason), expected)


# --- (l) SSE chunk builder ----------------------------------------------------

class SSEChunkTests(unittest.TestCase):
    def test_sse_chunk_is_valid_json_prefixed_and_blank_line_terminated(self) -> None:
        payload = {"a": 1}
        raw = shim.sse_chunk(payload)
        self.assertTrue(raw.startswith(b"data: "))
        self.assertTrue(raw.endswith(b"\n\n"))
        body = raw[len(b"data: "):-2]
        self.assertEqual(json.loads(body), payload)

    def test_tool_call_chunk_envelope_carries_index_id_name_arguments(self) -> None:
        envelope = shim.chunk_envelope("chatcmpl-1", "claude-sonnet-5", 123, {
            "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": ""},
            }]
        })
        delta = envelope["choices"][0]["delta"]
        call = delta["tool_calls"][0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(call["function"]["arguments"], "")

    def test_done_sentinel_shape(self) -> None:
        # The literal terminal frame the shim writes after the final chunk.
        sentinel = b"data: [DONE]\n\n"
        self.assertTrue(sentinel.startswith(b"data: "))
        self.assertTrue(sentinel.endswith(b"\n\n"))
        self.assertEqual(sentinel[len(b"data: "):-2], b"[DONE]")


# --- (m) non-stream completion object from a fake Message -------------------

class NonStreamCompletionTests(unittest.TestCase):
    def test_completion_has_text_tool_calls_reasoning_and_usage(self) -> None:
        message = FakeMessage(
            content=[
                FakeBlock(type="thinking", thinking="because reasons"),
                FakeBlock(type="text", text="here is the answer"),
                FakeBlock(type="tool_use", id="call_1", name="get_weather",
                          input={"city": "NYC"}),
            ],
            stop_reason="tool_use",
            usage=FakeUsage(input_tokens=100, output_tokens=20,
                             cache_read_input_tokens=30, cache_creation_input_tokens=5),
        )
        ctx = shim.RequestContext(model="claude-sonnet-5", anthropic_model="claude-sonnet-5",
                                   effort=None, stream=False, max_tokens=100,
                                   tool_names={"get_weather": "get_weather"})
        completion = shim.openai_completion_from_message(message, ctx, 111)
        msg = completion["choices"][0]["message"]
        self.assertEqual(msg["content"], "here is the answer")
        self.assertEqual(msg["reasoning_content"], "because reasons")
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]),
                          {"city": "NYC"})
        self.assertEqual(completion["choices"][0]["finish_reason"], "tool_calls")
        usage = completion["usage"]
        self.assertEqual(usage["prompt_tokens"], 100 + 30 + 5)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 30)


# --- (n) thinking-block replay via the bounded LRU --------------------------

class ThinkingReplayTests(unittest.TestCase):
    def test_cached_thinking_block_is_reattached_before_tool_use(self) -> None:
        cache = shim.ThinkingCache()
        thinking_block = {"type": "thinking", "thinking": "reasoning...", "signature": "sig"}
        cache.put("call_1", [thinking_block])

        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"},
        }]}
        blocks, replayed = shim.assistant_content_blocks(msg, [], cache)
        self.assertEqual(replayed, 1)
        self.assertEqual(blocks[0], thinking_block)
        self.assertEqual(blocks[-1]["type"], "tool_use")

    def test_remember_thinking_keys_by_the_tool_use_id_it_produced(self) -> None:
        cache = shim.ThinkingCache()
        message = FakeMessage(content=[
            FakeBlock(type="thinking", thinking="reasoning...", signature="sig"),
            FakeBlock(type="tool_use", id="call_9", name="x", input={}),
        ])
        shim.remember_thinking(message, cache)
        remembered = cache.get("call_9")
        self.assertIsNotNone(remembered)
        self.assertEqual(remembered[0]["type"], "thinking")

    def test_no_tool_calls_means_no_thinking_replay_from_openai_history(self) -> None:
        # Plain OpenAI history (no tool_calls on the assistant turn) carries no
        # thinking field at all; the shim must not invent a replay from it.
        cache = shim.ThinkingCache()
        cache.put("call_1", [{"type": "thinking", "thinking": "x"}])
        msg = {"role": "assistant", "content": "just text, no tool call"}
        blocks, replayed = shim.assistant_content_blocks(msg, [], cache)
        self.assertEqual(replayed, 0)
        self.assertTrue(all(b["type"] != "thinking" for b in blocks))


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stdout)
    result = runner.run(suite)
    total = result.testsRun
    failed_count = len(result.failures) + len(result.errors)
    passed_count = total - failed_count
    print("%d/%d pass, %d fail" % (passed_count, total, failed_count))
    sys.exit(1 if failed_count else 0)
