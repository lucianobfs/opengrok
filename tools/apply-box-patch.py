#!/usr/bin/env python3
"""apply-box-patch — install the hop-executor consumer into a STOCK Grok Bot
cloud host bundle (sand-host version aea062b).

BACKGROUND: the previous version of this tool targeted a privately modified
host bundle. Its anchors (resolvedTopLevelModelId, createOpenAiHopSession,
openaiBaseUrl, provenanceAgentId, model-bindings, openai-hop-session) occur
ZERO times in the stock bundle. This rewrite targets the real, stock
bundle as reverse-engineered in
/Users/lucianobfs/Developer/GitHub/opengrok-box-analysis-1bcef91.txt
and re-verified against live sand-host aea062b (the in-box updater replaced
1bcef91; the unique strings below survived).

VERIFIED FACTS (re-checked against live /home/box/sand-host/host-main.cjs,
25,634,503 bytes, md5 ba86c15a449c5daa2be9e9a0f68fa7a7, version aea062b):
  - b'const baseExecutor = session.getExecutor();'   count 1, byte 25208903
      (the main-turn seam: this is where the live host asks the session for
      the executor it will drive the turn with; `host` is in scope).
  - memory / self-summary / dreaming also call getExecutor() and bypass that
    seam, so they still hit Cursor. Unique anchors, all count 1:
      summarizeEpisode call     byte 25241549  (ctx in scope; conversation
                                id is ctx.get(conversationIdKey2) =
                                host.getTranscriptId())
      extractMemories call      byte 25242281  (same)
      executeSelfSummaryWithRetry body  byte 17390575
                                (ctx in scope; try conversationIdKey2 then
                                conversationIdKey)
      MemoryService.runAgent    bytes 23342702 / 23343636
                                (agentId in scope; dreaming/synthesis)
  - b'function fromRedactedCoreMessages'              count 1, byte 16082646
    and its full three-line top-level declaration (ANCHOR_HELPER_AFTER below)
    also count 1. It is a plain top-level statement in the flat esbuild
    concatenation (`function ...` at column 0, preceded by a
    `// ../packages/...` module banner) — NOT inside an __esm lazy-init
    wrapper, and it sits far before the seams.

WHY THAT ANCHOR, NOW THAT THE HELPER NEEDS NOTHING IN SCOPE: the injected
helper closes over NO bundle identifier (see below), so any verified-unique
top-level position would do. The fromRedactedCoreMessages declaration is kept
purely as a PLACEMENT point because it is the one already proven unique and
proven top-level against this exact bundle — changing it would buy nothing and
would need a fresh uniqueness proof. Its count is still asserted == 1.

WHAT THIS TOOL DOES (byte-surgical, idempotent, anchor counts asserted == 1;
refuses loudly and changes nothing if an anchor is missing or duplicated):
  1. Replaces each seam in SEAMS (main turn, episode summary, memory
     extraction, self-summary, memory-synthesis, memory-verification) so
     the hop executor is used when the conversation is bound. Memory and
     self-summary sites do not have `host`; they pass a duck
     `{ getConversationId: () => ctx.get(conversationIdKey2) }` (or the
     `agentId` string at the dreaming sites). hop-executor.cjs accepts
     either a host object or a conversation-id string.
  2. Injects, once, immediately after the top-level fromRedactedCoreMessages
     function declaration, a helper:
       function __opengrokHopExecutor(host, session) {
         try {
           const m = require('/home/box/sand-data/hop-executor.cjs');
           return m.createHopExecutor(host, session, {});
         } catch (e) {
           process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');
           return null;
         }
       }
     The third argument is an empty deps bag: hop-executor.cjs treats deps as
     an optional { log? } and requires nothing from the bundle. The messages
     the inner executor holds are PLAIN core messages — RedactedPromptToolExecutor
     (@19539275) unwraps on the way in and re-wraps on the way out, OUTSIDE the
     inner executor — so there is no unwrap step and nothing to close over.

MIGRATION FROM THE OLD HELPER: an earlier generation of this tool injected the
same block with `{ fromRedactedCoreMessages, PrivacyCapability }` as the third
argument, and that unwrap step crashed a live turn with
"message.content.unwrap is not a function". That exact old text is frozen in
OLD_HELPER_CODE and is recognised, so a box already carrying it is migrated in
place instead of being left stale or double-patched.

Idempotency — states, from the file's bytes alone:
  unpatched         the marker "__opengrokHopExecutor" is absent
                    -> full patch (all seam replacements + new helper)
  patched-stale     OLD_HELPER_CODE present verbatim, exactly once
                    -> the old helper block is replaced by the new one, then
                       any still-original extra seams are applied.
  patched-partial   current HELPER_CODE present, but at least one extra
                    seam (memory / self-summary / dreaming) is still original
                    -> those remaining seams are applied; helper left alone.
  patched-current   HELPER_CODE present verbatim AND every SEAMS replacement
                    present verbatim -> "already patched", NOTHING is written
  marker present, neither helper block verbatim -> a foreign or hand-edited
                    patch. Every command refuses loudly with exit 1 and
                    writes nothing.
A migration runs the full safety chain: `node --check` on the ORIGINAL bytes,
a timestamped backup, the write, `node --check` on the result, and restore the
backup then exit non-zero if that post-check fails.

Before patching (unless --force), the tool refuses unless the bundle is the
expected one: either /home/box/sand-host/version (sibling "version" file next
to --host, override with --version-file) reads "aea062b", or the host file's
md5 equals ba86c15a449c5daa2be9e9a0f68fa7a7. --force skips this version check
but the anchor-count assertions ALWAYS run — --force never causes a half-patch.

Every real write is preceded by a backup (default
/home/box/sand-data/host-backups/<version>-<utc>.cjs, override with
--backup-dir) and by `node --check` on the ORIGINAL file (a broken original
is refused, not patched over) and, after writing, `node --check` on the
PATCHED file — a post-patch syntax failure restores the backup and exits
non-zero.

USAGE:
    python3 apply-box-patch.py --host /home/box/sand-host/host-main.cjs
    python3 apply-box-patch.py --dry-run
    python3 apply-box-patch.py --check-only
    python3 apply-box-patch.py --revert

EXIT CODES:
  0   success: patched (or migrated / already patched / dry-run reported /
      revert done); --check-only: bundle is patched with the CURRENT helper
  1   --check-only: bundle is unpatched (stock, unmodified); also the generic
      failure code (missing file, node --check failure, revert with no
      backup found, a foreign patch this tool did not write, etc.)
  2   version/md5 refusal (patch mode without --force sees an unexpected
      bundle and neither --force nor a version match is present);
      --check-only: bundle version/md5 does not match anything known
  3   carries two meanings that cannot collide, because the two commands are
      mutually exclusive:
        patch mode:   anchor-count assertion failed (an anchor is missing or
                      duplicated — refuses to half-patch; nothing is written)
        --check-only: PATCHED-STALE or PATCHED-PARTIAL, the bundle carries
                      an older helper and/or is missing the extra seams and
                      needs another run. --check-only never runs the
                      anchor assertions, so it can never mean the other thing.

After patching, bounce the host with tools/box-restart-host.py (the
supervisor "restart" R1 protocol — never kill the process directly). See
docs/CLOUD-HOST.md.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time

EXPECTED_VERSION = "aea062b"
EXPECTED_MD5 = "ba86c15a449c5daa2be9e9a0f68fa7a7"

ANCHOR_SEAM = b"const baseExecutor = session.getExecutor();"
REPLACEMENT_SEAM = (
    b"const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();"
)

# Extra seams: memory extension + self-summary + dreaming. Each string is
# unique (count==1) in aea062b. The helper is unchanged; these sites do not
# have `host`, so they pass a duck host (ctx.get(conversationIdKey2)) or a
# conversation-id string (agentId). hop-executor.cjs accepts both.
ANCHOR_EPISODE = (
    b"      const narrative = await summarizeEpisode({\n"
    b"        executor: session.getExecutor(),"
)
REPLACEMENT_EPISODE = (
    b"      const narrative = await summarizeEpisode({\n"
    b"        executor: __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) }, session) ?? session.getExecutor(),"
)

ANCHOR_EXTRACT = (
    b"    const extraction = await extractMemories({\n"
    b"      executor: session.getExecutor(),"
)
REPLACEMENT_EXTRACT = (
    b"    const extraction = await extractMemories({\n"
    b"      executor: __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) }, session) ?? session.getExecutor(),"
)

ANCHOR_SELF_SUMMARY = b"        const executor = summarizationPromptSession.getExecutor();"
REPLACEMENT_SELF_SUMMARY = (
    b"        const executor = __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) ?? ctx.get(conversationIdKey) }, summarizationPromptSession) ?? summarizationPromptSession.getExecutor();"
)

ANCHOR_SYNTHESIS = b'            executor: this.options.createExecutor("synthesis"),'
REPLACEMENT_SYNTHESIS = (
    b"            executor: __opengrokHopExecutor(agentId, null) ?? this.options.createExecutor(\"synthesis\"),"
)

ANCHOR_VERIFICATION = b'            executor: this.options.createExecutor("verification"),'
REPLACEMENT_VERIFICATION = (
    b"            executor: __opengrokHopExecutor(agentId, null) ?? this.options.createExecutor(\"verification\"),"
)

# (name, original, replacement) — applied in this order. The main-turn seam
# is first so a partial patch from the previous generation (helper + main
# seam only) is recognised: remaining() then lists the extra seams.
SEAMS = (
    ("main-turn", ANCHOR_SEAM, REPLACEMENT_SEAM),
    ("summarize-episode", ANCHOR_EPISODE, REPLACEMENT_EPISODE),
    ("extract-memories", ANCHOR_EXTRACT, REPLACEMENT_EXTRACT),
    ("self-summary", ANCHOR_SELF_SUMMARY, REPLACEMENT_SELF_SUMMARY),
    ("memory-synthesis", ANCHOR_SYNTHESIS, REPLACEMENT_SYNTHESIS),
    ("memory-verification", ANCHOR_VERIFICATION, REPLACEMENT_VERIFICATION),
)

# The full, verbatim, unique top-level declaration of fromRedactedCoreMessages.
# The injected helper closes over NOTHING, so this is only a proven-unique,
# proven-top-level PLACEMENT point (see "WHY THAT ANCHOR" in the module
# docstring) — not a scope dependency.
ANCHOR_HELPER_AFTER = (
    b"function fromRedactedCoreMessages(messages2, purpose, opts) {\n"
    b"  return messages2.map((msg) => fromRedactedCoreMessage(msg, purpose, opts));\n"
    b"}"
)

HELPER_MARKER = b"__opengrokHopExecutor"

HELPER_CODE = (
    b"\n"
    b"function __opengrokHopExecutor(host, session) {\n"
    b"  try {\n"
    b"    const m = require('/home/box/sand-data/hop-executor.cjs');\n"
    b"    return m.createHopExecutor(host, session, {});\n"
    b"  } catch (e) {\n"
    b"    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');\n"
    b"    return null;\n"
    b"  }\n"
    b"}\n"
)

# HISTORY, FROZEN. The helper the previous generation of this tool injected and
# that is installed on the live box right now. It differs from HELPER_CODE in
# exactly one line: the deps bag it passed to createHopExecutor. That deps bag
# drove an unwrap step that crashed a real turn with
# "message.content.unwrap is not a function".
# Never regenerate this from HELPER_CODE — it is a record of what was written,
# and it must keep matching those bytes even when HELPER_CODE moves again.
OLD_HELPER_CODE = (
    b"\n"
    b"function __opengrokHopExecutor(host, session) {\n"
    b"  try {\n"
    b"    const m = require('/home/box/sand-data/hop-executor.cjs');\n"
    b"    return m.createHopExecutor(host, session, { fromRedactedCoreMessages, PrivacyCapability });\n"
    b"  } catch (e) {\n"
    b"    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');\n"
    b"    return null;\n"
    b"  }\n"
    b"}\n"
)

DEFAULT_HOST = "/home/box/sand-host/host-main.cjs"
DEFAULT_BACKUP_DIR = "/home/box/sand-data/host-backups"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VERSION_REFUSAL = 2
EXIT_ANCHOR_FAIL = 3
# Same number, deliberately: the two meanings belong to two mutually exclusive
# commands (--check-only never runs the anchor assertions). See the docstring.
EXIT_STALE = 3


def log(msg):
    print(msg, file=sys.stderr)


def die(msg, code=EXIT_GENERIC):
    log(f"ERROR: {msg}")
    sys.exit(code)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


STATE_UNPATCHED = "unpatched"
STATE_STALE = "patched-stale"
STATE_PARTIAL = "patched-partial"
STATE_CURRENT = "patched-current"
STATE_FOREIGN = "patched-foreign"

FOREIGN_MESSAGE = (
    "{host} contains the marker '__opengrokHopExecutor' but neither the current "
    "nor the old helper block verbatim — a foreign or hand-edited patch. "
    "Refusing to touch it. Restore a known bundle (apply-box-patch.py --revert, "
    "or a fresh sand-host copy) and run this tool again."
)


def remaining_seams(data):
    """Return (name, anchor, replacement) for every SEAMS entry whose
    replacement is not yet present. Callers must still assert that each
    leftover original anchor occurs exactly once before replacing.
    """
    leftover = []
    for name, anchor, replacement in SEAMS:
        if data.count(replacement) == 1:
            continue
        leftover.append((name, anchor, replacement))
    return leftover


def patch_state(data):
    """Classify the bundle from its bytes alone. See the module docstring's
    idempotency table.
    """
    current = data.count(HELPER_CODE)
    stale = data.count(OLD_HELPER_CODE)
    if current == 1 and stale == 0:
        if not remaining_seams(data):
            return STATE_CURRENT
        return STATE_PARTIAL
    if stale == 1 and current == 0:
        return STATE_STALE
    if HELPER_MARKER in data:
        return STATE_FOREIGN
    return STATE_UNPATCHED


def default_version_file(host_path):
    return os.path.join(os.path.dirname(os.path.abspath(host_path)), "version")


def bundle_version_ok(host_path, version_file):
    """Return True if the bundle at host_path is the expected aea062b bundle,
    checked first by the sibling version file, then by md5 of the file
    itself. Returns False if neither check confirms the expected bundle.
    """
    if version_file and os.path.exists(version_file):
        try:
            v = open(version_file, encoding="utf-8").read().strip()
        except OSError:
            v = None
        if v == EXPECTED_VERSION:
            return True
        if v:
            # a version file exists and names a different version: that is
            # conclusive, do not fall through to the md5 check.
            return False
    try:
        data = read_bytes(host_path)
    except OSError:
        return False
    return hashlib.md5(data).hexdigest() == EXPECTED_MD5


def node_check_bytes(data, label):
    """Write data to a throwaway temp file and run `node --check` on it."""
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".cjs")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"node --check failed for {label}:\n{r.stderr}")
    finally:
        os.unlink(tmp)


def assert_unique(data, blob, label):
    count = data.count(blob)
    if count != 1:
        die(
            f"anchor {label!r} count={count} (expected 1) — "
            "upstream bundle changed; refusing to half-patch",
            EXIT_ANCHOR_FAIL,
        )


def apply_seams(data, seams):
    """Replace each (name, anchor, replacement) after asserting the original
    anchor is unique. `data` must not already contain the replacement.
    """
    new_data = data
    for name, anchor, replacement in seams:
        assert_unique(new_data, anchor, name)
        new_data = new_data.replace(anchor, replacement, 1)
    return new_data


def compute_patch(data):
    """Return the fully patched bytes for an UNPATCHED bundle.
    Raises SystemExit (via die) if an anchor is missing or duplicated.
    """
    assert_unique(data, ANCHOR_HELPER_AFTER, "fromRedactedCoreMessages declaration")
    new_data = apply_seams(data, SEAMS)
    new_data = new_data.replace(
        ANCHOR_HELPER_AFTER, ANCHOR_HELPER_AFTER + HELPER_CODE, 1
    )
    return new_data


def compute_migration(data):
    """Return the bytes of a PATCHED-STALE bundle with the old helper block
    swapped for the current one, then any still-original extra seams applied.
    patch_state() has already established that OLD_HELPER_CODE occurs exactly
    once. The main-turn seam replacement is identical in both generations and
    is therefore already correct; remaining() skips it.
    """
    new_data = data.replace(OLD_HELPER_CODE, HELPER_CODE, 1)
    return apply_seams(new_data, remaining_seams(new_data))


def compute_extend(data):
    """Return the bytes of a PATCHED-PARTIAL bundle with the leftover extra
    seams applied. The current helper and the main-turn seam are left alone.
    """
    leftover = remaining_seams(data)
    if not leftover:
        return data
    return apply_seams(data, leftover)


def cmd_check_only(args):
    if not os.path.exists(args.host):
        die(f"host not found: {args.host}")
    data = read_bytes(args.host)
    state = patch_state(data)
    if state == STATE_FOREIGN:
        die(FOREIGN_MESSAGE.format(host=args.host))
    if state == STATE_CURRENT:
        print(f"PATCHED: {args.host}")
        return EXIT_OK
    if state == STATE_STALE:
        print(
            f"PATCHED-STALE: {args.host} carries the OLD helper "
            "(createHopExecutor with { fromRedactedCoreMessages, PrivacyCapability }); "
            "re-run apply-box-patch.py to migrate it in place, then restart the host"
        )
        return EXIT_STALE
    if state == STATE_PARTIAL:
        names = ", ".join(name for name, _, _ in remaining_seams(data))
        print(
            f"PATCHED-PARTIAL: {args.host} has the current helper but is missing "
            f"extra seams ({names}); re-run apply-box-patch.py to extend it, "
            "then restart the host"
        )
        return EXIT_STALE
    version_file = args.version_file or default_version_file(args.host)
    if not bundle_version_ok(args.host, version_file):
        print(f"UNKNOWN BUNDLE: {args.host} (version/md5 does not match {EXPECTED_VERSION})")
        return EXIT_VERSION_REFUSAL
    print(f"UNPATCHED: {args.host}")
    return EXIT_GENERIC


def newest_backup(backup_dir):
    if not os.path.isdir(backup_dir):
        return None
    entries = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.endswith(".cjs")
    ]
    if not entries:
        return None
    entries.sort(key=os.path.getmtime)
    return entries[-1]


def cmd_revert(args):
    backup_dir = args.backup_dir
    backup = newest_backup(backup_dir)
    if backup is None:
        die(f"no backup found in {backup_dir}")
    if not os.path.exists(args.host):
        die(f"host not found: {args.host}")
    print(f"reverting {args.host} from {backup}")
    shutil.copy2(backup, args.host)
    print("revert done")
    return EXIT_OK


def write_patched(args, version_file, data, new_data, verb):
    """The full safety chain shared by a fresh patch and a migration:
    node --check the ORIGINAL bytes, back the file up, write, node --check the
    result, and restore the backup then die if that post-check fails.
    """
    print("== syntax check (pre-patch) ==")
    node_check_bytes(data, args.host)
    print(f"  ok: node --check {args.host} (pre-patch)")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    version_label = EXPECTED_VERSION
    if version_file and os.path.exists(version_file):
        try:
            v = open(version_file, encoding="utf-8").read().strip()
            if v:
                version_label = v
        except OSError:
            pass
    os.makedirs(args.backup_dir, exist_ok=True)
    backup_path = os.path.join(args.backup_dir, f"{version_label}-{stamp}.cjs")
    shutil.copy2(args.host, backup_path)
    print(f"  backup -> {backup_path}")

    write_bytes(args.host, new_data)
    print(f"  [host] {args.host} {verb}")

    print("== syntax check (post-patch) ==")
    r = subprocess.run(["node", "--check", args.host], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR: node --check failed after {verb}:\n{r.stderr}")
        log(f"restoring backup from {backup_path}")
        shutil.copy2(backup_path, args.host)
        die(f"post-{verb} syntax check failed; original restored")
    print(f"  ok: node --check {args.host} (post-patch)")


NEXT_STEPS = """
DONE. Next steps (see docs/CLOUD-HOST.md):
  1. Make sure tools/hop-executor.cjs is installed at
     /home/box/sand-data/hop-executor.cjs (box-bootstrap.sh does this).
  2. Bounce the host with tools/box-restart-host.py (supervisor-safe R1
     restart — NEVER kill the process directly).
  3. Send a normal message in a Bot conversation bound to a hop model and
     confirm codex-shim.log sees the request.
