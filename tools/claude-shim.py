#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1"]
# ///
"""Claude hop shim (:18776) — speaks OpenAI chat/completions on the front,
native Anthropic Messages on the back, and carries the credential itself.

Why this exists (docs/MODEL-GUIDELINES.md §1 Step 4, §2):
  - model-bindings.json may hold ports and slugs only, never a key, so the
    credential has to live shim-side (secrets law).
  - Grok Bot speaks OpenAI chat/completions; Anthropic's own wire is the
    Messages API. A generic OpenAI proxy cannot pin the fields Claude was
    trained with, so the lane feels dumb (§4 "generic harness shape").
  - tools/provider-maps.cjs routes `claude-*`/`:18776` as "claude-passthrough"
    and tools/provider-maps-hop.cjs route "claude-plans" marks thinking
    "shim-owned": THIS file owns thinking, and the hop only forwards an
    explicit `reasoning_effort`.

Behavior:
  - GET  /healthz             -> {"ok":true,"upstream":<bool>} from a cached,
                                 rate-limited probe; never wakes the upstream
                                 per call.
  - GET  /health              -> opengrok route table (the picker scans it).
  - GET  /v1/models           -> OpenAI list shape, from client.models.list().
  - POST /v1/chat/completions -> Anthropic Messages, streamed or not.
  - anything else             -> 404, OpenAI-shaped error JSON.
  - Thinking is pinned to {"type":"adaptive","display":"summarized"} on every
    request. budget_tokens is REMOVED on the Claude 5 family (400) and is
    never emitted. Effort rides the slug suffix (-low|-medium|-high|-xhigh|
    -max, §3) or body `reasoning_effort`; absent both, the API default (high)
    stands and `output_config` is omitted.
  - temperature / top_p / top_k are REMOVED on the Claude 5 family (400), so
    they are dropped and named in the audit log line — never silently honored.
  - Prompt caching: one ephemeral breakpoint on the system block, one on the
    last user block, so a growing agent transcript reads its own prefix.
  - Credentials: the SDK's own chain resolves them (ANTHROPIC_API_KEY ->
    ANTHROPIC_AUTH_TOKEN -> `ant auth login` profile). An inbound
    Authorization header is ignored, never forwarded, never logged. Bodies,
    prompts and tool arguments are never logged.

Env:
  CLAUDE_SHIM_HOST        (default 127.0.0.1 — loopback; see below)
  CLAUDE_SHIM_PORT        (default 18776)
  CLAUDE_SHIM_TIMEOUT     (default 1800 seconds; long agent turns)
  CLAUDE_SHIM_MAX_TOKENS  (default 128000 — the model ceiling; also the clamp)
  CLAUDE_SHIM_LOG_LEVEL   (default INFO)

  Bind stays on loopback by default. A remote Grok Bot cloud box reaches this
  shim by binding it to a Tailscale IP (or 0.0.0.0 behind a firewall):
  CLAUDE_SHIM_HOST=100.x.y.z, or --host. Anything non-loopback is an open
  Anthropic credential to whoever can route to it — put it on a tailnet only.

Run:
  uv run tools/claude-shim.py            # PEP 723 metadata installs anthropic
  python3 tools/claude-shim.py           # plain python3, anthropic installed
  python3 tools/claude-shim.py --check   # probe upstream, exit 0/1 (doctor)

Run persistence:
  macOS launchd plist example in examples/ (see docs); Windows Startup .vbs
  calling `pythonw claude-shim.py`, systemd unit on Linux — same shape as the
  sibling shims.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.parse import urlsplit

try:  # the pure translation helpers stay importable (and unit-testable) offline
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised only without the dep
    anthropic = None  # type: ignore[assignment]

log = logging.getLogger("claude-shim")

HOST = os.environ.get("CLAUDE_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_SHIM_PORT", "18776"))
TIMEOUT = float(os.environ.get("CLAUDE_SHIM_TIMEOUT", "1800"))
MAX_TOKENS_CAP = int(os.environ.get("CLAUDE_SHIM_MAX_TOKENS", "128000"))
LOG_LEVEL = os.environ.get("CLAUDE_SHIM_LOG_LEVEL", "INFO").upper()

DEFAULT_MAX_TOKENS_STREAM = 64000
DEFAULT_MAX_TOKENS_BLOCKING = 16000
MAX_BODY = 64 * 1024 * 1024
PROBE_TTL = 60.0          # /healthz probe cache
MODELS_TTL = 900.0        # models.list() cache
THINKING_CACHE_SIZE = 512

# Effort ladder for the Claude 5 family (output_config.effort).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# OpenAI's reasoning_effort has one token the Anthropic ladder lacks.
EFFORT_ALIASES = {"minimal": "low"}
# Pinned on every request: budget_tokens is rejected (400) on Fable 5 /
# Opus 5 / Sonnet 5; adaptive is the only shape, and "summarized" is what
# makes the reasoning visible at all (the default is "omitted", empty text).
THINKING = {"type": "adaptive", "display": "summarized"}

# Static fallback for /health and /v1/models when the Models API is unreachable.
FALLBACK_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5"]

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)

# OpenAI body keys with no wire on the Claude 5 family. Dropped, never faked:
#   temperature/top_p/top_k -> removed on Claude 5 (400 if sent)
#   n, logprobs, top_logprobs, penalties, seed, logit_bias -> no Messages field
#   response_format -> Anthropic has output_config.format, but silently
#     reshaping a caller's schema is a different contract; fail closed.
DROPPED_KEYS = (
    "temperature", "top_p", "top_k", "n", "logprobs", "top_logprobs",
    "presence_penalty", "frequency_penalty", "seed", "logit_bias",
    "response_format", "service_tier", "store", "prediction",
)

CRED_HELP = (
    "no Anthropic credentials resolved: set ANTHROPIC_API_KEY (or "
    "ANTHROPIC_AUTH_TOKEN) in this shim's environment, or run `ant auth login` "
    "as the user that runs the shim"
)

# anthropic SDK 1.x message emitted at REQUEST time (not construction time) when
# neither ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN nor an `ant auth login`
# profile resolves. anthropic.Anthropic() itself constructs fine with zero
# credentials on this SDK version; the client raises a builtins.TypeError
# lazily, the first time a call is actually made.
CRED_RESOLUTION_MSG = "Could not resolve authentication method"


def is_credential_resolution_error(exc: Exception) -> bool:
    """True for the SDK 1.x TypeError raised at request time with no credential.

    Pure/offline-testable: matches on exception type and message text only,
    never touches the network or the client.
    """
    return isinstance(exc, TypeError) and CRED_RESOLUTION_MSG in str(exc)


class ShimError(Exception):
    """Client-visible failure with an HTTP status and OpenAI error shape."""

    def __init__(self, status: int, message: str, type_: str = "invalid_request_error",
                 code: str | None = None, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.retry_after = retry_after

    def payload(self) -> dict[str, Any]:
        return error_payload(self.message, self.type, self.code)


def error_payload(message: str, type_: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code}}


# --- thinking-block replay ---------------------------------------------------
# shared/model-migration.md (Fable 5 / Opus 5 / Sonnet 5): "When continuing a
# conversation on the same model, pass thinking blocks back to the API
# unchanged (the standard multi-turn pattern; dropping or editing them breaks
# the turn)" and "Don't strip regular thinking blocks either: removing them can
# trigger ordering/signature 400s." The OpenAI wire has no field that carries
# them, so the shim keeps its own bounded map from tool_use id -> that
# assistant turn's thinking blocks and re-attaches them when the client replays
# the same assistant message in a tool-use loop.

class ThinkingCache:
    """Bounded LRU: tool_use id -> that turn's thinking blocks (verbatim)."""

    def __init__(self, capacity: int = THINKING_CACHE_SIZE) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def put(self, key: str, blocks: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items[key] = blocks
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get(self, key: str) -> list[dict[str, Any]] | None:
        with self._lock:
            blocks = self._items.get(key)
            if blocks is not None:
                self._items.move_to_end(key)
            return blocks


THINKING_CACHE = ThinkingCache()


@dataclass
class RequestContext:
    """Per-request bookkeeping the response path and the audit line need."""

    model: str                              # the slug the client asked for
    anthropic_model: str                    # slug with the effort suffix removed
    effort: str | None
    stream: bool
    max_tokens: int
    dropped: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)  # sanitized -> original
    include_usage: bool = True
    replayed_thinking: int = 0
    usage: dict[str, Any] | None = None      # OpenAI-shaped, for the audit line
    cache_create: int = 0                    # cache_creation_input_tokens

    def original_tool_name(self, name: str) -> str:
        return self.tool_names.get(name, name)

    def route_label(self) -> str:
        return "effort=%s thinking=adaptive/summarized replay=%d" % (
            self.effort or "api-default-high", self.replayed_thinking)


