"""Local Git integration for config versioning (commit only, never push)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List

from .config import ROOT


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def commit_file(path: Path, message: str, repo: Path = ROOT) -> Dict:
    """Stage and commit exactly ``path``. Other staged changes are left untouched."""
    try:
        if _git(["rev-parse", "--is-inside-work-tree"], repo).returncode != 0:
            return {"ok": False, "status": "no_repo", "detail": "project is not a git repository"}
        rel = str(path.resolve().relative_to(repo.resolve()))
        add = _git(["add", "--", rel], repo)
        if add.returncode != 0:
            return {"ok": False, "status": "error", "detail": add.stderr.strip()}
        if _git(["diff", "--cached", "--quiet", "--", rel], repo).returncode == 0:
            return {"ok": True, "status": "no_changes", "detail": "config unchanged since last commit"}
        res = _git(["commit", "-m", message, "--", rel], repo)
        if res.returncode != 0:
            return {"ok": False, "status": "error", "detail": (res.stderr or res.stdout).strip()}
        sha = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        return {"ok": True, "status": "committed", "commit": sha, "detail": message}
    except FileNotFoundError:
        return {"ok": False, "status": "no_git", "detail": "git executable not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "error", "detail": "git timed out"}
