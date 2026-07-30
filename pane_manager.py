"""
pane_manager.py — Split-screen / multi-pane editor backend.

Manages an arbitrarily-nested tree of editor panes. Each leaf pane
holds its own TabsManager instance (reuses tabs_manager.py). Supports
horizontal and vertical splits, moving tabs between panes, and tracking
the currently focused pane.

Usage (GUI hooks):
    from pane_manager import PaneManager

    pm = PaneManager(on_pane_change=gui_rebuild_panes)

    # Splitting
    pane_a = pm.root_pane
    pane_b = pm.split(pane_a.pane_id, "vertical")   # or "horizontal"

    # Each pane has its own tabs
    tab = pane_a.tabs.open_file("/foo.py")
    tab2 = pane_b.tabs.open_file("/bar.py")

    # Focus tracking
    pm.focus_pane(pane_a.pane_id)
    active = pm.active_pane   # Pane or None
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict, Any, Literal

from tabs_manager import TabsManager, TabInfo


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SplitDirection = Literal["horizontal", "vertical"]


@dataclass
class Pane:
    """A node in the split-pane tree.  Leaf panes hold tab managers."""
    pane_id: str
    parent: Optional[Pane] = field(default=None, repr=False)
    # For split nodes
    direction: Optional[SplitDirection] = None
    child_a: Optional[Pane] = field(default=None, repr=False)
    child_b: Optional[Pane] = field(default=None, repr=False)
    # For leaf nodes
    tabs: Optional[TabsManager] = None
    # Weight / size ratio (0.0..1.0)
    weight: float = 0.5

    @property
    def is_split(self) -> bool:
        return self.child_a is not None

    @property
    def is_leaf(self) -> bool:
        return not self.is_split


# ---------------------------------------------------------------------------
# PaneManager
# ---------------------------------------------------------------------------

class PaneManager:
    """Manages the pane layout tree for the editor area."""

    def __init__(
        self,
        on_structure_change: Optional[Callable[[], None]] = None,
        on_active_pane_change: Optional[Callable[[Optional[str]], None]] = None,
    ) -> None:
        self._root: Pane = Pane(
            pane_id=str(uuid.uuid4()),
            tabs=TabsManager(),
        )
        self._active_pane_id: Optional[str] = self._root.pane_id
        self._pane_map: Dict[str, Pane] = {self._root.pane_id: self._root}

        self.on_structure_change = on_structure_change
        self.on_active_pane_change = on_active_pane_change

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root_pane(self) -> Pane:
        return self._root

    @property
    def active_pane(self) -> Optional[Pane]:
        if self._active_pane_id:
            return self._pane_map.get(self._active_pane_id)
        return None

    @property
    def active_pane_id(self) -> Optional[str]:
        return self._active_pane_id

    def get_pane(self, pane_id: str) -> Optional[Pane]:
        return self._pane_map.get(pane_id)

    def get_all_leaf_panes(self) -> List[Pane]:
        """Return every leaf pane (those holding tabs) in the tree."""
        leaves: List[Pane] = []

        def _walk(p: Pane) -> None:
            if p.is_leaf:
                leaves.append(p)
            else:
                if p.child_a:
                    _walk(p.child_a)
                if p.child_b:
                    _walk(p.child_b)

        _walk(self._root)
        return leaves

    def get_active_tabs(self) -> Optional[TabsManager]:
        """Convenience: tabs manager of the currently focused pane."""
        pane = self.active_pane
        return pane.tabs if pane else None

    def get_active_tab(self) -> Optional[TabInfo]:
        """Convenience: the active tab in the focused pane."""
        tabs = self.get_active_tabs()
        return tabs.active_tab if tabs else None

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def focus_pane(self, pane_id: str) -> None:
        """Set the focused pane for keyboard shortcuts / Run button."""
        if pane_id in self._pane_map:
            self._active_pane_id = pane_id
            if self.on_active_pane_change:
                self.on_active_pane_change(pane_id)

    # ------------------------------------------------------------------
    # Split / unsplit
    # ------------------------------------------------------------------

    def split(self, pane_id: str, direction: SplitDirection) -> Pane:
        """Split *pane_id* into two panes.
        The original pane becomes child_a; a new pane is child_b.
        Returns the new child pane.
        """
        original = self._pane_map.get(pane_id)
        if original is None:
            raise ValueError(f"No pane: {pane_id!r}")

        # Create a new split container
        split_pane = Pane(
            pane_id=str(uuid.uuid4()),
            direction=direction,
            child_a=original,
            child_b=Pane(
                pane_id=str(uuid.uuid4()),
                tabs=TabsManager(),
            ),
        )
        self._pane_map[split_pane.pane_id] = split_pane

        # Reparent
        if original.parent is not None:
            parent = original.parent
            if parent.child_a is original:
                parent.child_a = split_pane
            else:
                parent.child_b = split_pane
        else:
            self._root = split_pane

        original.parent = split_pane
        split_pane.child_b.parent = split_pane  # type: ignore[union-attr]
        self._pane_map[split_pane.child_b.pane_id] = split_pane.child_b  # type: ignore[union-attr]

        if self.on_structure_change:
            self.on_structure_change()
        return split_pane.child_b  # type: ignore[return-value]

    def close_pane(self, pane_id: str) -> List[TabInfo]:
        """Close a pane and re-parent its sibling.
        Returns list of unsaved tabs in the closed pane so the GUI can prompt.
        """
        pane = self._pane_map.get(pane_id)
        if pane is None or pane is self._root:
            return []

        unsaved = self._get_unsaved_in_pane(pane)

        parent = pane.parent
        if parent is None:
            return unsaved

        # Determine sibling
        sibling = parent.child_b if parent.child_a is pane else parent.child_a
        if sibling is None:
            return unsaved

        # Promote sibling to grandparent's child
        grandparent = parent.parent
        if grandparent is None:
            # parent is root
            sibling.parent = None
            self._root = sibling
        else:
            sibling.parent = grandparent
            if grandparent.child_a is parent:
                grandparent.child_a = sibling
            else:
                grandparent.child_b = sibling

        # Remove from map
        self._pane_map.pop(pane.pane_id, None)
        self._pane_map.pop(parent.pane_id, None)

        # Fix focus
        if self._active_pane_id == pane.pane_id:
            self._active_pane_id = sibling.pane_id

        if self.on_structure_change:
            self.on_structure_change()
        if self.on_active_pane_change:
            self.on_active_pane_change(self._active_pane_id)

        return unsaved

    def close_all_panes_except(self, keep_pane_id: str) -> List[TabInfo]:
        """Close every leaf pane except *keep_pane_id*.
        Returns all unsaved tabs from closed panes.
        """
        all_unsaved: List[TabInfo] = []
        leaves = self.get_all_leaf_panes()
        for leaf in leaves:
            if leaf.pane_id != keep_pane_id:
                unsaved = self._get_unsaved_in_pane(leaf)
                all_unsaved.extend(unsaved)
                self.close_pane(leaf.pane_id)
        return all_unsaved

    def equalize_sizes(self, pane: Optional[Pane] = None) -> None:
        """Reset all split weights to equal proportions."""
        p = pane or self._root

        def _walk(node: Pane) -> None:
            if node.is_split:
                node.weight = 0.5
                if node.child_a:
                    _walk(node.child_a)
                if node.child_b:
                    _walk(node.child_b)

        _walk(p)
        if self.on_structure_change:
            self.on_structure_change()

    def reset_layout(self) -> None:
        """Destroy all splits; return to a single pane."""
        self._root = Pane(
            pane_id=str(uuid.uuid4()),
            tabs=TabsManager(),
        )
        self._active_pane_id = self._root.pane_id
        self._pane_map = {self._root.pane_id: self._root}
        if self.on_structure_change:
            self.on_structure_change()
        if self.on_active_pane_change:
            self.on_active_pane_change(self._active_pane_id)

    # ------------------------------------------------------------------
    # Move tabs between panes
    # ------------------------------------------------------------------

    def move_tab(self, tab_id: str, from_pane_id: str, to_pane_id: str, index: int = -1) -> bool:
        """Move a tab from one pane to another."""
        from_pane = self._pane_map.get(from_pane_id)
        to_pane = self._pane_map.get(to_pane_id)
        if from_pane is None or to_pane is None:
            return False
        if from_pane.tabs is None or to_pane.tabs is None:
            return False

        tab = from_pane.tabs.get_tab(tab_id)
        if tab is None:
            return False

        # Remove from source
        from_pane.tabs.close_tab(tab_id, force=True)

        # Add to destination
        new_tab = to_pane.tabs.open_file(tab.path, content=tab.content)
        if tab.is_modified:
            to_pane.tabs.mark_dirty(new_tab.tab_id, True)

        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_unsaved_in_pane(pane: Pane) -> List[TabInfo]:
        """Return unsaved tabs in *pane* and all its descendants."""
        unsaved: List[TabInfo] = []

        def _collect(p: Pane) -> None:
            if p.is_leaf and p.tabs:
                for t in p.tabs.tabs:
                    if t.is_modified:
                        unsaved.append(t)
            else:
                if p.child_a:
                    _collect(p.child_a)
                if p.child_b:
                    _collect(p.child_b)

        _collect(pane)
        return unsaved

    # ------------------------------------------------------------------
    # Serialization (optional)
    # ------------------------------------------------------------------

    def get_layout_data(self) -> dict:
        """Serialize the pane tree to a dict for session persistence."""
        def _serialize(pane: Pane) -> dict:
            if pane.is_leaf:
                return {
                    "type": "leaf",
                    "id": pane.pane_id,
                    "weight": pane.weight,
                }
            return {
                "type": "split",
                "id": pane.pane_id,
                "direction": pane.direction,
                "weight": pane.weight,
                "child_a": _serialize(pane.child_a),  # type: ignore[arg-type]
                "child_b": _serialize(pane.child_b),  # type: ignore[arg-type]
            }
        return _serialize(self._root)


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pm = PaneManager()

    print(f"Root: {pm.root_pane.pane_id[:8]}...")

    # Split
    right = pm.split(pm.root_pane.pane_id, "vertical")
    print(f"Split created right pane: {right.pane_id[:8]}...")

    bottom_right = pm.split(right.pane_id, "horizontal")
    print(f"Split bottom-right: {bottom_right.pane_id[:8]}...")

    leaves = pm.get_all_leaf_panes()
    print(f"Leaf panes: {len(leaves)}")

    # Open a file in left pane
    pm.focus_pane(pm.root_pane.pane_id)
    active = pm.active_pane
    if active and active.tabs:
        tab = active.tabs.open_file("/dummy/test.py", "print('hi')")

    print(f"Active pane: {pm.active_pane_id[:8] if pm.active_pane_id else None}...")
    print(f"Layout: {pm.get_layout_data()}")