"""


def cmd_patch(args):
    if not os.path.exists(args.host):
        die(f"host not found: {args.host}")
    data = read_bytes(args.host)
    state = patch_state(data)
    version_file = args.version_file or default_version_file(args.host)

    if state == STATE_FOREIGN:
        die(FOREIGN_MESSAGE.format(host=args.host))

    if state == STATE_CURRENT:
        print(f"already patched: {args.host}")
        return EXIT_OK

    if state == STATE_STALE:
        # Detected BEFORE the version/md5 gate on purpose: an already-patched
        # bundle can never match the stock md5, so the gate would block every
        # migration. The main-turn seam is already replaced and is identical
        # in both generations; remaining extra seams are applied after the
        # helper swap.
        new_data = compute_migration(data)
        if args.dry_run:
            leftover = remaining_seams(data.replace(OLD_HELPER_CODE, HELPER_CODE, 1))
            extra = ", ".join(name for name, _, _ in leftover) or "(none)"
            print("DRY RUN: would MIGRATE the old helper to the current one")
            print(f"  old helper block count: {data.count(OLD_HELPER_CODE)}")
            print(f"  extra seams to apply: {extra}")
            print("  host: CHANGED")
            return EXIT_OK
        write_patched(args, version_file, data, new_data, "migrated")
        print(NEXT_STEPS)
        return EXIT_OK

    if state == STATE_PARTIAL:
        new_data = compute_extend(data)
        leftover = remaining_seams(data)
        extra = ", ".join(name for name, _, _ in leftover)
        if args.dry_run:
            print(f"DRY RUN: would EXTEND with seams: {extra}")
            for name, anchor, _replacement in leftover:
                print(f"  seam {name} count: {data.count(anchor)}")
            print("  host: CHANGED")
            return EXIT_OK
        write_patched(args, version_file, data, new_data, "extended")
        print(NEXT_STEPS)
        return EXIT_OK

    if not args.force:
        if not bundle_version_ok(args.host, version_file):
            die(
                f"{args.host} does not look like the expected {EXPECTED_VERSION} bundle "
                f"(version file {version_file!r} / md5 mismatch) — pass --force to skip "
                "the version check (anchor counts are still asserted)",
                EXIT_VERSION_REFUSAL,
            )

    new_data = compute_patch(data)

    if args.dry_run:
        print("DRY RUN: would patch")
        for name, anchor, _replacement in SEAMS:
            print(f"  seam {name} count: {data.count(anchor)}")
        print(f"  helper anchor count: {data.count(ANCHOR_HELPER_AFTER)}")
        print("  host: CHANGED")
        return EXIT_OK

    write_patched(args, version_file, data, new_data, "patched")
    print(NEXT_STEPS)
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        description="Install the hop-executor consumer into a stock Grok Bot "
        "cloud host bundle (sand-host version aea062b).",
    )
    ap.add_argument("--host", default=DEFAULT_HOST, help="path to live host-main.cjs")
    ap.add_argument(
        "--version-file",
        default=None,
        help="path to the sibling 'version' file (default: 'version' next to --host)",
    )
    ap.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_DIR,
        help="directory to write timestamped backups into (and to read from for --revert)",
    )
    ap.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="exit 0 if patched with the current helper, 1 if unpatched, "
        "2 if the bundle is unrecognized, 3 if patched with the OLD helper "
        "or missing extra seams (run the tool again to migrate/extend); write nothing",
    )
    ap.add_argument("--revert", action="store_true", help="restore the newest backup this tool wrote")
    ap.add_argument(
        "--force",
        action="store_true",
        help="skip the version/md5 check (anchor counts are still asserted == 1)",
    )
    args = ap.parse_args()

    if args.check_only:
        sys.exit(cmd_check_only(args))
    if args.revert:
        sys.exit(cmd_revert(args))
    sys.exit(cmd_patch(args))


if __name__ == "__main__":
    main()
