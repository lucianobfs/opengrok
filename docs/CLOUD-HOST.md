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
declarations of `fromRedactedCoreMessages` and `PrivacyCapability` (the two
bundle-internal symbols the helper needs — see "Why those two symbols" below):

```js
function __opengrokHopExecutor(host, session) {
  try {
    const m = require('/home/box/sand-data/hop-executor.cjs');
    return m.createHopExecutor(host, session, { fromRedactedCoreMessages, PrivacyCapability });
  } catch (e) {
    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\n');
    return null;
  }
}
```

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

### Why those two symbols

`fromRedactedCoreMessages` and `PrivacyCapability` are the bundle-internal
helpers the stock runner already uses to unwrap redacted core messages before
handing them to a model. The injected executor needs the same unwrap step
(the messages it receives from the builder are REDACTED core messages, not
plain ones), so the helper resolves both symbols from the bundle's own scope
and passes them into `createHopExecutor` as `deps` rather than reimplementing
message-redaction logic in `tools/hop-executor.cjs`. `apply-box-patch.py`
verifies both are reachable at the injection point before it writes anything.

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
   python3 tools/apply-box-patch.py --check-only; echo $?   # 0 patched, 1 unpatched, 2 unknown bundle
   python3 tools/apply-box-patch.py --dry-run                # show what would change
   ```

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
grep -c "__opengrokHopExecutor" /home/box/sand-host/host-main.cjs   # expect 1
curl -fsS http://127.0.0.1:18777/healthz                  # codex-shim up
tail -f /home/box/sand-data/codex-shim.log                # while sending a message
```
