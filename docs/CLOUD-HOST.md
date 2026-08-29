# Cloud-host integration: making a saved binding actually route

**The missing step (issue #1).** Saving a binding and pushing
`model-bindings.json` to the box is **not** enough. The stock Grok Bot cloud
host bundle (`/home/box/sand-host/host-main.cjs`) never reads
`model-bindings.json` — nothing in it can call a third-party model API, so a
saved hop binding is silently ignored and the agent falls back to its
original model.

An earlier version of `tools/apply-box-patch.py` in this repo targeted a
different, non-stock bundle shape. Its anchors do not exist in the stock
bundle (verified against sand-host version `1bcef91`), so it silently did
nothing on a real box. This document describes the current, verified design.

## The seam

The only place in the stock bundle that both (a) knows the current
conversation's agent id and (b) builds the executor used for a normal chat
turn is:

```js
const baseExecutor = session.getExecutor();
```

`tools/apply-box-patch.py` replaces that one line with:

```js
const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();
```

and injects a small top-level helper, once, right after the bundle's own
`fromRedactedCoreMessages` declaration:

```js
function __opengrokHopExecutor(host, session) {
  try {
    const m = require('/home/box/sand-data/hop-executor.cjs');
    return m.createHopExecutor(host, session, {});
  } catch (e) {
    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\n');
    return null;
  }
}
```

The helper **closes over nothing**. The third argument is an empty deps bag
(`createHopExecutor` treats `deps` as an optional `{ log? }`), so the injection
point is only a *placement* choice, not a scope dependency — see "Why the
messages are plain" below. `fromRedactedCoreMessages` is kept as the anchor
because it is the one declaration already proven unique and proven top-level
against this exact bundle.

`createHopExecutor` (in `tools/hop-executor.cjs`, installed at
`/home/box/sand-data/hop-executor.cjs`) looks up `host.getConversationId()` in
`model-bindings.json`. No binding for that agent → it returns `null` and the
line falls through to the stock `session.getExecutor()`, so an unbound agent
behaves exactly as before the patch. A binding exists → it returns an
executor that implements the same `BasePromptExecutor` contract the stock
executor does, but streams from the bound `hopBaseUrl` instead of the
built-in model.

The patch is byte-surgical and idempotent: every anchor is asserted
`count==1` before it is touched, so a changed upstream bundle makes
`apply-box-patch.py` refuse loudly instead of half-patching. See
`tools/apply-box-patch.py --help` for `--dry-run`, `--check-only`, and
`--revert`.

### Why the messages are plain

The messages the injected executor holds at this seam are **plain core
messages**, not redacted wrappers. Redaction is handled one layer OUT:

- `RedactedPromptToolExecutor` (bundle @19539275) *wraps* an inner executor —
  the slot `__opengrokHopExecutor` fills. Its `appendMessages` calls
  `fromRedactedCoreMessages(arr, PrivacyCapability.UNSAFE_ALWAYS_ALLOWED)` and
  forwards the **plain** result to `innerToolExecutor.appendMessages`; its
  `getState`/`getMessages` re-wrap on the way out with
  `toRedactedCoreMessages`; its `stream` forwards untouched.
- The stock `ProtoPromptExecutor` therefore feeds `this.builder.getMessages()`
  straight into `coreMessageToProto` (@23011518), which branches on
  `typeof msg.content === "string"` and `Array.isArray(msg.content)`.
- The runner call site that *does* unwrap (@19795174) operates on the outer
  redacted wrapper, never on the inner executor's own storage.

So `tools/hop-executor.cjs` converts what it stores directly to
chat/completions, exactly as `coreMessageToProto` does, and needs no
bundle-internal symbol at all. An earlier version of this design unwrapped its
own storage and crashed a live turn — see "Corrections" at the end of this
document.

### Patch states and migration

`apply-box-patch.py` classifies a bundle from its bytes alone:

| state | meaning | patch command does | `--check-only` exit |
|---|---|---|---|
| unpatched | the marker `__opengrokHopExecutor` is absent | full patch: seam + helper | 1 |
| patched-stale | the OLD helper block is present verbatim | **migrates in place**: swaps the old helper for the current one, leaves the (identical) seam replacement alone | 3, printed as `PATCHED-STALE:` |
| patched-current | the current helper block is present verbatim | prints `already patched`, writes nothing | 0 |
| foreign | the marker is present but neither block matches verbatim | refuses loudly, writes nothing | 1 |

A migration is as safe as a fresh patch: `node --check` on the original bytes,
a timestamped backup into `--backup-dir` **before** the write, the write, then
`node --check` on the result — and a failed post-check restores the backup and
exits non-zero. Running the tool twice is a no-op the second time.
`--dry-run` on a stale bundle prints `would MIGRATE` and writes nothing.
`--revert` is unchanged: it restores the newest backup in `--backup-dir`.

## File layout on the box

Everything opengrok owns lives under `/home/box/sand-data/`, never under
`/home/box/sand-host/`:

```
/home/box/sand-data/
├── model-bindings.json          # agent -> {modelId, hopBaseUrl, ...}
├── hop-executor.cjs             # the injected executor (tools/hop-executor.cjs)
├── agents/active-agent.json     # {"activeAgentId": "<uuid>"}, read by box-bind.py --agent active
├── codex-shim.log               # stdout/stderr of the running codex-shim.py
└── host-backups/                # timestamped pre-patch backups, written by apply-box-patch.py
    └── <version>-<utc>.cjs
```

This matters because of the upgrade-fragility risk below: only
`/home/box/sand-host/` gets pruned by the in-box updater, so nothing opengrok
needs may live there except the patched `host-main.cjs` itself.

## Operator flow

```
 local machine                              box (cloud computer)
 ─────────────                              ────────────────────
 setup.py / model-picker.py                  tools/box-bootstrap.sh
   writes model-bindings.json                  - checks out/updates ~/opengrok
   (push it to the box however you            - installs hop-executor.cjs
    already get files there — scp, the         - starts codex-shim.py :18777
    file relay, git)                           - runs apply-box-patch.py
                                                - restarts the host via the
                                                  supervisor if the patch changed it
                                              tools/box-bind.py
                                                - upserts one agent's binding
                                              tools/box-restart-host.py
                                                - supervisor-safe restart (never
                                                  a raw kill)
```

1. **Bootstrap** (on the box, as the box user):
   ```bash
   bash tools/box-bootstrap.sh
   ```
   Idempotent — re-run it after any change (a new opengrok commit, a Grok Bot
   version bump, a lost codex-shim process). It only acts on what is actually
   stale; see the file's header comment for the exact steps and env
   overrides (`OPENGROK_REPO`, `OPENGROK_BRANCH`, `OPENGROK_DIR`).

2. **Bind** an agent to a model:
   ```bash
   python3 tools/box-bind.py --agent active --model gpt-5.6-sol-high \
     --hop http://127.0.0.1:18777/v1
   ```
   `hop-executor.cjs` reads `model-bindings.json` fresh on every request (no
   restart needed for a binding change to take effect) — only a change to the
   *patch itself* needs a host restart.

3. **Patch** the host (bootstrap already does this; run it standalone to
   check state):
   ```bash
   # 0 patched (current), 1 unpatched, 2 unknown bundle, 3 patched-STALE
   python3 tools/apply-box-patch.py --check-only; echo $?
   python3 tools/apply-box-patch.py --dry-run                # show what would change
   python3 tools/apply-box-patch.py                          # patch, or migrate a stale helper
   ```
   Exit 3 from `--check-only` means the host carries an OLD helper. Run the
   patch command with no flags: it migrates the helper in place (backup and
   `node --check` both ways, same as a fresh patch) and is a no-op if you run
   it again. A migration changes the running code, so step 4 is required.

4. **Restart** (supervisor-safe, never a raw kill):
   ```bash
   python3 tools/box-restart-host.py
   ```
   Implements the supervisor's R1 restart protocol: read
   `/home/box/sand-data/gateway.json` for the health port and token, wait
   until `/health` reports `isBusy: false`, write
   `/tmp/sand-supervisor/command.json` (atomically) with a `restart` command,
   then wait for `/tmp/sand-supervisor/status.json` to report `hostRunning:
   true` again with a new host pid.

5. **Verify routing.** A "no changes needed" result from
   `apply-box-patch.py` proves the patch is installed, not that a real turn
   used it. Tail the shim log while sending a normal message in the bound
   conversation:
   ```bash
   tail -f /home/box/sand-data/codex-shim.log
   ```
   A request line appearing timestamped with your turn is the only proof the
   turn was actually routed through the hop.

## Upgrade fragility

The in-box auto-update watcher runs even when `settings.json` sets
`autoUpdateWhenIdleOptIn: false` — the in-box branch defeats that opt-out.
When it fires, `swapHostBundle` replaces `host-main.cjs` with a fresh stock
copy **and prunes everything else under `/home/box/sand-host/`**. That
silently undoes the patch (the version file still reads `1bcef91` afterward,
so `status.json` and the desktop "up to date" view misreport the running
code as unchanged).

Consequences:
- Never put an opengrok file under `/home/box/sand-host/` except the patched
  `host-main.cjs` itself — anything else there can be pruned without notice.
  Keep everything else under `/home/box/sand-data/` (never pruned).
- **Re-run `tools/box-bootstrap.sh` after any version bump** (or on a cron —
  it is idempotent and a no-op when nothing changed). It re-detects an
  unpatched host and re-applies.
- Do not trust the on-disk version string alone as proof the patch survived —
  use `apply-box-patch.py --check-only` (or grep for the injected helper name
  `__opengrokHopExecutor`) as the real signal.

## Verification checklist

```bash
python3 tools/apply-box-patch.py --check-only; echo $?   # expect 0 after bootstrap
                                                         # 3 = stale helper, re-run the patch
grep -c "__opengrokHopExecutor" /home/box/sand-host/host-main.cjs   # expect 2
grep -c "createHopExecutor(host, session, {})" /home/box/sand-host/host-main.cjs   # expect 1
curl -fsS http://127.0.0.1:18777/healthz                  # codex-shim up
tail -f /home/box/sand-data/codex-shim.log                # while sending a message
```

The marker appears **twice** in a correctly patched bundle: once at the seam
call site and once as the injected helper's own name. The second `grep` is the
one that distinguishes the current helper from the stale one.

## Corrections

**The seam does not hold redacted messages.** The first analysis of the stock
bundle claimed the executor at
`const baseExecutor = session.getExecutor();` receives REDACTED core messages,
and concluded the injected executor had to unwrap them with
`fromRedactedCoreMessages(msgs, PrivacyCapability.UNSAFE_ALWAYS_ALLOWED)`. The
first shipped version of `tools/hop-executor.cjs` did exactly that.

**The live evidence that disproved it.** A real turn on the box failed with

```
[opengrok] hop conversation=7e509c66-... model=gpt-5.6-sol-xhigh status=0 \
  error=message.content.unwrap is not a function
```

surfaced on the desktop as "Technical details: message.content.unwrap is not a
function". `fromRedactedSystemMessage` (@16083633) is
`message.content.unwrap(purpose, opts)` — it threw because the system prompt
handed to it was a plain string, not a redacted wrapper.

**The offsets that settle it.**

- `@19539275` `RedactedPromptToolExecutor` wraps the inner executor. It calls
  `fromRedactedCoreMessages` on the way IN and `toRedactedCoreMessages` on the
  way OUT, so unwrapping happens outside the executor this patch replaces.
- `@23011518` `coreMessageToProto` reads `msg.content` as a string or an array
  — the stock executor never unwraps either.
- `@16083633` `fromRedactedSystemMessage` is the function that threw.

**What changed.** The unwrap step and the `fromRedactedCoreMessages` /
`PrivacyCapability` deps are gone; `deps` is now an optional `{ log? }` bag and
the injected helper passes `{}`. `tools/test-hop-executor.cjs` carries a named
regression check that drives the exact message shape that crashed.
