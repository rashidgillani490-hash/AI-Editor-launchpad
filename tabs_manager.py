"""
tabs_manager.py — Editor tab state management.

Tracks open files as tab objects with dirty-state, cursor position,
and external-change detection. Provides save/close/reorder logic
plus optional session persistence via JSON.

Usage (GUI hooks):
    from tabs_manager import TabsManager, TabInfo

    tabs = TabsManager()

    # Open a file
    tab = tabs.open_file("/path/to/foo.py", "print('hello')")

    # Mark dirty
    tabs.mark_dirty(tab.tab_id, True)

    # Close with unsaved check
    can_close, unsaved = tabs.prepare_close(tab.tab_id)
    if not can_close:
        # GUI shows "unsaved changes" dialog

    # Save
    tabs.save(tab.tab_id)          # save to current path
    tabs.save_as(tab.tab_id, "/new/path.py")

    # Session persistence
    open_paths = tabs.get_open_paths()
    # ... save to JSON ...
    tabs.restore_session(open_paths)
"""

from __future__ import annotations

import os
import json
import uuid
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Optional, Callable, List, Dict, Any, Set, Iterator,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TabError(Exception):
    """Base exception for tab operations."""


class TabNotFoundError(TabError):
    """Referenced tab does not exist."""


class SaveError(TabError):
    """Failed to write file to disk."""


