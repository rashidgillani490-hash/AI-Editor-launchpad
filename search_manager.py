"""
search_manager.py — Project-wide find/replace, async and cancellable.

Runs searches in a background thread so the GUI stays responsive.
Returns detailed match info: file path, line number, line text,
match start/end columns. Supports regex, case-sensitivity, whole-word,
and include/exclude glob filters.

Usage (GUI hooks):
    from search_manager import SearchManager

    sm = SearchManager(project_root="/project")

    # Search
    sm.search("def foo", on_results=my_callback)
    # ...
    if sm.is_searching:
        sm.cancel()

    # Replace
    count = sm.replace("foo", "bar")           # single match
    count = sm.replace_all_in_file("/f.py", "foo", "bar")
    count = sm.replace_all("foo", "bar")
"""

from __future__ import annotations

import os
import re
import fnmatch
import threading
from pathlib import Path
from typing import Optional, Callable, List, Dict, Tuple
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class SearchMatch:
    """A single match found during a project-wide search."""
    file_path: str
    line_number: int     # 1-based
    line_text: str
    match_start: int     # 0-based column
    match_end: int       # 0-based column


SearchCallback = Callable[[List[SearchMatch]], None]
ProgressCallback = Callable[[int, int], None]  # (files_scanned, total_approx)


# ---------------------------------------------------------------------------
# SearchManager
# ---------------------------------------------------------------------------

