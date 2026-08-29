#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Codex hop shim (:18777) — speaks OpenAI chat/completions on the front, the
ChatGPT Codex backend (Responses API) on the back, and carries the credential
itself.

Why this exists (docs/MODEL-GUIDELINES.md §1 Step 4, §2):
  - model-bindings.json may hold ports and slugs only, never a key, so the
    credential has to live shim-side (secrets law).
  - Grok Bot speaks OpenAI chat/completions; the ChatGPT Codex backend speaks
    the Responses API and authenticates by ChatGPT plan session, not by an API
    key. That is exactly the "subscription-auth" case that needs a hop shim.
  - The plan credential is the Codex CLI "Sign in with ChatGPT" OAuth token.
    OpenAI supports that token outside the Codex CLI, so this shim reuses the
    CLI's own store (~/.codex/auth.json) instead of minting a second identity.

Behavior:
  - GET  /healthz             -> {"ok":true,"upstream":<bool>} from a cached,
                                 rate-limited probe (one authenticated GET on
                                 the backend model list; no inference).
  - GET  /health              -> opengrok route table under key "codex-plans".
  - GET  /v1/models           -> OpenAI list shape, from ~/.codex/models_cache.json.
  - POST /v1/chat/completions -> Responses API, streamed or not.
  - anything else             -> 404, OpenAI-shaped error JSON.
  - Effort rides the slug suffix (-low|-medium|-high|-xhigh|-max|-ultra, §3) or
    body `reasoning_effort` (which wins). An optional `-fast` suffix comes LAST,
    after the effort suffix (`gpt-5.6-sol-high-fast`), and maps to
    service_tier="priority".
  - temperature / top_p / max_tokens have no wire here (the backend answers 400
    "Unsupported parameter"), so they are dropped and named in the audit log
    line — never silently honored, never faked.
  - Reasoning: {"effort": ..., "summary": "auto"} plus
    include=["reasoning.encrypted_content"] with store=false. Encrypted
    reasoning items are cached per function call_id and replayed on the next
    turn, which is what keeps a multi-turn tool loop coherent when the caller's
    OpenAI-shaped transcript has no field to carry them.
  - Credentials: ~/.codex/auth.json only (CODEX_HOME overrides the directory).
    OPENAI_API_KEY is NEVER read as a fallback: that would silently move the
    lane from the ChatGPT plan to API billing. An inbound Authorization header
    is stripped, never forwarded, never logged. Bodies, prompts, tool arguments
    and tokens are never logged.

Credential step (the only one):
  codex login                  # or: codex login --device-auth  (headless box)

  The shim refreshes that token in place, in the SAME auth.json the CLI uses.
  Refresh tokens ROTATE and are single-use (the backend answers
  "refresh_token_reused" to a replay), so the shim and the Codex CLI must share
  ONE auth.json — they do, by design. Copying auth.json to another machine and
  refreshing there invalidates the token here: log in on each machine instead.

Env:
  CODEX_SHIM_HOST         (default 127.0.0.1 — loopback; see below)
  CODEX_SHIM_PORT         (default 18777)
  CODEX_SHIM_TIMEOUT      (default 1800 seconds; long agent turns)
  CODEX_HOME              (default ~/.codex — auth.json + models_cache.json)
  CODEX_SHIM_LOG_LEVEL    (default INFO)

  Bind stays on loopback by default. A remote Grok Bot cloud box reaches this
  shim by binding it to a Tailscale IP (or 0.0.0.0 behind a firewall):
  CODEX_SHIM_HOST=100.x.y.z, or --host. Anything non-loopback is an open
  ChatGPT plan credential to whoever can route to it — put it on a tailnet only.

Run:
  python3 tools/codex-shim.py            # stdlib only, nothing to install
  uv run tools/codex-shim.py             # same thing (PEP 723, no dependencies)
  python3 tools/codex-shim.py --check    # probe upstream, exit 0/1 (doctor)

  TLS trust: every outbound call verifies the certificate, always. The trust
  store is Python's default one (SSL_CERT_FILE / SSL_CERT_DIR included); when
  that store is EMPTY — a python.org build whose "Install Certificates.command"
  never ran points at a CA file that does not exist — the shim loads the host
  bundle OpenSSL and curl already use (/etc/ssl/cert.pem and the Linux
  equivalents), or certifi when it is importable. Verification is never
  disabled: with no anchors anywhere the call fails closed as a 502 that names
  the fix.

Run persistence:
  macOS launchd plist example in examples/ (see docs); Windows Startup .vbs
  calling `pythonw codex-shim.py`, systemd unit on Linux — same shape as the
  sibling shims.

## Wire facts (sources)

