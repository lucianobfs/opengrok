#!/usr/bin/env python3
"""apply-box-patch — install the hop-executor consumer into a STOCK Grok Bot
cloud host bundle (sand-host version 1bcef91).

BACKGROUND: the previous version of this tool targeted a privately modified
host bundle. Its anchors (resolvedTopLevelModelId, createOpenAiHopSession,
openaiBaseUrl, provenanceAgentId, model-bindings, openai-hop-session) occur
ZERO times in the stock 1bcef91 bundle. This rewrite targets the real, stock
bundle as reverse-engineered in
/Users/lucianobfs/Developer/GitHub/opengrok-box-analysis-1bcef91.txt.

VERIFIED FACTS (re-checked against /tmp/gb/host-main.cjs, 25,701,134 bytes,
md5 7b7ab6046aa1c343e7baefeed99ef402, during this rewrite):
  - b'const baseExecutor = session.getExecutor();'   count 1, byte 25270519
      (the patch seam: this is where the live host asks the session for the
      executor it will drive the turn with).
  - b'function fromRedactedCoreMessages'              count 1, byte 16074547
  - b'var PrivacyCapability'                          count 1, byte 16040271
  Both fromRedactedCoreMessages and PrivacyCapability are declared as plain
  top-level statements in the flat esbuild concatenation (a `function ...`
  and a `var ...` at column 0, each preceded by a `// ../packages/...` module
  banner comment) — NOT inside an __esm lazy-init wrapper. Both declarations
  sit far before byte 25270519, so a helper function textually injected right
  after the fromRedactedCoreMessages declaration has both identifiers in
  lexical scope at the seam. The full three-line function body used as the
  injection anchor (see ANCHOR_HELPER_AFTER below) also occurs exactly once
  in the stock bundle.

WHAT THIS TOOL DOES (byte-surgical, idempotent, anchor counts asserted == 1;
refuses loudly and changes nothing if an anchor is missing or duplicated):
  1. Replaces the seam
       const baseExecutor = session.getExecutor();
     with
       const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();
  2. Injects, once, immediately after the top-level fromRedactedCoreMessages
     function declaration, a helper:
       function __opengrokHopExecutor(host, session){
         try {
           const m = require('/home/box/sand-data/hop-executor.cjs');
           return m.createHopExecutor(host, session, { fromRedactedCoreMessages, PrivacyCapability });
         } catch (e) {
           process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');
           return null;
         }
       }
     __opengrokHopExecutor closes over the module-scope fromRedactedCoreMessages
     and PrivacyCapability bindings already in scope at its injection point, and
     hands them to hop-executor.cjs as explicit deps (see risk 2 in the report:
     the builder's messages are REDACTED core messages and must be unwrapped
     with fromRedactedCoreMessages(msgs, PrivacyCapability.UNSAFE_ALWAYS_ALLOWED)
     before the executor can use them).

Idempotency: a file already containing the string "__opengrokHopExecutor" is
reported "already patched" and NOTHING is written (no backup either).

Before patching (unless --force), the tool refuses unless the bundle is the
expected one: either /home/box/sand-host/version (sibling "version" file next
to --host, override with --version-file) reads "1bcef91", or the host file's
md5 equals 7b7ab6046aa1c343e7baefeed99ef402. --force skips this version check
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
  0   success: patched (or already patched / dry-run reported / revert done);
      --check-only: bundle is patched
  1   --check-only: bundle is unpatched (stock, unmodified); also the generic
      failure code (missing file, node --check failure, revert with no
      backup found, etc.)
  2   version/md5 refusal (patch mode without --force sees an unexpected
      bundle and neither --force nor a version match is present);
      --check-only: bundle version/md5 does not match anything known
  3   anchor-count assertion failed (an anchor is missing or duplicated —
      refuses to half-patch; nothing is written)

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

EXPECTED_VERSION = "1bcef91"
EXPECTED_MD5 = "7b7ab6046aa1c343e7baefeed99ef402"

ANCHOR_SEAM = b"const baseExecutor = session.getExecutor();"
REPLACEMENT_SEAM = (
    b"const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();"
)

# The full, verbatim, unique top-level declaration of fromRedactedCoreMessages.
# Used both as the injection anchor and (implicitly) to prove the identifier
# is declared at module scope, not inside an __esm lazy-init wrapper.
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


def is_patched(data):
    return HELPER_MARKER in data


def default_version_file(host_path):
    return os.path.join(os.path.dirname(os.path.abspath(host_path)), "version")


def bundle_version_ok(host_path, version_file):
    """Return True if the bundle at host_path is the expected 1bcef91 bundle,
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


