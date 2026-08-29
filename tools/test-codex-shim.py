#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Offline unit tests for tools/codex-shim.py.

Run: python3 tools/test-codex-shim.py

No network. No real credentials. The module is imported by path (importlib);
importing it must never start a server or make a network call. Fake auth.json
fixtures live in a tempdir and CODEX_HOME is pointed at it; every token value
is a visibly-fake string.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import stat
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SHIM_PATH = Path(__file__).resolve().parent / "codex-shim.py"


def _load_shim() -> types.ModuleType:
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_shim_under_test", SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass() introspects sys.modules[cls.__module__] at class-creation
    # time, so the module must be registered before exec_module() runs it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shim = _load_shim()


# --- fake JWT builder (no signature, no verification: the shim never checks
# a signature — the backend does) -------------------------------------------

def _b64u(doc: dict) -> str:
    raw = json.dumps(doc).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def fake_jwt(exp: float | None = None, account_id: str | None = "acct_fake_test") -> str:
    header = _b64u({"alg": "none", "typ": "JWT"})
    payload: dict = {}
    if exp is not None:
        payload["exp"] = exp
    if account_id is not None:
        payload["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}
    body = _b64u(payload)
    return "%s.%s.fake-sig-not-real" % (header, body)


def fake_auth_doc(exp: float | None = None, auth_mode: str = "chatgpt",
                  access_token: str | None = None, refresh_token: str = "refresh-fake-token",
                  account_id: str | None = "acct_fake_test", extra: dict | None = None) -> dict:
    access = access_token if access_token is not None else fake_jwt(exp=exp, account_id=account_id)
    doc = {
        "OPENAI_API_KEY": None,
        "auth_mode": auth_mode,
        "tokens": {
            "id_token": fake_jwt(exp=exp, account_id=account_id),
            "access_token": access,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-01-01T00:00:00.000000Z",
    }
    if extra:
        doc.update(extra)
    return doc


class TempCodexHome:
    """A tempdir holding a fake auth.json, usable as a context manager."""

    def __init__(self, doc: dict | None = None) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = self._tmpdir.name
        self.auth_path = os.path.join(self.path, "auth.json")
        if doc is not None:
            self.write(doc)

    def write(self, doc: dict) -> None:
        with open(self.auth_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

    def cleanup(self) -> None:
        self._tmpdir.cleanup()


# --- (a) auth loading, expiry detection, 401s --------------------------------

class AuthLoadTests(unittest.TestCase):
    def test_valid_chatgpt_auth_loads(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        try:
            auth = shim.load_auth(home.auth_path)
            self.assertEqual(auth.account_id, "acct_fake_test")
            self.assertFalse(auth.expired(time.time()))
        finally:
            home.cleanup()

    def test_missing_file_is_401(self) -> None:
        home = TempCodexHome()
        try:
            with self.assertRaises(shim.ShimError) as cm:
                shim.load_auth(home.auth_path)
            self.assertEqual(cm.exception.status, 401)
        finally:
            home.cleanup()

    def test_non_chatgpt_auth_mode_is_401(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600, auth_mode="apikey"))
        try:
            with self.assertRaises(shim.ShimError) as cm:
                shim.load_auth(home.auth_path)
            self.assertEqual(cm.exception.status, 401)
            self.assertIn("chatgpt", cm.exception.message)
        finally:
            home.cleanup()

    def test_missing_tokens_object_is_401(self) -> None:
        doc = fake_auth_doc(exp=time.time() + 3600)
        del doc["tokens"]
        home = TempCodexHome(doc)
        try:
            with self.assertRaises(shim.ShimError) as cm:
                shim.load_auth(home.auth_path)
            self.assertEqual(cm.exception.status, 401)
        finally:
            home.cleanup()

    def test_incomplete_tokens_is_401(self) -> None:
        doc = fake_auth_doc(exp=time.time() + 3600)
        doc["tokens"]["refresh_token"] = ""
        home = TempCodexHome(doc)
        try:
            with self.assertRaises(shim.ShimError) as cm:
                shim.load_auth(home.auth_path)
            self.assertEqual(cm.exception.status, 401)
        finally:
            home.cleanup()

    def test_not_json_object_is_401(self) -> None:
        home = TempCodexHome()
        with open(home.auth_path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]")
        try:
            with self.assertRaises(shim.ShimError) as cm:
                shim.load_auth(home.auth_path)
            self.assertEqual(cm.exception.status, 401)
        finally:
            home.cleanup()

    def test_expiry_detection_true_when_within_margin(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 10))
        try:
            auth = shim.load_auth(home.auth_path)
            self.assertTrue(auth.expired(time.time(), margin=300.0))
        finally:
            home.cleanup()

    def test_expiry_detection_false_when_comfortably_ahead(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        try:
            auth = shim.load_auth(home.auth_path)
            self.assertFalse(auth.expired(time.time(), margin=300.0))
        finally:
            home.cleanup()

    def test_unreadable_exp_counts_as_expired(self) -> None:
        # access_token is not a JWT at all -> jwt_expiry() returns None.
        home = TempCodexHome(fake_auth_doc(access_token="not-a-jwt-token"))
        try:
            auth = shim.load_auth(home.auth_path)
            self.assertIsNone(auth.expires_at)
            self.assertTrue(auth.expired(time.time()))
        finally:
            home.cleanup()

    def test_account_id_falls_back_to_id_token_claim(self) -> None:
        doc = fake_auth_doc(exp=time.time() + 3600, account_id="acct_claim_only")
        doc["tokens"]["account_id"] = None
        home = TempCodexHome(doc)
        try:
            auth = shim.load_auth(home.auth_path)
            self.assertEqual(auth.account_id, "acct_claim_only")
        finally:
            home.cleanup()


# --- (b) atomic rewrite preserves unrelated keys -----------------------------

class WriteAuthFileTests(unittest.TestCase):
    def test_rewrite_preserves_unrelated_keys_and_mode(self) -> None:
        home = TempCodexHome()
        try:
            original = fake_auth_doc(exp=time.time() + 3600)
            original["some_other_cli_field"] = "keep-me"
            merged = shim.merge_refresh(
                original,
                {"access_token": fake_jwt(exp=time.time() + 7200), "id_token": "new-id-tok",
                 "refresh_token": "new-refresh-tok"},
                now=time.time(),
            )
            shim.write_auth_file(home.auth_path, merged)
            with open(home.auth_path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["some_other_cli_field"], "keep-me")
            self.assertEqual(on_disk["tokens"]["refresh_token"], "new-refresh-tok")
            self.assertEqual(on_disk["auth_mode"], "chatgpt")
            mode = stat.S_IMODE(os.stat(home.auth_path).st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            home.cleanup()

    def test_merge_refresh_keeps_previous_value_for_omitted_field(self) -> None:
        original = fake_auth_doc(exp=time.time() + 3600)
        original["tokens"]["id_token"] = "old-id-tok"
        merged = shim.merge_refresh(original, {"access_token": "new-access-tok"}, now=time.time())
        self.assertEqual(merged["tokens"]["id_token"], "old-id-tok")
        self.assertEqual(merged["tokens"]["access_token"], "new-access-tok")

    def test_merge_refresh_sets_last_refresh_iso_utc(self) -> None:
        original = fake_auth_doc(exp=time.time() + 3600)
        merged = shim.merge_refresh(original, {}, now=0.0)
        self.assertEqual(merged["last_refresh"], "1970-01-01T00:00:00.000000Z")


# --- (c) slug suffix -> reasoning.effort, all levels incl. ultra -----------

class EffortSlugTests(unittest.TestCase):
    def _catalog(self) -> dict:
        return shim.parse_catalog({
            "client_version": "0.150.1",
            "models": [{
                "slug": "gpt-5.6-sol",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": e} for e in shim.EFFORTS],
                "additional_speed_tiers": ["fast"],
            }],
        })

    def test_each_effort_suffix_maps_to_wire(self) -> None:
        catalog = self._catalog()
        for eff in shim.EFFORTS:
            with self.subTest(effort=eff):
                model = "gpt-5.6-sol-%s" % eff
                body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
                payload, ctx = shim.to_responses(body, catalog=catalog)
                self.assertEqual(payload["model"], "gpt-5.6-sol")
                self.assertEqual(ctx.effort, eff)
                if eff == "ultra":
                    self.assertEqual(payload["reasoning"]["effort"], "max")
                    self.assertEqual(ctx.wire_effort, "max")
                else:
                    self.assertEqual(payload["reasoning"]["effort"], eff)

    def test_body_reasoning_effort_overrides_slug_suffix(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol-low", "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "hi"}]}
        payload, ctx = shim.to_responses(body, catalog=catalog)
        self.assertEqual(ctx.effort, "high")
        self.assertEqual(payload["reasoning"]["effort"], "high")

    def test_minimal_alias_folds_to_low(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol", "reasoning_effort": "minimal",
                "messages": [{"role": "user", "content": "hi"}]}
        payload, _ctx = shim.to_responses(body, catalog=catalog)
        self.assertEqual(payload["reasoning"]["effort"], "low")

    def test_unknown_reasoning_effort_is_400(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol", "reasoning_effort": "bogus",
                "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog=catalog)
        self.assertEqual(cm.exception.status, 400)

    def test_non_string_reasoning_effort_is_400(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol", "reasoning_effort": 3,
                "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog=catalog)
        self.assertEqual(cm.exception.status, 400)

    def test_no_suffix_omits_effort(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]}
        payload, ctx = shim.to_responses(body, catalog=catalog)
        self.assertNotIn("effort", payload["reasoning"])
        self.assertIsNone(ctx.effort)

    def test_unsupported_effort_for_model_is_400(self) -> None:
        catalog = shim.parse_catalog({
            "models": [{"slug": "gpt-5.6-sol", "visibility": "list",
                       "supported_reasoning_levels": [{"effort": "low"}]}],
        })
        body = {"model": "gpt-5.6-sol-high", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog=catalog)
        self.assertEqual(cm.exception.status, 400)


# --- (d) -fast suffix handling ------------------------------------------------

class FastSuffixTests(unittest.TestCase):
    def _catalog(self) -> dict:
        return shim.parse_catalog({
            "models": [{
                "slug": "gpt-5.6-sol",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": e} for e in shim.EFFORTS],
                "additional_speed_tiers": ["fast"],
            }],
        })

    def test_fast_after_effort_sets_service_tier(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol-high-fast",
                "messages": [{"role": "user", "content": "hi"}]}
        payload, ctx = shim.to_responses(body, catalog=catalog)
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(ctx.effort, "high")
        self.assertTrue(ctx.fast)
        self.assertEqual(payload["service_tier"], shim.FAST_SERVICE_TIER)

    def test_fast_without_effort(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-5.6-sol-fast", "messages": [{"role": "user", "content": "hi"}]}
        payload, ctx = shim.to_responses(body, catalog=catalog)
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertIsNone(ctx.effort)
        self.assertTrue(ctx.fast)

    def test_fast_unsupported_by_model_is_400(self) -> None:
        catalog = shim.parse_catalog({
            "models": [{"slug": "gpt-5.6-sol", "visibility": "list",
                       "supported_reasoning_levels": [{"effort": "low"}],
                       "additional_speed_tiers": []}],
        })
        body = {"model": "gpt-5.6-sol-fast", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog=catalog)
        self.assertEqual(cm.exception.status, 400)

    def test_no_catalog_accepts_any_gpt_slug(self) -> None:
        body = {"model": "gpt-5.6-sol-high-fast",
                "messages": [{"role": "user", "content": "hi"}]}
        payload, ctx = shim.to_responses(body, catalog={})
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(ctx.effort, "high")
        self.assertTrue(ctx.fast)

    def test_no_catalog_rejects_non_gpt_slug(self) -> None:
        body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog={})
        self.assertEqual(cm.exception.status, 400)

    def test_unknown_model_with_catalog_is_400(self) -> None:
        catalog = self._catalog()
        body = {"model": "gpt-9-nonexistent", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(shim.ShimError) as cm:
            shim.to_responses(body, catalog=catalog)
        self.assertEqual(cm.exception.status, 400)


# --- (e) system/developer -> instructions ------------------------------------

class InstructionsTests(unittest.TestCase):
    def test_system_and_developer_join_into_instructions(self) -> None:
        body = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "sys-part"},
                {"role": "developer", "content": "dev-part"},
                {"role": "user", "content": "hi"},
            ],
        }
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertIn("sys-part", payload["instructions"])
        self.assertIn("dev-part", payload["instructions"])

    def test_no_system_or_developer_omits_instructions(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertNotIn("instructions", payload)


# --- (f) tools -> Responses function tools; tool_choice variants ------------

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

    def test_tools_map_to_flat_function_shape(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                "tools": self._tools()}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertEqual(len(payload["tools"]), 1)
        tool = payload["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["name"], "get_weather")
        self.assertEqual(tool["parameters"]["properties"]["city"]["type"], "string")
        self.assertIn("strict", tool)
        self.assertNotIn("function", tool)  # flat, not chat/completions nesting

    def test_tool_choice_auto_none_required(self) -> None:
        for choice, expected in (("auto", "auto"), ("none", "none"), ("required", "required")):
            with self.subTest(choice=choice):
                body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                        "tools": self._tools(), "tool_choice": choice}
                payload, _ctx = shim.to_responses(body, catalog={})
                self.assertEqual(payload["tool_choice"], expected)

    def test_tool_choice_named_function(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                "tools": self._tools(),
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}}}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "get_weather"})

    def test_tool_choice_unsupported_is_dropped_not_raised(self) -> None:
        dropped: list = []
        result = shim.convert_tool_choice({"weird": "shape"}, dropped)
        self.assertIsNone(result)
        self.assertIn("tool_choice:unsupported", dropped)

    def test_parallel_tool_calls_forwarded_when_bool(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                "tools": self._tools(), "parallel_tool_calls": False}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertFalse(payload["parallel_tool_calls"])

    def test_tool_choice_without_tools_is_omitted(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto"}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("tools", payload)


# --- (g) assistant tool_calls -> function_call; tool role -> function_call_output

class ToolCallAndResultTests(unittest.TestCase):
    def test_assistant_tool_calls_become_function_call_items(self) -> None:
        body = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
            ],
        }
        payload, _ctx = shim.to_responses(body, catalog={})
        items = payload["input"]
        function_calls = [i for i in items if i.get("type") == "function_call"]
        self.assertEqual(len(function_calls), 1)
        self.assertEqual(function_calls[0]["call_id"], "call_1")
        self.assertEqual(function_calls[0]["arguments"], '{"city": "NYC"}')
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["call_id"], "call_1")
        self.assertEqual(outputs[0]["output"], "72F")

    def test_tool_message_without_tool_call_id_is_400(self) -> None:
        with self.assertRaises(shim.ShimError):
            shim.tool_result_item({"role": "tool", "content": "x"})

    def test_assistant_dict_arguments_are_json_encoded(self) -> None:
        ctx = shim.RequestContext(model="m", backend_model="m", effort=None,
                                  wire_effort=None, fast=False, stream=False)
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": {"city": "NYC"}},
        }]}
        items = shim.assistant_items(msg, ctx, None)
        call = next(i for i in items if i["type"] == "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"city": "NYC"})


# --- (g.1) call_id longer than 64 chars gets a deterministic short wire id --

class WireCallIdTests(unittest.TestCase):
    def test_short_ids_pass_through_unchanged(self) -> None:
        self.assertEqual(shim.wire_call_id("call_1"), "call_1")
        exact = "x" * 64
        self.assertEqual(shim.wire_call_id(exact), exact)
        self.assertEqual(shim.wire_call_id(""), "")

    def test_long_id_maps_to_a_deterministic_64_char_id(self) -> None:
        long_id = "a" * 85
        mapped = shim.wire_call_id(long_id)
        self.assertEqual(len(mapped), 64)
        self.assertTrue(mapped.startswith("call_"))
        self.assertNotEqual(mapped, long_id)
        self.assertEqual(shim.wire_call_id(long_id), mapped)
        other_long_id = "b" * 85
        self.assertNotEqual(shim.wire_call_id(other_long_id), mapped)

    def test_end_to_end_long_tool_call_id_maps_consistently(self) -> None:
        long_id = "c" * 85
        body = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": long_id, "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                }]},
                {"role": "tool", "tool_call_id": long_id, "content": "72F"},
            ],
        }
        payload, _ctx = shim.to_responses(body, catalog={})
        items = payload["input"]
        function_calls = [i for i in items if i.get("type") == "function_call"]
        self.assertEqual(len(function_calls), 1)
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 1)
        call_id = function_calls[0]["call_id"]
        self.assertEqual(call_id, outputs[0]["call_id"])
        self.assertLessEqual(len(call_id), 64)
        self.assertNotEqual(call_id, long_id)
        self.assertEqual(function_calls[0]["name"], "get_weather")
        self.assertEqual(function_calls[0]["arguments"], '{"city": "NYC"}')
        self.assertEqual(outputs[0]["output"], "72F")
        user_messages = [i for i in items if i.get("role") == "user"]
        self.assertEqual(len(user_messages), 1)

    def test_end_to_end_short_tool_call_id_is_unchanged(self) -> None:
        body = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
            ],
        }
        payload, _ctx = shim.to_responses(body, catalog={})
        items = payload["input"]
        function_calls = [i for i in items if i.get("type") == "function_call"]
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        self.assertEqual(function_calls[0]["call_id"], "call_1")
        self.assertEqual(outputs[0]["call_id"], "call_1")