Every backend field and header below is copied from a primary source, not from
memory. Anything a source did not show is dropped, not guessed.

  1. Base URL + path: `https://chatgpt.com/backend-api/codex/responses`.
     openai/codex codex-rs/http-client/src/chatgpt_cloudflare_cookies.rs and
     codex-rs/response-debug-context/src/lib.rs; chatgpt_base_url default in
     codex-rs/config/defaults.toml. Same URL in the opencode plugin:
     https://github.com/numman-ali/opencode-openai-codex-auth/blob/HEAD/lib/constants.ts
  2. Model list probe: `GET <base>/models?client_version=<v>`.
     codex-rs/codex-api/src/endpoint/models.rs (path "models", client_version
     query, ETag). Confirmed live: 200 + {"models":[...]}.
  3. Headers: Authorization: Bearer <access_token>; chatgpt-account-id;
     originator: codex_cli_rs; OpenAI-Beta: responses=experimental; session_id;
     Accept: text/event-stream; User-Agent.
     - chatgpt-account-id: codex-rs/model-provider/src/auth.rs
       (`headers.insert("ChatGPT-Account-ID", ...)`, HTTP header names are
       case-insensitive).
     - originator default "codex_cli_rs": codex-rs/login/src/auth/default_client.rs
       (DEFAULT_ORIGINATOR).
     - OpenAI-Beta: responses=experimental and session_id: the opencode plugin
       lib/constants.ts + lib/request/fetch-helpers.ts (createCodexHeaders).
       The current CLI sends `session-id`/`thread-id`
       (codex-rs/codex-api/src/requests/headers.rs) and uses the OpenAI-Beta
       header for its websocket transport only. A live probe on this account
       showed the backend accepts the request with any of these four headers
       absent, so they are sent for fidelity (a workspace account needs the
       account id), not because a 400 was observed without them.
  4. Body fields, exactly the set codex-rs serializes
     (codex-rs/codex-api/src/common.rs, `struct ResponsesApiRequest`):
     model, instructions, input, tools, tool_choice, parallel_tool_calls,
     reasoning, store, stream, include, service_tier, prompt_cache_key, text.
     There is NO max_output_tokens field, and a live probe returns
     400 {"detail":"Unsupported parameter: max_output_tokens"} — so
     max_tokens/max_completion_tokens are dropped. temperature likewise returns
     400 {"detail":"Unsupported parameter: temperature"}.
  5. store=false + include=["reasoning.encrypted_content"]: the opencode plugin
     lib/request/request-transformer.ts ("ChatGPT backend REQUIRES store=false",
     "Context is maintained through ... reasoning.encrypted_content (for
     reasoning continuity)"). reasoning.summary "auto" comes from the same file
     ("Changed from 'detailed' to match Codex CLI") and from
     codex-rs/protocol/src/config_types.rs (`enum ReasoningSummary`, default
     Auto).
  6. Input item shapes: codex-rs/protocol/src/models.rs — `enum ResponseItem`
     is `#[serde(tag = "type", rename_all = "snake_case")]`, giving
     message / reasoning / function_call / function_call_output; `enum
     ContentItem` gives input_text / input_image {image_url} / output_text.
     function_call carries {name, arguments (JSON string), call_id};
     function_call_output carries {call_id, output}. Item ids are stripped on
     replay because store=false is stateless (plugin `filterInput`); a live
     probe confirmed both variants are accepted. The backend limits call_id
     to 64 characters and answers a longer value with 400
     string_above_max_length, so this shim maps a longer id to
     "call_" + sha256(id)[:59]. The mapping is deterministic, so a
     function_call and its matching function_call_output get the same wire
     id.
  7. Tool shape: codex-rs/tools/src/responses_api.rs — `ResponsesApiTool`
     serializes {name, description, strict, parameters} under
     `#[serde(tag = "type")] Function` => {"type":"function", ...} (flat, not
     the chat/completions {"function":{...}} nesting).
  8. tool_choice: "none" | "auto" | "required"
     (openai/openai-python src/openai/types/responses/tool_choice_options.py)
     or {"type":"function","name":...}
     (openai/openai-python src/openai/types/responses/tool_choice_function.py).
  9. Effort wire values: codex-rs/protocol/src/openai_models.rs
     (`ReasoningEffort::as_str`) — none/minimal/low/medium/high/xhigh/max/ultra.
     "ultra" is a CLIENT-side delegation setting: codex-rs/core/src/client.rs
     `reasoning_effort_for_request` rewrites Ultra to the model's
     multi_agent_reasoning_effort, else Max, before the request. A live probe
     confirms the backend rejects "ultra"
     ("Supported values are: 'none', 'minimal', 'low', 'medium', 'high',
     'xhigh', and 'max'"), so `-ultra` is sent as "max" and the audit line says
     so.
 10. Speed tier: `-fast` -> service_tier "priority".
     codex-rs/protocol/src/config_types.rs — `ServiceTier::Fast.request_value()
     == "priority"`; the model catalog advertises it as
     additional_speed_tiers ["fast"] / service_tiers [{"id":"priority"}].
 11. SSE event names consumed: response.created, response.output_item.added,
     response.output_text.delta, response.reasoning_summary_text.delta,
     response.reasoning_text.delta, response.function_call_arguments.delta,
     response.output_item.done, response.completed, response.incomplete,
     response.failed, error.
     codex-rs/codex-api/src/sse/responses.rs (`process_responses_event`).
     Usage on response.completed: input_tokens, input_tokens_details.cached_tokens,
     output_tokens, output_tokens_details.reasoning_tokens, total_tokens
     (same file, `struct ResponseCompletedUsage`). The backend does NOT send a
     terminal `data: [DONE]`; the stream simply ends after response.completed.
 12. OAuth: token endpoint https://auth.openai.com/oauth/token, client_id
     app_EMoamEEZ73f0CkXaXp7hrann, JSON body {client_id, grant_type:
     "refresh_token", refresh_token}, response {id_token?, access_token?,
     refresh_token?}. codex-rs/login/src/auth/manager.rs
     (REFRESH_TOKEN_URL, CLIENT_ID, `request_chatgpt_token_refresh`,
     `persist_tokens` which also sets last_refresh = now UTC). Same client_id
     and endpoint in https://github.com/numman-ali/opencode-openai-codex-auth
     lib/auth/auth.ts and in https://github.com/EvanZhouDev/openai-oauth.
 13. auth.json layout: codex-rs/login/src/auth/storage.rs (`AuthDotJson`:
     auth_mode, OPENAI_API_KEY, tokens, last_refresh, ...) and
     codex-rs/login/src/token_data.rs (`TokenData`: id_token, access_token,
     refresh_token, account_id). The account id claim lives in the id_token
     under "https://api.openai.com/auth" -> chatgpt_account_id
     (`struct AuthClaims`), which is also the plugin's JWT_CLAIM_PATH. The CLI
     writes the file with serde_json::to_string_pretty (2-space indent, no
     trailing newline) and mode 0600 (storage.rs `save`); this shim rewrites it
     the same way, atomically, preserving every key it does not own.
 14. Refresh-token rotation: codex-rs/login/src/auth/manager.rs classifies
     "refresh_token_reused" as terminal (RefreshTokenFailedReason::Exhausted),
     which is why two machines must not share one auth.json.
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
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Iterator

log = logging.getLogger("codex-shim")

HOST = os.environ.get("CODEX_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_SHIM_PORT", "18777"))
TIMEOUT = float(os.environ.get("CODEX_SHIM_TIMEOUT", "1800"))
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
LOG_LEVEL = os.environ.get("CODEX_SHIM_LOG_LEVEL", "INFO").upper()

AUTH_PATH = os.path.join(CODEX_HOME, "auth.json")
CATALOG_PATH = os.path.join(CODEX_HOME, "models_cache.json")

# Wire fact 1/2: the ChatGPT Codex backend, not the platform API.
BACKEND_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = BACKEND_BASE + "/responses"
MODELS_URL = BACKEND_BASE + "/models"
# Wire fact 12.
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Wire fact 3. The client version rides the models query and the User-Agent;
# models_cache.json carries the CLI's own value and wins when it is readable.
CLIENT_VERSION = "0.150.1"
ORIGINATOR = "codex_cli_rs"
OPENAI_BETA = "responses=experimental"
# Wire fact 13: the id_token claim that carries the workspace/account id.
AUTH_CLAIM = "https://api.openai.com/auth"

MAX_BODY = 64 * 1024 * 1024
MAX_ERROR_BODY = 4096       # upstream error bodies are read no further
PROBE_TTL = 60.0            # /healthz probe cache
CATALOG_TTL = 300.0         # models_cache.json re-read interval
REASONING_CACHE_SIZE = 512
REFRESH_MARGIN = 300.0      # refresh this many seconds before `exp`
PROBE_TIMEOUT = 20.0

# Wire fact 9. The ladder the slug suffix accepts, and what goes on the wire.
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_WIRE = {"ultra": "max"}
# OpenAI's reasoning_effort has one token the Codex ladder lacks.
EFFORT_ALIASES = {"minimal": "low"}
# Wire fact 10.
SPEED_SUFFIX = "-fast"
FAST_SERVICE_TIER = "priority"
# Wire fact 5.
REASONING_SUMMARY = "auto"
INCLUDE = ["reasoning.encrypted_content"]