class SearchManager:
    """Async project-wide search and replace."""

    DEFAULT_EXCLUDES = [
        ".git", "__pycache__", "node_modules",
        ".venv", "venv", "*.pyc", ".pytest_cache",
        ".mypy_cache", ".tox", "build", "dist",
        "*.egg-info",
    ]

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.abspath(project_root)
        self._cancel_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._is_searching = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_searching(self) -> bool:
        return self._is_searching

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        pattern: str,
        on_results: SearchCallback,
        on_progress: Optional[ProgressCallback] = None,
        on_done: Optional[Callable[[], None]] = None,
        *,
        case_sensitive: bool = False,
        whole_word: bool = False,
        regex: bool = False,
        include_glob: str = "*",
        exclude_globs: Optional[List[str]] = None,
    ) -> None:
        """Start a background search. Results are delivered via *on_results*
        in batches. *on_done* is called when the search finishes.
        """
        if self._is_searching:
            self.cancel()
            self._thread.join(timeout=1.0)

        self._cancel_event = threading.Event()
        self._is_searching = True

        excludes = exclude_globs or self.DEFAULT_EXCLUDES

        self._thread = threading.Thread(
            target=self._search_thread,
            args=(
                pattern, on_results, on_progress, on_done,
                case_sensitive, whole_word, regex,
                include_glob, excludes,
            ),
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Request cancellation of the running search."""
        if self._cancel_event:
            self._cancel_event.set()
        self._is_searching = False

    # ------------------------------------------------------------------
    # Replace
    # ------------------------------------------------------------------

    def replace(
        self,
        file_path: str,
        line_number: int,
        match_start: int,
        match_end: int,
        pattern: str,
        replacement: str,
        regex: bool = False,
    ) -> bool:
        """Replace a single match in a specific file. Returns True on success."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return False

        if line_number < 1 or line_number > len(lines):
            return False

        line = lines[line_number - 1]
        if regex:
            new_line = re.sub(pattern, replacement, line, count=1)
        else:
            new_line = line[:match_start] + replacement + line[match_end:]

        lines[line_number - 1] = new_line
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            return False
        return True

    def replace_all_in_file(
        self,
        file_path: str,
        pattern: str,
        replacement: str,
        case_sensitive: bool = False,
        whole_word: bool = False,
        regex: bool = False,
    ) -> int:
        """Replace all occurrences in one file. Returns replacement count."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return 0

        new_content, count = self._do_replace(
            content, pattern, replacement,
            case_sensitive, whole_word, regex,
        )
        if count == 0:
            return 0
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except OSError:
            return 0
        return count

    def replace_all(
        self,
        pattern: str,
        replacement: str,
        case_sensitive: bool = False,
        whole_word: bool = False,
        regex: bool = False,
        include_glob: str = "*",
        exclude_globs: Optional[List[str]] = None,
    ) -> int:
        """Replace all occurrences across the entire project. Returns total count."""
        excludes = exclude_globs or self.DEFAULT_EXCLUDES
        total = 0

        for fpath in self._iter_files(include_glob, excludes):
            count = self.replace_all_in_file(
                fpath, pattern, replacement,
                case_sensitive, whole_word, regex,
            )
            total += count
        return total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _search_thread(
        self,
        pattern: str,
        on_results: SearchCallback,
        on_progress: Optional[ProgressCallback],
        on_done: Optional[Callable[[], None]],
        case_sensitive: bool,
        whole_word: bool,
        regex: bool,
        include_glob: str,
        exclude_globs: List[str],
    ) -> None:
        batch: List[SearchMatch] = []
        files_scanned = 0
        batch_size = 50

        try:
            for fpath in self._iter_files(include_glob, exclude_globs):
                if self._cancel_event and self._cancel_event.is_set():
                    break

                files_scanned += 1
                matches = self._search_file(
                    fpath, pattern, case_sensitive, whole_word, regex
                )
                batch.extend(matches)

                if len(batch) >= batch_size:
                    on_results(batch)
                    batch.clear()

                if on_progress and files_scanned % 10 == 0:
                    on_progress(files_scanned, -1)

            # Flush remaining
            if batch and not (self._cancel_event and self._cancel_event.is_set()):
                on_results(batch)

        finally:
            self._is_searching = False
            if on_done:
                on_done()

    def _search_file(
        self,
        file_path: str,
        pattern: str,
        case_sensitive: bool,
        whole_word: bool,
        regex: bool,
    ) -> List[SearchMatch]:
        matches: List[SearchMatch] = []
        flags = 0 if case_sensitive else re.IGNORECASE

        try:
            compiled = self._compile_pattern(pattern, whole_word, regex, flags)
            if compiled is None:
                return matches
        except re.error:
            return matches

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for line_no, line_text in enumerate(f, start=1):
                    for m in compiled.finditer(line_text):
                        matches.append(SearchMatch(
                            file_path=file_path,
                            line_number=line_no,
                            line_text=line_text.rstrip("\n\r"),
                            match_start=m.start(),
                            match_end=m.end(),
                        ))
        except (OSError, UnicodeDecodeError):
            pass
        return matches

    def _iter_files(self, include_glob: str, exclude_globs: List[str]):
        """Yield file paths matching include/exclude globs."""
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            if self._cancel_event and self._cancel_event.is_set():
                break
            dirnames[:] = [
                d for d in dirnames
                if not any(fnmatch.fnmatch(d, g) for g in exclude_globs)
            ]
            for fname in filenames:
                if not any(fnmatch.fnmatch(fname, p)
                           for p in include_glob.split(";")):
                    continue
                if any(fnmatch.fnmatch(fname, g) for g in exclude_globs):
                    continue
                yield os.path.join(dirpath, fname)

    @staticmethod
    def _compile_pattern(
        pattern: str,
        whole_word: bool,
        regex: bool,
        flags: int,
    ) -> Optional[re.Pattern]:
        if regex:
            return re.compile(pattern, flags)
        escaped = re.escape(pattern)
        if whole_word:
            escaped = rf"\b{escaped}\b"
        return re.compile(escaped, flags)

    @staticmethod
    def _do_replace(
        content: str,
        pattern: str,
        replacement: str,
        case_sensitive: bool,
        whole_word: bool,
        regex: bool,
    ) -> Tuple[str, int]:
        flags = 0 if case_sensitive else re.IGNORECASE
        if regex:
            try:
                return re.subn(pattern, replacement, content, flags=flags)
            except re.error:
                return content, 0
        escaped = re.escape(pattern)
        if whole_word:
            escaped = rf"\b{escaped}\b"
        try:
            return re.subn(escaped, replacement, content, flags=flags)
        except re.error:
            return content, 0


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.py").write_text("def foo():\n    pass\n")
        Path(tmp, "b.py").write_text("foo = 1\nprint(foo)\n")

        sm = SearchManager(tmp)

        results: List[SearchMatch] = []

        def handler(matches: List[SearchMatch]) -> None:
            results.extend(matches)

        def done() -> None:
            print("Search complete")

        sm.search("foo", handler, on_done=done)

        import time
        time.sleep(0.3)  # let thread finish
        for r in results:
            print(f"  {r.file_path}:{r.line_number}: {r.line_text.strip()}")

        # Replace
        count = sm.replace_all_in_file(
            os.path.join(tmp, "b.py"), "foo", "bar"
        )
        print(f"Replacements: {count}")
        print(Path(tmp, "b.py").read_text())