# --- (h) encrypted reasoning replay LRU --------------------------------------

class ReasoningReplayTests(unittest.TestCase):
    def test_cached_reasoning_reattaches_before_function_call(self) -> None:
        cache = shim.ReasoningCache()
        reasoning_item = {"type": "reasoning", "summary": [], "encrypted_content": "enc-blob"}
        cache.put("call_1", [reasoning_item])

        ctx = shim.RequestContext(model="m", backend_model="m", effort=None,
                                  wire_effort=None, fast=False, stream=False)
        msg = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"},
        }]}
        items = shim.assistant_items(msg, ctx, cache)
        self.assertEqual(ctx.replayed_reasoning, 1)
        self.assertEqual(items[0], reasoning_item)
        self.assertEqual(items[-1]["type"], "function_call")

    def test_no_tool_calls_means_no_replay(self) -> None:
        cache = shim.ReasoningCache()
        cache.put("call_1", [{"type": "reasoning", "encrypted_content": "x"}])
        ctx = shim.RequestContext(model="m", backend_model="m", effort=None,
                                  wire_effort=None, fast=False, stream=False)
        msg = {"role": "assistant", "content": "just text, no tool call"}
        items = shim.assistant_items(msg, ctx, cache)
        self.assertEqual(ctx.replayed_reasoning, 0)
        self.assertTrue(all(i.get("type") != "reasoning" for i in items))

    def test_remember_reasoning_keys_by_tool_call_id(self) -> None:
        cache = shim.ReasoningCache()
        ctx = shim.RequestContext(model="m", backend_model="m", effort=None,
                                  wire_effort=None, fast=False, stream=False)
        ctx.reasoning_items = [{"type": "reasoning", "encrypted_content": "enc"}]
        ctx.tool_call_ids = ["call_9"]
        shim.remember_reasoning(ctx, cache)
        remembered = cache.get("call_9")
        self.assertIsNotNone(remembered)
        self.assertEqual(remembered[0]["encrypted_content"], "enc")

    def test_lru_evicts_oldest_beyond_capacity(self) -> None:
        cache = shim.ReasoningCache(capacity=2)
        cache.put("a", [{"type": "reasoning"}])
        cache.put("b", [{"type": "reasoning"}])
        cache.put("c", [{"type": "reasoning"}])
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))


