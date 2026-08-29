#!/usr/bin/env python3
"""Offline unit tests for tools/box-restart-host.py.

Run: python3 tools/test-box-restart-host.py

No real network: a local http.server on 127.0.0.1 (ephemeral port) plays the
role of the gateway's /health endpoint. gateway.json, status.json and
command.json all live under a per-test tempdir, driven through the real
script's --gateway/--supervisor-dir flags (never imported/mocked — the
script is exercised exactly as it runs on the box, just pointed at fakes).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "box-restart-host.py"


class FakeHealthState:
    """Shared, lock-protected state for the fake /health endpoint."""

    def __init__(self, pid):
        self.lock = threading.Lock()
        self.pid = pid
        self.is_busy = False

    def payload(self):
        with self.lock:
            return {"ok": True, "pid": self.pid, "isBusy": self.is_busy}

    def set_busy(self, value):
        with self.lock:
            self.is_busy = value

    def bounce(self, new_pid, delay=0.0):
        def _do():
            if delay:
                time.sleep(delay)
            with self.lock:
                self.pid = new_pid
        threading.Thread(target=_do, daemon=True).start()


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            if self.path == "/health":
                body = json.dumps(state.payload()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def run_tool(*args):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r


class BoxRestartHostTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="box-restart-host-test-")
        self.supervisor_dir = os.path.join(self.tmpdir, "sand-supervisor")
        os.makedirs(self.supervisor_dir, exist_ok=True)
        self.gateway_path = os.path.join(self.tmpdir, "gateway.json")
        self.status_path = os.path.join(self.supervisor_dir, "status.json")

        self.state = FakeHealthState(pid=1111)
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        with open(self.gateway_path, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": "fake-test-token"}, f)
        self._write_status(host_running=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_status(self, host_running):
        with open(self.status_path, "w", encoding="utf-8") as f:
            json.dump({"hostBundlePresent": True, "hostRunning": host_running}, f)

    def _common_args(self):
        return ["--gateway", self.gateway_path, "--supervisor-dir", self.supervisor_dir,
                "--poll-interval", "0.05"]

    def test_happy_path_busy_then_idle_then_restart(self):
        # host reports busy for a short window, then goes idle
        self.state.set_busy(True)

        def _go_idle():
            time.sleep(0.15)
            self.state.set_busy(False)

        threading.Thread(target=_go_idle, daemon=True).start()

        # once the restart command lands, flip the pid after a short delay to
        # simulate the supervisor bouncing the host on its next tick.
        def _watch_and_bounce():
            command_path = os.path.join(self.supervisor_dir, "command.json")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if os.path.exists(command_path):
                    self.state.bounce(new_pid=2222, delay=0.1)
                    self._write_status(host_running=True)
                    return
                time.sleep(0.02)

        threading.Thread(target=_watch_and_bounce, daemon=True).start()

        r = run_tool(*self._common_args(), "--busy-timeout", "5", "--restart-timeout", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("old pid=1111, new pid=2222", r.stdout)

        command = json.load(open(os.path.join(self.supervisor_dir, "command.json"), encoding="utf-8"))
        self.assertEqual(command["kind"], "restart")
        self.assertTrue(command["id"].startswith("opengrok-restart-"))
        self.assertIn("issuedAtMs", command)

    def test_busy_timeout_gives_up_and_writes_no_command(self):
        self.state.set_busy(True)
        r = run_tool(*self._common_args(), "--busy-timeout", "0.2", "--restart-timeout", "1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.supervisor_dir, "command.json")))

    def test_restart_timeout_when_pid_never_changes(self):
        # supervisor never bounces the host: pid stays the same forever
        r = run_tool(*self._common_args(), "--busy-timeout", "5", "--restart-timeout", "0.3")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        # the command must still have been written (we got past the idle wait)
        self.assertTrue(os.path.exists(os.path.join(self.supervisor_dir, "command.json")))

    def test_command_json_written_atomically_with_fresh_id(self):
        # drive two restarts back to back and confirm both ids differ and no
        # stray .part file is left behind.
        def _bounce_once(target_pid):
            command_path = os.path.join(self.supervisor_dir, "command.json")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if os.path.exists(command_path):
                    self.state.bounce(new_pid=target_pid, delay=0.05)
                    self._write_status(host_running=True)
                    return
                time.sleep(0.02)

        threading.Thread(target=_bounce_once, args=(3333,), daemon=True).start()
        r1 = run_tool(*self._common_args(), "--busy-timeout", "5", "--restart-timeout", "5")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        command1 = json.load(open(os.path.join(self.supervisor_dir, "command.json"), encoding="utf-8"))

        threading.Thread(target=_bounce_once, args=(4444,), daemon=True).start()
        r2 = run_tool(*self._common_args(), "--busy-timeout", "5", "--restart-timeout", "5")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        command2 = json.load(open(os.path.join(self.supervisor_dir, "command.json"), encoding="utf-8"))

        self.assertNotEqual(command1["id"], command2["id"])
        self.assertFalse(os.path.exists(os.path.join(self.supervisor_dir, "command.json.part")))


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
