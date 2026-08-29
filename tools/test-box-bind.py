#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Offline unit tests for tools/box-bind.py.

Run: python3 tools/test-box-bind.py

No network. Drives the real script as a subprocess against temp-dir fixtures
so every path (bindings file, active-agent file) is overridden by flags.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "box-bind.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )


class TestBoxBind(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bindings = self.tmp / "model-bindings.json"
        self.active_agent = self.tmp / "active-agent.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_from_missing(self):
        self.assertFalse(self.bindings.exists())
        r = run(
            "--agent", "11111111-1111-4111-8111-111111111111",
            "--model", "gpt-5.6-sol-high",
            "--hop", "http://127.0.0.1:18777/v1",
            "--bindings", str(self.bindings),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.bindings.exists())
        data = json.loads(self.bindings.read_text())
        entry = data["agents"]["11111111-1111-4111-8111-111111111111"]
        self.assertEqual(entry["modelId"], "gpt-5.6-sol-high")
        self.assertEqual(entry["hopBaseUrl"], "http://127.0.0.1:18777/v1")
        self.assertNotIn("maxMode", entry)

    def test_upsert_preserves_siblings(self):
        seed = {
            "agents": {
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {
                    "name": "Keep Me",
                    "modelId": "old-model",
                    "hopBaseUrl": "http://127.0.0.1:18790/v1",
                    "parameters": [{"id": "effort", "value": "high"}],
                },
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": {
                    "modelId": "other-model",
                    "hopBaseUrl": "http://127.0.0.1:18791/v1",
                },
            }
        }
        self.bindings.write_text(json.dumps(seed))
        r = run(
            "--agent", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "--model", "new-model",
            "--hop", "http://127.0.0.1:18777/v1",
            "--max-mode",
            "--bindings", str(self.bindings),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(self.bindings.read_text())
        updated = data["agents"]["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
        # unknown/untouched keys survive
        self.assertEqual(updated["name"], "Keep Me")
        self.assertEqual(updated["parameters"], [{"id": "effort", "value": "high"}])
        # touched keys updated
        self.assertEqual(updated["modelId"], "new-model")
        self.assertEqual(updated["hopBaseUrl"], "http://127.0.0.1:18777/v1")
        self.assertIs(updated["maxMode"], True)
        # sibling entry untouched
        sibling = data["agents"]["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]
        self.assertEqual(sibling["modelId"], "other-model")

    def test_active_agent_resolution(self):
        self.active_agent.write_text(json.dumps({"activeAgentId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}))
        r = run(
            "--agent", "active",
            "--model", "glm-5.3-flash",
            "--hop", "http://127.0.0.1:18792/v1",
            "--bindings", str(self.bindings),
            "--active-agent", str(self.active_agent),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(self.bindings.read_text())
        self.assertIn("cccccccc-cccc-4ccc-8ccc-cccccccccccc", data["agents"])

    def test_non_loopback_hop_rejected(self):
        r = run(
            "--agent", "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "--model", "some-model",
            "--hop", "http://box.example.com:18777/v1",
            "--bindings", str(self.bindings),
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("loopback", r.stderr)
        self.assertFalse(self.bindings.exists())


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stdout)
    result = runner.run(suite)
    total = result.testsRun
    failed_count = len(result.failures) + len(result.errors)
    passed_count = total - failed_count
    print("%d/%d pass, %d fail" % (passed_count, total, failed_count))
    sys.exit(1 if failed_count else 0)
