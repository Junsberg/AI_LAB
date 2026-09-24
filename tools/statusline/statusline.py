#!/usr/bin/env python3
"""Claude Code statusline — 3 lines, lime model badge, cyan/purple slanted gauges.

Reads the statusline JSON on stdin. No plugins, no network, no credentials.
Wire it from ~/.claude/settings.json (see README.md next to this file).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone

# ---------- colors ----------
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GRAY = "\x1b[38;5;245m"
DARK = "\x1b[38;5;238m"
BRIGHT = "\x1b[97m"
MID = "\x1b[38;5;250m"
CYAN = "\x1b[38;2;34;211;238m"      # #22D3EE
PURPLE = "\x1b[38;2;167;139;250m"   # #A78BFA
ORANGE = "\x1b[38;2;251;146;60m"
RED = "\x1b[38;2;248;113;113m"
BADGE = "\x1b[48;2;163;230;53m\x1b[38;2;17;24;39m"  # bg #A3E635, fg #111827

ASCII = os.environ.get("STATUSLINE_ASCII") == "1" or (sys.stdout.encoding or "").lower().replace("-", "") not in ("utf8",)
FULL, EMPTY, STAR, SEP, RESET_GLYPH, CHECK = ("#", "-", "*", "|", "~", "ok") if ASCII else ("▰", "▱", "✦", "│", "↻", "✓")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def vis_width(s: str) -> int:
    s = ANSI_RE.sub("", s)
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def level_color(pct: float | None) -> str:
    if pct is None:
        return BRIGHT
    if pct >= 90:
        return RED
    if pct >= 70:
        return ORANGE
    return BRIGHT


def gauge(pct: float, accent: str, cells: int = 10) -> str:
    filled = int(round(pct / 100 * cells))
    filled = max(0, min(cells, filled))
    color = accent if pct < 70 else level_color(pct)
    return f"{color}{FULL * filled}{DARK}{EMPTY * (cells - filled)}{RESET}"


def fmt_duration(ms: float | None) -> str | None:
    if ms is None:
        return None
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{sec:02d}s"


def fmt_reset(resets_at) -> str | None:
    if resets_at in (None, ""):
        return None
    try:
        if isinstance(resets_at, (int, float)):
            t = datetime.fromtimestamp(resets_at if resets_at < 1e12 else resets_at / 1000, tz=timezone.utc)
        else:
            t = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    delta = (t - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "갱신 대기"
    d, rem = divmod(int(delta), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def fmt_tokens(n: float | None) -> str | None:
    if n is None:
        return None
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def git_info(cwd: str) -> tuple[str | None, str | None]:
    """(branch, status_glyph). Glyph is None when git cannot be queried — never a false ✓."""
    if not cwd or not shutil.which("git"):
        return None, None
    try:
        b = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=1.5)
        if b.returncode != 0:
            return None, None
        branch = b.stdout.strip() or None
        st = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=1.5)
        if st.returncode != 0:
            return branch, None
        return branch, (STAR if st.stdout.strip() else CHECK)
    except Exception:
        return None, None


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def build(data: dict, columns: int) -> list[str]:
    model = (data.get("model") or {}).get("display_name")
    effort = (data.get("effort") or {}).get("level")
    cwd = (data.get("workspace") or {}).get("current_dir") or ""
    dur = fmt_duration((data.get("cost") or {}).get("total_duration_ms"))
    ctx = data.get("context_window") or {}
    ctx_pct = ctx.get("used_percentage")
    ctx_max = ctx.get("context_window_size")
    cu = ctx.get("current_usage") or {}
    ctx_now = None
    if cu:
        ctx_now = sum(float(cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    rl = data.get("rate_limits") or {}
    five, week = rl.get("five_hour") or {}, rl.get("seven_day") or {}

    narrow = columns < 100
    very_narrow = columns < 72
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else None
    branch, glyph = git_info(cwd)
    if narrow:
        project = truncate(project, 14) if project else None
        branch = truncate(branch, 16) if branch else None

    # ---- line 1
    parts = []
    if model:
        parts.append(f"{BADGE} {STAR} {BOLD}{model}{RESET}{BADGE} {RESET}" + (f"  {GRAY}{effort}{RESET}" if effort else ""))
    proj = " / ".join(x for x in (f"{BRIGHT}{project}{RESET}" if project else None, f"{MID}{branch}{RESET}" if branch else None) if x)
    if proj:
        if glyph:
            proj += f" {MID}{glyph}{RESET}"
        parts.append(proj)
    if dur and not very_narrow:
        parts.append(f"{GRAY}{dur}{RESET}")
    line1 = f"  {GRAY}{SEP}{RESET}  ".join(parts)

    # ---- line 2
    line2 = ""
    if ctx_pct is not None:
        c = level_color(float(ctx_pct))
        toks = ""
        if ctx_now is not None and ctx_max and not very_narrow:
            toks = f"{BRIGHT}{fmt_tokens(ctx_now)} / {fmt_tokens(ctx_max)}{RESET}  "
        line2 = f"{GRAY}컨텍스트{RESET}  {toks}{c}{BOLD}{float(ctx_pct):.0f}%{RESET}{GRAY} 사용{RESET}"

    # ---- line 3
    cells = 6 if very_narrow else 10

    def limit(label: str, d: dict, accent: str) -> str | None:
        pct = d.get("used_percentage")
        if pct is None:
            return None
        pct = float(pct)
        c = level_color(pct)
        out = f"{GRAY}{label}{RESET} {gauge(pct, accent, cells)} {c}{BOLD}{pct:.0f}%{RESET}{GRAY} 사용{RESET}"
        r = fmt_reset(d.get("resets_at"))
        if r and not very_narrow:
            out += f"  {GRAY}{RESET_GLYPH}{r}{RESET}"
        return out

    segs = [x for x in (limit("5시간", five, CYAN), limit("주간", week, PURPLE)) if x]
    line3 = f"  {GRAY}{SEP}{RESET}  ".join(segs)

    lines = [ln for ln in (line1, line2, line3) if ln]
    # keep one column of right margin; drop the least important trailing part if still too wide
    fixed = []
    for ln in lines:
        while vis_width(ln) > columns - 1 and "  " + GRAY + SEP in ln:
            ln = ln.rsplit(f"  {GRAY}{SEP}{RESET}  ", 1)[0]
        fixed.append(ln + RESET)
    return fixed


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    try:
        columns = int(os.environ.get("COLUMNS") or shutil.get_terminal_size((120, 20)).columns)
    except Exception:
        columns = 120
    for ln in build(data, columns):
        print(ln)


if __name__ == "__main__":
    main()