class ExternalChangeError(TabError):
    """File on disk differs from in-memory content."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TabInfo:
    """Metadata for one open editor tab."""

    tab_id: str                     # unique id (uuid)
    path: str                       # absolute file path
    content: str                    # latest in-memory content
    original_content: str           # content as of last save (for dirty detection)
    is_modified: bool = False
    cursor_row: int = 0
    cursor_col: int = 0
    encoding: str = "utf-8"
    last_saved_mtime: float = 0.0   # os.path.getmtime at save time
    created_at: float = field(default_factory=time.time)

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    @property
    def is_untitled(self) -> bool:
        """True for new, never-saved files."""
        return not os.path.exists(self.path)


# ---------------------------------------------------------------------------
# TabsManager
# ---------------------------------------------------------------------------

class TabsManager:
    """Manages the lifecycle of open editor tabs."""

    SESSION_FILE = ".nexcore_session.json"

    def __init__(
        self,
        on_tab_change: Optional[Callable[[str, Optional[TabInfo]], None]] = None,
        # on_tab_change(action, tab_info_or_none): action ∈ {"opened","closed","modified","saved"}
        session_dir: str = "",
    ) -> None:
        self._tabs: List[TabInfo] = []                  # ordered list
        self._by_path: Dict[str, TabInfo] = {}          # path -> TabInfo
        self._by_id: Dict[str, TabInfo] = {}            # tab_id -> TabInfo
        self._active_tab_id: Optional[str] = None
        self.on_tab_change = on_tab_change
        self._session_dir = session_dir
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tabs(self) -> List[TabInfo]:
        """Return a snapshot of the tab list."""
        with self._lock:
            return list(self._tabs)

    @property
    def active_tab(self) -> Optional[TabInfo]:
        with self._lock:
            if self._active_tab_id:
                return self._by_id.get(self._active_tab_id)
            return None

    @property
    def active_tab_id(self) -> Optional[str]:
        return self._active_tab_id

    @property
    def tab_count(self) -> int:
        return len(self._tabs)

    def get_tab(self, tab_id: str) -> Optional[TabInfo]:
        with self._lock:
            return self._by_id.get(tab_id)

    def get_tab_by_path(self, path: str) -> Optional[TabInfo]:
        with self._lock:
            return self._by_path.get(path)

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_file(self, path: str, content: Optional[str] = None) -> TabInfo:
        """Open (or focus) a file as a tab. Reads content from disk if not provided.
        Returns the existing tab if the file is already open.
        """
        abs_path = os.path.abspath(path)
        with self._lock:
            existing = self._by_path.get(abs_path)
            if existing is not None:
                self._active_tab_id = existing.tab_id
                self._emit("focused", existing)
                return existing

            if content is None:
                if os.path.exists(abs_path):
                    content = self._read_file(abs_path)
                    last_mtime = os.path.getmtime(abs_path)
                else:
                    content = ""
                    last_mtime = 0.0
            else:
                last_mtime = os.path.getmtime(abs_path) if os.path.exists(abs_path) else 0.0

            tab = TabInfo(
                tab_id=str(uuid.uuid4()),
                path=abs_path,
                content=content,
                original_content=content,
                last_saved_mtime=last_mtime,
            )
            self._tabs.append(tab)
            self._by_path[abs_path] = tab
            self._by_id[tab.tab_id] = tab
            self._active_tab_id = tab.tab_id
            self._emit("opened", tab)
            return tab

    def create_untitled(self, base_name: str = "Untitled") -> TabInfo:
        """Create a new, unsaved tab."""
        counter = 1
        while True:
            name = f"{base_name}-{counter}" if counter > 1 else base_name
            if not any(t.filename == name for t in self._tabs):
                break
            counter += 1
        tab = TabInfo(
            tab_id=str(uuid.uuid4()),
            path=name,  # not a real path
            content="",
            original_content="",
        )
        self._tabs.append(tab)
        self._by_id[tab.tab_id] = tab
        self._active_tab_id = tab.tab_id
        self._emit("opened", tab)
        return tab

    def prepare_close(self, tab_id: str) -> tuple:
        """Check if a tab can be closed.
        Returns (can_close: bool, needs_prompt: bool).
        *needs_prompt* is True when the file has unsaved changes.
        """
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                raise TabNotFoundError(f"No tab with id: {tab_id!r}")
            needs_prompt = tab.is_modified
            return (True, needs_prompt)

    def close_tab(self, tab_id: str, force: bool = False) -> Optional[TabInfo]:
        """Close a tab. If *force* is False and unsaved changes exist, raises TabError.
        Returns the closed TabInfo or None.
        """
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                raise TabNotFoundError(f"No tab with id: {tab_id!r}")
            if tab.is_modified and not force:
                raise TabError("Tab has unsaved changes; use force=True to discard.")
            self._tabs.remove(tab)
            self._by_path.pop(tab.path, None)
            self._by_id.pop(tab_id, None)
            if self._active_tab_id == tab_id:
                self._active_tab_id = self._tabs[-1].tab_id if self._tabs else None
            self._emit("closed", tab)
            return tab

    def close_others(self, keep_tab_id: str) -> List[TabInfo]:
        """Close all tabs except *keep_tab_id*. Returns list of unsaved tabs that
        the GUI should prompt about.
        """
        unsaved: List[TabInfo] = []
        with self._lock:
            to_close = [t for t in self._tabs if t.tab_id != keep_tab_id]
            for t in to_close:
                if t.is_modified:
                    unsaved.append(t)
                else:
                    self._tabs.remove(t)
                    self._by_path.pop(t.path, None)
                    self._by_id.pop(t.tab_id, None)
                    self._emit("closed", t)
            self._active_tab_id = keep_tab_id
        return unsaved

    def close_all(self) -> List[TabInfo]:
        """Close all tabs. Returns list of unsaved tabs."""
        unsaved: List[TabInfo] = []
        with self._lock:
            for t in list(self._tabs):
                if t.is_modified:
                    unsaved.append(t)
                else:
                    self._tabs.remove(t)
                    self._by_path.pop(t.path, None)
                    self._by_id.pop(t.tab_id, None)
                    self._emit("closed", t)
            self._active_tab_id = None
        return unsaved

    def close_saved(self) -> None:
        """Close all tabs that have no unsaved changes."""
        with self._lock:
            for t in list(self._tabs):
                if not t.is_modified:
                    self._tabs.remove(t)
                    self._by_path.pop(t.path, None)
                    self._by_id.pop(t.tab_id, None)
                    self._emit("closed", t)
            if self._active_tab_id and self._active_tab_id not in self._by_id:
                self._active_tab_id = self._tabs[-1].tab_id if self._tabs else None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, tab_id: str) -> TabInfo:
        """Save a tab's content to its current path."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                raise TabNotFoundError(f"No tab with id: {tab_id!r}")
            self._write_file(tab.path, tab.content)
            tab.original_content = tab.content
            tab.is_modified = False
            tab.last_saved_mtime = os.path.getmtime(tab.path)
            self._emit("saved", tab)
            return tab

    def save_as(self, tab_id: str, new_path: str) -> TabInfo:
        """Save a tab's content to a new path and update tab metadata."""
        abs_path = os.path.abspath(new_path)
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                raise TabNotFoundError(f"No tab with id: {tab_id!r}")
            self._write_file(abs_path, tab.content)
            old_path = tab.path
            self._by_path.pop(old_path, None)
            tab.path = abs_path
            tab.original_content = tab.content
            tab.is_modified = False
            tab.last_saved_mtime = os.path.getmtime(abs_path)
            self._by_path[abs_path] = tab
            self._emit("saved", tab)
            return tab

    def save_all(self) -> List[TabInfo]:
        """Save all modified tabs. Returns list of saved tabs."""
        saved: List[TabInfo] = []
        with self._lock:
            for t in list(self._tabs):
                if t.is_modified:
                    if t.is_untitled:
                        continue  # skip untitled; GUI must handle
                    self._write_file(t.path, t.content)
                    t.original_content = t.content
                    t.is_modified = False
                    t.last_saved_mtime = os.path.getmtime(t.path)
                    saved.append(t)
                    self._emit("saved", t)
        return saved

    def revert(self, tab_id: str) -> TabInfo:
        """Reload tab content from disk, discarding unsaved changes."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                raise TabNotFoundError(f"No tab with id: {tab_id!r}")
            if not os.path.exists(tab.path):
                raise TabError(f"File no longer exists: {tab.path!r}")
            content = self._read_file(tab.path)
            tab.content = content
            tab.original_content = content
            tab.is_modified = False
            tab.last_saved_mtime = os.path.getmtime(tab.path)
            self._emit("reverted", tab)
            return tab

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def mark_dirty(self, tab_id: str, dirty: bool) -> None:
        """Mark a tab as modified or clean."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                return
            was = tab.is_modified
            tab.is_modified = dirty
            if not dirty:
                tab.original_content = tab.content
            if was != dirty:
                self._emit("modified", tab)

    def update_content(self, tab_id: str, new_content: str) -> None:
        """Update in-memory content and auto-detect dirty state."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is None:
                return
            tab.content = new_content
            tab.is_modified = (tab.content != tab.original_content)

    def update_cursor(self, tab_id: str, row: int, col: int) -> None:
        """Update tracked cursor position."""
        with self._lock:
            tab = self._by_id.get(tab_id)
            if tab is not None:
                tab.cursor_row = row
                tab.cursor_col = col

    # ------------------------------------------------------------------
    # Set active tab
    # ------------------------------------------------------------------

    def set_active_tab(self, tab_id: str) -> None:
        with self._lock:
            if tab_id in self._by_id:
                self._active_tab_id = tab_id
                self._emit("focused", self._by_id[tab_id])

    # ------------------------------------------------------------------
    # Reorder
    # ------------------------------------------------------------------

    def move_tab(self, from_index: int, to_index: int) -> None:
        """Reorder tabs in the list. Indices are 0-based."""
        with self._lock:
            if 0 <= from_index < len(self._tabs) and 0 <= to_index < len(self._tabs):
                tab = self._tabs.pop(from_index)
                self._tabs.insert(to_index, tab)

    # ------------------------------------------------------------------
    # External change detection
    # ------------------------------------------------------------------

    def check_external_changes(self) -> List[TabInfo]:
        """Check all open tabs against disk. Returns tabs whose file
        on disk differs from last-saved mtime.
        """
        changed: List[TabInfo] = []
        with self._lock:
            for tab in self._tabs:
                if tab.is_untitled:
                    continue
                try:
                    disk_mtime = os.path.getmtime(tab.path)
                except OSError:
                    disk_mtime = 0.0
                if disk_mtime > tab.last_saved_mtime + 0.001:
                    changed.append(tab)
        return changed

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def get_open_paths(self) -> List[str]:
        """Return paths of all open (saved) files for session storage."""
        with self._lock:
            return [t.path for t in self._tabs if not t.is_untitled]

    def save_session(self) -> None:
        """Persist open file paths to a JSON file."""
        if not self._session_dir:
            return
        session_path = os.path.join(self._session_dir, self.SESSION_FILE)
        paths = self.get_open_paths()
        data = {
            "open_files": paths,
            "active_index": (
                next((i for i, t in enumerate(self._tabs)
                      if t.tab_id == self._active_tab_id), 0)
            ),
        }
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def restore_session(self) -> List[str]:
        """Restore previously-open file paths from JSON.
        Returns list of file paths that still exist on disk.
        """
        if not self._session_dir:
            return []
        session_path = os.path.join(self._session_dir, self.SESSION_FILE)
        if not os.path.exists(session_path):
            return []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        existing = [p for p in data.get("open_files", []) if os.path.exists(p)]
        return existing

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                return f.read()

    @staticmethod
    def _write_file(path: str, content: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            raise SaveError(f"Cannot write {path!r}: {exc}") from exc

    def _emit(self, action: str, tab: Optional[TabInfo]) -> None:
        if self.on_tab_change:
            self.on_tab_change(action, tab)


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # Create a test file
        test_file = os.path.join(tmp, "hello.py")
        Path(test_file).write_text("print('hello')")

        tm = TabsManager(session_dir=tmp)

        # Open
        tab = tm.open_file(test_file)
        print(f"Opened: {tab.filename}  id={tab.tab_id[:8]}...")

        # Update content, mark dirty
        tm.update_content(tab.tab_id, "print('hello world')")
        print(f"Dirty: {tab.is_modified}")

        # Save
        tm.save(tab.tab_id)
        print(f"After save, dirty: {tab.is_modified}")

        # Session
        tm.save_session()
        restored = tm.restore_session()
        print(f"Session restore: {restored}")

        # Close
        tm.close_tab(tab.tab_id)
        print(f"Tab count after close: {tm.tab_count}")
