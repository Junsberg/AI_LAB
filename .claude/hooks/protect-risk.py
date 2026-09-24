#!/usr/bin/env python3
"""PreToolUse hook: block automated edits to the frozen `risk:` block in params.yaml
and to any secret/key file. Exit 2 = deny with message.
"""
import json
import re
import sys

payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
inp = payload.get("tool_input", {}) or {}
path = str(inp.get("file_path", "")).replace("\\", "/")

SECRET = re.compile(r"(\.env$|keypair.*\.json$|\.key$)")
if tool in {"Edit", "Write", "MultiEdit"} and SECRET.search(path):
    print(f"blocked: {path} is a secret file; edit it by hand.", file=sys.stderr)
    sys.exit(2)

if tool in {"Edit", "Write", "MultiEdit"} and path.endswith("strategy/params.yaml"):
    new = inp.get("new_string") or inp.get("content") or ""
    old = inp.get("old_string") or ""
    for e in inp.get("edits") or []:  # MultiEdit carries its changes here
        new += "\n" + str(e.get("new_string") or "")
        old += "\n" + str(e.get("old_string") or "")
    if re.search(r"^\s*risk\s*:", new, re.M) or re.search(r"^\s*risk\s*:", old, re.M) or any(
        k in (new + old) for k in ("max_position_sol", "max_open_positions", "max_daily_loss_sol", "hard_stop_pct")
    ):
        print("blocked: `risk:` block in params.yaml is frozen; owner edits by hand.", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