# Display-only fallback for /health and /v1/models when models_cache.json is
# missing. Validation then accepts any gpt-* slug instead of a catalog check.
FALLBACK_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# OpenAI body keys with no wire on this backend. Dropped, never faked:
#   temperature/top_p -> 400 "Unsupported parameter" (wire fact 4)
#   max_tokens/max_completion_tokens -> no max_output_tokens field, 400 (fact 4)
#   n, logprobs, penalties, seed, logit_bias, stop -> no Responses field here
#   response_format -> the Responses API spells structured output differently
#     (text.format); silently reshaping a caller's schema is another contract,
#     so it fails closed instead.
#   service_tier -> this shim owns it, via the -fast slug suffix.
DROPPED_KEYS = (
    "temperature", "top_p", "top_k", "n", "logprobs", "top_logprobs",
    "presence_penalty", "frequency_penalty", "seed", "logit_bias", "stop",
    "max_tokens", "max_completion_tokens", "response_format", "service_tier",
    "store", "prediction", "modalities",
)

LOGIN_HELP = (
    "no ChatGPT Codex credentials: run `codex login` (or `codex login "
    "--device-auth` on a headless box) as the user that runs this shim; the "
    "shim reads %s and never falls back to OPENAI_API_KEY" % AUTH_PATH
)


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


# --- TLS trust ---------------------------------------------------------------
# Every outbound call of this shim is HTTPS and every one of them verifies the
# certificate. The stdlib default context is the first choice, but it is not
# always usable: a python.org framework build whose "Install
# Certificates.command" never ran points at a CA file that does not exist, so
# its trust store holds ZERO anchors and every HTTPS call dies with
# CERTIFICATE_VERIFY_FAILED, on a host where curl works. The sibling
# tools/claude-shim.py never sees this because the Anthropic SDK carries
# certifi. This shim has no dependencies (PEP 723), so it resolves the same
# thing itself: default store, else the host bundle OpenSSL and curl already
# use, else certifi when it happens to be importable.
#
# Verification is never turned off. With no anchors anywhere the context stays
# empty and the request fails closed, reported as a 502 carrying TLS_HELP.

# Well-known OpenSSL CA bundles, by platform.
SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",                    # macOS, FreeBSD
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL, Fedora
    "/etc/ssl/ca-bundle.pem",               # SUSE
    "/etc/ssl/certs/ca-bundle.crt",         # older RHEL layout
)

TLS_HELP = ("no TLS trust anchors on this host: set SSL_CERT_FILE to a CA "
            "bundle (macOS: /etc/ssl/cert.pem), or run Python's "
            "\"Install Certificates.command\" once")


def ca_anchor_count(context: ssl.SSLContext) -> int:
    """Number of CA certificates loaded in a context. 0 means it can verify
    nothing at all, which is the broken-install case, not a policy choice."""
    try:
        return int(context.cert_store_stats().get("x509_ca", 0))
    except (AttributeError, ValueError):  # pragma: no cover - stdlib always has it
        return 0


def ca_bundle_candidates() -> Iterator[str]:
    """Bundle paths to try, best first. SSL_CERT_FILE/SSL_CERT_DIR are already
    honored by the default context, so they never reach this list."""
    for path in SYSTEM_CA_FILES:
        if os.path.isfile(path):
            yield path
    try:
        import certifi
    except ImportError:
        return
    path = certifi.where()
    if os.path.isfile(path):
        yield path


def build_ssl_context() -> ssl.SSLContext:
    """A verifying SSLContext with a trust store that is actually populated."""
    context = ssl.create_default_context()
    if ca_anchor_count(context):
        return context
    for path in ca_bundle_candidates():
        try:
            context.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError):
            continue
        if ca_anchor_count(context):
            log.debug("TLS trust store loaded from %s", path)
            return context
    log.warning("%s", TLS_HELP)
    return context


_ssl_context: ssl.SSLContext | None = None
_ssl_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """Process-wide context, built once. Import stays free of file reads."""
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = build_ssl_context()
        return _ssl_context


# --- credentials -------------------------------------------------------------
# Wire facts 12-14. Pure helpers first; the store below owns the file and the
# refresh lock. No token value is ever logged, returned to a client, or written
# anywhere but auth.json.

def jwt_claims(token: str) -> dict[str, Any]:
    """Claims of a JWT payload. Signature is not verified: the backend does
    that. Returns {} for anything unparsable — callers fail closed."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def jwt_expiry(token: str) -> float | None:
    """Unix `exp` of an access token, or None when the claim is absent."""
    exp = jwt_claims(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def account_id_from(tokens: dict[str, Any]) -> str | None:
    """tokens.account_id, else the id_token's chatgpt_account_id claim."""
    stored = tokens.get("account_id")
    if isinstance(stored, str) and stored:
        return stored
    claim = jwt_claims(str(tokens.get("id_token") or "")).get(AUTH_CLAIM)
    if isinstance(claim, dict):
        value = claim.get("chatgpt_account_id")
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class CodexAuth:
    """One usable ChatGPT plan credential, read from auth.json."""

    access_token: str
    refresh_token: str
    account_id: str | None
    expires_at: float | None

    def expired(self, now: float, margin: float = REFRESH_MARGIN) -> bool:
        """True when the access token is gone within `margin` seconds. An
        unreadable `exp` counts as expired: refreshing costs one call, serving
        with a dead token costs the whole turn."""
        return self.expires_at is None or self.expires_at - margin <= now


def load_auth(path: str) -> CodexAuth:
    """auth.json -> CodexAuth. Fails closed with a 401 naming `codex login`."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise ShimError(401, LOGIN_HELP, type_="authentication_error", code="401") from None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ShimError(401, "%s (%s is unreadable: %s)"
                        % (LOGIN_HELP, path, type(exc).__name__),
                        type_="authentication_error", code="401") from None
    if not isinstance(doc, dict):
        raise ShimError(401, "%s (%s is not a JSON object)" % (LOGIN_HELP, path),
                        type_="authentication_error", code="401")
    if doc.get("auth_mode") != "chatgpt":
        raise ShimError(401, "%s (auth_mode is %r, not \"chatgpt\": this lane is the "
                             "ChatGPT plan, not API billing)" % (LOGIN_HELP, doc.get("auth_mode")),
                        type_="authentication_error", code="401")
    tokens = doc.get("tokens")
    if not isinstance(tokens, dict):
        raise ShimError(401, "%s (no tokens in %s)" % (LOGIN_HELP, path),
                        type_="authentication_error", code="401")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise ShimError(401, "%s (tokens in %s are incomplete)" % (LOGIN_HELP, path),
                        type_="authentication_error", code="401")
    return CodexAuth(access_token=access, refresh_token=refresh,
                     account_id=account_id_from(tokens), expires_at=jwt_expiry(access))


def iso_utc(now: float) -> str:
    """last_refresh in the CLI's own format: RFC 3339 UTC, microseconds, Z."""
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def merge_refresh(doc: dict[str, Any], refreshed: dict[str, Any], now: float) -> dict[str, Any]:
    """auth.json + token endpoint response -> the document to write back.

    Pure. Every key this shim does not own survives untouched, and a field the
    endpoint omitted keeps its previous value (the CLI does the same in
    `persist_tokens`).
    """
    out = dict(doc)
    tokens = dict(out.get("tokens") or {})
    for key in ("id_token", "access_token", "refresh_token"):
        value = refreshed.get(key)
        if isinstance(value, str) and value:
            tokens[key] = value
    out["tokens"] = tokens
    out["last_refresh"] = iso_utc(now)
    return out


