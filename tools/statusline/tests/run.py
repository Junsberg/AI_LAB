"""Smoke tests for the statusline: run each fixture through build() and check widths."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import statusline as sl  # noqa: E402

now = datetime.now(timezone.utc)
base = {
    "model": {"display_name": "Opus"},
    "effort": {"level": "high"},
    "workspace": {"current_dir": os.getcwd()},
    "cost": {"total_duration_ms": 527_000},
    "context_window": {
        "used_percentage": 47.2,
        "context_window_size": 200_000,
        "current_usage": {"input_tokens": 60_000, "cache_creation_input_tokens": 4_400, "cache_read_input_tokens": 30_000, "output_tokens": 9_000},
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 32, "resets_at": (now + timedelta(hours=1, minutes=1)).isoformat()},
        "seven_day": {"used_percentage": 18, "resets_at": (now + timedelta(days=2, hours=7)).isoformat()},
    },
}


def case(name, mutate, columns=120):
    import copy

    d = copy.deepcopy(base)
    mutate(d)
    lines = sl.build(d, columns)
    print(f"--- {name} (COLUMNS={columns})")
    for ln in lines:
        print(ln)
        w = sl.vis_width(ln)
        assert w <= columns - 1, f"{name}: line too wide {w} > {columns - 1}"
    return lines


case("47%", lambda d: None)
case("0%", lambda d: (d["context_window"].__setitem__("used_percentage", 0), d["rate_limits"]["five_hour"].__setitem__("used_percentage", 0)))
case("90%", lambda d: (d["context_window"].__setitem__("used_percentage", 90), d["rate_limits"]["seven_day"].__setitem__("used_percentage", 91)))
case("100%", lambda d: d["rate_limits"]["five_hour"].__setitem__("used_percentage", 100))
case("missing limits", lambda d: d.__setitem__("rate_limits", {}))
case("null five_hour pct", lambda d: d["rate_limits"]["five_hour"].__setitem__("used_percentage", None))
case("expired reset", lambda d: d["rate_limits"]["five_hour"].__setitem__("resets_at", (now - timedelta(minutes=5)).isoformat()))
case("no model/effort", lambda d: (d.pop("model"), d.pop("effort")))
case("long branch narrow", lambda d: None, columns=80)
case("very narrow", lambda d: None, columns=60)
print("ALL OK")
