# Codex shim (`tools/codex-shim.py`)

`tools/doctor.py` and `setup.py` have always watched port `18777` for a
service named `codex-shim`. That shim now ships in this repo:
`tools/codex-shim.py`, a single-file PEP 723 script — stdlib only, zero
dependencies (`ThreadingHTTPServer`, `urllib`).

It speaks OpenAI `chat/completions` on the front (what Grok Bot's hop sends)
and the ChatGPT Codex backend's Responses API on the back
(`https://chatgpt.com/backend-api/codex/responses` — the same endpoint the
Codex CLI itself calls), so a `hopBaseUrl` pointed at `18777` has something
real to talk to.

## Credentials

The shim never reads the inbound `Authorization` header — it is ignored and
never logged. It also never reads `OPENAI_API_KEY`: that would silently move
billing from the ChatGPT plan to API pay-as-you-go. Instead it reuses the
Codex CLI's own OAuth token store, `~/.codex/auth.json` (`CODEX_HOME`
overrides the directory) — `auth_mode: "chatgpt"`,
`tokens.{id_token,access_token,refresh_token,account_id}`, `last_refresh`.

**The only credential step is `codex login` on the machine that runs the
shim:**

```bash
npm i -g @openai/codex        # install the Codex CLI once
codex login                   # opens a browser, writes ~/.codex/auth.json
codex login --device-auth     # headless / cloud box: prints a URL + code instead
```

