#!/usr/bin/env python3
"""Offline unit tests for tools/apply-box-patch.py.

Run: python3 tools/test-apply-box-patch.py

No network. Builds a small SYNTHETIC bundle fixture (in a tempdir) that
contains the real, verbatim anchor strings the tool looks for, and drives the
real script as a subprocess (never imported — it is a __main__ script) so the
test exercises the exact CLI contract. If /tmp/gb/host-main.cjs (the real,
byte-identical stock aea062b bundle copy) exists on this machine, an extra
block asserts --dry-run against the REAL bundle reports CHANGED and both
anchor counts are exactly 1; that block is skipped (with a printed SKIP line,
never silently) when the file is absent.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "apply-box-patch.py"
REAL_BUNDLE = Path("/tmp/gb/host-main.cjs")

EXPECTED_VERSION = "aea062b"
MARKER_AFTER_PATCH = 7  # 6 seam call sites + the helper's own name

SEAM = "const baseExecutor = session.getExecutor();"
HELPER_ANCHOR = (
    "function fromRedactedCoreMessages(messages2, purpose, opts) {\n"
    "  return messages2.map((msg) => fromRedactedCoreMessage(msg, purpose, opts));\n"
    "}"
)
MARKER = "__opengrokHopExecutor"
REPLACEMENT_SEAM = (
    "const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();"
)

ANCHOR_EPISODE = (
    "      const narrative = await summarizeEpisode({\n"
    "        executor: session.getExecutor(),"
)
REPLACEMENT_EPISODE = (
    "      const narrative = await summarizeEpisode({\n"
    "        executor: __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) }, session) ?? session.getExecutor(),"
)
ANCHOR_EXTRACT = (
    "    const extraction = await extractMemories({\n"
    "      executor: session.getExecutor(),"
)
REPLACEMENT_EXTRACT = (
    "    const extraction = await extractMemories({\n"
    "      executor: __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) }, session) ?? session.getExecutor(),"
)
ANCHOR_SELF_SUMMARY = "        const executor = summarizationPromptSession.getExecutor();"
REPLACEMENT_SELF_SUMMARY = (
    "        const executor = __opengrokHopExecutor({ getConversationId: () => ctx.get(conversationIdKey2) ?? ctx.get(conversationIdKey) }, summarizationPromptSession) ?? summarizationPromptSession.getExecutor();"
)
ANCHOR_SYNTHESIS = '            executor: this.options.createExecutor("synthesis"),'
REPLACEMENT_SYNTHESIS = (
    '            executor: __opengrokHopExecutor(agentId, null) ?? this.options.createExecutor("synthesis"),'
)
ANCHOR_VERIFICATION = '            executor: this.options.createExecutor("verification"),'
REPLACEMENT_VERIFICATION = (
    '            executor: __opengrokHopExecutor(agentId, null) ?? this.options.createExecutor("verification"),'
)

ALL_REPLACEMENTS = (
    REPLACEMENT_SEAM,
    REPLACEMENT_EPISODE,
    REPLACEMENT_EXTRACT,
    REPLACEMENT_SELF_SUMMARY,
    REPLACEMENT_SYNTHESIS,
    REPLACEMENT_VERIFICATION,
)

# The helper block the CURRENT tool injects. Duplicated here on purpose, in the
# same style as SEAM/HELPER_ANCHOR/MARKER: the script is a __main__ CLI and is
# never imported, so the test must state the contract independently.
HELPER_CODE = (
    "\n"
    "function __opengrokHopExecutor(host, session) {\n"
    "  try {\n"
    "    const m = require('/home/box/sand-data/hop-executor.cjs');\n"
    "    return m.createHopExecutor(host, session, {});\n"
    "  } catch (e) {\n"
    "    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');\n"
    "    return null;\n"
    "  }\n"
    "}\n"
)

# The helper block the PREVIOUS generation of the tool injected, and that the
# live box carries right now. Copied byte-for-byte from
# `git show HEAD~:tools/apply-box-patch.py`. It differs from HELPER_CODE in one
# line only: the deps bag passed to createHopExecutor.
OLD_HELPER_CODE = (
    "\n"
    "function __opengrokHopExecutor(host, session) {\n"
    "  try {\n"
    "    const m = require('/home/box/sand-data/hop-executor.cjs');\n"
    "    return m.createHopExecutor(host, session, { fromRedactedCoreMessages, PrivacyCapability });\n"
    "  } catch (e) {\n"
    "    process.stderr.write('[opengrok] hop executor disabled: ' + (e && e.message) + '\\n');\n"
    "    return null;\n"
    "  }\n"
    "}\n"
)


def extra_seam_block(copies=1):
    """The extra unique getExecutor sites (memory + self-summary + dreaming)."""
    chunks = []
    for _ in range(copies):
        chunks.append(
            "async function runTurnMemory(memoryStore, episodeProgress, session, ctx, turnTs, exchange) {\n"
            + ANCHOR_EPISODE
            + "\n        ctx,\n        turns: pending\n      });\n"
            + ANCHOR_EXTRACT
            + "\n      ctx,\n      userMessage: exchange.user\n    });\n"
            "}\n"
            "async function executeSelfSummaryWithRetry(parentCtx, summarizationPromptSession, stateHandler, interactionListener, summarizationInputMessages, tools, extraT, options2) {\n"
            + ANCHOR_SELF_SUMMARY
            + "\n}\n"
            "async function runAgent(agentId) {\n"
            "  const synthesisText = await streamText({\n"
            + ANCHOR_SYNTHESIS
            + "\n  });\n"
            "  const verificationText = await streamText({\n"
            + ANCHOR_VERIFICATION
            + "\n  });\n"
            "}\n"
        )
    return "".join(chunks)


def fixture_source(seam_copies=1, helper_copies=1, extra_copies=1):
    """Build a small, syntactically valid .cjs fixture containing the real
    anchor strings the given number of times each.
    """
    parts = [
        "// ../packages/fake/redact.js\n",
        "function toRedactedCoreMessages(messages2, modeOrContext) {\n"
        "  return messages2.map((msg) => toRedactedCoreMessage(msg, modeOrContext));\n"
        "}\n",
    ]
    for _ in range(helper_copies):
        parts.append(HELPER_ANCHOR + "\n")
    parts.append(
        "var PrivacyCapability;\n"
        "(function(PrivacyCapability2) {\n"
        '  PrivacyCapability2["UNSAFE_ALWAYS_ALLOWED"] = "unsafe_always_allowed";\n'
        "})(PrivacyCapability || (PrivacyCapability = {}));\n"
        "var conversationIdKey = { id: 'conversationId' };\n"
        "var conversationIdKey2 = { id: 'conversationId2' };\n"
    )
    parts.append(
        "function runTurn(host, session) {\n"
        "  const other = { getExecutor() { return null; } };\n"
    )
    for i in range(seam_copies):
        # keep the exact anchor text unique-ish per copy only in surrounding
        # context, never in the anchor text itself.
        parts.append(f"  // copy {i}\n  {SEAM}\n")
    parts.append(
        "  return baseExecutor;\n"
        "}\n"
    )
    if extra_copies:
        parts.append(extra_seam_block(extra_copies))
    parts.append("module.exports = { runTurn };\n")
    return "".join(parts)


def old_patched_fixture_source():
    """Reproduce exactly what the PREVIOUS generation of the tool wrote: the
    seam replacement plus the OLD helper injected after the anchor. This is the
    state the live box is in.
    """
    src = fixture_source()
    assert src.count(SEAM) == 1
    src = src.replace(SEAM, REPLACEMENT_SEAM, 1)
    assert src.count(HELPER_ANCHOR) == 1
    src = src.replace(HELPER_ANCHOR, HELPER_ANCHOR + OLD_HELPER_CODE, 1)
    return src


def run_tool(*args, cwd=None):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return r


class ApplyBoxPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="apply-box-patch-test-")
        self.host = os.path.join(self.tmpdir, "host-main.cjs")
        self.version_file = os.path.join(self.tmpdir, "version")
        self.backup_dir = os.path.join(self.tmpdir, "host-backups")
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source())
        with open(self.version_file, "w", encoding="utf-8") as f:
            f.write(EXPECTED_VERSION)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _common_args(self):
        return ["--host", self.host, "--backup-dir", self.backup_dir]

    def test_check_only_reports_unpatched_before_patching(self):
        r = run_tool("--check-only", *self._common_args())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("UNPATCHED", r.stdout)

    def test_dry_run_writes_nothing_and_reports_changed(self):
        original = open(self.host, encoding="utf-8").read()
        r = run_tool("--dry-run", *self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CHANGED", r.stdout)
        self.assertIn("seam main-turn count: 1", r.stdout)
        self.assertIn("seam summarize-episode count: 1", r.stdout)
        self.assertIn("helper anchor count: 1", r.stdout)
        self.assertEqual(open(self.host, encoding="utf-8").read(), original)
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_patch_then_idempotent_then_check_only_patched(self):
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        patched = open(self.host, encoding="utf-8").read()
        self.assertIn(MARKER, patched)
        # the marker appears once per seam call site plus the helper name.
        self.assertEqual(patched.count(MARKER), MARKER_AFTER_PATCH)
        for replacement in ALL_REPLACEMENTS:
            self.assertIn(replacement, patched)
        self.assertIn(
            "const baseExecutor = __opengrokHopExecutor(host, session) ?? session.getExecutor();",
            patched,
        )
        # a backup must have been written
        backups = os.listdir(self.backup_dir)
        self.assertEqual(len(backups), 1)

        # second run: no-op, no new backup, file byte-identical
        r2 = run_tool(*self._common_args())
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("already patched", r2.stdout)
        self.assertEqual(open(self.host, encoding="utf-8").read(), patched)
        self.assertEqual(len(os.listdir(self.backup_dir)), 1)

        r3 = run_tool("--check-only", *self._common_args())
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        self.assertIn("PATCHED", r3.stdout)

    def test_revert_restores_byte_identical_original(self):
        original = open(self.host, "rb").read()
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(open(self.host, "rb").read(), original)

        r2 = run_tool("--revert", *self._common_args())
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(open(self.host, "rb").read(), original)

    def test_revert_with_no_backup_fails(self):
        r = run_tool("--revert", *self._common_args())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_refuses_when_seam_anchor_missing(self):
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(seam_copies=0))
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn(MARKER, open(self.host, encoding="utf-8").read())
        self.assertFalse(os.path.isdir(self.backup_dir))

    def test_refuses_when_seam_anchor_duplicated(self):
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(seam_copies=2))
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn(MARKER, open(self.host, encoding="utf-8").read())

    def test_refuses_when_extra_seam_missing(self):
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(extra_copies=0))
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn(MARKER, open(self.host, encoding="utf-8").read())

    def test_refuses_when_extra_seam_duplicated(self):
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(extra_copies=2))
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn(MARKER, open(self.host, encoding="utf-8").read())

    def test_refuses_when_helper_anchor_missing(self):
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(helper_copies=0))
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_refuses_unknown_bundle_without_force(self):
        os.remove(self.version_file)
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source() + "\n// not the real bundle, no md5 match\n")
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn(MARKER, open(self.host, encoding="utf-8").read())

    def test_check_only_unknown_bundle(self):
        os.remove(self.version_file)
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source() + "\n// not the real bundle, no md5 match\n")
        r = run_tool("--check-only", *self._common_args())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("UNKNOWN BUNDLE", r.stdout)

    def test_force_skips_version_check_but_still_asserts_anchors(self):
        os.remove(self.version_file)
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source() + "\n// not the real bundle, no md5 match\n")
        r = run_tool("--force", *self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(MARKER, open(self.host, encoding="utf-8").read())

        # --force still refuses a half-patch: seam duplicated
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(fixture_source(seam_copies=2))
        r2 = run_tool("--force", *self._common_args())
        self.assertEqual(r2.returncode, 3, r2.stdout + r2.stderr)


class MigrateOldHelperTests(unittest.TestCase):
    """The live box carries the OLD helper (the one whose unwrap step crashed a
    real turn with 'message.content.unwrap is not a function'). The tool must
    recognise that state and migrate it in place.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="apply-box-patch-migrate-")
        self.host = os.path.join(self.tmpdir, "host-main.cjs")
        self.version_file = os.path.join(self.tmpdir, "version")
        self.backup_dir = os.path.join(self.tmpdir, "host-backups")
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(old_patched_fixture_source())
        with open(self.version_file, "w", encoding="utf-8") as f:
            f.write(EXPECTED_VERSION)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _common_args(self):
        return ["--host", self.host, "--backup-dir", self.backup_dir]

    def test_check_only_reports_stale_distinctly_with_exit_3(self):
        r = run_tool("--check-only", *self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("PATCHED-STALE:", r.stdout)
        # never confusable with the plain patched line
        self.assertFalse(r.stdout.startswith("PATCHED:"))

    def test_dry_run_reports_migrate_intent_and_writes_nothing(self):
        before = open(self.host, "rb").read()
        r = run_tool("--dry-run", *self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would MIGRATE", r.stdout)
        self.assertNotIn("would patch", r.stdout)
        self.assertEqual(open(self.host, "rb").read(), before)
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_patch_migrates_old_helper_in_place(self):
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        migrated = open(self.host, encoding="utf-8").read()

        self.assertIn(HELPER_CODE, migrated)
        self.assertNotIn(OLD_HELPER_CODE, migrated)
        self.assertNotIn("fromRedactedCoreMessages, PrivacyCapability", migrated)
        # the seam replacement is identical in both generations: left alone
        self.assertEqual(migrated.count(REPLACEMENT_SEAM), 1)
        self.assertEqual(migrated.count(SEAM), 0)
        self.assertEqual(migrated.count(MARKER), MARKER_AFTER_PATCH)
        for replacement in ALL_REPLACEMENTS:
            self.assertIn(replacement, migrated)

        backups = os.listdir(self.backup_dir)
        self.assertEqual(len(backups), 1)
        backup = open(os.path.join(self.backup_dir, backups[0]), encoding="utf-8").read()
        self.assertIn(OLD_HELPER_CODE, backup)

        check = subprocess.run(["node", "--check", self.host], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

        r2 = run_tool("--check-only", *self._common_args())
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("PATCHED:", r2.stdout)

    def test_migration_is_idempotent(self):
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        migrated = open(self.host, "rb").read()

        r2 = run_tool(*self._common_args())
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("already patched", r2.stdout)
        self.assertEqual(open(self.host, "rb").read(), migrated)
        self.assertEqual(len(os.listdir(self.backup_dir)), 1)

    def test_foreign_patch_refuses_and_changes_nothing(self):
        foreign = fixture_source() + (
            "\nfunction __opengrokHopExecutor(host, session) { return null; }\n"
        )
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(foreign)
        for argv in ([], ["--check-only"], ["--dry-run"]):
            r = run_tool(*argv, *self._common_args())
            self.assertEqual(r.returncode, 1, f"{argv}: {r.stdout}{r.stderr}")
            self.assertIn("foreign or hand-edited", r.stderr)
            self.assertEqual(open(self.host, encoding="utf-8").read(), foreign)
            self.assertFalse(os.path.exists(self.backup_dir))


class ExtendPartialTests(unittest.TestCase):
    """A bundle that already has the current helper + main-turn seam but not
    the extra memory/self-summary/dreaming seams must be extended in place.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="apply-box-patch-partial-")
        self.host = os.path.join(self.tmpdir, "host-main.cjs")
        self.version_file = os.path.join(self.tmpdir, "version")
        self.backup_dir = os.path.join(self.tmpdir, "host-backups")
        src = fixture_source()
        src = src.replace(SEAM, REPLACEMENT_SEAM, 1)
        src = src.replace(HELPER_ANCHOR, HELPER_ANCHOR + HELPER_CODE, 1)
        with open(self.host, "w", encoding="utf-8") as f:
            f.write(src)
        with open(self.version_file, "w", encoding="utf-8") as f:
            f.write(EXPECTED_VERSION)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _common_args(self):
        return ["--host", self.host, "--backup-dir", self.backup_dir]

    def test_check_only_reports_partial_with_exit_3(self):
        r = run_tool("--check-only", *self._common_args())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("PATCHED-PARTIAL:", r.stdout)
        self.assertFalse(r.stdout.startswith("PATCHED:"))

    def test_dry_run_reports_extend_intent_and_writes_nothing(self):
        before = open(self.host, "rb").read()
        r = run_tool("--dry-run", *self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would EXTEND", r.stdout)
        self.assertIn("summarize-episode", r.stdout)
        self.assertNotIn("would patch", r.stdout)
        self.assertEqual(open(self.host, "rb").read(), before)

    def test_patch_extends_extra_seams_in_place(self):
        r = run_tool(*self._common_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        extended = open(self.host, encoding="utf-8").read()
        self.assertIn(HELPER_CODE, extended)
        for replacement in ALL_REPLACEMENTS:
            self.assertIn(replacement, extended)
        self.assertEqual(extended.count(MARKER), MARKER_AFTER_PATCH)
        self.assertEqual(extended.count(ANCHOR_EPISODE), 0)
        backups = os.listdir(self.backup_dir)
        self.assertEqual(len(backups), 1)
        r2 = run_tool("--check-only", *self._common_args())
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("PATCHED:", r2.stdout)
        self.assertFalse(r2.stdout.startswith("PATCHED-PARTIAL"))


class RealBundleTests(unittest.TestCase):
    """Optional block against a real stock host-main.cjs, if present on
    this machine. Skipped (with a printed line) when the file is absent —
    never silently passed.
    """

    def test_dry_run_against_real_bundle_reports_changed_and_unique_anchors(self):
        if not REAL_BUNDLE.exists():
            print(f"SKIP: {REAL_BUNDLE} not present on this machine")
            self.skipTest(f"{REAL_BUNDLE} not present")
        r = run_tool("--host", str(REAL_BUNDLE), "--dry-run", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CHANGED", r.stdout)
        self.assertIn("seam main-turn count: 1", r.stdout)
        self.assertIn("seam summarize-episode count: 1", r.stdout)
        self.assertIn("helper anchor count: 1", r.stdout)
        # must never write to the real file
        with open(REAL_BUNDLE, "rb") as f:
            head = f.read(64)
        self.assertNotIn(b"__opengrokHopExecutor", head)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(ApplyBoxPatchTests))
    suite.addTests(loader.loadTestsFromTestCase(MigrateOldHelperTests))
    suite.addTests(loader.loadTestsFromTestCase(ExtendPartialTests))
    suite.addTests(loader.loadTestsFromTestCase(RealBundleTests))
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stdout)
    result = runner.run(suite)
    total = result.testsRun
    failed_count = len(result.failures) + len(result.errors)
    skipped_count = len(result.skipped)
    passed_count = total - failed_count - skipped_count
    print("%d/%d pass, %d fail, %d skip" % (passed_count, total, failed_count, skipped_count))
    sys.exit(1 if failed_count else 0)