# --- (i) stream:true / store:false / include always set ---------------------

class WireDefaultsTests(unittest.TestCase):
    def test_store_always_false_and_include_encrypted_content(self) -> None:
        for stream_flag in (True, False, None):
            with self.subTest(stream=stream_flag):
                body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]}
                if stream_flag is not None:
                    body["stream"] = stream_flag
                payload, ctx = shim.to_responses(body, catalog={})
                self.assertIs(payload["store"], False)
                self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
                self.assertIs(payload["stream"], True)  # upstream is always streamed
                self.assertEqual(ctx.stream, bool(stream_flag))

    def test_reasoning_summary_always_auto(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]}
        payload, _ctx = shim.to_responses(body, catalog={})
        self.assertEqual(payload["reasoning"]["summary"], "auto")


# --- (j) dropped knobs recorded, never faked --------------------------------

class DroppedKeyTests(unittest.TestCase):
    def test_unhonorable_knobs_are_dropped_and_recorded(self) -> None:
        body = {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 100,
            "response_format": {"type": "json_object"},
        }
        payload, ctx = shim.to_responses(body, catalog={})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)
        for key in ("temperature", "top_p", "max_tokens", "response_format"):
            self.assertIn(key, ctx.dropped)

    def test_default_valued_knobs_are_not_reported(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                "n": 1, "logprobs": False}
        _payload, ctx = shim.to_responses(body, catalog={})
        self.assertNotIn("n", ctx.dropped)
        self.assertNotIn("logprobs", ctx.dropped)

    def test_n_other_than_one_is_reported(self) -> None:
        body = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}], "n": 2}
        _payload, ctx = shim.to_responses(body, catalog={})
        self.assertIn("n", ctx.dropped)


