"""Publish the built dashboard to a ``gh-pages`` branch (GitHub Pages).

The branch holds exactly one commit that is amended and force-pushed on every run, so the
~1 MB page never piles up in the repository history.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BRANCH = "gh-pages"
_IDENT = ["-c", "user.name=btc-chart", "-c", "user.email=noreply@users.noreply.github.com"]


class PublishError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(
        ["git", *_IDENT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    if check and res.returncode != 0:
        raise PublishError(f"git {' '.join(args)}: {(res.stderr or res.stdout).strip()}")
    return res


def publish(index_html: Path, pages_dir: Path, remote_url: str) -> None:
    """Copy ``index_html`` into ``pages_dir`` (a scratch repo) and force-push it to ``remote_url``."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    if not (pages_dir / ".git").exists():
        _git(pages_dir, "init", "-q", "-b", BRANCH)
    _git(pages_dir, "remote", "remove", "origin", check=False)
    _git(pages_dir, "remote", "add", "origin", remote_url)

    shutil.copyfile(index_html, pages_dir / "index.html")
    (pages_dir / ".nojekyll").write_text("")  # serve the file as-is, skip Jekyll
    _git(pages_dir, "add", "-A")

    has_head = _git(pages_dir, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    if has_head:
        _git(pages_dir, "commit", "-q", "--amend", "--no-edit", "--allow-empty")
    else:
        _git(pages_dir, "commit", "-q", "-m", "BTC dashboard")
    _git(pages_dir, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}")


def origin_url(repo_dir: Path) -> str:
    return _git(repo_dir, "remote", "get-url", "origin").stdout.strip()