# --- pure translation helpers ------------------------------------------------

def effort_from_slug(model: str) -> tuple[str, str | None]:
    """Split `<model-id>[-<effort>]`. Unknown suffixes are part of the id."""
    for eff in EFFORTS:
        suffix = "-" + eff
        if model.endswith(suffix) and len(model) > len(suffix):
            return model[: -len(suffix)], eff
    return model, None


def normalize_effort(value: Any) -> str:
    """Map a body `reasoning_effort` onto the ladder. Fail closed on junk."""
    if not isinstance(value, str):
        raise ShimError(400, "reasoning_effort must be a string, one of: %s"
                        % ", ".join(EFFORTS))
    token = EFFORT_ALIASES.get(value.strip().lower(), value.strip().lower())
    if token not in EFFORTS:
        raise ShimError(400, "unknown reasoning_effort %r; expected one of: %s"
                        % (value, ", ".join(EFFORTS)))
    return token


def sanitize_tool_name(name: str) -> str:
    """Deterministic ^[a-zA-Z0-9_-]{1,128}$ name; a digest keeps it unique."""
    if isinstance(name, str) and TOOL_NAME_RE.match(name):
        return name
    raw = name if isinstance(name, str) else str(name)
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw) or "tool"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]  # naming, not security
    return (cleaned[:119] + "_" + digest)[:128]