# --- (k) SSE event -> OpenAI chunk mapping -----------------------------------

def _run_translate(events: list[dict], ctx=None) -> list[dict]:
    if ctx is None:
        ctx = shim.RequestContext(model="gpt-5.6-sol", backend_model="gpt-5.6-sol",
                                  effort=None, wire_effort=None, fast=False, stream=True)
    return list(shim.translate_events(events, ctx, created=123))


class SSETranslationTests(unittest.TestCase):
    def test_text_delta_becomes_content_chunk(self) -> None:
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_text.delta", "delta": "hello"},
            {"type": "response.completed", "response": {"usage": {}}},
        ]
        chunks = _run_translate(events)
        contents = [c["choices"][0]["delta"].get("content") for c in chunks]
        self.assertIn("hello", contents)

    def test_reasoning_delta_becomes_reasoning_content_chunk(self) -> None:
        events = [
            {"type": "response.reasoning_text.delta", "delta": "thinking..."},
            {"type": "response.completed", "response": {"usage": {}}},
        ]
        chunks = _run_translate(events)
        reasonings = [c["choices"][0]["delta"].get("reasoning_content") for c in chunks]
        self.assertIn("thinking...", reasonings)

    def test_function_call_added_then_arguments_delta(self) -> None:
        events = [
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "item_1", "call_id": "call_1",
                      "name": "get_weather"}},
            {"type": "response.function_call_arguments.delta", "item_id": "item_1",
             "delta": '{"city":'},
            {"type": "response.function_call_arguments.delta", "item_id": "item_1",
             "delta": '"NYC"}'},
            {"type": "response.completed", "response": {"usage": {}}},
        ]
        ctx = shim.RequestContext(model="gpt-5.6-sol", backend_model="gpt-5.6-sol",
                                  effort=None, wire_effort=None, fast=False, stream=True,
                                  tool_names={"get_weather": "get_weather"})
        chunks = _run_translate(events, ctx)
        tool_call_chunks = [c for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
        self.assertGreaterEqual(len(tool_call_chunks), 3)
        first = tool_call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(first["id"], "call_1")
        self.assertEqual(first["function"]["name"], "get_weather")
        arg_deltas = "".join(
            c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
            for c in tool_call_chunks
        )
        self.assertIn('"city":', arg_deltas)
        self.assertIn('"NYC"}', arg_deltas)
        final = chunks[-1]
        self.assertEqual(final["choices"][0]["finish_reason"], "tool_calls")

    def test_function_call_done_without_deltas_emits_arguments_once(self) -> None:
        events = [
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "item_1", "call_id": "call_1",
                      "name": "get_weather"}},
            {"type": "response.output_item.done",
             "item": {"type": "function_call", "id": "item_1", "call_id": "call_1",
                      "arguments": '{"city": "NYC"}'}},
            {"type": "response.completed", "response": {"usage": {}}},
        ]
        chunks = _run_translate(events)
        arg_chunks = [c for c in chunks if c["choices"][0]["delta"].get("tool_calls")
                     and c["choices"][0]["delta"]["tool_calls"][0].get("function", {}).get("arguments")]
        self.assertEqual(len(arg_chunks), 1)
        self.assertEqual(
            arg_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"],
            '{"city": "NYC"}')

    def test_completed_maps_finish_reason_stop_and_usage(self) -> None:
        events = [
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"usage": {
                "input_tokens": 100, "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 30},
                "output_tokens_details": {"reasoning_tokens": 5},
            }}},
        ]
        chunks = _run_translate(events)
        final = chunks[-1]
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        usage = final["usage"]
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 30)
        self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 5)

    def test_incomplete_maps_length_for_max_output_tokens(self) -> None:
        events = [
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.incomplete",
             "response": {"incomplete_details": {"reason": "max_output_tokens"}, "usage": {}}},
        ]
        chunks = _run_translate(events)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "length")

    def test_incomplete_maps_content_filter_otherwise(self) -> None:
        events = [
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.incomplete",
             "response": {"incomplete_details": {"reason": "other"}, "usage": {}}},
        ]
        chunks = _run_translate(events)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "content_filter")

    def test_failed_event_raises_shim_error(self) -> None:
        events = [
            {"type": "response.failed",
             "response": {"error": {"message": "boom", "code": "server_error"}}},
        ]
        with self.assertRaises(shim.ShimError) as cm:
            list(_run_translate(events))
        self.assertEqual(cm.exception.status, 502)

    def test_rate_limit_error_maps_to_429(self) -> None:
        events = [{"type": "error", "error": {"message": "slow down",
                                              "code": "rate_limit_exceeded"}}]
        with self.assertRaises(shim.ShimError) as cm:
            list(_run_translate(events))
        self.assertEqual(cm.exception.status, 429)

    def test_include_usage_false_omits_usage_on_final_chunk(self) -> None:
        ctx = shim.RequestContext(model="gpt-5.6-sol", backend_model="gpt-5.6-sol",
                                  effort=None, wire_effort=None, fast=False, stream=True,
                                  include_usage=False)
        events = [
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 1}}},
        ]
        chunks = _run_translate(events, ctx)
        self.assertNotIn("usage", chunks[-1])