def write_auth_file(path: str, doc: dict[str, Any]) -> None:
    """Atomic 0600 rewrite in the CLI's format (2-space pretty, no newline)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".auth-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AuthStore:
    """The Codex CLI's auth.json, shared with the CLI and refreshed in place.

    One lock serializes refreshes, so N concurrent turns burn exactly one
    single-use refresh token (wire fact 14). The file is re-read whenever its
    mtime moves, so a `codex login` or a CLI-side refresh is picked up without
    restarting the shim.
    """

    def __init__(self, path: str = AUTH_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._auth: CodexAuth | None = None
        self._mtime: float | None = None

    def configured(self) -> bool:
        """auth.json present and in chatgpt mode. No network, no refresh."""
        try:
            load_auth(self.path)
        except ShimError:
            return False
        return True

    def credentials(self, force_refresh: bool = False) -> CodexAuth:
        now = time.time()
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except OSError:
                raise ShimError(401, LOGIN_HELP, type_="authentication_error",
                                code="401") from None
            if self._auth is None or mtime != self._mtime:
                self._auth = load_auth(self.path)
                self._mtime = mtime
            if force_refresh or self._auth.expired(now):
                self._auth = self._refresh(self._auth, now)
            return self._auth

    def _refresh(self, auth: CodexAuth, now: float) -> CodexAuth:
        """One rotation against the OAuth token endpoint, persisted in place."""
        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps({"client_id": OAUTH_CLIENT_ID,
                             "grant_type": "refresh_token",
                             "refresh_token": auth.refresh_token}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": user_agent(),
                     "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT,
                                        context=ssl_context()) as response:
                refreshed = json.loads(response.read(MAX_ERROR_BODY * 8))
        except urllib.error.HTTPError as exc:
            code = refresh_error_code(exc.read(MAX_ERROR_BODY))
            log.warning("token refresh rejected: status=%d code=%s", exc.code, code or "-")
            raise ShimError(401, "token refresh rejected (status %d, code %s): run "
                                 "`codex login` again on this machine"
                            % (exc.code, code or "unknown"),
                            type_="authentication_error", code="401") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ValueError, OSError) as exc:
            log.warning("token refresh failed: %s", type(exc).__name__)
            raise ShimError(401, "token refresh failed (%s): check connectivity, then run "
                                 "`codex login`" % type(exc).__name__,
                            type_="authentication_error", code="401") from None
        if not isinstance(refreshed, dict) or not isinstance(refreshed.get("access_token"), str):
            raise ShimError(401, "token refresh returned no access_token: run `codex login`",
                            type_="authentication_error", code="401")
        with open(self.path, encoding="utf-8") as fh:   # re-read: the CLI may have written
            doc = json.load(fh)
        merged = merge_refresh(doc if isinstance(doc, dict) else {}, refreshed, now)
        write_auth_file(self.path, merged)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            self._mtime = None
        log.info("refreshed the Codex OAuth token in %s", self.path)
        return load_auth(self.path)


def refresh_error_code(body: bytes) -> str | None:
    """OAuth error code out of a refusal body — the code only, never the body
    text (which can quote the credential)."""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    error = doc.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(error, str):
        return error
    return doc.get("code") if isinstance(doc.get("code"), str) else None


AUTH_STORE = AuthStore()


def user_agent() -> str:
    """Honest identity: this shim, not a spoofed terminal (wire fact 3)."""
    return "codex_cli_rs/%s (opengrok codex-shim)" % client_version()


# --- model catalog -----------------------------------------------------------

@dataclass(frozen=True)
class ModelEntry:
    """One slug from ~/.codex/models_cache.json (wire facts 9-10)."""

    slug: str
    efforts: tuple[str, ...]
    default_effort: str | None
    speed_tiers: tuple[str, ...]
    listed: bool


def parse_catalog(doc: Any) -> dict[str, ModelEntry]:
    """models_cache.json -> {slug: ModelEntry}. Pure; {} for junk."""
    if not isinstance(doc, dict):
        return {}
    entries: dict[str, ModelEntry] = {}
    for raw in doc.get("models") or []:
        if not isinstance(raw, dict):
            continue
        slug = raw.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        efforts = tuple(
            level["effort"] for level in raw.get("supported_reasoning_levels") or []
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        )
        tiers = tuple(t for t in raw.get("additional_speed_tiers") or [] if isinstance(t, str))
        default = raw.get("default_reasoning_level")
        entries[slug] = ModelEntry(
            slug=slug, efforts=efforts,
            default_effort=default if isinstance(default, str) else None,
            speed_tiers=tiers, listed=raw.get("visibility") == "list")
    return entries


_catalog_state: dict[str, Any] = {"ts": 0.0, "entries": {}, "version": CLIENT_VERSION}
_catalog_lock = threading.Lock()


def cached_catalog() -> dict[str, ModelEntry]:
    """models_cache.json, re-read at most every CATALOG_TTL seconds."""
    now = time.monotonic()
    with _catalog_lock:
        if _catalog_state["ts"] and (now - float(_catalog_state["ts"])) < CATALOG_TTL:
            return dict(_catalog_state["entries"])
        try:
            with open(CATALOG_PATH, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            doc = {}
        version = doc.get("client_version") if isinstance(doc, dict) else None
        _catalog_state.update({
            "ts": now,
            "entries": parse_catalog(doc),
            "version": version if isinstance(version, str) and version else CLIENT_VERSION,
        })
        return dict(_catalog_state["entries"])


def client_version() -> str:
    cached_catalog()
    return str(_catalog_state["version"])


def catalog_models() -> tuple[list[str], str]:
    """(slugs for /v1/models, source label)."""
    entries = cached_catalog()
    listed = [entry.slug for entry in entries.values() if entry.listed]
    if listed:
        return listed, "models_cache.json"
    return list(FALLBACK_MODELS), "static-fallback"


# --- slug + effort -----------------------------------------------------------

def split_slug(model: str, catalog: dict[str, ModelEntry]) -> tuple[str, str | None, bool]:
    """`<model>[-<effort>][-fast]` -> (base, effort, fast).

    `-fast` comes LAST, after the effort suffix (`gpt-5.6-sol-high-fast`), and
    `gpt-5.6-sol-fast` (no effort) is equally valid. A slug the catalog knows
    verbatim is never split, so a real model whose name ends in a ladder word
    (`gpt-5.1-codex-max`) keeps its name.
    """
    if model in catalog:
        return model, None, False
    fast = model.endswith(SPEED_SUFFIX) and len(model) > len(SPEED_SUFFIX)
    base = model[: -len(SPEED_SUFFIX)] if fast else model
    if base in catalog:
        return base, None, fast
    for effort in EFFORTS:
        suffix = "-" + effort
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)], effort, fast
    return base, None, fast


def normalize_effort(value: Any) -> str:
    """Body `reasoning_effort` onto the ladder. Fail closed on junk."""
    if not isinstance(value, str):
        raise ShimError(400, "reasoning_effort must be a string, one of: %s"
                        % ", ".join(EFFORTS))
    token = value.strip().lower()
    token = EFFORT_ALIASES.get(token, token)
    if token not in EFFORTS:
        raise ShimError(400, "unknown reasoning_effort %r; expected one of: %s"
                        % (value, ", ".join(EFFORTS)))
    return token


def resolve_model(model: str, effort_override: Any,
                  catalog: dict[str, ModelEntry]) -> tuple[str, str | None, bool]:
    """(base model, effort, fast), validated against the catalog when present.

    With no catalog the shim cannot check a slug, so it accepts any `gpt-*` id
    and lets the backend rule — but it still refuses anything else rather than
    forwarding a slug this lane demonstrably does not serve.
    """
    base, slug_effort, fast = split_slug(model, catalog)
    effort = slug_effort
    if effort_override is not None:
        effort = normalize_effort(effort_override)     # explicit body value wins
    entry = catalog.get(base)
    if entry is None:
        if catalog:
            raise ShimError(400, "unknown model %r; this lane serves: %s"
                            % (model, ", ".join(sorted(catalog))))
        if not base.startswith("gpt-"):
            raise ShimError(400, "unknown model %r and no %s to check it against; this "
                                 "lane serves gpt-* slugs only" % (model, CATALOG_PATH))
    else:
        if effort is not None and entry.efforts and effort not in entry.efforts:
            raise ShimError(400, "model %s does not support effort %r; it supports: %s"
                            % (base, effort, ", ".join(entry.efforts)))
        if fast and "fast" not in entry.speed_tiers:
            raise ShimError(400, "model %s has no fast speed tier; drop the -fast suffix"
                            % base)
    return base, effort, fast


# --- encrypted-reasoning replay ----------------------------------------------
# Wire fact 5: store=false makes every turn stateless, and the reasoning that
# produced a tool call only survives as an encrypted item the client is
# expected to send back. The OpenAI chat/completions wire has no field that
# carries it, so the shim keeps its own bounded map from function call_id ->
# that turn's reasoning items and re-attaches them when the client replays the
# same assistant message in a tool-use loop. Same shape as claude-shim's
# ThinkingCache, for the same reason.

class ReasoningCache:
    """Bounded LRU: function call_id -> that turn's reasoning items."""

    def __init__(self, capacity: int = REASONING_CACHE_SIZE) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def put(self, key: str, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items[key] = items
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get(self, key: str) -> list[dict[str, Any]] | None:
        with self._lock:
            items = self._items.get(key)
            if items is not None:
                self._items.move_to_end(key)
            return items


REASONING_CACHE = ReasoningCache()


@dataclass
class RequestContext:
    """Per-request bookkeeping the response path and the audit line need."""

    model: str                              # the slug the client asked for
    backend_model: str                      # slug with the suffixes removed
    effort: str | None                      # ladder value the caller asked for
    wire_effort: str | None                 # what actually goes on the wire
    fast: bool
    stream: bool
    dropped: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)  # sanitized -> original
    session_id: str = ""
    include_usage: bool = True
    replayed_reasoning: int = 0
    usage: dict[str, Any] | None = None      # OpenAI-shaped, for the audit line
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)

    def original_tool_name(self, name: str) -> str:
        return self.tool_names.get(name, name)

    def route_label(self) -> str:
        return "effort=%s/%s tier=%s summary=%s replay=%d" % (
            self.effort or "backend-default", self.wire_effort or "backend-default",
            FAST_SERVICE_TIER if self.fast else "default",
            REASONING_SUMMARY, self.replayed_reasoning)


