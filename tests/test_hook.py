import json
import subprocess
import sys


def run_hook(payload: dict) -> int:
    return subprocess.run([sys.executable, ".claude/hooks/protect-risk.py"], input=json.dumps(payload), text=True, capture_output=True).returncode


def test_windows_path_is_protected():
    assert run_hook({"tool_name": "Edit", "tool_input": {"file_path": "C:\\Users\\me\\AI_LAB\\strategy\\params.yaml", "old_string": "max_position_sol: 0.5", "new_string": "max_position_sol: 5"}}) == 2


def test_multiedit_edits_array_is_inspected():
    assert run_hook({"tool_name": "MultiEdit", "tool_input": {"file_path": "strategy/params.yaml", "edits": [{"old_string": "max_daily_loss_sol: 2.0", "new_string": "max_daily_loss_sol: 20"}]}}) == 2


def test_non_risk_edit_allowed():
    assert run_hook({"tool_name": "Edit", "tool_input": {"file_path": "strategy/params.yaml", "old_string": "trailing_stop_pct: 35", "new_string": "trailing_stop_pct: 30"}}) == 0


def test_secret_file_blocked():
    assert run_hook({"tool_name": "Write", "tool_input": {"file_path": "/x/.env", "content": "KEY=1"}}) == 2
