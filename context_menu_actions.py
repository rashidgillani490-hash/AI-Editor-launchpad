"""
context_menu_actions.py — Right-click context menu backend actions.

Provides pure-logic handlers for operations triggered from the
File Explorer context menu.  No Tkinter imports — the GUI calls
these and handles dialogs / popups.

Usage (GUI hooks):
    from context_menu_actions import ContextMenuActions
    from file_tree import FileTree

    tree = FileTree("/project")
    actions = ContextMenuActions(tree)

    # Right-click on a file:
    content = actions.open_file("/project/main.py")
    actions.rename("/project/main.py", "app.py")
    actions.delete("/project/old.py", use_trash=True)

    # Copy/paste:
    actions.cut(["/project/a.py"])
    actions.paste("/project/subdir")

    # Folder operations:
    actions.new_file("/project/src", "utils.py")
    actions.new_folder("/project/src", "helpers")
    actions.reveal_in_file_manager("/project/src")
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional, List, Callable, Any, Tuple
from dataclasses import dataclass, field

from file_tree import (
    FileTree, FileNode, InvalidNameError,
    FileAlreadyExistsError, NodeNotFoundError,
)


# ---------------------------------------------------------------------------
# Clipboard item
# ---------------------------------------------------------------------------

@dataclass
class ClipboardItem:
    paths: List[str]
    operation: str  # "copy" | "cut"


# ---------------------------------------------------------------------------
# ContextMenuActions
# ---------------------------------------------------------------------------

class ContextMenuActions:
    """Backend handlers for context-menu operations on files and folders."""

    def __init__(
        self,
        file_tree: FileTree,
        on_file_open: Optional[Callable[[str, str], None]] = None,
        # (file_path, file_content) -> GUI loads into tab
        on_delete: Optional[Callable[[str, bool], bool]] = None,
        # (path, is_folder) -> return True if GUI confirms deletion
        on_refresh: Optional[Callable[[], None]] = None,
    ) -> None:
        self._tree = file_tree
        self._clipboard: Optional[ClipboardItem] = None
        self.on_file_open = on_file_open
        self.on_delete = on_delete
        self.on_refresh = on_refresh

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_file(self, path: str) -> Tuple[str, str]:
        """Read a file from disk and return (path, content).
        Calls on_file_open callback if set.
        """
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if self.on_file_open:
            self.on_file_open(path, content)
        return path, content

    def rename(self, node_path: str, new_name: str) -> FileNode:
        """Rename a file or folder. Validates name and checks for collisions."""
        return self._tree.rename_node(node_path, new_name)

    def delete(self, node_path: str, use_trash: bool = True) -> bool:
        """Delete a file or folder. If *on_delete* is set, calls it for GUI confirmation.
        Returns True if deleted, False if cancelled by GUI.
        """
        is_dir = os.path.isdir(node_path)
        if self.on_delete:
            confirmed = self.on_delete(node_path, is_dir)
            if not confirmed:
                return False
        self._tree.delete_node(node_path, use_trash=use_trash)
        return True

    def cut(self, paths: List[str]) -> None:
        """Store paths in clipboard for a 'cut' operation."""
        self._clipboard = ClipboardItem(paths=list(paths), operation="cut")

    def copy(self, paths: List[str]) -> None:
        """Store paths in clipboard for a 'copy' operation."""
        self._clipboard = ClipboardItem(paths=list(paths), operation="copy")

    def paste(self, target_dir: str) -> List[str]:
        """Paste clipboard items into *target_dir*. Returns list of new paths."""
        if self._clipboard is None or not self._clipboard.paths:
            return []

        new_paths: List[str] = []
        for src in self._clipboard.paths:
            name = os.path.basename(src)
            dest = self._resolve_paste_dest(target_dir, name)
            if os.path.isdir(src):
                import shutil
                shutil.copytree(src, dest)
            else:
                import shutil
                shutil.copy2(src, dest)
            new_paths.append(dest)

        if self._clipboard.operation == "cut":
            for src in self._clipboard.paths:
                try:
                    self._tree.delete_node(src, use_trash=False)
                except NodeNotFoundError:
                    pass
            self._clipboard = None

        # Refresh parent
        self._tree._invalidate_parent(target_dir)
        if self.on_refresh:
            self.on_refresh()
        return new_paths

    def has_clipboard(self) -> bool:
        return self._clipboard is not None

    def get_clipboard_operation(self) -> Optional[str]:
        if self._clipboard:
            return self._clipboard.operation
        return None

    def duplicate(self, node_path: str) -> FileNode:
        """Duplicate a file or folder alongside the original."""
        return self._tree.duplicate_node(node_path)

    def copy_path_to_clipboard(self, node_path: str) -> str:
        """Return absolute path (GUI can push to OS clipboard)."""
        return self._tree.copy_path(node_path)

    def copy_relative_path_to_clipboard(self, node_path: str) -> str:
        """Return relative path."""
        return self._tree.copy_relative_path(node_path)

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def new_file(self, parent_dir: str, name: str) -> FileNode:
        """Create a new empty file inside *parent_dir*."""
        return self._tree.create_file(parent_dir, name)

    def new_folder(self, parent_dir: str, name: str) -> FileNode:
        """Create a new sub-folder inside *parent_dir*."""
        return self._tree.create_folder(parent_dir, name)

    def reveal_in_file_manager(self, path: str) -> None:
        """Open the OS file manager at *path* (or its parent if it's a file)."""
        target = path if os.path.isdir(path) else os.path.dirname(path)
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(target)
            elif system == "Darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception:
            pass  # best-effort

    def open_in_terminal(self, folder_path: str) -> str:
        """Return the folder path so the GUI/terminal widget can `cd` into it."""
        return folder_path

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Refresh the entire tree."""
        self._tree.load_root()
        if self.on_refresh:
            self.on_refresh()

    def collapse_all(self) -> None:
        """Set all folder nodes to collapsed."""
        for node in self._tree._node_map.values():
            if node.is_dir:
                node.is_expanded = False

    def expand_all(self) -> None:
        """Recursively expand all folders (eagerly loads entire tree)."""
        def _expand(n: FileNode) -> None:
            if n.is_dir:
                self._tree.expand_node(n.path)
                for child in n.children:
                    _expand(child)
        _expand(self._tree.root)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_paste_dest(target_dir: str, name: str) -> str:
        """Find an unused name in *target_dir* for paste (append ' copy' etc.)."""
        dest = os.path.join(target_dir, name)
        if not os.path.exists(dest):
            return dest
        stem, ext = os.path.splitext(name)
        counter = 1
        while True:
            new_name = f"{stem} copy{counter}{ext}"
            dest = os.path.join(target_dir, new_name)
            if not os.path.exists(dest):
                return dest
            counter += 1


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tree = FileTree(tmp)
        tree.load_root()
        actions = ContextMenuActions(tree)

        # Create a file
        new_file = actions.new_file(tmp, "test.py")
        assert os.path.exists(new_file.path)

        # Duplicate
        dup = actions.duplicate(new_file.path)
        print(f"Duplicated to: {dup.path}")

        # Rename
        renamed = actions.rename(dup.path, "test_renamed.py")
        print(f"Renamed: {renamed.path}")

        # Copy / paste
        actions.copy([renamed.path])
        sub = actions.new_folder(tmp, "subdir")
        pasted = actions.paste(sub.path)
        print(f"Pasted: {pasted}")

        # Cleanup
        actions.delete(sub.path, use_trash=False)
        print("Done.")