# --- (l) sse_events parsing, including [DONE] sentinel ----------------------

class SSEEventsParsingTests(unittest.TestCase):
    def test_parses_data_lines_and_ignores_non_data(self) -> None:
        lines = [
            b": comment\n",
            b'data: {"type": "response.output_text.delta", "delta": "hi"}\n',
            b"\n",
        ]
        events = list(shim.sse_events(lines))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["delta"], "hi")

    def test_done_sentinel_stops_iteration(self) -> None:
        lines = [
            b'data: {"type": "response.output_text.delta", "delta": "a"}\n',
            b"data: [DONE]\n",
            b'data: {"type": "response.output_text.delta", "delta": "b"}\n',
        ]
        events = list(shim.sse_events(lines))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["delta"], "a")

    def test_malformed_json_is_skipped(self) -> None:
        lines = [b"data: {not json}\n",
                 b'data: {"type": "response.output_text.delta", "delta": "ok"}\n']
        events = list(shim.sse_events(lines))
        self.assertEqual(len(events), 1)


# --- (m) non-stream assembly --------------------------------------------------

class NonStreamAssemblyTests(unittest.TestCase):
    def test_completion_from_chunks_assembles_text_tool_calls_and_usage(self) -> None:
        ctx = shim.RequestContext(model="gpt-5.6-sol", backend_model="gpt-5.6-sol",
                                  effort=None, wire_effort=None, fast=False, stream=False,
                                  tool_names={"get_weather": "get_weather"})
        events = [
            {"type": "response.reasoning_text.delta", "delta": "because reasons"},
            {"type": "response.output_text.delta", "delta": "here is the answer"},
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "item_1", "call_id": "call_1",
                      "name": "get_weather"}},
            {"type": "response.function_call_arguments.delta", "item_id": "item_1",
             "delta": '{"city": "NYC"}'},
            {"type": "response.completed", "response": {"usage": {
                "input_tokens": 100, "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 30},
                "output_tokens_details": {"reasoning_tokens": 5},
            }}},
        ]
        chunks = shim.translate_events(events, ctx, created=111)
        completion = shim.completion_from_chunks(chunks, ctx, created=111)
        msg = completion["choices"][0]["message"]
        self.assertEqual(msg["content"], "here is the answer")
        self.assertEqual(msg["reasoning_content"], "because reasons")
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]),
                          {"city": "NYC"})
        self.assertEqual(completion["choices"][0]["finish_reason"], "tool_calls")
        usage = completion["usage"]
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 30)


