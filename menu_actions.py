"""
menu_actions.py — Top Menu Bar backend action handlers.

Pure-logic functions for every menu item. The GUI just wires its
menu callbacks to these.  Keeps track of global UI toggle state
(panel visibility, zoom, auto-save).

Usage (GUI hooks):
    from menu_actions import MenuActions

    menu = MenuActions(tabs_mgr, terminal_mgr, file_tree)

    # File menu
    content = menu.open_file("/path/to/file.py")   # returns (path, content)
    menu.save_active()
    menu.save_all()
    menu.exit_app()

    # Edit menu
    menu.find_replace(editor_content, "pattern", "replacement")
    results = menu.find_in_files("pattern", root_dir="/project")

    # View menu
    menu.toggle_sidebar()
    menu.zoom_in()
"""

from __future__ import annotations

import os
import re
import time
import threading
from pathlib import Path
from typing import (
    Optional, Callable, List, Dict, Any, Tuple, Set,
)
from dataclasses import dataclass, field

from tabs_manager import TabsManager, TabInfo, TabNotFoundError
from terminal_manager import TerminalManager
from file_tree import FileTree, FileNode


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class FindMatch:
    """Result of a Find-in-Files search."""
    file_path: str
    line_number: int
    line_text: str
    match_start: int  # column offset
    match_end: int


@dataclass
class UndoEntry:
    """A single undo/redo entry for a file."""
    tab_id: str
    old_text: str
    new_text: str
    cursor_row: int
    cursor_col: int


# ---------------------------------------------------------------------------
# MenuActions
# ---------------------------------------------------------------------------