# --- pure translation --------------------------------------------------------

def sanitize_tool_name(name: str) -> str:
    """Deterministic ^[a-zA-Z0-9_-]{1,128}$ name; a digest keeps it unique."""
    if isinstance(name, str) and TOOL_NAME_RE.match(name):
        return name
    raw = name if isinstance(name, str) else str(name)
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw) or "tool"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]  # naming, not security
    return (cleaned[:119] + "_" + digest)[:128]


def wire_call_id(call_id: str) -> str:
    """Deterministic call_id that fits the backend's 64-char wire limit.

    A call_id of 64 characters or fewer passes through unchanged. A longer
    call_id is replaced by a derived id of exactly 64 characters, so a
    function_call and its matching function_call_output still map to the
    same wire id.
    """
    if len(call_id) <= 64:
        return call_id
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:59]  # naming, not security
    return "call_" + digest


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
            if isinstance(part, dict) and part.get("type") in ("text", "input_text",
                                                               "output_text"):
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def user_content_items(content: Any, dropped: list[str]) -> list[dict[str, Any]]:
    """OpenAI user content -> Responses input_text / input_image items."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content)}]
    items: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                items.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            dropped.append("content:non-object")
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text"):
            text = str(part.get("text", ""))
            if text:
                items.append({"type": "input_text", "text": text})
        elif ptype == "image_url":
            ref = part.get("image_url")
            url = ref.get("url", "") if isinstance(ref, dict) else str(ref or "")
            if url:
                # Wire fact 6. A data: URL is always accepted; codex's own
                # app-server rejects remote http(s) image URLs, so one may come
                # back as an upstream 400 rather than being rewritten here.
                items.append({"type": "input_image", "image_url": url})
            else:
                dropped.append("image_url:empty")
        else:
            dropped.append("content:%s" % (ptype or "unknown"))
    return items


def assistant_items(msg: dict[str, Any], ctx: RequestContext,
                    cache: ReasoningCache | None) -> list[dict[str, Any]]:
    """Assistant text + tool_calls -> message / function_call items, with the
    encrypted reasoning of that turn replayed in front of the calls."""
    items: list[dict[str, Any]] = []
    text = _text_of(msg.get("content"))
    if text:
        items.append({"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]})
    calls: list[dict[str, Any]] = []
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            ctx.dropped.append("tool_call:non-object")
            continue
        fn = call.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            arguments = raw_args or "{}"
        elif raw_args is None:
            arguments = "{}"
        else:
            arguments = json.dumps(raw_args, separators=(",", ":"))
        calls.append({"type": "function_call",
                      "call_id": wire_call_id(str(call.get("id") or "")),
                      "name": sanitize_tool_name(str(fn.get("name") or "")),
                      "arguments": arguments})
    if calls and cache is not None:
        for call in calls:
            remembered = cache.get(call["call_id"])
            if remembered:
                items.extend(remembered)
                ctx.replayed_reasoning += len(remembered)
                break
    items.extend(calls)
    return items


def tool_result_item(msg: dict[str, Any]) -> dict[str, Any]:
    call_id = msg.get("tool_call_id")
    if not call_id:
        raise ShimError(400, "tool message without tool_call_id")
    return {"type": "function_call_output", "call_id": wire_call_id(str(call_id)),
            "output": _text_of(msg.get("content"))}


def convert_tools(tools: Any, ctx_names: dict[str, str],
                  dropped: list[str]) -> list[dict[str, Any]]:
    """OpenAI tools -> Responses function tools (wire fact 7)."""
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
        out.append({
            "type": "function",
            "name": name,
            "description": str(fn.get("description") or ""),
            # strict=true additionally requires a closed schema with every
            # property required; the caller's schema is taken as given.
            "strict": False,
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def convert_tool_choice(choice: Any, dropped: list[str]) -> Any:
    """OpenAI tool_choice -> Responses tool_choice (wire fact 8)."""
    if choice is None:
        return None
    if isinstance(choice, str):
        if choice in ("auto", "none", "required"):
            return choice
        dropped.append("tool_choice:%s" % choice)
        return None
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        name = fn.get("name") or (choice.get("name") if choice.get("type") == "function" else None)
        if name:
            return {"type": "function", "name": sanitize_tool_name(str(name))}
    dropped.append("tool_choice:unsupported")
    return None


def conversation_key(instructions: str, input_items: list[dict[str, Any]]) -> str:
    """Deterministic id for this conversation: the instructions plus its first
    input item. It stays stable as the transcript grows, which is what makes
    prompt_cache_key (and the session_id header) worth sending; it changes when
    a different conversation starts. No clock, no randomness — a resumed run
    reproduces it."""
    first = json.dumps(input_items[0], sort_keys=True, separators=(",", ":")) if input_items else ""
    digest = hashlib.sha256((instructions + "\x00" + first).encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "opengrok-codex-shim:" + digest))


def to_responses(body: dict[str, Any],
                 catalog: dict[str, ModelEntry] | None = None,
                 cache: ReasoningCache | None = REASONING_CACHE
                 ) -> tuple[dict[str, Any], RequestContext]:
    """OpenAI chat/completions body -> Codex Responses request body.

    Pure: no I/O, no credential, no clock. Everything the response path needs
    afterwards rides in the returned RequestContext.
    """
    if not isinstance(body, dict):
        raise ShimError(400, "request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ShimError(400, "model is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ShimError(400, "messages must be a non-empty array")

    entries = cached_catalog() if catalog is None else catalog
    backend_model, effort, fast = resolve_model(model, body.get("reasoning_effort"), entries)
    ctx = RequestContext(model=model, backend_model=backend_model, effort=effort,
                         wire_effort=EFFORT_WIRE.get(effort or "", effort), fast=fast,
                         stream=bool(body.get("stream")), dropped=dropped_keys(body))

    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            raise ShimError(400, "each message must be a JSON object")
        role = msg.get("role")
        if role in ("system", "developer"):
            text = _text_of(msg.get("content"))
            if text:
                instructions.append(text)
        elif role == "user":
            items = user_content_items(msg.get("content"), ctx.dropped)
            if items:
                input_items.append({"type": "message", "role": "user", "content": items})
        elif role == "assistant":
            input_items.extend(assistant_items(msg, ctx, cache))
        elif role == "tool":
            input_items.append(tool_result_item(msg))
        else:
            raise ShimError(400, "unsupported message role %r" % (role,))

    if not input_items:
        raise ShimError(400, "messages contain no content the Responses API accepts")

    instruction_text = "\n".join(instructions)
    ctx.session_id = conversation_key(instruction_text, input_items)

    reasoning: dict[str, Any] = {"summary": REASONING_SUMMARY}
    if ctx.wire_effort:
        reasoning["effort"] = ctx.wire_effort
    payload: dict[str, Any] = {
        "model": backend_model,
        "input": input_items,
        "reasoning": reasoning,
        "store": False,      # wire fact 5: the backend requires it
        "stream": True,      # the only shape this backend streams; see _serve_*
        "include": list(INCLUDE),
        "prompt_cache_key": ctx.session_id,
    }
    if instruction_text:
        payload["instructions"] = instruction_text
    if fast:
        payload["service_tier"] = FAST_SERVICE_TIER

    tools = convert_tools(body.get("tools"), ctx.tool_names, ctx.dropped)
    if tools:
        payload["tools"] = tools
    choice = convert_tool_choice(body.get("tool_choice"), ctx.dropped)
    if choice is not None and tools:
        payload["tool_choice"] = choice
    if isinstance(body.get("parallel_tool_calls"), bool):
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]

    options = body.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage") is False:
        ctx.include_usage = False
    return payload, ctx


# --- response translation ----------------------------------------------------

def usage_dict(usage: Any) -> dict[str, Any]:
    """Responses usage -> OpenAI usage (wire fact 11). input_tokens already
    counts the cached prefix, so cached_tokens is a subset of prompt_tokens."""
    if not isinstance(usage, dict):
        return {}
    def _int(value: Any) -> int:
        return int(value) if isinstance(value, (int, float)) else 0
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    prompt = _int(usage.get("input_tokens"))
    completion = _int(usage.get("output_tokens"))
    total = _int(usage.get("total_tokens")) or prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {
            "cached_tokens": _int((input_details or {}).get("cached_tokens"))
            if isinstance(input_details, dict) else 0},
        "completion_tokens_details": {
            "reasoning_tokens": _int((output_details or {}).get("reasoning_tokens"))
            if isinstance(output_details, dict) else 0},
    }


def incomplete_finish_reason(response: Any) -> str:
    """response.incomplete -> an OpenAI finish_reason."""
    reason = ""
    if isinstance(response, dict):
        details = response.get("incomplete_details")
        if isinstance(details, dict):
            reason = str(details.get("reason") or "")
    return "length" if reason == "max_output_tokens" else "content_filter"


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


def stream_error(event: dict[str, Any]) -> ShimError:
    """response.failed / error event -> a client-visible error."""
    source = event.get("response") if isinstance(event.get("response"), dict) else event
    error = source.get("error") if isinstance(source, dict) else None
    if not isinstance(error, dict):
        error = {}
    code = error.get("code") or error.get("type")
    message = str(error.get("message") or "upstream stream failed")
    if code == "rate_limit_exceeded":
        return ShimError(429, message, type_="rate_limit_error", code=str(code))
    return ShimError(502, message, type_="api_error", code=str(code) if code else None)


def sse_events(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Raw SSE lines -> event objects. Pure over any byte iterator."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def translate_events(events: Iterable[dict[str, Any]], ctx: RequestContext,
                     created: int) -> Iterator[dict[str, Any]]:
    """Responses SSE events -> OpenAI chat.completion.chunk payloads.

    Pure over the event iterator (it only mutates `ctx`, which is this
    request's own bookkeeping), so the same generator serves the streaming and
    the blocking path. Raises ShimError on a terminal upstream failure.
    """
    chat_id = "chatcmpl-" + ctx.session_id
    opened = False
    tool_index = -1
    index_of_item: dict[str, int] = {}
    streamed_args: set[str] = set()
    saw_tool_call = False

    def open_stream() -> Iterator[dict[str, Any]]:
        nonlocal opened
        if not opened:
            opened = True
            yield chunk_envelope(chat_id, ctx.model, created,
                                 {"role": "assistant", "content": ""})

    for event in events:
        kind = event.get("type")
        if kind == "response.created":
            response = event.get("response")
            if not opened and isinstance(response, dict) and response.get("id"):
                chat_id = "chatcmpl-" + str(response["id"])
            yield from open_stream()
        elif kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                yield from open_stream()
                tool_index += 1
                saw_tool_call = True
                index_of_item[str(item.get("id") or item.get("call_id") or "")] = tool_index
                yield chunk_envelope(chat_id, ctx.model, created, {"tool_calls": [{
                    "index": tool_index,
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {"name": ctx.original_tool_name(str(item.get("name") or "")),
                                 "arguments": ""},
                }]})
        elif kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield from open_stream()
                yield chunk_envelope(chat_id, ctx.model, created, {"content": delta})
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield from open_stream()
                yield chunk_envelope(chat_id, ctx.model, created, {"reasoning_content": delta})
        elif kind == "response.function_call_arguments.delta":
            delta = event.get("delta")
            item_id = str(event.get("item_id") or event.get("call_id") or "")
            index = index_of_item.get(item_id)
            if isinstance(delta, str) and delta and index is not None:
                streamed_args.add(item_id)
                yield from open_stream()
                yield chunk_envelope(chat_id, ctx.model, created, {"tool_calls": [{
                    "index": index,
                    "function": {"arguments": delta},
                }]})
        elif kind == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning":
                # Stripped of its id: store=false is stateless, so the id means
                # nothing on the next turn (wire fact 6).
                ctx.reasoning_items.append({
                    "type": "reasoning",
                    "summary": item.get("summary") or [],
                    "encrypted_content": item.get("encrypted_content"),
                })
            elif item.get("type") == "function_call":
                item_id = str(item.get("id") or item.get("call_id") or "")
                call_id = str(item.get("call_id") or "")
                if call_id:
                    ctx.tool_call_ids.append(call_id)
                index = index_of_item.get(item_id)
                arguments = item.get("arguments")
                # Only when the backend sent no deltas for this call: emitting
                # both would duplicate the arguments.
                if index is not None and item_id not in streamed_args \
                        and isinstance(arguments, str) and arguments:
                    yield chunk_envelope(chat_id, ctx.model, created, {"tool_calls": [{
                        "index": index,
                        "function": {"arguments": arguments},
                    }]})
        elif kind == "response.completed":
            response = event.get("response")
            ctx.usage = usage_dict(response.get("usage") if isinstance(response, dict) else None)
            yield from open_stream()
            yield chunk_envelope(chat_id, ctx.model, created, {},
                                 "tool_calls" if saw_tool_call else "stop",
                                 ctx.usage if ctx.include_usage else None)
            return
        elif kind == "response.incomplete":
            response = event.get("response")
            ctx.usage = usage_dict(response.get("usage") if isinstance(response, dict) else None)
            yield from open_stream()
            yield chunk_envelope(chat_id, ctx.model, created, {},
                                 incomplete_finish_reason(response),
                                 ctx.usage if ctx.include_usage else None)
            return
        elif kind in ("response.failed", "error"):
            raise stream_error(event)


def completion_from_chunks(chunks: Iterable[dict[str, Any]], ctx: RequestContext,
                           created: int) -> dict[str, Any]:
    """The same chunk stream, folded into one chat.completion object."""
    chat_id = "chatcmpl-" + ctx.session_id
    texts: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_reason: str | None = None
    for chunk in chunks:
        chat_id = str(chunk.get("id") or chat_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            texts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning.append(delta["reasoning_content"])
        for call in delta.get("tool_calls") or []:
            index = int(call.get("index", 0))
            while len(tool_calls) <= index:
                tool_calls.append({"index": len(tool_calls), "id": "", "type": "function",
                                   "function": {"name": "", "arguments": ""}})
            entry = tool_calls[index]
            if call.get("id"):
                entry["id"] = str(call["id"])
            fn = call.get("function") or {}
            if fn.get("name"):
                entry["function"]["name"] = str(fn["name"])
            if isinstance(fn.get("arguments"), str):
                entry["function"]["arguments"] += fn["arguments"]
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    text = "".join(texts)
    message: dict[str, Any] = {"role": "assistant",
                               "content": text if text or not tool_calls else None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": ctx.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
        "usage": ctx.usage or {},
    }


def remember_reasoning(ctx: RequestContext, cache: ReasoningCache = REASONING_CACHE) -> None:
    """Key this turn's reasoning items by the tool calls they produced, so the
    next request can replay them verbatim (wire fact 5)."""
    if not ctx.reasoning_items:
        return
    for call_id in ctx.tool_call_ids:
        cache.put(call_id, list(ctx.reasoning_items))


# --- upstream ----------------------------------------------------------------

def backend_headers(auth: CodexAuth, session_id: str, accept: str) -> dict[str, str]:
    """Wire fact 3. The inbound Authorization header never reaches this dict."""
    headers = {
        "Authorization": "Bearer " + auth.access_token,
        "originator": ORIGINATOR,
        "OpenAI-Beta": OPENAI_BETA,
        "User-Agent": user_agent(),
        "Accept": accept,
    }
    if auth.account_id:
        headers["chatgpt-account-id"] = auth.account_id
    if session_id:
        headers["session_id"] = session_id
    return headers


def shim_error_from_http(exc: urllib.error.HTTPError) -> ShimError:
    """Upstream HTTP failure -> a real status the caller can act on."""
    try:
        raw = exc.read(MAX_ERROR_BODY)
    except Exception:  # noqa: BLE001 - a body we cannot read is not a new failure
        raw = b""
    message = ""
    code = None
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        doc = None
    if isinstance(doc, dict):
        error = doc.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            code = error.get("code") or error.get("type")
        elif isinstance(doc.get("detail"), str):
            message = doc["detail"]
    if not message:
        message = "upstream returned HTTP %d" % exc.code
    if exc.code == 401:
        return ShimError(401, "%s (upstream: %s)" % (LOGIN_HELP, message),
                         type_="authentication_error", code="401")
    if exc.code == 429:
        return ShimError(429, message, type_="rate_limit_error", code="429",
                         retry_after=exc.headers.get("Retry-After"))
    type_ = "invalid_request_error" if 400 <= exc.code < 500 else "api_error"
    return ShimError(exc.code, message, type_=type_,
                     code=str(code) if code else str(exc.code))


def shim_error_from_urlerror(exc: urllib.error.URLError) -> ShimError:
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        # An empty trust store is a broken install and has a fix; a populated
        # one that rejected the chain is a different problem (proxy, clock).
        if ca_anchor_count(ssl_context()):
            detail = "the certificate chain was rejected by a populated trust store"
        else:
            detail = TLS_HELP
        return ShimError(502, "TLS verification failed: %s" % detail,
                         type_="api_connection_error", code="502")
    return ShimError(502, "upstream unreachable: %s" % type(reason).__name__,
                     type_="api_connection_error", code="502")


def open_responses(payload: dict[str, Any], session_id: str, timeout: float,
                   store: AuthStore = AUTH_STORE) -> Any:
    """POST the Responses request; return the open SSE response.

    A 401 buys exactly one forced refresh and one retry: the usual cause is an
    access token that expired mid-idle or a rotation done by the CLI.
    """
    data = json.dumps(payload).encode("utf-8")

    def attempt(force_refresh: bool) -> Any:
        auth = store.credentials(force_refresh=force_refresh)
        headers = backend_headers(auth, session_id, "text/event-stream")
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(RESPONSES_URL, data=data, headers=headers,
                                         method="POST")
        return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())

    try:
        return attempt(False)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise shim_error_from_http(exc) from None
        exc.close()
    except urllib.error.URLError as exc:
        raise shim_error_from_urlerror(exc) from None
    try:
        return attempt(True)
    except urllib.error.HTTPError as exc:
        raise shim_error_from_http(exc) from None
    except urllib.error.URLError as exc:
        raise shim_error_from_urlerror(exc) from None


_probe_state: dict[str, Any] = {"ts": 0.0, "ok": False, "detail": "not probed"}
_probe_lock = threading.Lock()


def probe_upstream(force: bool = False, store: AuthStore = AUTH_STORE) -> tuple[bool, str]:
    """One authenticated GET on the backend model list, cached, so /healthz
    never spends a token and never wakes inference (wire fact 2)."""
    now = time.monotonic()
    with _probe_lock:
        if not force and _probe_state["ts"] and (now - float(_probe_state["ts"])) < PROBE_TTL:
            return bool(_probe_state["ok"]), str(_probe_state["detail"])
        try:
            auth = store.credentials()
            url = "%s?client_version=%s" % (MODELS_URL, client_version())
            request = urllib.request.Request(
                url, headers=backend_headers(auth, "", "application/json"), method="GET")
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT,
                                        context=ssl_context()) as response:
                doc = json.loads(response.read(4 * 1024 * 1024))
            count = len(doc.get("models") or []) if isinstance(doc, dict) else 0
            ok, detail = True, "models=%d" % count
        except ShimError as exc:
            ok, detail = False, "no_credentials" if exc.status == 401 else exc.type
        except urllib.error.HTTPError as exc:
            ok, detail = False, "http_%d" % exc.code
        except Exception as exc:  # noqa: BLE001 - class name only; never the body
            ok, detail = False, type(exc).__name__
        _probe_state.update({"ts": now, "ok": ok, "detail": detail})
        return ok, detail


# --- route table -------------------------------------------------------------

def health_table(host: str, port: int) -> dict[str, Any]:
    models, source = catalog_models()
    ok, detail = probe_upstream()
    return {
        "codex-plans": {
            "configured": AUTH_STORE.configured(),
            "keys": False,
            "upstream": "chatgpt-codex-responses-api",
            "shim": "http://%s:%d/v1" % (host, port),
            "models": models,
            "models_source": source,
            "effort": "slug suffix -low|-medium|-high|-xhigh|-max|-ultra, or body "
                      "reasoning_effort (which wins); -ultra rides the wire as max, "
                      "matching the Codex CLI; backend default when omitted",
            "speed_tier": "optional -fast suffix, last (gpt-5.6-sol-high-fast) -> "
                          "service_tier=%s" % FAST_SERVICE_TIER,
            "reasoning": {"summary": REASONING_SUMMARY, "include": list(INCLUDE),
                          "store": False},
            "credential": "Codex CLI OAuth token in %s (`codex login`)" % AUTH_PATH,
            "probe": detail,
            "upstream_reachable": ok,
            "note": "OpenAI chat/completions in, ChatGPT Codex Responses API out, on "
                    "the ChatGPT plan. temperature/top_p/max_tokens have no wire here "
                    "(the backend answers 400) and are dropped, not honored.",
        }
    }


def openai_models_payload() -> dict[str, Any]:
    models, _source = catalog_models()
    # `created` has no counterpart in the Codex model catalog, so it is
    # reported as 0 rather than invented.
    return {"object": "list",
            "data": [{"id": slug, "object": "model", "owned_by": "openai", "created": 0}
                     for slug in models]}


# --- HTTP server -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-shim/1"

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

    def _write_chunk(self, payload: dict[str, Any]) -> None:
        self._write_sse(b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8")
                        + b"\n\n")

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        started = time.monotonic()
        try:
            if path == "/healthz":
                ok, _detail = probe_upstream()
                self._json(200, {"ok": True, "service": "codex-shim",
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
        path = self.path.split("?", 1)[0]
        started = time.monotonic()
        if path != "/v1/chat/completions":
            err = ShimError(404, "unknown path %s" % path, type_="not_found_error", code="404")
            self._fail(err)
            self._audit("POST", path, 404, started)
            return
        ctx: RequestContext | None = None
        try:
            body = self._read_body()
            payload, ctx = to_responses(body)
            status = self._serve(payload, ctx)
        except ShimError as err:
            self._fail(err)
            self._audit("POST", path, err.status, started, ctx)
            return
        except (BrokenPipeError, ConnectionResetError):
            log.info("client aborted mid-response")
            return
        except Exception as exc:  # noqa: BLE001 - mapped to a status, never leaked
            err = ShimError(500, "shim failure: %s" % type(exc).__name__, type_="api_error")
            self._fail(err)
            self._audit("POST", path, err.status, started, ctx)
            return
        self._audit("POST", path, status, started, ctx)

    def _serve(self, payload: dict[str, Any], ctx: RequestContext) -> int:
        """One upstream call, rendered streamed or blocking.

        The upstream request is always streamed (the backend's only shape), and
        the SSE headers go out only after it answered 200, so a 401/429/5xx
        surfaces as that status instead of hiding inside a 200 stream.
        """
        created = int(time.time())
        response = open_responses(payload, ctx.session_id, TIMEOUT)
        opened = False
        try:
            chunks = translate_events(sse_events(response), ctx, created)
            if not ctx.stream:
                completion = completion_from_chunks(chunks, ctx, created)
                remember_reasoning(ctx)
                self._json(200, completion)
                return 200
            for chunk in chunks:
                if not opened:
                    self._start_sse()
                    opened = True
                self._write_chunk(chunk)
            if not opened:
                self._start_sse()
                opened = True
            remember_reasoning(ctx)
            self._write_sse(b"data: [DONE]\n\n")
            self._end_sse()
            return 200
        except (BrokenPipeError, ConnectionResetError):
            log.info("client aborted mid-stream")
            return 200
        except ShimError as err:
            if not opened:
                raise
            log.warning("stream failed: %s", err.type)
            self._write_chunk(err.payload())
            self._write_sse(b"data: [DONE]\n\n")
            self._end_sse()
            return err.status
        finally:
            response.close()

    # -- audit --
    def _audit(self, method: str, path: str, status: int, started: float,
               ctx: RequestContext | None = None) -> None:
        """Metadata only. No headers, no tokens, no prompts, no tool arguments."""
        usage = (ctx.usage if ctx else None) or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        log.info(
            "%s %s model=%s stream=%s status=%d duration_ms=%d input_tokens=%s "
            "output_tokens=%s cached=%s reasoning_tokens=%s dropped=%s route=%s",
            method, path, ctx.model if ctx else "-",
            str(bool(ctx and ctx.stream)).lower(), status,
            int((time.monotonic() - started) * 1000),
            usage.get("prompt_tokens", "-"), usage.get("completion_tokens", "-"),
            prompt_details.get("cached_tokens", "-"),
            completion_details.get("reasoning_tokens", "-"),
            ctx.dropped if ctx else [], ctx.route_label() if ctx else "-",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex hop shim: OpenAI wire -> "
                                                 "ChatGPT Codex backend (Responses API)")
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
            print("codex-shim upstream UNREACHABLE: %s" % LOGIN_HELP)
            return 1
        print("codex-shim upstream %s (%s)" % ("OK" if ok else "UNREACHABLE", detail))
        return 0 if ok else 1

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    ok, detail = probe_upstream(force=True)
    log.info("codex-shim listening http://%s:%d/v1 -> %s (upstream_reachable=%s, %s)",
             args.host, args.port, RESPONSES_URL, ok, detail)
    if not ok:
        log.warning("upstream probe failed (%s) — %s", detail, LOGIN_HELP)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