def compute_patch(data):
    """Return the patched bytes, or None if data is already patched.
    Raises SystemExit (via die) if an anchor is missing or duplicated.
    """
    if is_patched(data):
        return None

    seam_count = data.count(ANCHOR_SEAM)
    helper_count = data.count(ANCHOR_HELPER_AFTER)
    if seam_count != 1:
        die(
            f"anchor 'baseExecutor seam' count={seam_count} (expected 1) — "
            "upstream bundle changed; refusing to half-patch",
            EXIT_ANCHOR_FAIL,
        )
    if helper_count != 1:
        die(
            f"anchor 'fromRedactedCoreMessages declaration' count={helper_count} "
            "(expected 1) — upstream bundle changed; refusing to half-patch",
            EXIT_ANCHOR_FAIL,
        )

    new_data = data.replace(ANCHOR_SEAM, REPLACEMENT_SEAM, 1)
    new_data = new_data.replace(
        ANCHOR_HELPER_AFTER, ANCHOR_HELPER_AFTER + HELPER_CODE, 1
    )
    return new_data


def cmd_check_only(args):
    if not os.path.exists(args.host):
        die(f"host not found: {args.host}")
    data = read_bytes(args.host)
    if is_patched(data):
        print(f"PATCHED: {args.host}")
        return EXIT_OK
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


def cmd_patch(args):
    if not os.path.exists(args.host):
        die(f"host not found: {args.host}")
    data = read_bytes(args.host)

    if is_patched(data):
        print(f"already patched: {args.host}")
        return EXIT_OK

    version_file = args.version_file or default_version_file(args.host)
    if not args.force:
        if not bundle_version_ok(args.host, version_file):
            die(
                f"{args.host} does not look like the expected {EXPECTED_VERSION} bundle "
                f"(version file {version_file!r} / md5 mismatch) — pass --force to skip "
                "the version check (anchor counts are still asserted)",
                EXIT_VERSION_REFUSAL,
            )

    new_data = compute_patch(data)
    if new_data is None:
        print(f"already patched: {args.host}")
        return EXIT_OK

    if args.dry_run:
        print("DRY RUN: would patch")
        print(f"  seam anchor count:   {data.count(ANCHOR_SEAM)}")
        print(f"  helper anchor count: {data.count(ANCHOR_HELPER_AFTER)}")
        print("  host: CHANGED")
        return EXIT_OK

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
    print(f"  [host] {args.host} patched")

    print("== syntax check (post-patch) ==")
    r = subprocess.run(["node", "--check", args.host], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR: node --check failed after patching:\n{r.stderr}")
        log(f"restoring backup from {backup_path}")
        shutil.copy2(backup_path, args.host)
        die("post-patch syntax check failed; original restored")
    print(f"  ok: node --check {args.host} (post-patch)")

    print(
        """
DONE. Next steps (see docs/CLOUD-HOST.md):
  1. Make sure tools/hop-executor.cjs is installed at
     /home/box/sand-data/hop-executor.cjs (box-bootstrap.sh does this).
  2. Bounce the host with tools/box-restart-host.py (supervisor-safe R1
     restart — NEVER kill the process directly).
  3. Send a normal message in a Bot conversation bound to a hop model and
     confirm codex-shim.log sees the request.
"""
    )
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        description="Install the hop-executor consumer into a stock Grok Bot "
        "cloud host bundle (sand-host version 1bcef91).",
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
        help="exit 0 if patched, 1 if unpatched, 2 if the bundle is unrecognized; write nothing",
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
