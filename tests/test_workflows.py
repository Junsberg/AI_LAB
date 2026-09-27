"""Guards on .github/workflows that a code review can miss."""
import re
from pathlib import Path

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def test_bot_commits_use_the_actions_identity():
    # `<login>@users.noreply.github.com` belongs to the real GitHub account <login>:
    # 09-27 "stats@…" credited 49 automated commits to a stranger named stats.
    for f in WF.glob("*.yml"):
        for email in re.findall(r'user\.email\s+"([^"]+)"', f.read_text()):
            assert email == BOT_EMAIL, f"{f.name}: commit email {email!r} is not the Actions bot"
