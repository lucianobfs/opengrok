# Claude shim (`tools/claude-shim.py`)

`tools/provider-maps.cjs` (`isClaudeRoute` / `claude-passthrough`) and
`tools/provider-maps-hop.cjs` (route `claude-plans`) have always assumed a hop
shim on `http://127.0.0.1:18776/v1` that owns Claude's thinking wire. That shim
now ships in this repo: `tools/claude-shim.py`, a single-file PEP 723 script
(stdlib `ThreadingHTTPServer`, one dependency: the official `anthropic` SDK).

It speaks OpenAI `chat/completions` on the front (what Grok Bot sends) and the
native Anthropic Messages API on the back (what the model was trained on), so
the maps' `claude-passthrough` / `claude-plans` routes have something real to
point at.

## Credentials

The shim never reads the inbound `Authorization` header — it is ignored and
never logged. Instead the official SDK's own credential chain resolves the
call, in order:

1. `ANTHROPIC_API_KEY` env var
2. `ANTHROPIC_AUTH_TOKEN` env var
3. an `ant auth login` profile on disk

**A Claude Pro/Max consumer subscription is licensed for claude.ai and Claude
Code only.** It is not a credential you can hand this shim, and it will not
authenticate `anthropic.Anthropic()` calls. Usage through this shim bills to a
separate Anthropic Console / API account (pay-as-you-go or a workspace with
API access). Confirm which account you are billing before wiring an agent to
this lane.

## Run it

```bash
uv run tools/claude-shim.py            # PEP 723 metadata installs anthropic
python3 tools/claude-shim.py           # plain python3, anthropic already installed
python3 tools/claude-shim.py --check   # probe upstream once, exit 0/1 (doctor-friendly)
```

Env vars (all optional):

| Var | Default | Meaning |
|---|---|---|
| `CLAUDE_SHIM_HOST` | `127.0.0.1` | bind address — loopback unless you know why not (see topologies below) |
| `CLAUDE_SHIM_PORT` | `18776` | bind port — the maps hardcode this port |
| `CLAUDE_SHIM_TIMEOUT` | `1800` | seconds, per request (long agent turns) |
| `CLAUDE_SHIM_MAX_TOKENS` | `128000` | model ceiling and clamp |
| `CLAUDE_SHIM_LOG_LEVEL` | `INFO` | stdlib logging level |

`--host` / `--port` flags override the env vars for a one-off run.

## Model slug + effort convention

Slugs follow `<model-id>-<effort>`, effort one of `low|medium|high|xhigh|max`
(`minimal` is accepted as an alias for `low`). Example: `claude-opus-5-xhigh`
resolves to model `claude-opus-5` with `output_config.effort = "xhigh"`.

The body field `reasoning_effort` overrides the slug suffix when both are
present. An unknown or non-string effort value fails closed with an HTTP 400 —
it is never silently coerced to a default.

## What is pinned vs. a documented noop

Pinned on every request:
- `thinking: {"type": "adaptive", "display": "summarized"}` — always on, no
  `budget_tokens` (the Claude 5 family 400s on that field).
- `output_config.effort` — set only when an effort was resolved from the slug
  or body; otherwise omitted and the API default (high) stands.
- Prompt caching — one ephemeral breakpoint on the last system block and one
  on the last user block, so a growing agent transcript reuses its own prefix.

Documented noop, dropped and named in the shim's audit log line — never
silently honored:
- `temperature`, `top_p`, `top_k` — the Claude 5 family 400s on these; the
  shim removes them rather than let the call fail.

## Deployment topologies (Grok Bot cloud box)

**(a) Run the shim ON the box, recommended.** Bind
`CLAUDE_SHIM_HOST=127.0.0.1` (the default) and point the binding's
`hopBaseUrl` at `http://127.0.0.1:18776/v1` — no network hop, no exposed
credential surface beyond the box itself.

**(b) Run the shim on your Mac, box reaches it over Tailscale.** Bind the shim
to your Mac's Tailscale IP (`CLAUDE_SHIM_HOST=100.x.y.z`, never `0.0.0.0`) and
point `tools/hop-server.py` on the box at
`http://<your-tailnet-ip>:18776/v1`. Anything non-loopback is an open
Anthropic credential to whoever can route to it — put it on a tailnet only,
never a public interface.

Example `model-bindings.json` entry:

```json
{
  "agents": {
    "00000000-0000-4000-8000-000000000099": {
      "name": "Claude Opus 5 (xhigh)",
      "modelId": "claude-opus-5-xhigh",
      "provider": "anthropic-direct",
      "hopBaseUrl": "http://127.0.0.1:18776/v1",
      "parameters": []
    }
  }
}
```

## Verification checklist

```bash
curl -s http://127.0.0.1:18776/healthz          # {"ok":true,"upstream":true}
curl -s http://127.0.0.1:18776/v1/models         # OpenAI list shape
curl -N -s http://127.0.0.1:18776/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-opus-5-xhigh","stream":true,"messages":[{"role":"user","content":"say hi"}]}'
                                                  # SSE chunks, ends [DONE]

# negative control — no credential resolvable (unset both env vars, no
# ant auth login profile) must fail closed:
ANTHROPIC_API_KEY= ANTHROPIC_AUTH_TOKEN= curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:18776/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-opus-5","messages":[{"role":"user","content":"hi"}]}'
                                                  # 401
```

If both the direct probe and the negative control return 200, the auth
boundary is decorative — stop and fix it before trusting the lane.

## Persistence

- **macOS** — `examples/com.opengrok.claude-shim.plist` (launchd LaunchAgent,
  `RunAtLoad` + `KeepAlive`). Copy it, replace the `PLACEHOLDER` paths, then
  `launchctl bootstrap gui/$(id -u) <path-to-plist>`. See the comments in that
  file for how to supply `ANTHROPIC_API_KEY` without ever putting it in the
  plist.
- **Linux (the box)** — a systemd unit with the same shape: `ExecStart=uv run
  /path/to/opengrok/tools/claude-shim.py`, `Restart=always`,
  `Environment=CLAUDE_SHIM_HOST=127.0.0.1`, credential loaded via
  `EnvironmentFile=` pointing at a chmod-600 file — never inline in the unit.
