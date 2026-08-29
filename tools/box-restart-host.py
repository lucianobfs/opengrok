#!/usr/bin/env python3
"""box-restart-host — bounce the patched sand-host process the SAFE way.

Implements procedure R1 from the reverse-engineering report
(/Users/lucianobfs/Developer/GitHub/opengrok-box-analysis-1bcef91.txt,
section 5, SUPERVISOR AND RESTART): the supervisor (sand-supervisor.mjs)
polls /tmp/sand-supervisor/command.json for a `{"kind":"restart"}` command
and, on seeing one, calls stopHost() (a plain SIGTERM, exit marked
"expected" so no crash marker is written) and relaunches the host from the
on-disk bundle on its next 5-second tick. This is the ONLY sanctioned way to
bounce the host: this tool NEVER sends a signal to any process itself.

Sequence:
  1. Read gateway.json for the loopback port (and, if present, a token — not
     required for /health but forwarded as a bearer token if the file has
     one, in case a future host build starts requiring it).
  2. Poll GET http://127.0.0.1:<port>/health until the payload reports
     isBusy: false (a restart is silently DEFERRED by the supervisor while
     the host is busy, so racing ahead of this would look like a no-op).
     Record the pid reported at that point (the "old" pid).
  3. Write /tmp/sand-supervisor/command.json ATOMICALLY (a .part file, then
     os.replace) with {"id": "opengrok-restart-<utc ms>", "kind": "restart",
     "issuedAtMs": <now>}. The id must be unique — the supervisor checks
     acks/<sanitized id> and silently ignores a reused id.
  4. Wait until status.json reports hostRunning: true AND /health reports a
     NEW pid (different from the recorded old pid), then print the result.

Every path (gateway.json, the supervisor directory) is overridable by a flag
or environment variable so this can be driven against a local mock in tests.

USAGE:
    python3 box-restart-host.py
    python3 box-restart-host.py --gateway /home/box/sand-data/gateway.json \\
        --supervisor-dir /tmp/sand-supervisor --busy-timeout 60 --restart-timeout 120

EXIT CODES:
  0   success: host restarted, new pid observed
  1   generic failure (gateway.json missing/unparseable, /health unreachable)
  2   timed out waiting for the host to go idle (isBusy stayed true)
  3   timed out waiting for the restart to complete (no new pid observed)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_GATEWAY = os.environ.get("SAND_GATEWAY_PATH", "/home/box/sand-data/gateway.json")
DEFAULT_SUPERVISOR_DIR = os.environ.get("SAND_SUPERVISOR_DIR", "/tmp/sand-supervisor")

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_BUSY_TIMEOUT = 2
EXIT_RESTART_TIMEOUT = 3


def log(msg):
    print(msg, file=sys.stderr)


def die(msg, code=EXIT_GENERIC):
    log(f"ERROR: {msg}")
    sys.exit(code)


def read_gateway(path):
    """Read {port, token?} from gateway.json. Accepts a couple of plausible
    key spellings since the exact on-box schema is not fully documented in
    the report; port is required, token is optional.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except OSError as e:
        die(f"cannot read gateway file {path}: {e}")
    except json.JSONDecodeError as e:
        die(f"cannot parse gateway file {path}: {e}")
    port = doc.get("port", doc.get("gatewayPort"))
    if port is None:
        die(f"gateway file {path} has no 'port' field")
    token = doc.get("token", doc.get("gatewayToken", doc.get("authToken")))
    return int(port), token


def http_get_json(url, token=None, timeout=5.0):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def wait_for_idle(health_url, token, timeout, poll_interval):
    deadline = time.monotonic() + timeout
    last_err = None
    last_payload = None
    while time.monotonic() < deadline:
        try:
            payload = http_get_json(health_url, token=token)
            last_payload = payload
            if not payload.get("isBusy"):
                return payload
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
        time.sleep(poll_interval)
    if last_payload is not None:
        die(
            f"timed out after {timeout}s waiting for isBusy=false "
            f"(last payload: {last_payload})",
            EXIT_BUSY_TIMEOUT,
        )
    die(f"timed out after {timeout}s waiting for {health_url} ({last_err})", EXIT_BUSY_TIMEOUT)


def write_command_atomic(supervisor_dir):
    os.makedirs(supervisor_dir, exist_ok=True)
    issued_at_ms = int(time.time() * 1000)
    command = {
        "id": f"opengrok-restart-{issued_at_ms}",
        "kind": "restart",
        "issuedAtMs": issued_at_ms,
    }
    command_path = os.path.join(supervisor_dir, "command.json")
    part_path = command_path + ".part"
    with open(part_path, "w", encoding="utf-8") as f:
        json.dump(command, f)
    os.replace(part_path, command_path)
    return command


def wait_for_restart(status_path, health_url, token, old_pid, timeout, poll_interval):
    deadline = time.monotonic() + timeout
    last_status = None
    last_pid = old_pid
    while time.monotonic() < deadline:
        host_running = False
        try:
            with open(status_path, encoding="utf-8") as f:
                last_status = json.load(f)
            host_running = bool(last_status.get("hostRunning"))
        except (OSError, ValueError):
            pass
        new_pid = None
        try:
            payload = http_get_json(health_url, token=token)
            new_pid = payload.get("pid")
            last_pid = new_pid
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if host_running and new_pid is not None and new_pid != old_pid:
            return new_pid
        time.sleep(poll_interval)
    die(
        f"timed out after {timeout}s waiting for the host to come back "
        f"(last status: {last_status}, last pid seen: {last_pid}, old pid: {old_pid})",
        EXIT_RESTART_TIMEOUT,
    )


def main():
    ap = argparse.ArgumentParser(
        description="Bounce the patched sand-host process via the supervisor "
        "R1 restart command (command.json) — never a direct signal."
    )
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY, help="path to gateway.json")
    ap.add_argument(
        "--supervisor-dir",
        default=DEFAULT_SUPERVISOR_DIR,
        help="directory holding command.json / status.json / acks/",
    )
    ap.add_argument(
        "--busy-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for /health isBusy to go false before giving up",
    )
    ap.add_argument(
        "--restart-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for a new host pid after issuing the restart command",
    )
    ap.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds between polls of /health and status.json",
    )
    args = ap.parse_args()

    port, token = read_gateway(args.gateway)
    health_url = f"http://127.0.0.1:{port}/health"
    status_path = os.path.join(args.supervisor_dir, "status.json")

    print(f"waiting for host to go idle ({health_url}) ...")
    idle_payload = wait_for_idle(health_url, token, args.busy_timeout, args.poll_interval)
    old_pid = idle_payload.get("pid")
    print(f"  idle: pid={old_pid}")

    start = time.monotonic()
    command = write_command_atomic(args.supervisor_dir)
    print(f"issued restart command: {command['id']}")

    new_pid = wait_for_restart(
        status_path, health_url, token, old_pid, args.restart_timeout, args.poll_interval
    )
    elapsed = time.monotonic() - start
    print(f"restart complete: old pid={old_pid}, new pid={new_pid}, elapsed={elapsed:.1f}s")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
