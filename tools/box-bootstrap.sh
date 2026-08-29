#!/usr/bin/env bash
# box-bootstrap.sh — idempotent setup for the box-side binding consumer.
#
# Run this ON the box, as the box user (the account sand-host runs as). Safe
# to re-run any number of times: every step checks current state first and
# only acts when something is actually missing or stale.
#
# What it does, in order:
#   1. ensures ~/.local/bin is on PATH for this shell
#   2. verifies `codex login status` reports logged in (Codex shim needs the
#      Codex CLI's own OAuth token; see docs/CODEX-SHIM.md)
#   3. installs or updates ~/opengrok from git (clone if absent, pull if not)
#   4. copies tools/hop-executor.cjs to /home/box/sand-data/hop-executor.cjs
#   5. starts tools/codex-shim.py on 127.0.0.1:18777 if /healthz is not
#      already answering
#   6. runs tools/apply-box-patch.py (idempotent), and runs
#      tools/box-restart-host.py ONLY when the patch actually changed the
#      live host file
#   7. prints a summary of what changed and what was already fine
#
# Env overrides:
#   OPENGROK_REPO    git remote to clone/pull  (default: https://github.com/lucianobfs/opengrok.git)
#   OPENGROK_BRANCH  branch to track           (default: claude-shim)
#   OPENGROK_DIR     local checkout path       (default: $HOME/opengrok)
#
# See docs/CLOUD-HOST.md for the full operator flow (bootstrap -> bind ->
# patch -> supervisor restart -> verify) and the upgrade-fragility notes.
set -euo pipefail

OPENGROK_REPO="${OPENGROK_REPO:-https://github.com/lucianobfs/opengrok.git}"
OPENGROK_BRANCH="${OPENGROK_BRANCH:-claude-shim}"
OPENGROK_DIR="${OPENGROK_DIR:-$HOME/opengrok}"
SAND_DATA="${SAND_DATA:-/home/box/sand-data}"
CODEX_SHIM_PORT="${CODEX_SHIM_PORT:-18777}"
CODEX_SHIM_LOG="${CODEX_SHIM_LOG:-$SAND_DATA/codex-shim.log}"

SUMMARY=()
note() { SUMMARY+=("$1"); echo "-- $1"; }

# 1. PATH ---------------------------------------------------------------
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  export PATH="$HOME/.local/bin:$PATH"
  note "PATH: added ~/.local/bin for this run (add it to your shell rc to persist)"
else
  note "PATH: ~/.local/bin already present"
fi

# 2. Codex login ----------------------------------------------------------
if ! command -v codex >/dev/null 2>&1; then
  echo "ERROR: codex CLI not found on PATH. Install it, then re-run this script." >&2
  exit 1
fi
if ! codex login status >/dev/null 2>&1; then
  echo "ERROR: Codex CLI is not logged in." >&2
  echo "Run this first, then re-run box-bootstrap.sh:" >&2
  echo "    codex login" >&2
  exit 1
fi
note "codex login: logged in"

# 3. Install/update ~/opengrok ---------------------------------------------
if [[ -d "$OPENGROK_DIR/.git" ]]; then
  git -C "$OPENGROK_DIR" fetch --quiet origin "$OPENGROK_BRANCH"
  git -C "$OPENGROK_DIR" checkout --quiet "$OPENGROK_BRANCH"
  before_sha="$(git -C "$OPENGROK_DIR" rev-parse HEAD)"
  git -C "$OPENGROK_DIR" merge --ff-only --quiet "origin/$OPENGROK_BRANCH"
  after_sha="$(git -C "$OPENGROK_DIR" rev-parse HEAD)"
  if [[ "$before_sha" == "$after_sha" ]]; then
    note "repo: $OPENGROK_DIR already up to date ($after_sha)"
  else
    note "repo: $OPENGROK_DIR updated $before_sha -> $after_sha"
  fi
else
  git clone --quiet --branch "$OPENGROK_BRANCH" "$OPENGROK_REPO" "$OPENGROK_DIR"
  note "repo: cloned $OPENGROK_REPO ($OPENGROK_BRANCH) into $OPENGROK_DIR"
fi

# 4. hop-executor.cjs -------------------------------------------------------
mkdir -p "$SAND_DATA"
src_executor="$OPENGROK_DIR/tools/hop-executor.cjs"
dst_executor="$SAND_DATA/hop-executor.cjs"
if [[ ! -f "$src_executor" ]]; then
  echo "ERROR: $src_executor not found in the checkout — repo out of date?" >&2
  exit 1
fi
if [[ -f "$dst_executor" ]] && cmp -s "$src_executor" "$dst_executor"; then
  note "hop-executor.cjs: $dst_executor already current"
else
  cp "$src_executor" "$dst_executor"
  note "hop-executor.cjs: installed to $dst_executor"
fi

# 5. codex-shim.py ----------------------------------------------------------
if curl -fsS -o /dev/null "http://127.0.0.1:${CODEX_SHIM_PORT}/healthz" 2>/dev/null; then
  note "codex-shim: already answering on 127.0.0.1:${CODEX_SHIM_PORT}"
else
  setsid nohup python3 "$OPENGROK_DIR/tools/codex-shim.py" --port "$CODEX_SHIM_PORT" \
    >>"$CODEX_SHIM_LOG" 2>&1 < /dev/null &
  disown || true
  # give it a moment to bind before we check, then verify it actually came up
  for _ in 1 2 3 4 5; do
    sleep 1
    if curl -fsS -o /dev/null "http://127.0.0.1:${CODEX_SHIM_PORT}/healthz" 2>/dev/null; then
      break
    fi
  done
  if curl -fsS -o /dev/null "http://127.0.0.1:${CODEX_SHIM_PORT}/healthz" 2>/dev/null; then
    note "codex-shim: started on 127.0.0.1:${CODEX_SHIM_PORT} (log: $CODEX_SHIM_LOG)"
  else
    echo "ERROR: codex-shim did not come up on 127.0.0.1:${CODEX_SHIM_PORT}; see $CODEX_SHIM_LOG" >&2
    exit 1
  fi
fi

# 6. patch the host, restart only if it actually changed --------------------
patch_out="$(mktemp)"
trap 'rm -f "$patch_out"' EXIT
if python3 "$OPENGROK_DIR/tools/apply-box-patch.py" | tee "$patch_out"; then
  if grep -qi "no changes needed\|already patched" "$patch_out"; then
    note "apply-box-patch: host already patched, no restart needed"
  else
    note "apply-box-patch: host changed"
    python3 "$OPENGROK_DIR/tools/box-restart-host.py"
    note "box-restart-host: supervisor restart requested"
  fi
else
  echo "ERROR: apply-box-patch.py failed; see output above" >&2
  exit 1
fi

# 7. summary ------------------------------------------------------------
echo
echo "== box-bootstrap summary =="
for line in "${SUMMARY[@]}"; do
  echo "  - $line"
done