OpenAI explicitly supports using the Codex CLI's "Sign in with ChatGPT" OAuth
token outside the CLI itself — see
[OpenClaw: OAuth](https://docs.openclaw.ai/concepts/oauth) ("OpenAI Codex OAuth
is explicitly supported for use outside the Codex CLI") and OpenAI's own
Codex / ChatGPT help center for the plan-auth model. This shim is exactly that
use: it reads and refreshes the CLI's token, it does not mint a second
identity.

**A ChatGPT Plus/Pro/Business plan is licensed per plan seat, the same way a
Claude Pro/Max subscription is licensed for claude.ai and Claude Code.**
Usage through this shim bills to whichever ChatGPT plan is signed in via
`codex login` on this machine. Confirm that is the account you intend to bill
before wiring an agent to this lane.

### Refresh-token rotation — one `auth.json` per machine

Refresh tokens **rotate and are single-use**: the backend answers
`refresh_token_reused` to a replay. The shim refreshes the token in place, in
the *same* `auth.json` the Codex CLI reads and writes, so the shim and the CLI
must share exactly one file per machine.

- Do **not** copy `~/.codex/auth.json` to another machine "to save a login" —
  the first refresh on either copy invalidates the token on the other.
- Run `codex login` again on each machine that hosts this shim instead.
- Treat `auth.json` like a private key: `chmod 600`, never commit it, never
  paste it into a chat or a bug report.

## Run it

```bash
python3 tools/codex-shim.py            # stdlib only, nothing to install
uv run tools/codex-shim.py             # same thing (PEP 723, no dependencies)
python3 tools/codex-shim.py --check    # probe upstream once, exit 0/1 (doctor-friendly)
```

Env vars (all optional):

| Var | Default | Meaning |
|---|---|---|
| `CODEX_SHIM_HOST` | `127.0.0.1` | bind address — loopback unless you know why not (see topologies below) |
| `CODEX_SHIM_PORT` | `18777` | bind port — `doctor.py` / `setup.py` hardcode this port for `codex-shim` |
| `CODEX_SHIM_TIMEOUT` | `1800` | seconds, per request (long agent turns) |
| `CODEX_HOME` | `~/.codex` | directory holding `auth.json` + `models_cache.json` |
| `CODEX_SHIM_LOG_LEVEL` | `INFO` | stdlib logging level |

`--host` / `--port` flags override the env vars for a one-off run.

### TLS trust

Every outbound call verifies the certificate, and the shim makes sure it has
something to verify against. It uses Python's default trust store first
(`SSL_CERT_FILE` / `SSL_CERT_DIR` included). That store is EMPTY on a python.org
build whose `Install Certificates.command` never ran — it points at a CA file
that does not exist, so every HTTPS call fails `CERTIFICATE_VERIFY_FAILED` on a
machine where `curl` works. In that case the shim loads the host bundle OpenSSL
and `curl` already use (`/etc/ssl/cert.pem` on macOS, `ca-certificates.crt` on
Linux), or `certifi` when it is importable.

Verification is never disabled. With no anchors anywhere the call fails closed
with a 502 that names the fix, and `--check` exits 1.

## Model slug + effort convention

Slugs follow `<model-id>[-<effort>][-fast]`, effort one of
`low|medium|high|xhigh|max|ultra` (`minimal` is accepted as an alias for
`low`). The `-fast` suffix, when present, always comes **last**, after the
effort suffix. Examples:

- `gpt-5.6-sol-xhigh` -> model `gpt-5.6-sol`, `reasoning.effort = "xhigh"`
- `gpt-5.6-sol-high-fast` -> model `gpt-5.6-sol`, `reasoning.effort = "high"`,
  `service_tier = "priority"`
- `gpt-5.6-sol-fast` (no effort) -> `service_tier = "priority"`, effort unset

The body field `reasoning_effort` overrides the slug suffix when both are
present. `-ultra` goes on the wire as `max` (the backend rejects the literal
string `"ultra"`; the Codex CLI does the same substitution). An unknown or
non-string effort value, or a `-fast` suffix on a model whose catalog entry
has no `fast` speed tier, fails closed with an HTTP 400 — never silently
coerced or dropped.

Effort and `-fast` are both validated per-model against
`~/.codex/models_cache.json` (`supported_reasoning_levels`,
`additional_speed_tiers`) when that catalog is present; a `gpt-*` slug absent
from the catalog is passed through unvalidated (fails at the backend
instead), and a non-`gpt-*` slug without a catalog entry is rejected outright.

## What is pinned vs. dropped

Pinned on every request:

| Field | Value | Why |
|---|---|---|
| `store` | `false` | the ChatGPT backend requires it; the shim is stateless per call |
| `include` | `["reasoning.encrypted_content"]` | carries encrypted reasoning across turns since nothing is stored server-side |
| `reasoning.summary` | `"auto"` | matches the Codex CLI default |
| `stream` | `true` (upstream) | the shim always streams the backend call, then re-assembles a single JSON body when the caller did not ask for SSE |
| `service_tier` | `"priority"` when the slug carries `-fast`, otherwise omitted | the only wire for "fast" here |

Dropped and named in the shim's audit log line — never silently honored,
never faked:

| Caller field | Why dropped |
|---|---|
| `temperature`, `top_p`, `top_k` | backend answers 400 "Unsupported parameter" |
| `max_tokens`, `max_completion_tokens` | no `max_output_tokens` field on this backend; same 400 |
| `response_format` | the Responses API spells structured output differently (`text.format`); reshaping a caller's schema silently is a different contract, so it fails closed instead |
| `service_tier` (from the caller directly) | this shim owns it exclusively via the `-fast` slug suffix |
| `n`, `logprobs`, `top_logprobs`, `presence_penalty`, `frequency_penalty`, `seed`, `logit_bias`, `stop`, `store`, `prediction`, `modalities` | no Responses field on this backend |

## Deployment topologies (Grok Bot cloud box)

**(a) Run the shim ON the box, recommended.** Bind
`CODEX_SHIM_HOST=127.0.0.1` (the default), run `codex login` as the same user
that runs the shim, and point the binding's `hopBaseUrl` at
`http://127.0.0.1:18777/v1` — no network hop, no exposed credential surface
beyond the box itself.

**(b) Run the shim on your Mac, box reaches it over Tailscale.** Bind the shim
to your Mac's Tailscale IP (`CODEX_SHIM_HOST=100.x.y.z`, never `0.0.0.0`) and
point `tools/hop-server.py` on the box at
`http://<your-tailnet-ip>:18777/v1`. Anything non-loopback is an open ChatGPT
plan credential to whoever can route to it — put it on a tailnet only, never
a public interface.

Example `model-bindings.json` entry:

```json
{
  "agents": {
    "00000000-0000-4000-8000-000000000098": {
      "name": "Codex Sol (xhigh)",
      "modelId": "gpt-5.6-sol-xhigh",
      "provider": "chatgpt-codex-oauth",
      "hopBaseUrl": "http://127.0.0.1:18777/v1",
      "parameters": []
    }
  }
}
```

## Verification checklist

```bash
python3 tools/codex-shim.py --check              # exit 0/1, doctor-friendly

curl -s http://127.0.0.1:18777/healthz           # {"ok":true,"upstream":true}
curl -s http://127.0.0.1:18777/v1/models         # OpenAI list shape, from models_cache.json
curl -N -s http://127.0.0.1:18777/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5.6-sol-xhigh","stream":true,"messages":[{"role":"user","content":"say hi"}]}'
                                                  # SSE chunks, ends [DONE]

# negative control — a bogus CODEX_HOME with no auth.json must fail closed:
CODEX_HOME=/tmp/no-such-codex-home python3 tools/codex-shim.py --check; echo $?
                                                  # 1 (401 upstream, no credentials)

# same idea against a running shim pointed at the bogus home:
CODEX_HOME=/tmp/no-such-codex-home python3 tools/codex-shim.py --port 18787 &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"hi"}]}'
                                                  # 401
kill %1
```

If the direct probe returns 200 and the negative control also returns 200 (or
`--check` also exits 0), the auth boundary is decorative — stop and fix it
before trusting the lane.

## Persistence

- **macOS** — `examples/com.opengrok.codex-shim.plist` (launchd LaunchAgent,
  `RunAtLoad` + `KeepAlive`). Copy it, replace the `PLACEHOLDER` paths, then
  `launchctl bootstrap gui/$(id -u) <path-to-plist>`. No credential goes in
  the plist — the credential is `~/.codex/auth.json`, written once by
  `codex login` and refreshed in place by the shim.
- **Linux (the box)** — `examples/opengrok-codex-shim.service` (systemd user
  unit, same shape as `examples/opengrok-claude-shim.service`):
  `ExecStart=uv run /path/to/opengrok/tools/codex-shim.py`,
  `Restart=always`, `Environment=CODEX_SHIM_HOST=127.0.0.1`. There is no
  `EnvironmentFile=` credential for this shim (unlike the Claude shim's API
  key) — the credential lives in `CODEX_HOME/auth.json` on disk, written by
  `codex login` as the same user the unit runs as.

## Testing

```bash
python3 tools/test-codex-shim.py      # 79/79 — Codex shim, offline, no network
python3 tools/qa.py                   # repo self-check: leaks, refs, tests
```
