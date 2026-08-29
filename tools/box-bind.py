#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""box-bind — upsert one entry into model-bindings.json on the box.

The bindings file format is the "agents" wrapper (see
examples/model-bindings.example.json):

    { "agents": { "<agentUuid>": { "modelId", "hopBaseUrl", ... } } }

This tool writes exactly one entry, preserving every other agent already in
the file and every key already on the entry it touches (so re-binding a
model does not clobber a hand-set `name` or `parameters`).

Usage:
    python3 tools/box-bind.py --agent <uuid|active> --model <slug> \
        --hop http://127.0.0.1:18777/v1 [--max-mode] \
        [--bindings /home/box/sand-data/model-bindings.json]

`--agent active` resolves the agent uuid from
/home/box/sand-data/agents/active-agent.json, whose content is
`{"activeAgentId": "<uuid>"}` (override the path with --active-agent or
env SAND_ACTIVE_AGENT_FILE — used by tools/test-box-bind.py).

Credentials never go in this file (secrets law, docs/MODEL-GUIDELINES.md).
hopBaseUrl must be a loopback http://127.0.0.1:<port>/... URL — anything
else is refused so a binding can never point off-box.
"""
import argparse
import json
import os
import re
import sys
import tempfile

DEFAULT_BINDINGS = "/home/box/sand-data/model-bindings.json"
DEFAULT_ACTIVE_AGENT = "/home/box/sand-data/agents/active-agent.json"

LOOPBACK_HOP = re.compile(r"^http://127\.0\.0\.1:\d+(/.*)?$")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_bindings(path):
    if not os.path.exists(path):
        return {"agents": {}}
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        die(f"{path} must contain a JSON object")
    if "agents" not in data or not isinstance(data.get("agents"), dict):
        data["agents"] = {}
    return data


def write_atomic(path, data):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".model-bindings-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def resolve_agent(agent_arg, active_agent_path):
    if agent_arg != "active":
        return agent_arg
    if not os.path.exists(active_agent_path):
        die(f"--agent active: no active-agent file at {active_agent_path}")
    with open(active_agent_path, encoding="utf-8") as f:
        try:
            doc = json.load(f)
        except json.JSONDecodeError as e:
            die(f"{active_agent_path} is not valid JSON: {e}")
    agent_id = doc.get("activeAgentId")
    if not agent_id:
        die(f"{active_agent_path} has no activeAgentId key")
    return agent_id


def main():
    ap = argparse.ArgumentParser(description="Upsert one entry into model-bindings.json")
    ap.add_argument("--agent", required=True, help="agent uuid, or 'active' to resolve the active agent")
    ap.add_argument("--model", required=True, help="modelId slug to bind")
    ap.add_argument("--hop", required=True, help="hopBaseUrl, must be http://127.0.0.1:<port>/...")
    ap.add_argument("--max-mode", action="store_true", help="set maxMode true on the entry")
    ap.add_argument("--bindings", default=os.environ.get("SAND_BINDINGS_FILE", DEFAULT_BINDINGS),
                    help="path to model-bindings.json")
    ap.add_argument("--active-agent",
                     default=os.environ.get("SAND_ACTIVE_AGENT_FILE", DEFAULT_ACTIVE_AGENT),
                     help="path to agents/active-agent.json (used with --agent active)")
    args = ap.parse_args()

    if not LOOPBACK_HOP.match(args.hop):
        die(f"--hop must be a loopback URL like http://127.0.0.1:<port>/v1, got: {args.hop!r}")

    agent_id = resolve_agent(args.agent, args.active_agent)

    data = load_bindings(args.bindings)
    entry = data["agents"].get(agent_id)
    if not isinstance(entry, dict):
        entry = {}
    entry["modelId"] = args.model
    entry["hopBaseUrl"] = args.hop
    if args.max_mode:
        entry["maxMode"] = True
    data["agents"][agent_id] = entry

    write_atomic(args.bindings, data)

    print(f"bound agent {agent_id} -> {json.dumps(entry, indent=2)}")
    print(f"written: {args.bindings}")


if __name__ == "__main__":
    main()