# --- (n) tool-name sanitization ------------------------------------------------

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
        tools = [{"type": "function",
                 "function": {"name": "weather.lookup!v2", "parameters": {}}}]
        ctx_names: dict = {}
        dropped: list = []
        converted = shim.convert_tools(tools, ctx_names, dropped)
        sanitized = converted[0]["name"]
        self.assertNotEqual(sanitized, "weather.lookup!v2")
        self.assertEqual(ctx_names[sanitized], "weather.lookup!v2")
        ctx = shim.RequestContext(model="m", backend_model="m", effort=None, wire_effort=None,
                                  fast=False, stream=False, tool_names=ctx_names)
        self.assertEqual(ctx.original_tool_name(sanitized), "weather.lookup!v2")


# --- (o) AuthStore end-to-end against a fake CODEX_HOME ----------------------

class AuthStoreTests(unittest.TestCase):
    def test_configured_true_for_valid_store(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        try:
            store = shim.AuthStore(path=home.auth_path)
            self.assertTrue(store.configured())
        finally:
            home.cleanup()

    def test_configured_false_when_file_absent(self) -> None:
        home = TempCodexHome()
        try:
            store = shim.AuthStore(path=home.auth_path)
            self.assertFalse(store.configured())
        finally:
            home.cleanup()

    def test_credentials_reads_without_refresh_when_not_expired(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        try:
            store = shim.AuthStore(path=home.auth_path)
            auth = store.credentials()
            self.assertEqual(auth.account_id, "acct_fake_test")
        finally:
            home.cleanup()

    def test_credentials_picks_up_mtime_change(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600, account_id="acct_one"))
        try:
            store = shim.AuthStore(path=home.auth_path)
            first = store.credentials()
            self.assertEqual(first.account_id, "acct_one")
            time.sleep(0.01)
            home.write(fake_auth_doc(exp=time.time() + 3600, account_id="acct_two"))
            os.utime(home.auth_path, None)
            second = store.credentials()
            self.assertEqual(second.account_id, "acct_two")
        finally:
            home.cleanup()


# --- (l) TLS trust ----------------------------------------------------------
# A self-signed CA generated once for these tests. It is a public certificate
# with no private key here and no host it can authenticate: it only gives
# load_verify_locations() something real to count. Expiry does not matter --
# loading a bundle never validates dates.
TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBnTCCAUOgAwIBAgIUR3KtI5HKOCiKxM+C6IhsE14MEfcwCgYIKoZIzj0EAwIw
IzEhMB8GA1UEAwwYb3Blbmdyb2stb2ZmbGluZS10ZXN0LWNhMCAXDTI2MDgyOTAw
NTkxOVoYDzIxMjYwODA1MDA1OTE5WjAjMSEwHwYDVQQDDBhvcGVuZ3Jvay1vZmZs
aW5lLXRlc3QtY2EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATDU6xZxMLsISRj
2TuHdXIG2Q2shMhVB65SkBMEgmPUgSWXpOcehM9TWiaWhQAAJKlpDzRD6eteleor
f8WVvS+ho1MwUTAdBgNVHQ4EFgQULc18V3KNuinSNJVquA2rd21GXu0wHwYDVR0j
BBgwFoAULc18V3KNuinSNJVquA2rd21GXu0wDwYDVR0TAQH/BAUwAwEB/zAKBggq
hkjOPQQDAgNIADBFAiEAnXy7JkjSItJv8PzNBnJyPUiOblKWskRj7LPFxJev9SAC
IACpj/L6u85BAKByscQ5w0zGSGD2ad68xZW1qgJkWYbn
-----END CERTIFICATE-----
"""


def _empty_context() -> ssl.SSLContext:
    """What a python.org build with no installed CA file gives you: verifying,
    but with zero anchors, so every HTTPS call dies CERTIFICATE_VERIFY_FAILED."""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _FakeResponse:
    def __init__(self, doc: dict) -> None:
        self._raw = json.dumps(doc).encode("utf-8")

    def read(self, _limit: int | None = None) -> bytes:
        return self._raw

    def close(self) -> None:
        pass

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class TlsTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_context = shim._ssl_context
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ca_path = os.path.join(self._tmpdir.name, "test-ca.pem")
        with open(self.ca_path, "w", encoding="utf-8") as fh:
            fh.write(TEST_CA_PEM)

    def tearDown(self) -> None:
        shim._ssl_context = self._saved_context
        self._tmpdir.cleanup()

    def test_populated_default_store_is_used_untouched(self) -> None:
        default = _empty_context()
        default.load_verify_locations(cafile=self.ca_path)

        def no_candidates():
            raise AssertionError("candidates consulted while the default store works")

        with mock.patch.object(shim.ssl, "create_default_context", lambda: default), \
                mock.patch.object(shim, "ca_bundle_candidates", no_candidates):
            self.assertIs(shim.build_ssl_context(), default)

    def test_empty_default_store_is_repaired_from_a_bundle(self) -> None:
        with mock.patch.object(shim.ssl, "create_default_context", _empty_context), \
                mock.patch.object(shim, "SYSTEM_CA_FILES", (self.ca_path,)):
            context = shim.build_ssl_context()
        self.assertGreater(shim.ca_anchor_count(context), 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_unusable_bundle_is_skipped_for_the_next_candidate(self) -> None:
        junk = os.path.join(self._tmpdir.name, "junk.pem")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("not a certificate\n")
        missing = os.path.join(self._tmpdir.name, "absent.pem")
        with mock.patch.object(shim.ssl, "create_default_context", _empty_context), \
                mock.patch.object(shim, "SYSTEM_CA_FILES", (missing, junk, self.ca_path)):
            context = shim.build_ssl_context()
        self.assertGreater(shim.ca_anchor_count(context), 0)

    def test_no_anchors_anywhere_still_verifies(self) -> None:
        """Fail closed. A missing trust store never downgrades verification."""
        with mock.patch.object(shim.ssl, "create_default_context", _empty_context), \
                mock.patch.object(shim, "ca_bundle_candidates", lambda: iter(())), \
                self.assertLogs(shim.log, level="WARNING") as logged:
            context = shim.build_ssl_context()
        self.assertEqual(shim.ca_anchor_count(context), 0)
        self.assertIn("SSL_CERT_FILE", "\n".join(logged.output))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_candidates_skip_paths_that_do_not_exist(self) -> None:
        missing = os.path.join(self._tmpdir.name, "absent.pem")
        with mock.patch.object(shim, "SYSTEM_CA_FILES", (missing, self.ca_path)):
            self.assertNotIn(missing, list(shim.ca_bundle_candidates()))

    def test_context_is_built_once_and_shared(self) -> None:
        built = []

        def build():
            built.append(1)
            return _empty_context()

        shim._ssl_context = None
        with mock.patch.object(shim, "build_ssl_context", build):
            first = shim.ssl_context()
            second = shim.ssl_context()
        self.assertIs(first, second)
        self.assertEqual(len(built), 1)

    def test_probe_sends_the_verified_context(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        seen: dict = {}

        def fake_urlopen(_request, **kwargs):
            seen.update(kwargs)
            return _FakeResponse({"models": [{"slug": "gpt-5.6-sol"}]})

        try:
            store = shim.AuthStore(path=home.auth_path)
            with mock.patch.object(shim.urllib.request, "urlopen", fake_urlopen):
                ok, detail = shim.probe_upstream(force=True, store=store)
        finally:
            home.cleanup()
        self.assertTrue(ok)
        self.assertEqual(detail, "models=1")
        self.assertIs(seen.get("context"), shim.ssl_context())

    def test_responses_call_sends_the_verified_context(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() + 3600))
        seen: dict = {}
        sentinel = object()

        def fake_urlopen(_request, **kwargs):
            seen.update(kwargs)
            return sentinel

        try:
            store = shim.AuthStore(path=home.auth_path)
            with mock.patch.object(shim.urllib.request, "urlopen", fake_urlopen):
                response = shim.open_responses({"model": "gpt-5.6-sol"}, "sess-1", 5.0,
                                               store=store)
        finally:
            home.cleanup()
        self.assertIs(response, sentinel)
        self.assertIs(seen.get("context"), shim.ssl_context())

    def test_token_refresh_sends_the_verified_context(self) -> None:
        home = TempCodexHome(fake_auth_doc(exp=time.time() - 10))
        seen: dict = {}

        def fake_urlopen(_request, **kwargs):
            seen.update(kwargs)
            return _FakeResponse({"access_token": fake_jwt(exp=time.time() + 3600),
                                  "refresh_token": "refresh-fake-rotated"})

        try:
            store = shim.AuthStore(path=home.auth_path)
            with mock.patch.object(shim.urllib.request, "urlopen", fake_urlopen):
                store.credentials()
        finally:
            home.cleanup()
        self.assertIs(seen.get("context"), shim.ssl_context())

    def test_empty_store_verification_failure_names_the_fix(self) -> None:
        shim._ssl_context = _empty_context()
        err = shim.shim_error_from_urlerror(
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "verify failed")))
        self.assertEqual(err.status, 502)
        self.assertEqual(err.type, "api_connection_error")
        self.assertIn("SSL_CERT_FILE", err.message)

    def test_populated_store_verification_failure_does_not_blame_the_store(self) -> None:
        context = _empty_context()
        context.load_verify_locations(cafile=self.ca_path)
        shim._ssl_context = context
        err = shim.shim_error_from_urlerror(
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "verify failed")))
        self.assertEqual(err.status, 502)
        self.assertNotIn("SSL_CERT_FILE", err.message)

    def test_other_url_errors_stay_generic(self) -> None:
        err = shim.shim_error_from_urlerror(urllib.error.URLError(OSError("down")))
        self.assertEqual(err.status, 502)
        self.assertIn("unreachable", err.message)
        self.assertNotIn("SSL_CERT_FILE", err.message)


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