class MenuActions:
    """Backend handlers for the main menu bar."""

    def __init__(
        self,
        tabs_manager: TabsManager,
        terminal_manager: TerminalManager,
        file_tree: Optional[FileTree] = None,
        on_exit: Optional[Callable[[], None]] = None,
        on_file_opened: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._tabs = tabs_manager
        self._terminal = terminal_manager
        self._file_tree = file_tree
        self.on_exit = on_exit
        self.on_file_opened = on_file_opened

        # Undo stacks per file
        self._undo_stacks: Dict[str, List[UndoEntry]] = {}
        self._redo_stacks: Dict[str, List[UndoEntry]] = {}

        # UI toggles (read by GUI)
        self.sidebar_visible = True
        self.terminal_visible = True
        self.panel_visibility: Dict[str, bool] = {
            "explorer": True,
            "search": False,
            "terminal": True,
            "source_control": False,
            "debug": False,
            "testing": False,
            "extensions": False,
            "remote": False,
        }
        self.zoom_level = 0  # steps from default (0 = default, +1, -1 etc.)
        self.auto_save_enabled = False
        self._auto_save_interval = 30  # seconds
        self._auto_save_thread: Optional[threading.Thread] = None
        self._auto_save_running = False

    # ==================================================================
    # File menu
    # ==================================================================

    def new_file(self, target_dir: str = "") -> TabInfo:
        """Create a new untitled tab. Returns the TabInfo."""
        return self._tabs.create_untitled()

    def new_folder(self, parent_dir: str, name: str) -> FileNode:
        """Create a new folder in the file tree."""
        if self._file_tree is None:
            raise RuntimeError("No FileTree set on MenuActions")
        return self._file_tree.create_folder(parent_dir, name)

    def open_file(self, path: str) -> Tuple[str, str]:
        """Open a file by path. Returns (path, content)."""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Not a file: {path!r}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        tab = self._tabs.open_file(path, content=content)
        if self.on_file_opened:
            self.on_file_opened(path, content)
        return path, content

    def open_folder(self, path: str) -> str:
        """Open a folder as the project root. Returns the path (GUI updates tree)."""
        return os.path.abspath(path)

    def save_active(self) -> Optional[TabInfo]:
        """Save the currently active tab."""
        tab = self._tabs.active_tab
        if tab is None:
            return None
        if tab.is_untitled:
            return None  # GUI must handle 'Save As' for untitled
        return self._tabs.save(tab.tab_id)

    def save_as(self, tab_id: str, new_path: str) -> TabInfo:
        """Save active tab to a new path."""
        return self._tabs.save_as(tab_id, new_path)

    def save_all(self) -> List[TabInfo]:
        """Save all modified tabs. Returns list of saved TabInfo."""
        return self._tabs.save_all()

    def close_file(self, tab_id: str, force: bool = False) -> None:
        """Close a tab."""
        if not force:
            can, needs = self._tabs.prepare_close(tab_id)
            if needs:
                raise RuntimeError("Unsaved changes")
        self._tabs.close_tab(tab_id, force=force)

    def close_folder(self) -> None:
        """Close the project folder (close all tabs, clear tree)."""
        unsaved = self._tabs.close_all()
        if unsaved:
            raise RuntimeError(f"{len(unsaved)} file(s) have unsaved changes")

    def revert_file(self, tab_id: str) -> TabInfo:
        """Reload tab from disk, discarding changes."""
        return self._tabs.revert(tab_id)

    def exit_app(self) -> None:
        """Request clean shutdown. Kills terminals, saves session."""
        self._tabs.save_session()
        self._terminal.shutdown()
        if self.on_exit:
            self.on_exit()

    # -- Auto-save ----------------------------------------------------------

    def toggle_auto_save(self, enabled: bool) -> None:
        """Enable or disable auto-save loop."""
        self.auto_save_enabled = enabled
        if enabled and not self._auto_save_running:
            self._start_auto_save()
        elif not enabled:
            self._stop_auto_save()

    def _start_auto_save(self) -> None:
        self._auto_save_running = True
        self._auto_save_thread = threading.Thread(target=self._auto_save_loop, daemon=True)
        self._auto_save_thread.start()

    def _stop_auto_save(self) -> None:
        self._auto_save_running = False

    def _auto_save_loop(self) -> None:
        while self._auto_save_running:
            time.sleep(self._auto_save_interval)
            if self._auto_save_running:
                self._tabs.save_all()

    # ==================================================================
    # Edit menu
    # ==================================================================

    def push_undo(
        self,
        tab_id: str,
        old_text: str,
        new_text: str,
        cursor_row: int = 0,
        cursor_col: int = 0,
    ) -> None:
        """Record an undo entry for *tab_id*."""
        if tab_id not in self._undo_stacks:
            self._undo_stacks[tab_id] = []
        entry = UndoEntry(
            tab_id=tab_id,
            old_text=old_text,
            new_text=new_text,
            cursor_row=cursor_row,
            cursor_col=cursor_col,
        )
        self._undo_stacks[tab_id].append(entry)
        self._redo_stacks.pop(tab_id, None)  # clear redo on new action

    def undo(self, tab_id: str) -> Optional[UndoEntry]:
        """Undo last change for *tab_id*. Returns the UndoEntry (GUI applies text)."""
        stack = self._undo_stacks.get(tab_id, [])
        if not stack:
            return None
        entry = stack.pop()
        if tab_id not in self._redo_stacks:
            self._redo_stacks[tab_id] = []
        self._redo_stacks[tab_id].append(entry)
        return entry

    def redo(self, tab_id: str) -> Optional[UndoEntry]:
        """Redo last undone change for *tab_id*."""
        stack = self._redo_stacks.get(tab_id, [])
        if not stack:
            return None
        entry = stack.pop()
        if tab_id not in self._undo_stacks:
            self._undo_stacks[tab_id] = []
        self._undo_stacks[tab_id].append(entry)
        return entry

    def get_selected_text(self) -> str:
        """Placeholder — the GUI provides the actual selected text directly.
        This method is here for completeness; the GUI typically calls
        its own text widget's `get(SEL_FIRST, SEL_LAST)` and passes the
        result to other methods.
        """
        return ""

    @staticmethod
    def replace_text(
        content: str,
        search: str,
        replacement: str,
        start_pos: str = "1.0",
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> Tuple[str, int]:
        """Search and replace within *content*.
        Returns (new_content, count_of_replacements).
        """
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                new_content, count = re.subn(search, replacement, content, flags=flags)
            except re.error:
                return content, 0
        else:
            if not case_sensitive:
                pattern = re.escape(search)
                try:
                    new_content, count = re.subn(
                        pattern, replacement, content,
                        flags=re.IGNORECASE,
                    )
                except re.error:
                    return content, 0
            else:
                count = content.count(search)
                new_content = content.replace(search, replacement)
        return new_content, count

    @staticmethod
    def find_in_text(
        content: str,
        pattern: str,
        case_sensitive: bool = False,
        whole_word: bool = False,
        regex: bool = False,
    ) -> List[Tuple[int, int, int]]:
        """Search within a single file's content.
        Returns list of (line_number, col_start, col_end) for each match.
        Line numbers are 0-based.
        """
        matches: List[Tuple[int, int, int]] = []
        lines = content.splitlines()
        for line_no, line_text in enumerate(lines):
            for m in MenuActions._iter_matches(
                line_text, pattern, case_sensitive, whole_word, regex
            ):
                matches.append((line_no, m[0], m[1]))
        return matches

    def find_in_files(
        self,
        pattern: str,
        root_dir: str,
        include_glob: str = "*",
        exclude_globs: Optional[List[str]] = None,
        case_sensitive: bool = False,
        whole_word: bool = False,
        regex: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> List[FindMatch]:
        """Project-wide search. Runs synchronously (wrap in a thread from the GUI).
        If *cancel_event* is set, the search will exit early.
        """
        if exclude_globs is None:
            exclude_globs = [".git", "__pycache__", "node_modules", ".venv", "venv", "*.pyc"]

        results: List[FindMatch] = []
        patterns = include_glob.split(";") if ";" in include_glob else [include_glob]

        for dirpath, dirnames, filenames in os.walk(root_dir):
            if cancel_event and cancel_event.is_set():
                break

            # Apply exclude globs to directories
            dirnames[:] = [
                d for d in dirnames
                if not any(self._glob_match(d, g) for g in exclude_globs)
            ]

            for fname in filenames:
                if cancel_event and cancel_event.is_set():
                    return results

                # Check include
                if not any(self._glob_match(fname, p) for p in patterns):
                    continue
                # Check exclude
                if any(self._glob_match(fname, g) for g in exclude_globs):
                    continue

                full_path = os.path.join(dirpath, fname)
                # Skip binary-like files
                if fname.endswith((".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg")):
                    continue

                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line_text in enumerate(f):
                            for m_start, m_end in self._iter_matches(
                                line_text, pattern, case_sensitive, whole_word, regex
                            ):
                                results.append(FindMatch(
                                    file_path=full_path,
                                    line_number=line_no + 1,
                                    line_text=line_text.rstrip("\n"),
                                    match_start=m_start,
                                    match_end=m_end,
                                ))
                except (OSError, UnicodeDecodeError):
                    continue

        return results

    # ==================================================================
    # Selection menu  (text-level)
    # ==================================================================

    @staticmethod
    def select_all_text(content: str) -> Tuple[int, int]:
        """Return (start_pos, end_pos) for selecting all text."""
        return 0, len(content)

    @staticmethod
    def get_line_range(content: str, cursor_pos: int) -> Tuple[int, int]:
        """Return (start, end) character positions of the line at cursor_pos."""
        # Find line boundaries
        start = content.rfind("\n", 0, cursor_pos) + 1
        end = content.find("\n", cursor_pos)
        if end == -1:
            end = len(content)
        return start, end

    # ==================================================================
    # View menu
    # ==================================================================

    def toggle_sidebar(self) -> bool:
        self.sidebar_visible = not self.sidebar_visible
        return self.sidebar_visible

    def toggle_terminal(self) -> bool:
        self.terminal_visible = not self.terminal_visible
        return self.terminal_visible

    def toggle_panel(self, panel_name: str) -> bool:
        current = self.panel_visibility.get(panel_name, False)
        self.panel_visibility[panel_name] = not current
        return self.panel_visibility[panel_name]

    def set_panel_visibility(self, panel_name: str, visible: bool) -> None:
        self.panel_visibility[panel_name] = visible

    def zoom_in(self) -> int:
        self.zoom_level += 1
        return self.zoom_level

    def zoom_out(self) -> int:
        self.zoom_level -= 1
        return self.zoom_level

    def zoom_reset(self) -> int:
        self.zoom_level = 0
        return self.zoom_level

    # ==================================================================
    # Terminal menu
    # ==================================================================

    def new_terminal(self, cwd: str = "") -> str:
        inst = self._terminal.create_terminal(cwd)
        return inst.terminal_id

    def kill_terminal(self, terminal_id: str) -> None:
        self._terminal.remove_terminal(terminal_id)

    def clear_terminal(self, terminal_id: str) -> None:
        inst = self._terminal.get_terminal(terminal_id)
        if inst:
            inst.clear_output()

    # ==================================================================
    # Run menu (delegates to run_manager — see run_manager.py)
    # ==================================================================

    # These are thin wrappers; real logic is in run_manager.
    # Provided here so the menu can call them without importing extra modules.

    def run_active_file(self) -> None:
        """Placeholder — wire to run_manager.run_active_file()."""
        raise NotImplementedError("Wire this to run_manager.run_active_file()")

    def stop_active_run(self) -> None:
        """Placeholder — wire to run_manager.stop()."""
        raise NotImplementedError("Wire this to run_manager.stop()")

    # ==================================================================
    # Utility
    # ==================================================================

    @staticmethod
    def _iter_matches(
        line_text: str,
        pattern: str,
        case_sensitive: bool,
        whole_word: bool,
        regex: bool,
    ):
        """Yield (start, end) column tuples for each match on a line."""
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                for m in re.finditer(pattern, line_text, flags=flags):
                    yield m.start(), m.end()
            except re.error:
                return
        else:
            flags = 0 if case_sensitive else re.IGNORECASE
            escaped = re.escape(pattern)
            if whole_word:
                escaped = rf"\b{escaped}\b"
            try:
                for m in re.finditer(escaped, line_text, flags=flags):
                    yield m.start(), m.end()
            except re.error:
                return

    @staticmethod
    def _glob_match(name: str, pattern: str) -> bool:
        """Simple glob matching (fnmatch)."""
        import fnmatch
        return fnmatch.fnmatch(name, pattern)


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from tabs_manager import TabsManager
    from terminal_manager import TerminalManager

    tabs = TabsManager()
    tm = TerminalManager()
    menu = MenuActions(tabs, tm)

    # Open a temp file
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test.py").write_text("def foo():\n    pass\n")
        path, content = menu.open_file(os.path.join(tmp, "test.py"))
        print(f"Opened: {path}")

        tab = tabs.active_tab
        assert tab is not None

        # Find
        matches = menu.find_in_text(content, "foo")
        print(f"Matches for 'foo': {matches}")

        # Undo test
        menu.push_undo(tab.tab_id, "old", "new")
        entry = menu.undo(tab.tab_id)
        print(f"Undo entry: {entry}")

        # Find in files
        results = menu.find_in_files("def", tmp, include_glob="*.py")
        print(f"Find-in-files: {len(results)} matches")

        menu.exit_app()
