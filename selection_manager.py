"""
selection_manager.py — Multi-selection handling for the File Explorer tree.

Tracks selected file/folder paths with support for:
- Single-click selection (replaces current)
- Ctrl+Click (toggle add/remove)
- Shift+Click (range select between anchor and clicked item)
- Exposes the current selection set to the GUI.

Usage (GUI hooks):
    from selection_manager import SelectionManager

    sel = SelectionManager()

    # In your Treeview bindings:
    sel.single_click(path)            # replaces selection with [path]
    sel.toggle(path)                  # Ctrl+Click
    sel.range_select(path, all_items) # Shift+Click (all_items = ordered list)
    selected = sel.get_selection()
    sel.clear()
"""

from __future__ import annotations

from typing import Optional, List, Set, Callable, Any


class SelectionManager:
    """Manages multi-selection state for a tree or list widget."""

    def __init__(
        self,
        on_selection_changed: Optional[Callable[[List[str]], None]] = None,
    ) -> None:
        self._selected: Set[str] = set()         # selected paths
        self._anchor: Optional[str] = None        # last single-clicked path (for Shift+Click anchor)
        self._last_clicked: Optional[str] = None  # most recently clicked path
        self.on_selection_changed = on_selection_changed

    # ------------------------------------------------------------------
    # Selection operations
    # ------------------------------------------------------------------

    def single_click(self, path: str) -> None:
        """Replace current selection with *path* only."""
        self._selected.clear()
        self._selected.add(path)
        self._anchor = path
        self._last_clicked = path
        self._notify()

    def toggle(self, path: str) -> None:
        """Ctrl+Click: add or remove *path* from the selection."""
        if path in self._selected:
            self._selected.discard(path)
        else:
            self._selected.add(path)
        self._last_clicked = path
        self._anchor = path  # reset anchor on Ctrl+Click
        self._notify()

    def range_select(self, path: str, all_items: List[str]) -> None:
        """Shift+Click: select everything between *anchor* and *path* in *all_items*.
        *all_items* must be the full ordered list of selectable items (e.g. tree node paths).
        """
        if self._anchor is None:
            # No anchor yet; treat as single click
            self.single_click(path)
            return

        try:
            idx_anchor = all_items.index(self._anchor)
            idx_clicked = all_items.index(path)
        except ValueError:
            self.single_click(path)
            return

        start = min(idx_anchor, idx_clicked)
        end = max(idx_anchor, idx_clicked)

        self._selected.clear()
        for i in range(start, end + 1):
            self._selected.add(all_items[i])
        self._last_clicked = path
        self._notify()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_selection(self) -> List[str]:
        """Return the currently selected paths as a list."""
        return list(self._selected)

    def get_single_selection(self) -> Optional[str]:
        """If exactly one item is selected, return it; otherwise None."""
        if len(self._selected) == 1:
            return next(iter(self._selected))
        return None

    def is_selected(self, path: str) -> bool:
        return path in self._selected

    def selection_count(self) -> int:
        return len(self._selected)

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def set_selection(self, paths: List[str]) -> None:
        """Replace current selection with an explicit list."""
        self._selected.clear()
        for p in paths:
            self._selected.add(p)
        if paths:
            self._anchor = paths[0]
            self._last_clicked = paths[-1]
        self._notify()

    def clear(self) -> None:
        """Clear all selections."""
        self._selected.clear()
        self._anchor = None
        self._last_clicked = None
        self._notify()

    def select_all(self, all_items: List[str]) -> None:
        """Select every item in *all_items*."""
        self._selected = set(all_items)
        self._last_clicked = all_items[-1] if all_items else None
        self._notify()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _notify(self) -> None:
        if self.on_selection_changed:
            self.on_selection_changed(self.get_selection())


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sel = SelectionManager(on_selection_changed=lambda s: print(f"Selection: {s}"))

    items = ["/a", "/a/b", "/a/c", "/a/d", "/a/e"]

    sel.single_click("/a/b")
    sel.range_select("/a/d", items)
    # set order is non-deterministic — compare as sets
    assert set(sel.get_selection()) == {"/a/b", "/a/c", "/a/d"}
    print("Shift+Click OK")

    sel.toggle("/a/d")
    sel.toggle("/a/e")
    assert set(sel.get_selection()) == {"/a/b", "/a/c", "/a/e"}
    print("Ctrl+Click toggle OK")
