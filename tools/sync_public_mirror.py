"""Synchronize a sanitized snapshot to an empty or existing public Git remote.

This module never builds a snapshot and never reads runtime data.  It only
copies a snapshot that has already passed the public-release safety gates.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


PUBLIC_REPOSITORY = "kkytmkmmk/personal-os"
PUBLIC_MIRROR_BRANCH = "master"


@dataclass(frozen=True)
class SyncResult:
    mode: str
    changed: bool


class PublicMirrorGitError(RuntimeError):
    """A Git failure with credentials and authorization headers removed."""


def sanitize_git_error(text: str) -> str:
    text = re.sub(r"https?://[^\s/@:]+:[^\s/@]+@", "https://<redacted>@", text)
    text = re.sub(r"(Authorization:\s*Bearer\s+)\S+", r"\1<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pous]_[A-Za-z0-9_]+|ghs_[A-Za-z0-9_]+)\b", "<redacted>", text)
    text = re.sub(r"(PUBLIC_REPO_TOKEN=)[^\s]+", r"\1<redacted>", text)
    return text.strip()


def _git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        detail = sanitize_git_error(result.stderr or result.stdout)
        raise PublicMirrorGitError(f"Git command failed (exit {result.returncode}). {detail}".strip())
    return result


def _clear_worktree(worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_snapshot(snapshot: Path, worktree: Path) -> None:
    for source in snapshot.rglob("*"):
        relative = source.relative_to(snapshot)
        if ".git" in relative.parts:
            raise ValueError("The public snapshot must not contain Git metadata")
        target = worktree / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def sync_snapshot(
    snapshot: Path,
    remote: str,
    worktree: Path,
    dry_run: bool = False,
    branch: str = PUBLIC_MIRROR_BRANCH,
) -> SyncResult:
    """Make ``remote/branch`` match ``snapshot`` without exposing private history."""
    snapshot = snapshot.resolve()
    worktree = worktree.resolve()
    if not snapshot.is_dir() or not (snapshot / "README.md").is_file():
        raise ValueError("A validated public snapshot containing README.md is required")
    if worktree == snapshot or snapshot in worktree.parents:
        raise ValueError("The mirror worktree must be outside the snapshot")
    if worktree.exists() and not (worktree / ".git").is_dir():
        raise ValueError("The mirror worktree exists but is not a Git repository")
    if not worktree.exists():
        worktree.mkdir(parents=True)
        _git(["init", "--initial-branch", branch], cwd=worktree)
        _git(["remote", "add", "origin", remote], cwd=worktree)
    else:
        _git(["remote", "set-url", "origin", remote], cwd=worktree)
    existing = _git(["fetch", "--depth=1", "origin", branch], cwd=worktree, check=False).returncode == 0
    if existing:
        _git(["checkout", "-B", branch, "FETCH_HEAD"], cwd=worktree)
    _clear_worktree(worktree)
    _copy_snapshot(snapshot, worktree)
    _git(["add", "--all"], cwd=worktree)
    changed = bool(_git(["status", "--porcelain"], cwd=worktree).stdout.strip())
    if not changed:
        return SyncResult(mode="NO CHANGES", changed=False)
    if dry_run:
        return SyncResult(mode="DRY RUN", changed=True)
    _git(["config", "user.name", "github-actions[bot]"], cwd=worktree)
    _git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=worktree)
    message = "Sync public snapshot" if existing else "Initial public release"
    _git(["commit", "-m", message], cwd=worktree)
    _git(["push", "origin", f"HEAD:{branch}"], cwd=worktree)
    return SyncResult(mode="PUBLISHED", changed=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--branch", default=PUBLIC_MIRROR_BRANCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = sync_snapshot(args.snapshot, args.remote, args.worktree, args.dry_run, args.branch)
    print(f"Result: {result.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
