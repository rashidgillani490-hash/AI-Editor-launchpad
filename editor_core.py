"""editor_core.py - Multi-Tab Text Editing Workspace Core for NexCore IDE.

This module models the *logical* state of a multi-tab code editor: which
tabs are open, what text buffer each one wraps, which file (if any) each
tab is bound to, and whether a tab has unsaved changes. It deliberately
knows nothing about CustomTkinter, tkinter, or any other GUI toolkit, so
it can be unit-tested and reasoned about in isolation.

Decoupling strategy
--------------------
The requirements ask for each tab to "wrap an independent text widget
instance". Rather than importing a specific GUI toolkit here (which
would defeat the purpose of a GUI-agnostic core), this module defines
the *minimal* interface a text buffer must satisfy (``get`` / ``insert``
/ ``delete``) and ships a small in-memory implementation
(:class:`InMemoryTextBuffer`) that satisfies it. That default lets this
module run and be tested with no GUI toolkit installed at all.

The real GUI layer (built with CustomTkinter) wires itself in by passing
a ``widget_factory`` callable into :class:`EditorWorkspace` - e.g.
``lambda: customtkinter.CTkTextbox(parent)``. Since ``CTkTextbox`` (and
plain ``tkinter.Text``) already implement ``get`` / ``insert`` / ``delete``
with matching signatures, no adapter code is required on either side.

Modification tracking
----------------------
Each tab carries a "revision index": a monotonically increasing integer
that is bumped every time the tab's content changes. The index value
recorded at the moment of the last successful save is kept alongside it.
A tab is "modified" exactly when the live index has moved past the
last-saved index. Additionally, a content hash is maintained to detect
true content changes vs. revision thrashing (e.g., type + undo to
original state). This dual approach provides both fast tracking and
accurate "actually different" detection. The GUI layer should call
:meth:`EditorTab.notify_change` from whatever change event its text
widget exposes (e.g. binding to ``<<Modified>>`` or ``<KeyRelease>``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import os
import re
from typing import Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class TextWidgetLike(Protocol):
    """Structural interface every backing text buffer must satisfy.

    ``tkinter.Text`` and CustomTkinter's ``CTkTextbox`` both already
    match this shape, so they can be used directly as a
    ``widget_factory`` return value with no wrapper class needed.
    """

    def get(self, start: str, end: Optional[str] = None) -> str:
        ...

    def insert(self, index: str, text: str) -> None:
        ...

    def delete(self, start: str, end: Optional[str] = None) -> None:
        ...


class InMemoryTextBuffer:
    """A minimal, dependency-free stand-in for a real GUI text widget.

    Implements just enough of the ``tkinter.Text`` surface area
    (``get`` / ``insert`` / ``delete``) for :class:`EditorTab` to operate
    on it. Supports positional insert/delete operations with ``"1.0"``
    and ``"end"`` style indices as well as plain integer offsets.

    Used as the default backing store so this module is fully standalone
    and testable without any GUI toolkit installed.

    Attributes:
        _text: The buffer content.
        _index_map: Maps tkinter-style indices to plain string offsets.
    """

    def __init__(self, initial_text: str = "") -> None:
        self._text = initial_text

    def _parse_index(self, index: str) -> int:
        """Convert a tkinter-style index to a plain string offset.

        Supports:
        - ``"1.0"`` → character 0
        - ``"end"`` → length of text (for append-like operations)
        - Plain integers (treated as string offsets directly)

        Args:
            index: Index in tkinter format or as a plain offset.

        Returns:
            Integer offset into the buffer.

        Raises:
            ValueError: If index format is unrecognized or out of bounds.
        """
        if index == "end":
            return len(self._text)
        if index == "1.0" or index == "start":
            return 0
        
        try:
            offset = int(index)
            if offset < 0 or offset > len(self._text):
                raise ValueError(f"Index {offset} out of bounds [0, {len(self._text)}]")
            return offset
        except ValueError as e:
            if "out of bounds" in str(e):
                raise
            raise ValueError(f"Unrecognized index format: {index!r}") from None

    def get(self, start: str = "1.0", end: Optional[str] = None) -> str:
        """Return the buffer contents in the given range.

        Args:
            start: Start index (``"1.0"``, ``"end"``, or integer offset).
                Defaults to ``"1.0"`` (start of buffer).
            end: End index. Defaults to ``None`` (end of buffer if
                ``start`` is specified, otherwise the same as ``start``).

        Returns:
            Substring from ``start`` to ``end`` (exclusive).
        """
        start_offset = self._parse_index(start)
        if end is None:
            end_offset = len(self._text)
        else:
            end_offset = self._parse_index(end)
        
        return self._text[start_offset:end_offset]

    def insert(self, index: str, text: str) -> None:
        """Insert text at the given index.

        Args:
            index: Position to insert at (``"1.0"``, ``"end"``, or
                integer offset).
            text: Text to insert.

        Raises:
            ValueError: If index is invalid or out of bounds.
        """
        offset = self._parse_index(index)
        self._text = self._text[:offset] + text + self._text[offset:]

    def delete(self, start: str = "1.0", end: Optional[str] = None) -> None:
        """Delete text in the given range.

        Args:
            start: Start of deletion range (``"1.0"``, ``"end"``, or
                integer offset). Defaults to ``"1.0"``.
            end: End of deletion range. If ``None``, deletes from
                ``start`` to end of buffer.

        Raises:
            ValueError: If indices are invalid or out of bounds.
        """
        start_offset = self._parse_index(start)
        if end is None:
            end_offset = len(self._text)
        else:
            end_offset = self._parse_index(end)
        
        self._text = self._text[:start_offset] + self._text[end_offset:]

    def __repr__(self) -> str:
        return f"InMemoryTextBuffer(text_len={len(self._text)})"


# A widget factory is any zero-argument callable that returns a fresh
# object satisfying TextWidgetLike. The GUI layer supplies its own
# (e.g. constructing a CTkTextbox bound to a specific parent frame).
WidgetFactory = Callable[[], TextWidgetLike]


def _compute_content_hash(content: str) -> str:
    """Compute a SHA256 hash of content for change detection.

    Args:
        content: The content to hash.

    Returns:
        Hex-encoded SHA256 hash.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class EditorTab:
    """A single open document tab and its associated text buffer.

    Attributes:
        tab_id: Unique identifier for this tab (stable for its lifetime).
        widget: The backing text widget instance this tab wraps.
        file_path: Absolute/relative path this tab is bound to, or
            ``None`` for an unsaved "Untitled" buffer.
        title: Display title for the tab (e.g. the filename).
    """

    tab_id: int
    widget: TextWidgetLike
    file_path: Optional[str] = None
    title: str = "Untitled"

    # The "revision index": bumped on every content-changing event.
    # Starts at 0 so a freshly created tab with no edits reports as
    # unmodified.
    _revision_index: int = dataclasses.field(default=0, init=False)

    # The revision index value as of the last successful save/load.
    _saved_index: int = dataclasses.field(default=0, init=False)

    # Content hash at last save/load, for accurate "actually different"
    # detection independent of revision thrashing.
    _saved_content_hash: str = dataclasses.field(default="", init=False)

    def __post_init__(self) -> None:
        """Initialize content hash from widget's current state."""
        self._saved_content_hash = _compute_content_hash(
            self.widget.get("1.0", "end")
        )

    def notify_change(self) -> None:
        """Record that the tab's content has changed.

        The GUI layer is responsible for calling this whenever its text
        widget reports an edit (for example from a ``<<Modified>>`` or
        ``<KeyRelease>`` binding). This keeps modification tracking
        correct without editor_core needing to poll widget content or
        depend on any specific toolkit's event system.
        """
        self._revision_index += 1

    def mark_saved(self) -> None:
        """Mark the tab's current state as persisted to disk.

        Call this after a successful ``file_io.save_file`` or
        ``file_io.save_file_as`` so :meth:`is_modified` starts
        reporting ``False`` again.
        """
        self._saved_index = self._revision_index
        self._saved_content_hash = _compute_content_hash(self.get_content())

    @property
    def is_modified(self) -> bool:
        """``True`` if the tab has unsaved changes.

        Uses both revision index (fast check) and content hash (accurate
        "actually different" detection) to report true modifications.
        """
        # Fast check: if revision hasn't moved past save, it's definitely
        # not modified. This covers the common case (edit, save, no edits).
        if self._revision_index == self._saved_index:
            return False
        
        # Revision has moved, but the content might be the same (e.g.,
        # type + undo). Use hash to decide.
        current_hash = _compute_content_hash(self.get_content())
        return current_hash != self._saved_content_hash

    @property
    def revision_index(self) -> int:
        """Current revision index (mainly useful for diagnostics)."""
        return self._revision_index

    def get_content(self) -> str:
        """Return the full current text content of this tab's widget."""
        return self.widget.get("1.0", "end")

    def set_content(self, content: str) -> None:
        """Replace the tab's widget content wholesale.

        Used when loading a file into a tab. Counts as a change unless
        immediately followed by :meth:`mark_saved` (which is exactly
        what :class:`EditorWorkspace.add_tab` does when opening a file
        that already exists on disk, so a freshly opened file starts
        out clean).

        Args:
            content: New content for the buffer.
        """
        self.widget.delete("1.0", "end")
        self.widget.insert("1.0", content)
        self.notify_change()

    def rebind(self, new_path: str) -> None:
        """Update file path and title after a "Save As" operation.

        Leaves revision tracking untouched so save state is preserved.
        This is called after a successful save-as to update the tab's
        binding without losing the "clean" state.

        Args:
            new_path: New file path to bind this tab to.
        """
        self.file_path = new_path
        self.title = EditorWorkspace._basename(new_path)

    def display_title(self) -> str:
        """Title suitable for rendering on the tab itself.

        Appends a "modified" marker so the GUI layer gets a sensible
        default label for free; it is free to ignore this and build its
        own label instead.

        Returns:
            Display title with optional ``*`` marker for unsaved changes.
        """
        marker = "*" if self.is_modified else ""
        return f"{self.title}{marker}"

    def __repr__(self) -> str:
        return (
            f"EditorTab(tab_id={self.tab_id}, title={self.title!r}, "
            f"file_path={self.file_path!r}, is_modified={self.is_modified})"
        )

    def __eq__(self, other: object) -> bool:
        """Compare tabs by identity (tab_id)."""
        if not isinstance(other, EditorTab):
            return NotImplemented
        return self.tab_id == other.tab_id

    def __hash__(self) -> int:
        """Hash by tab_id for use in sets/dicts."""
        return hash(self.tab_id)