def dropped_keys(body: dict[str, Any]) -> list[str]:
    """Body keys this lane cannot honor. Named in the audit line, never faked.

    A default-valued knob (n=1, logprobs=false) asks for nothing, so it is not
    reported — only a caller asking for behavior that has no wire shows up.
    """
    out: list[str] = []
    for key in DROPPED_KEYS:
        value = body.get(key)
        if value is None or value is False:
            continue
        if key == "n" and value == 1:
            continue
        out.append(key)
    return out


def _text_of(content: Any) -> str:
    """Flatten an OpenAI content value (string or part list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _image_block(url: str, dropped: list[str]) -> dict[str, Any] | None:
    match = DATA_URL_RE.match(url)
    if match:
        media_type = match.group(1)
        data = re.sub(r"\s+", "", match.group(2))
        try:  # reject a corrupt data: URL here rather than at the upstream
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            dropped.append("image_url:bad-base64")
            return None
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": media_type, "data": data}}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    dropped.append("image_url:unsupported-scheme")
    return None


def user_content_blocks(content: Any, dropped: list[str]) -> list[dict[str, Any]]:
    """OpenAI user content (string or part list) -> Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            dropped.append("content:non-object")
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = str(part.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": text})
        elif ptype == "image_url":
            ref = part.get("image_url")
            url = ref.get("url", "") if isinstance(ref, dict) else str(ref or "")
            block = _image_block(url, dropped)
            if block is not None:
                blocks.append(block)
        else:
            dropped.append("content:%s" % (ptype or "unknown"))
    return blocks


def assistant_content_blocks(msg: dict[str, Any], dropped: list[str],
                             thinking_cache: ThinkingCache | None) -> tuple[list[dict[str, Any]], int]:
    """Assistant text + tool_calls -> text/tool_use blocks, thinking replayed."""
    blocks: list[dict[str, Any]] = []
    text = _text_of(msg.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    tool_calls = msg.get("tool_calls") or []
    replayed = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            dropped.append("tool_call:non-object")
            continue
        fn = call.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args: Any = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                # The upstream needs an object; a broken replay must not 400 the
                # whole conversation, so pass {} and say so in the audit line.
                args = {}
                dropped.append("tool_call:unparsable-arguments")
        else:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
            dropped.append("tool_call:non-object-arguments")
        blocks.append({
            "type": "tool_use",
            "id": str(call.get("id") or ""),
            "name": sanitize_tool_name(str(fn.get("name") or "")),
            "input": args,
        })
    if tool_calls and thinking_cache is not None:
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            remembered = thinking_cache.get(str(call.get("id") or ""))
            if remembered:
                blocks = list(remembered) + blocks
                replayed = len(remembered)
                break
    return blocks, replayed


def tool_result_block(msg: dict[str, Any]) -> dict[str, Any]:
    tool_use_id = msg.get("tool_call_id")
    if not tool_use_id:
        raise ShimError(400, "tool message without tool_call_id")
    return {"type": "tool_result", "tool_use_id": str(tool_use_id),
            "content": _text_of(msg.get("content"))}


def convert_tools(tools: Any, ctx_names: dict[str, str],
                  dropped: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            dropped.append("tool:non-object")
            continue
        if tool.get("type") not in (None, "function"):
            dropped.append("tool:%s" % tool.get("type"))
            continue
        fn = tool.get("function") or {}
        original = str(fn.get("name") or "")
        if not original:
            dropped.append("tool:unnamed")
            continue
        name = sanitize_tool_name(original)
        ctx_names[name] = original
        entry: dict[str, Any] = {
            "name": name,
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        description = fn.get("description")
        if description:
            entry["description"] = str(description)
        out.append(entry)
    return out


def convert_tool_choice(choice: Any, dropped: list[str]) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        mapped = {"auto": {"type": "auto"}, "none": {"type": "none"},
                  "required": {"type": "any"}}.get(choice)
        if mapped is None:
            dropped.append("tool_choice:%s" % choice)
        return mapped
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        name = fn.get("name")
        if name:
            return {"type": "tool", "name": sanitize_tool_name(str(name))}
        ctype = choice.get("type")
        if ctype in ("auto", "any", "none"):
            return {"type": ctype}
    dropped.append("tool_choice:unsupported")
    return None


def to_anthropic(body: dict[str, Any],
                 thinking_cache: ThinkingCache | None = THINKING_CACHE
                 ) -> tuple[dict[str, Any], RequestContext]:
    """OpenAI chat/completions body -> messages.stream()/create() kwargs.

    Pure: no I/O, no client. Everything the response path needs afterwards
    rides in the returned RequestContext.
    """
    if not isinstance(body, dict):
        raise ShimError(400, "request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ShimError(400, "model is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ShimError(400, "messages must be a non-empty array")

    anthropic_model, slug_effort = effort_from_slug(model)
    effort = slug_effort
    if body.get("reasoning_effort") is not None:  # explicit body value wins
        effort = normalize_effort(body["reasoning_effort"])

    stream = bool(body.get("stream"))
    dropped: list[str] = dropped_keys(body)

    system_parts: list[str] = []
    anth_messages: list[dict[str, Any]] = []
    replayed = 0
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            anth_messages.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            raise ShimError(400, "each message must be a JSON object")
        role = msg.get("role")
        if role in ("system", "developer"):
            flush_results()
            text = _text_of(msg.get("content"))
            if text:
                system_parts.append(text)
        elif role == "tool":
            pending_results.append(tool_result_block(msg))
        elif role == "user":
            flush_results()
            blocks = user_content_blocks(msg.get("content"), dropped)
            if blocks:
                anth_messages.append({"role": "user", "content": blocks})
        elif role == "assistant":
            flush_results()
            blocks, count = assistant_content_blocks(msg, dropped, thinking_cache)
            replayed += count
            if blocks:
                anth_messages.append({"role": "assistant", "content": blocks})
        else:
            raise ShimError(400, "unsupported message role %r" % (role,))
    flush_results()

    if not anth_messages:
        raise ShimError(400, "messages contain no content the Messages API accepts")

    max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS_STREAM if stream else DEFAULT_MAX_TOKENS_BLOCKING
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        raise ShimError(400, "max_tokens must be an integer") from None
    if max_tokens < 1:
        raise ShimError(400, "max_tokens must be >= 1")
    max_tokens = min(max_tokens, MAX_TOKENS_CAP)

    kwargs: dict[str, Any] = {
        "model": anthropic_model,
        "max_tokens": max_tokens,
        "messages": anth_messages,
        "thinking": dict(THINKING),
    }
    if system_parts:
        # One cache breakpoint: tools + system render before messages, so the
        # marker on the system block caches both (shared/prompt-caching.md).
        kwargs["system"] = [{"type": "text", "text": "\n".join(system_parts),
                             "cache_control": {"type": "ephemeral"}}]
    if effort:
        kwargs["output_config"] = {"effort": effort}

    ctx = RequestContext(model=model, anthropic_model=anthropic_model, effort=effort,
                         stream=stream, max_tokens=max_tokens, dropped=dropped,
                         replayed_thinking=replayed)

    tools = convert_tools(body.get("tools"), ctx.tool_names, dropped)
    tool_choice = convert_tool_choice(body.get("tool_choice"), dropped)
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice  # incl. {"type":"none"} (tool-use-concepts.md)
    if body.get("parallel_tool_calls") is False and tools:
        # disable_parallel_tool_use rides on tool_choice (tool-use-concepts.md).
        choice = dict(kwargs.get("tool_choice") or {"type": "auto"})
        choice["disable_parallel_tool_use"] = True
        kwargs["tool_choice"] = choice

    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        kwargs["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        seqs = [str(s) for s in stop if isinstance(s, str) and s]
        if seqs:
            kwargs["stop_sequences"] = seqs

    # Second breakpoint: the last block of the last user turn, so the growing
    # agent transcript reads its own prefix on the next turn.
    for message in reversed(anth_messages):
        if message["role"] == "user" and message["content"]:
            last_block = message["content"][-1]
            if isinstance(last_block, dict) and "cache_control" not in last_block:
                last_block["cache_control"] = {"type": "ephemeral"}
            break

    options = body.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage") is False:
        ctx.include_usage = False
    return kwargs, ctx


def map_finish_reason(stop_reason: str | None) -> str | None:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "pause_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(stop_reason or "", "stop" if stop_reason else None)


def usage_token(usage: Any, name: str) -> int:
    value = getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) else 0


def usage_dict(usage: Any) -> dict[str, Any]:
    """Anthropic usage -> OpenAI usage. prompt_tokens counts cached tokens too,
    which is the OpenAI convention (cached_tokens is a subset of it)."""
    cache_read = usage_token(usage, "cache_read_input_tokens")
    cache_create = usage_token(usage, "cache_creation_input_tokens")
    prompt = usage_token(usage, "input_tokens") + cache_read + cache_create
    completion = usage_token(usage, "output_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cache_read},
    }


def sse_chunk(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n\n"


def chunk_envelope(chat_id: str, model: str, created: int, delta: dict[str, Any],
                   finish_reason: str | None = None,
                   usage: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def openai_completion_from_message(message: Any, ctx: RequestContext,
                                   created: int) -> dict[str, Any]:
    """Final Anthropic Message -> one OpenAI chat.completion object."""
    texts: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in getattr(message, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            texts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            summary = getattr(block, "thinking", "") or ""
            if summary:
                reasoning.append(summary)
        elif btype == "tool_use":
            tool_calls.append({
                "index": len(tool_calls),
                "id": getattr(block, "id", ""),
                "type": "function",
                "function": {
                    "name": ctx.original_tool_name(getattr(block, "name", "")),
                    "arguments": json.dumps(getattr(block, "input", {}) or {},
                                            separators=(",", ":")),
                },
            })
    text = "".join(texts)
    msg: dict[str, Any] = {"role": "assistant",
                           "content": text if text or not tool_calls else None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-" + str(getattr(message, "id", "") or ""),
        "object": "chat.completion",
        "created": created,
        "model": ctx.model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": map_finish_reason(getattr(message, "stop_reason", None))}],
        "usage": usage_dict(getattr(message, "usage", None)),
    }


def remember_thinking(message: Any, cache: ThinkingCache = THINKING_CACHE) -> None:
    """Keep this turn's thinking blocks keyed by the tool_use ids it produced,
    so the next request can replay them verbatim (model-migration.md)."""
    try:
        data = message.to_dict()
    except Exception:  # never let bookkeeping break a served response
        return
    content = data.get("content") or []
    thinking = [b for b in content if isinstance(b, dict)
                and b.get("type") in ("thinking", "redacted_thinking")]
    if not thinking:
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
            cache.put(str(block["id"]), thinking)


# --- upstream client ---------------------------------------------------------

_client_lock = threading.Lock()
_client: Any = None
_probe_state: dict[str, Any] = {"ts": 0.0, "ok": False, "detail": "not probed"}
_models_state: dict[str, Any] = {"ts": 0.0, "models": list(FALLBACK_MODELS), "live": False}


def get_client() -> Any:
    """Zero-arg client: the SDK resolves ANTHROPIC_API_KEY, then
    ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile. This shim never
    reads a key from a request, a keychain, or another tool's credential file."""
    global _client
    if anthropic is None:
        raise ShimError(500, "the `anthropic` SDK is not installed: run "
                             "`uv run tools/claude-shim.py` or `pip install 'anthropic>=1'`",
                        type_="api_error")
    with _client_lock:
        if _client is None:
            try:
                _client = anthropic.Anthropic()
            except Exception as exc:  # constructor only fails when nothing resolves
                raise ShimError(401, "%s (%s)" % (CRED_HELP, type(exc).__name__),
                                type_="authentication_error") from None
        return _client


def call_client() -> Any:
    return get_client().with_options(timeout=TIMEOUT)


def probe_upstream(force: bool = False) -> tuple[bool, str]:
    """One cheap models.list() call, cached, so /healthz never wakes the API."""
    now = time.monotonic()
    if not force and (now - float(_probe_state["ts"])) < PROBE_TTL and _probe_state["ts"]:
        return bool(_probe_state["ok"]), str(_probe_state["detail"])
    try:
        client = get_client().with_options(timeout=15.0)
        count = len(list_model_ids(client))
        ok, detail = True, "models=%d" % count
    except ShimError as exc:
        ok, detail = False, exc.type
    except Exception as exc:
        if is_credential_resolution_error(exc):
            ok, detail = False, "no_credentials"
        else:
            ok, detail = False, type(exc).__name__  # class name only; never the body
    _probe_state.update({"ts": now, "ok": ok, "detail": detail})
    return ok, detail


def list_model_ids(client: Any) -> list[str]:
    """Live claude-* ids from the Models API (iterate the page, not .data)."""
    ids: list[str] = []
    for model in client.models.list():
        model_id = getattr(model, "id", "")
        if isinstance(model_id, str) and model_id.startswith("claude-"):
            ids.append(model_id)
        if len(ids) >= 500:
            break
    return ids


def cached_models() -> tuple[list[str], bool]:
    """(model ids, live) — falls back to the static list when unreachable."""
    now = time.monotonic()
    if _models_state["ts"] and (now - float(_models_state["ts"])) < MODELS_TTL:
        return list(_models_state["models"]), bool(_models_state["live"])
    try:
        ids = list_model_ids(get_client().with_options(timeout=15.0))
    except Exception as exc:
        log.warning("models.list failed: %s", type(exc).__name__)
        ids = []
    if ids:
        _models_state.update({"ts": now, "models": ids, "live": True})
    else:
        _models_state.update({"ts": now, "models": list(FALLBACK_MODELS), "live": False})
    return list(_models_state["models"]), bool(_models_state["live"])


def health_table(host: str, port: int) -> dict[str, Any]:
    models, live = cached_models()
    ok, detail = probe_upstream()
    return {
        "claude-api": {
            "configured": ok,
            "keys": False,
            "upstream": "anthropic-messages-api",
            "shim": "http://%s:%d/v1" % (host, port),
            "models": models,
            "models_source": "models.list" if live else "static-fallback",
            "thinking": dict(THINKING),
            "effort": "slug suffix -low|-medium|-high|-xhigh|-max, or body "
                      "reasoning_effort; API default high when omitted",
            "max_tokens_ceiling": MAX_TOKENS_CAP,
            "probe": detail,
            "note": "OpenAI chat/completions in, native Anthropic Messages out. "
                    "Shim owns thinking (adaptive+summarized); temperature/top_p/"
                    "top_k are removed on Claude 5 and are dropped, not honored.",
        }
    }


def openai_models_payload() -> dict[str, Any]:
    models, _live = cached_models()
    # `created` has no counterpart on the Models API (no creation timestamp is
    # documented), so it is reported as 0 rather than invented.
    return {"object": "list",
            "data": [{"id": mid, "object": "model", "owned_by": "anthropic", "created": 0}
                     for mid in models]}


def shim_error_from_exception(exc: Exception) -> ShimError:
    """SDK exception -> client-visible error, most specific first."""
    if is_credential_resolution_error(exc):
        return ShimError(401, "%s (upstream: no credential resolved)" % CRED_HELP,
                         type_="authentication_error", code="401")
    if anthropic is not None:
        if isinstance(exc, anthropic.AuthenticationError):
            return ShimError(401, "%s (upstream: %s)" % (CRED_HELP, exc.message),
                             type_="authentication_error", code="401")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ShimError(403, exc.message, type_="permission_error", code="403")
        if isinstance(exc, anthropic.NotFoundError):
            return ShimError(404, exc.message, type_="not_found_error", code="404")
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = None
            headers = getattr(getattr(exc, "response", None), "headers", None)
            if headers is not None:
                retry_after = headers.get("retry-after")
            return ShimError(429, exc.message, type_="rate_limit_error", code="429",
                             retry_after=retry_after)
        if isinstance(exc, anthropic.APIStatusError):
            status = int(getattr(exc, "status_code", 500) or 500)
            return ShimError(status, exc.message,
                             type_=str(getattr(exc, "type", None) or "api_error"),
                             code=str(status))
        if isinstance(exc, anthropic.APIConnectionError):
            return ShimError(502, "upstream unreachable: %s" % type(exc).__name__,
                             type_="api_connection_error", code="502")
    return ShimError(500, "shim failure: %s" % type(exc).__name__, type_="api_error")


# --- HTTP server -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-shim/1"

    # Inbound Authorization is ignored on purpose: the credential is shim-side
    # (docs/MODEL-GUIDELINES.md §2). It is never read, forwarded, or logged.

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --
    def _json(self, code: int, payload: dict[str, Any],
              extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, err: ShimError) -> None:
        headers = [("Retry-After", err.retry_after)] if err.retry_after else []
        self._json(err.status, err.payload(), headers)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ShimError(413, "request body too large", type_="request_too_large")
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ShimError(400, "request body is not valid JSON") from None

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_sse(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        started = time.monotonic()
        try:
            if path == "/healthz":
                ok, _detail = probe_upstream()
                self._json(200, {"ok": True, "service": "claude-shim",
                                 "port": self.server.server_address[1], "upstream": ok})
            elif path == "/health":
                host, port = self.server.server_address[0], self.server.server_address[1]
                self._json(200, health_table(host, port))
            elif path == "/v1/models":
                self._json(200, openai_models_payload())
            else:
                raise ShimError(404, "unknown path %s" % path, type_="not_found_error",
                                code="404")
        except ShimError as err:
            self._fail(err)
            self._audit("GET", path, err.status, started)
            return
        self._audit("GET", path, 200, started)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        started = time.monotonic()
        if path != "/v1/chat/completions":
            err = ShimError(404, "unknown path %s" % path, type_="not_found_error", code="404")
            self._fail(err)
            self._audit("POST", path, 404, started)
            return
        ctx: RequestContext | None = None
        try:
            body = self._read_body()
            kwargs, ctx = to_anthropic(body)
            status = self._serve_stream(kwargs, ctx) if ctx.stream \
                else self._serve_blocking(kwargs, ctx)
        except ShimError as err:
            self._fail(err)
            self._audit("POST", path, err.status, started, ctx)
            return
        except (BrokenPipeError, ConnectionResetError):
            log.info("client aborted mid-response")
            return
        except Exception as exc:  # noqa: BLE001 - mapped to a status, never leaked
            err = shim_error_from_exception(exc)
            self._fail(err)
            self._audit("POST", path, err.status, started, ctx)
            return
        self._audit("POST", path, status, started, ctx)

    # -- upstream calls --
    def _serve_blocking(self, kwargs: dict[str, Any], ctx: RequestContext) -> int:
        # Always stream upstream: the SDK refuses blocking calls it estimates
        # will outlive the idle-connection window at large max_tokens.
        with call_client().messages.stream(**kwargs) as stream:
            for _event in stream:
                pass
            message = stream.get_final_message()
        remember_thinking(message)
        payload = openai_completion_from_message(message, ctx, int(time.time()))
        ctx.usage = payload["usage"]
        ctx.cache_create = usage_token(getattr(message, "usage", None),
                                       "cache_creation_input_tokens")
        self._json(200, payload)
        return 200

    def _serve_stream(self, kwargs: dict[str, Any], ctx: RequestContext) -> int:
        manager = call_client().messages.stream(**kwargs)
        try:  # entering makes the request: an auth/rate error still gets a status
            stream = manager.__enter__()
        except Exception as exc:  # noqa: BLE001 - mapped to an HTTP status
            raise shim_error_from_exception(exc) from None

        created = int(time.time())
        chat_id = "chatcmpl-"
        tool_index = -1
        opened = False
        try:
            self._start_sse()
            opened = True
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "message_start":
                    chat_id = "chatcmpl-" + str(getattr(event.message, "id", "") or "")
                    self._write_sse(sse_chunk(chunk_envelope(
                        chat_id, ctx.model, created, {"role": "assistant", "content": ""})))
                elif etype == "content_block_start":
                    block = event.content_block
                    if getattr(block, "type", "") == "tool_use":
                        tool_index += 1
                        self._write_sse(sse_chunk(chunk_envelope(
                            chat_id, ctx.model, created,
                            {"tool_calls": [{
                                "index": tool_index,
                                "id": getattr(block, "id", ""),
                                "type": "function",
                                "function": {
                                    "name": ctx.original_tool_name(getattr(block, "name", "")),
                                    "arguments": "",
                                },
                            }]})))
                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        self._write_sse(sse_chunk(chunk_envelope(
                            chat_id, ctx.model, created,
                            {"content": getattr(delta, "text", "")})))
                    elif dtype == "thinking_delta":
                        summary = getattr(delta, "thinking", "")
                        if summary:
                            self._write_sse(sse_chunk(chunk_envelope(
                                chat_id, ctx.model, created,
                                {"reasoning_content": summary})))
                    elif dtype == "input_json_delta" and tool_index >= 0:
                        self._write_sse(sse_chunk(chunk_envelope(
                            chat_id, ctx.model, created,
                            {"tool_calls": [{
                                "index": tool_index,
                                "function": {"arguments": getattr(delta, "partial_json", "")},
                            }]})))
            message = stream.get_final_message()
            remember_thinking(message)
            ctx.usage = usage_dict(getattr(message, "usage", None))
            ctx.cache_create = usage_token(getattr(message, "usage", None),
                                           "cache_creation_input_tokens")
            finish = map_finish_reason(getattr(message, "stop_reason", None))
            self._write_sse(sse_chunk(chunk_envelope(
                chat_id, ctx.model, created, {}, finish,
                ctx.usage if ctx.include_usage else None)))
            self._write_sse(b"data: [DONE]\n\n")
            self._end_sse()
        except (BrokenPipeError, ConnectionResetError):
            log.info("client aborted mid-stream")
            return 200
        except ShimError:
            raise
        except Exception as exc:  # noqa: BLE001 - the stream already carries 200
            err = shim_error_from_exception(exc)
            if not opened:
                raise err from None
            log.warning("stream failed: %s", err.type)
            self._write_sse(sse_chunk(err.payload()))
            self._write_sse(b"data: [DONE]\n\n")
            self._end_sse()
            return err.status
        finally:
            manager.__exit__(None, None, None)
        return 200

    # -- audit --
    def _audit(self, method: str, path: str, status: int, started: float,
               ctx: RequestContext | None = None) -> None:
        """Metadata only. No headers, no keys, no prompts, no tool arguments."""
        usage = (ctx.usage if ctx else None) or {}
        details = usage.get("prompt_tokens_details") or {}
        log.info(
            "%s %s model=%s stream=%s status=%d duration_ms=%d input_tokens=%s "
            "output_tokens=%s cache_read=%s cache_create=%s dropped=%s route=%s",
            method, path, ctx.model if ctx else "-",
            str(bool(ctx and ctx.stream)).lower(), status,
            int((time.monotonic() - started) * 1000),
            usage.get("prompt_tokens", "-"), usage.get("completion_tokens", "-"),
            details.get("cached_tokens", "-"), ctx.cache_create if usage else "-",
            ctx.dropped if ctx else [], ctx.route_label() if ctx else "-",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude hop shim: OpenAI wire -> "
                                                 "Anthropic Messages API")
    parser.add_argument("--host", default=HOST, help="bind address (default %s)" % HOST)
    parser.add_argument("--port", type=int, default=PORT, help="bind port (default %d)" % PORT)
    parser.add_argument("--check", action="store_true",
                        help="probe the upstream once, print the result, exit 0/1")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.check:
        ok, detail = probe_upstream(force=True)
        if not ok and detail == "no_credentials":
            print("claude-shim upstream UNREACHABLE: no Anthropic credentials resolved "
                  "(%s)" % CRED_HELP)
            return 1
        print("claude-shim upstream %s (%s)" % ("OK" if ok else "UNREACHABLE", detail))
        return 0 if ok else 1

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    ok, detail = probe_upstream(force=True)
    detail_msg = "no Anthropic credentials resolved" if detail == "no_credentials" else detail
    log.info("claude-shim listening http://%s:%d/v1 -> Anthropic Messages API "
             "(upstream_reachable=%s, %s)", args.host, args.port, ok, detail_msg)
    if not ok:
        log.warning("upstream probe failed (%s) — %s", detail_msg, CRED_HELP)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
