"""
git_manager.py — Source Control (Git) integration backend.

Wraps the `git` CLI via subprocess.  Provides status, diff, stage/unstage,
commit, branch management, push/pull, commit history, and reversion.

Usage (GUI hooks):
    from git_manager import GitManager

    git = GitManager("/path/to/repo")
    if git.is_repo:
        status = git.get_status()
        git.stage_file("src/main.py")
        git.commit("feat: add main")
        git.push()
"""

from __future__ import annotations

import os
import subprocess
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class GitStatusEntry:
    """One file's status in the working tree."""
    path: str
    status: str          # "M"=modified, "A"=added, "D"=deleted,
                         # "R"=renamed, "??"=untracked, etc.
    staged: bool         # True = in index (green), False = in working tree (red)
    old_path: Optional[str] = None  # For renames


@dataclass
class CommitInfo:
    hash: str
    author: str
    date: str
    message: str


@dataclass
class BranchInfo:
    name: str
    is_current: bool


# ---------------------------------------------------------------------------
# GitManager
# ---------------------------------------------------------------------------

class GitManager:
    """Git CLI wrapper for IDE source-control panel."""

    def __init__(
        self,
        repo_path: str,
        on_status_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.repo_path = os.path.abspath(repo_path)
        self.on_status_change = on_status_change

    # ------------------------------------------------------------------
    # Repo detection
    # ------------------------------------------------------------------

    @property
    def is_repo(self) -> bool:
        """Check if repo_path is inside a Git repository."""
        return self._git_dir() is not None

    def _git_dir(self) -> Optional[str]:
        """Return .git directory path, or None."""
        try:
            result = self._run(["rev-parse", "--git-dir"], check=False)
            if result.returncode == 0:
                return os.path.join(self.repo_path, result.stdout.strip())
        except Exception:
            pass
        return None

    def init_repo(self) -> bool:
        """Initialize a new Git repository at repo_path."""
        r = self._run(["init"], check=False)
        return r.returncode == 0

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> List[GitStatusEntry]:
        """Return parsed `git status --porcelain` output."""
        if not self.is_repo:
            return []
        result = self._run(["status", "--porcelain"], check=False)
        entries: List[GitStatusEntry] = []
        for line in result.stdout.splitlines():
            line = line.rstrip("\n")
            if not line:
                continue
            # Format: XY PATH  or  XY ORIG -> NEW (renames)
            status_code = line[:2]
            rest = line[3:]

            staged = status_code[0] != " " and status_code[0] != "?"
            working = status_code[1] != " "

            # Detect rename
            if " -> " in rest and (status_code[0] == "R" or status_code[1] == "R"):
                parts = rest.split(" -> ")
                entries.append(GitStatusEntry(
                    path=parts[1].strip(),
                    status="R",
                    staged=staged,
                    old_path=parts[0].strip(),
                ))
            else:
                entry_status = status_code.strip() or "M"
                entries.append(GitStatusEntry(
                    path=rest.strip(),
                    status=entry_status,
                    staged=staged,
                ))
        return entries

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def get_diff(self, file_path: Optional[str] = None, staged: bool = False) -> str:
        """Return unified diff for *file_path* (or all files if None).
        If *staged* is True, show staged changes (cached/index diff).
        """
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file_path:
            args.extend(["--", file_path])
        result = self._run(args, check=False)
        return result.stdout

    # ------------------------------------------------------------------
    # Stage / unstage
    # ------------------------------------------------------------------

    def stage_file(self, path: str) -> bool:
        r = self._run(["add", "--", path], check=False)
        self._emit_status_change()
        return r.returncode == 0

    def unstage_file(self, path: str) -> bool:
        r = self._run(["reset", "HEAD", "--", path], check=False)
        self._emit_status_change()
        return r.returncode == 0

    def stage_all(self) -> bool:
        r = self._run(["add", "-A"], check=False)
        self._emit_status_change()
        return r.returncode == 0

    def discard_changes(self, file_path: str) -> bool:
        """Destructively revert *file_path* to HEAD. True = success."""
        r = self._run(["checkout", "--", file_path], check=False)
        self._emit_status_change()
        return r.returncode == 0

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit(self, message: str) -> bool:
        r = self._run(["commit", "-m", message], check=False)
        self._emit_status_change()
        return r.returncode == 0

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def get_branches(self) -> List[BranchInfo]:
        """Return all branches; marks current with *."""
        result = self._run(["branch"], check=False)
        branches: List[BranchInfo] = []
        for line in result.stdout.splitlines():
            name = line.lstrip("* ").strip()
            branches.append(BranchInfo(
                name=name,
                is_current=line.startswith("*"),
            ))
        return branches

    def checkout_branch(self, name: str) -> bool:
        r = self._run(["checkout", name], check=False)
        self._emit_status_change()
        return r.returncode == 0

    def create_branch(self, name: str) -> bool:
        r = self._run(["checkout", "-b", name], check=False)
        self._emit_status_change()
        return r.returncode == 0

    def get_current_branch(self) -> Optional[str]:
        result = self._run(["branch", "--show-current"], check=False)
        name = result.stdout.strip()
        return name if name else None

    # ------------------------------------------------------------------
    # Remotes
    # ------------------------------------------------------------------

    def pull(self, on_output: Optional[Callable[[str, str], None]] = None) -> bool:
        """Pull from remote. *on_output(stream, text)* for progress."""
        return self._long_running(["pull"], on_output)

    def push(self, on_output: Optional[Callable[[str, str], None]] = None) -> bool:
        """Push to remote."""
        return self._long_running(["push"], on_output)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_commit_history(self, limit: int = 50) -> List[CommitInfo]:
        """Return recent commits."""
        fmt = "%H||%an||%aI||%s"
        result = self._run(
            ["log", f"-{limit}", f"--format={fmt}", "--"],
            check=False,
        )
        commits: List[CommitInfo] = []
        for line in result.stdout.splitlines():
            parts = line.split("||", 3)
            if len(parts) == 4:
                commits.append(CommitInfo(
                    hash=parts[0],
                    author=parts[1],
                    date=parts[2],
                    message=parts[3],
                ))
        return commits

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=check,
            encoding="utf-8",
            errors="replace",
        )

    def _long_running(
        self,
        args: List[str],
        on_output: Optional[Callable[[str, str], None]],
    ) -> bool:
        """Run a potentially long git command, streaming output line by line."""
        try:
            process = subprocess.Popen(
                ["git"] + args,
                cwd=self.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if on_output and process.stdout:
                for line in iter(process.stdout.readline, ""):
                    on_output("stdout", line)
            if on_output and process.stderr:
                for line in iter(process.stderr.readline, ""):
                    on_output("stderr", line)
            process.wait()
            return process.returncode == 0
        except Exception:
            return False

    def _emit_status_change(self) -> None:
        if self.on_status_change:
            self.on_status_change()


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        git = GitManager(tmp)
        print(f"Is repo: {git.is_repo}")

        if git.init_repo():
            print("Repo initialized")
            # Create a file and check status
            Path(tmp, "README.md").write_text("# Test\n")
            status = git.get_status()
            for s in status:
                print(f"  {s.status} {s.path}")

            git.stage_all()
            git.commit("Initial commit")

            branches = git.get_branches()
            print(f"Branches: {[b.name for b in branches]}")

            history = git.get_commit_history(5)
            print(f"History: {[c.message for c in history]}")