class EditorWorkspace:
    """Owns the collection of open :class:`EditorTab` instances.

    This is the class a GUI layer should hold a single instance of and
    wire its "New Tab", "Close Tab", tab-click, and Save button
    callbacks into.
    """

    def __init__(self, widget_factory: Optional[WidgetFactory] = None) -> None:
        """Create an empty workspace.

        Args:
            widget_factory: Zero-argument callable used to construct the
                backing text widget for each new tab. Defaults to
                :class:`InMemoryTextBuffer` so the workspace is usable
                with no GUI toolkit present. A real application should
                pass something like
                ``lambda: customtkinter.CTkTextbox(tab_parent_frame)``.
        """
        self._widget_factory: WidgetFactory = widget_factory or InMemoryTextBuffer
        self._tabs: Dict[int, EditorTab] = {}
        self._tab_order: List[int] = []
        self._active_tab_id: Optional[int] = None
        # Simple monotonic counter to hand out unique tab ids, immune to
        # reuse issues that could arise from recycling list indices after
        # tabs are closed.
        self._id_counter = itertools.count(start=1)

    # -- Tab lifecycle ----------------------------------------------------

    def add_tab(
        self,
        file_path: Optional[str] = None,
        content: str = "",
        title: Optional[str] = None,
        focus_existing: bool = False,
    ) -> EditorTab:
        """Open a new tab and make it the active tab.

        If ``focus_existing=True`` and a tab is already open for
        ``file_path``, switches to that tab instead of creating a
        duplicate.

        Args:
            file_path: Path this tab is bound to, or ``None`` for a new
                unsaved buffer.
            content: Initial text to populate the tab with (typically
                the result of ``file_io.open_file(...).content``).
            title: Display title. Defaults to the file's basename when
                ``file_path`` is given, otherwise ``"Untitled"``.
            focus_existing: If ``True`` and a tab for ``file_path``
                already exists, switch to that tab instead of opening
                a duplicate. Defaults to ``False``.

        Returns:
            The newly created :class:`EditorTab`, or the existing tab
            if ``focus_existing=True`` and a duplicate was found.
        """
        # Check for duplicate if requested and file_path is given.
        if focus_existing and file_path is not None:
            existing = self.find_tab_by_path(file_path)
            if existing is not None:
                self.switch_tab(existing.tab_id)
                return existing

        tab_id = next(self._id_counter)
        widget = self._widget_factory()

        if title is None:
            title = self._basename(file_path) if file_path else "Untitled"

        tab = EditorTab(tab_id=tab_id, widget=widget, file_path=file_path, title=title)

        if content:
            tab.set_content(content)
            # Content came straight from disk (or is deliberately seeded),
            # so the tab should not start out flagged as modified.
            tab.mark_saved()

        self._tabs[tab_id] = tab
        self._tab_order.append(tab_id)
        self._active_tab_id = tab_id
        return tab

    def close_tab(self, tab_id: int) -> bool:
        """Close a tab, discarding its widget reference.

        Callers (the GUI layer) are responsible for checking
        :meth:`EditorTab.is_modified` beforehand and prompting the user
        to save if needed - this method performs no such prompting
        itself, keeping it side-effect free and easy to test.

        Args:
            tab_id: Identifier of the tab to close.

        Returns:
            ``True`` if a tab with that id existed and was closed,
            ``False`` otherwise.
        """
        if tab_id not in self._tabs:
            return False

        del self._tabs[tab_id]
        self._tab_order.remove(tab_id)

        if self._active_tab_id == tab_id:
            # Fall back to the tab that was to the right, or otherwise
            # the new last tab, matching common IDE tab-close behaviour.
            self._active_tab_id = self._tab_order[-1] if self._tab_order else None

        return True

    def close_all_tabs(self) -> List[int]:
        """Close all open tabs.

        Returns:
            List of tab IDs that were closed (in order they were closed).
        """
        closed = []
        while self._tab_order:
            tab_id = self._tab_order[0]
            if self.close_tab(tab_id):
                closed.append(tab_id)
        return closed

    def close_other_tabs(self, keep_tab_id: int) -> List[int]:
        """Close all tabs except the one with ``keep_tab_id``.

        If ``keep_tab_id`` doesn't exist, closes all tabs.

        Args:
            keep_tab_id: Tab ID to keep open.

        Returns:
            List of closed tab IDs (in order).
        """
        closed = []
        tab_ids_to_close = [tid for tid in self._tab_order if tid != keep_tab_id]
        for tab_id in tab_ids_to_close:
            if self.close_tab(tab_id):
                closed.append(tab_id)
        return closed

    def close_unmodified_tabs(self) -> List[int]:
        """Close all tabs that have no unsaved changes.

        Returns:
            List of closed tab IDs (in order).
        """
        closed = []
        tab_ids_to_close = [
            tid for tid in self._tab_order if not self._tabs[tid].is_modified
        ]
        for tab_id in tab_ids_to_close:
            if self.close_tab(tab_id):
                closed.append(tab_id)
        return closed

    def switch_tab(self, tab_id: int) -> Optional[EditorTab]:
        """Make ``tab_id`` the active tab.

        Args:
            tab_id: Identifier of the tab to activate.

        Returns:
            The now-active :class:`EditorTab`, or ``None`` if no tab
            with that id exists (the active tab is left unchanged in
            that case).
        """
        if tab_id not in self._tabs:
            return None
        self._active_tab_id = tab_id
        return self._tabs[tab_id]

    def move_tab(self, tab_id: int, new_index: int) -> bool:
        """Reorder a tab to a new position.

        Used for drag-to-reorder tab bar functionality.

        Args:
            tab_id: ID of the tab to move.
            new_index: Desired position in the tab order (0-indexed).

        Returns:
            ``True`` if the move was successful, ``False`` if the tab
            doesn't exist or index is out of bounds.
        """
        if tab_id not in self._tabs or not (0 <= new_index < len(self._tab_order)):
            return False

        # Remove from current position and insert at new position.
        self._tab_order.remove(tab_id)
        self._tab_order.insert(new_index, tab_id)
        return True

    # -- Queries ------------------------------------------------------------

    def get_active_tab(self) -> Optional[EditorTab]:
        """Return the currently active tab, or ``None`` if none is open."""
        if self._active_tab_id is None:
            return None
        return self._tabs.get(self._active_tab_id)

    def get_tab(self, tab_id: int) -> Optional[EditorTab]:
        """Return the tab for ``tab_id``, or ``None`` if it doesn't exist."""
        return self._tabs.get(tab_id)

    def find_tab_by_path(self, file_path: str) -> Optional[EditorTab]:
        """Find an open tab by its file path.

        Args:
            file_path: File path to search for.

        Returns:
            The first open tab bound to that path, or ``None`` if none
            exists.
        """
        for tab in self._tabs.values():
            if tab.file_path == file_path:
                return tab
        return None

    def list_tabs(self) -> List[EditorTab]:
        """Return all open tabs in their display order."""
        return [self._tabs[tid] for tid in self._tab_order]

    def is_modified(self, tab_id: int) -> bool:
        """Return whether the given tab has unsaved changes.

        Args:
            tab_id: Identifier of the tab to query.

        Returns:
            ``True``/``False`` for a known tab. Returns ``False`` for an
            unknown ``tab_id`` rather than raising, so GUI polling code
            (e.g. refreshing tab-title asterisks) can call this safely
            even against a stale id.
        """
        tab = self._tabs.get(tab_id)
        return tab.is_modified if tab is not None else False

    def has_unsaved_changes(self) -> bool:
        """Return ``True`` if *any* open tab has unsaved changes.

        Handy for a single check before allowing the application to
        exit, or before a "close all" action.
        """
        return any(tab.is_modified for tab in self._tabs.values())

    def modified_tabs(self) -> List[EditorTab]:
        """Return all tabs that currently have unsaved changes."""
        return [tab for tab in self._tabs.values() if tab.is_modified]

    @staticmethod
    def _basename(path: Optional[str]) -> str:
        """Return just the filename portion of ``path``, OS-agnostically.

        Handles edge cases: trailing separators, empty strings, and ``None``.

        Args:
            path: File path, or ``None``.

        Returns:
            Filename (everything after the last separator), or empty
            string if path is ``None`` or empty.

        Raises:
            ValueError: If path is a separator-only string.
        """
        if path is None or path == "":
            return ""

        # Normalize to forward slashes for consistent parsing.
        normalized = path.replace("\\", "/")

        # Strip trailing slashes (common edge case).
        normalized = normalized.rstrip("/")

        # Check for separator-only paths.
        if normalized in ("", "/"):
            raise ValueError(f"Cannot extract basename from path: {path!r}")

        # Return the part after the last separator, or the whole string
        # if no separator exists.
        return normalized.rsplit("/", 1)[-1]


if __name__ == "__main__":
    # Minimal, self-contained demonstration of the public API using the
    # default in-memory buffer, so this runs with no GUI toolkit needed.
    workspace = EditorWorkspace()

    print("== Opening two tabs ==")
    tab_a = workspace.add_tab(file_path="C:/projects/demo/main.py", content="print('hi')\n")
    tab_b = workspace.add_tab()  # Untitled, empty buffer.
    print(f"tab_a: {tab_a.display_title()!r}, modified={tab_a.is_modified}")
    print(f"tab_b: {tab_b.display_title()!r}, modified={tab_b.is_modified}")

    print("\n== Simulating an edit in tab_b (GUI would call this on keypress) ==")
    tab_b.widget.insert("end", "x = 42\n")
    tab_b.notify_change()
    print(f"tab_b: {tab_b.display_title()!r}, modified={tab_b.is_modified}")

    print("\n== Switching active tab ==")
    workspace.switch_tab(tab_a.tab_id)
    print(f"active tab is now: {workspace.get_active_tab().title!r}")

    print("\n== Simulating a save of tab_b ==")
    tab_b.mark_saved()
    print(f"tab_b: {tab_b.display_title()!r}, modified={tab_b.is_modified}")

    print("\n== Workspace-wide status ==")
    print(f"has_unsaved_changes: {workspace.has_unsaved_changes()}")
    print(f"open tabs: {[t.title for t in workspace.list_tabs()]}")

    print("\n== Closing tab_a ==")
    workspace.close_tab(tab_a.tab_id)
    print(f"open tabs: {[t.title for t in workspace.list_tabs()]}")
    print(f"active tab is now: {workspace.get_active_tab().title!r}")
