"""
file_tree.py — File Explorer tree data structure and file operations.

Provides a lazy-loading tree model for the IDE's File Explorer sidebar.
Each node represents a file or folder; folders load their children only
when explicitly expanded. Supports refresh, filtering, sorting
(folders-first + alphabetical), and multi-selection.

Usage (GUI hooks):
    from file_tree import FileTree

    tree = FileTree(root_path="/path/to/project")
    tree.load_root()

    # When user expands a folder in the Treeview:
    node = tree.expand_node(node_path)

    # When user collapses:
    tree.collapse_node(node_path)

    # Refresh a node:
    tree.refresh_node(node_path)

    # Search:
    results = tree.search("pattern")

    # Cleanup:
    tree.shutdown()
"""

from __future__ import annotations

import os
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Iterator, List, Dict, Any


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class FileTreeError(Exception):
    """Base exception for file-tree operations."""


class InvalidNameError(FileTreeError):
    """Raised when a file or folder name contains illegal characters."""


class FileAlreadyExistsError(FileTreeError):
    """Raised when attempting to create a file/folder that already exists."""


class NodeNotFoundError(FileTreeError):
    """Raised when a referenced node cannot be found."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileNode:
    """Represents one entry (file or folder) in the file tree."""

    name: str
    path: str                     # absolute path
    is_dir: bool
    extension: str = ""           # e.g. ".py", "" for dirs or no-extension files
    children: List[FileNode] = field(default_factory=list)
    is_expanded: bool = False
    is_loaded: bool = False       # have children been read from disk?
    parent: Optional[FileNode] = field(default=None, repr=False)

    @property
    def is_file(self) -> bool:
        return not self.is_dir

    def __hash__(self) -> int:
        return hash(self.path)


# ---------------------------------------------------------------------------
# FileTree
# ---------------------------------------------------------------------------

class FileTree:
    """Lazy-loading tree model for a root project folder."""

    ILLEGAL_CHARS = frozenset(r'<>:"/\|?*')  # Windows + Unix unsafe

    def __init__(
        self,
        root_path: str,
        on_refresh: Optional[Callable[[], None]] = None,
        on_change: Optional[Callable[[FileNode, str], None]] = None,
        # on_change receives (node, action)  e.g. ("added", "removed", "renamed")
    ) -> None:
        self._root_path = os.path.abspath(root_path)
        self._root: Optional[FileNode] = None
        self._node_map: Dict[str, FileNode] = {}  # path -> node
        self.on_refresh = on_refresh
        self.on_change = on_change

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def validate_name(name: str) -> None:
        """Raise InvalidNameError if *name* is empty or contains illegal chars."""
        if not name or not name.strip():
            raise InvalidNameError("Name cannot be empty.")
        if set(name) & FileTree.ILLEGAL_CHARS:
            raise InvalidNameError(
                f"Name contains illegal characters: {name!r}"
            )

    @staticmethod
    def _sort_children(children: List[FileNode]) -> List[FileNode]:
        """Return a sorted copy: folders first, then alphabetical (case-insensitive)."""
        dirs = [c for c in children if c.is_dir]
        files = [c for c in children if c.is_file]
        dirs.sort(key=lambda c: c.name.lower())
        files.sort(key=lambda c: c.name.lower())
        return dirs + files

    @staticmethod
    def _get_extension(name: str) -> str:
        if not name:
            return ""
        _, ext = os.path.splitext(name)
        return ext.lower()

    # -- loading / building --------------------------------------------------

    def load_root(self) -> FileNode:
        """(Re-)scan the root folder and return the root node."""
        root = FileNode(
            name=os.path.basename(self._root_path) or self._root_path,
            path=self._root_path,
            is_dir=True,
        )
        self._root = root
        self._node_map.clear()
        self._node_map[root.path] = root
        self._load_children(root)
        return root

    def _load_children(self, node: FileNode) -> None:
        """Read a directory's contents from disk and populate *node.children*."""
        if not node.is_dir or node.is_loaded:
            return
        try:
            entries = os.listdir(node.path)
        except PermissionError:
            entries = []

        children: List[FileNode] = []
        for name in entries:
            # skip hidden unless explicitly wanted (configurable later)
            full = os.path.join(node.path, name)
            is_dir = os.path.isdir(full)
            child = FileNode(
                name=name,
                path=full,
                is_dir=is_dir,
                extension="" if is_dir else self._get_extension(name),
            )
            child.parent = node
            children.append(child)
            self._node_map[full] = child

        node.children = self._sort_children(children)
        node.is_loaded = True

    def expand_node(self, node_path: str) -> FileNode:
        """Expand a folder node — lazy-loads children if needed. Returns the node."""
        node = self._node_map.get(node_path)
        if node is None:
            raise NodeNotFoundError(f"No node at path: {node_path!r}")
        if node.is_dir:
            self._load_children(node)
            node.is_expanded = True
        return node

    def collapse_node(self, node_path: str) -> FileNode:
        """Collapse a folder node. Returns the node."""
        node = self._node_map.get(node_path)
        if node is None:
            raise NodeNotFoundError(f"No node at path: {node_path!r}")
        node.is_expanded = False
        return node

    # -- refresh -------------------------------------------------------------

    def refresh_node(self, node_path: str | None = None) -> FileNode:
        """Re-read a node's children from disk. If *node_path* is None, refresh root."""
        path = node_path or self._root_path
        node = self._node_map.get(path)
        if node is None:
            raise NodeNotFoundError(f"No node at path: {path!r}")

        if node.is_dir:
            # Preserve expand state before wiping children
            was_expanded = node.is_expanded
            node.is_loaded = False
            node.children.clear()
            self._load_children(node)
            node.is_expanded = was_expanded

        if self.on_refresh:
            self.on_refresh()
        if self.on_change:
            self.on_change(node, "refreshed")
        return node

    # -- search / filter -----------------------------------------------------

    def search(self, pattern: str) -> List[FileNode]:
        """Return all nodes (files + folders) whose name contains *pattern*.
        Loads the entire tree eagerly for accurate results under all
        currently-known folders.
        """
        results: List[FileNode] = []
        self._ensure_loaded_all(self.root)
        for node in self._node_map.values():
            if fnmatch.fnmatch(node.name.lower(), f"*{pattern.lower()}*"):
                results.append(node)
        return results

    def _ensure_loaded_all(self, node: FileNode) -> None:
        """Recursively load *node* and all its descendants."""
        if node.is_dir and not node.is_loaded:
            self._load_children(node)
        for child in node.children:
            self._ensure_loaded_all(child)

    # -- properties ----------------------------------------------------------

    @property
    def root(self) -> FileNode:
        """Return the root node (lazy-loads if needed)."""
        if self._root is None:
            return self.load_root()
        return self._root

    @property
    def root_path(self) -> str:
        return self._root_path

    def get_node(self, path: str) -> Optional[FileNode]:
        """Look up a node by absolute path."""
        return self._node_map.get(path)

    def get_parent(self, path: str) -> Optional[FileNode]:
        """Return the parent of the given path, if any."""
        parent_path = os.path.dirname(path)
        if parent_path == path:
            return None
        return self._node_map.get(parent_path)

    def shutdown(self) -> None:
        """Clean up any resources."""
        self._node_map.clear()
        self._root = None

    # -- CRUD operations -----------------------------------------------------

    def _emit_change(self, node: FileNode, action: str) -> None:
        if self.on_change:
            self.on_change(node, action)

    def create_file(self, parent_dir: str, name: str) -> FileNode:
        """Create a new empty file inside *parent_dir*. Returns the new node."""
        self.validate_name(name)
        full = os.path.join(parent_dir, name)
        if os.path.exists(full):
            raise FileAlreadyExistsError(f"Already exists: {full!r}")
        Path(full).touch()
        self._invalidate_parent(full)
        new_node = self.get_node(full) or FileNode(
            name=name, path=full, is_dir=False,
            extension=self._get_extension(name),
        )
        self._node_map[full] = new_node
        self._emit_change(new_node, "added")
        return new_node

    def create_folder(self, parent_dir: str, name: str) -> FileNode:
        """Create a new sub-folder inside *parent_dir*. Returns the new node."""
        self.validate_name(name)
        full = os.path.join(parent_dir, name)
        if os.path.exists(full):
            raise FileAlreadyExistsError(f"Already exists: {full!r}")
        os.makedirs(full, exist_ok=False)
        self._invalidate_parent(full)
        new_node = self.get_node(full) or FileNode(
            name=name, path=full, is_dir=True,
        )
        self._node_map[full] = new_node
        self._emit_change(new_node, "added")
        return new_node

    def rename_node(self, node_path: str, new_name: str) -> FileNode:
        """Rename a file/folder. Returns the renamed node."""
        self.validate_name(new_name)
        old_dir = os.path.dirname(node_path)
        new_path = os.path.join(old_dir, new_name)
        if os.path.exists(new_path):
            raise FileAlreadyExistsError(f"Target exists: {new_path!r}")
        os.rename(node_path, new_path)
        # Update node map
        old_node = self._node_map.pop(node_path, None)
        new_node = FileNode(
            name=new_name,
            path=new_path,
            is_dir=os.path.isdir(new_path),
            extension="" if os.path.isdir(new_path) else self._get_extension(new_name),
        )
        self._node_map[new_path] = new_node
        self._invalidate_parent(new_path)
        self._emit_change(new_node, "renamed")
        return new_node

    def delete_node(self, node_path: str, use_trash: bool = True) -> None:
        """Delete a file or folder (recursively if folder).
        If *use_trash* is True, attempts to use send2trash for safe deletion.
        """
        if not os.path.exists(node_path):
            raise NodeNotFoundError(f"Path does not exist: {node_path!r}")

        if use_trash:
            try:
                import send2trash
                send2trash.send2trash(node_path)
            except ImportError:
                self._permanent_delete(node_path)
        else:
            self._permanent_delete(node_path)

        # Clean up node map recursively
        to_remove = [p for p in self._node_map if p.startswith(node_path)]
        for p in to_remove:
            self._node_map.pop(p, None)
        self._invalidate_parent(node_path)
        self._emit_change(
            FileNode(name="", path=node_path, is_dir=False), "removed"
        )

    @staticmethod
    def _permanent_delete(node_path: str) -> None:
        if os.path.isdir(node_path):
            import shutil
            shutil.rmtree(node_path)
        else:
            os.unlink(node_path)

    def duplicate_node(self, node_path: str) -> FileNode:
        """Duplicate a file or folder. Returns the new node."""
        import shutil
        base = node_path.rstrip(os.sep)
        parent = os.path.dirname(base)
        name = os.path.basename(base)
        stem, ext = os.path.splitext(name)
        # Find unused name
        counter = 1
        while True:
            new_name = f"{stem} copy{counter}{ext}"
            new_path = os.path.join(parent, new_name)
            if not os.path.exists(new_path):
                break
            counter += 1
        if os.path.isdir(node_path):
            shutil.copytree(node_path, new_path)
        else:
            shutil.copy2(node_path, new_path)
        self._invalidate_parent(new_path)
        new_node = self.get_node(new_path)
        if new_node is None:
            new_node = FileNode(
                name=new_name, path=new_path,
                is_dir=os.path.isdir(new_path),
                extension="" if os.path.isdir(new_path) else self._get_extension(new_name),
            )
        self._node_map[new_path] = new_node
        self._emit_change(new_node, "added")
        return new_node

    def copy_path(self, node_path: str) -> str:
        """Return the absolute path (for the clipboard)."""
        return node_path

    def copy_relative_path(self, node_path: str) -> str:
        """Return the path relative to the project root."""
        return os.path.relpath(node_path, self._root_path)

    def _invalidate_parent(self, child_path: str) -> None:
        """Mark parent directory as not-loaded so it re-reads next expand."""
        parent_path = os.path.dirname(child_path)
        parent = self._node_map.get(parent_path)
        if parent and parent.is_dir:
            parent.is_loaded = False
            parent.children.clear()


# ---------------------------------------------------------------------------
# Example / quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # Create sample structure
        os.makedirs(os.path.join(tmp, "src", "utils"))
        Path(os.path.join(tmp, "src", "main.py")).touch()
        Path(os.path.join(tmp, "src", "utils", "__init__.py")).touch()
        Path(os.path.join(tmp, "README.md")).touch()

        tree = FileTree(tmp)

        # Load root
        root = tree.load_root()
        print(f"Root: {root.name} ({len(root.children)} children)")

        # Expand src
        src = tree.expand_node(os.path.join(tmp, "src"))
        print(f"src expanded, children: {[c.name for c in src.children]}")

        # Search
        results = tree.search("readme")
        print(f"Search for 'readme': {[r.path for r in results]}")

        # Create a file
        new_file = tree.create_file(os.path.join(tmp, "src"), "new_module.py")
        print(f"Created: {new_file.path}")

        # Rename
        renamed = tree.rename_node(new_file.path, "renamed_module.py")
        print(f"Renamed to: {renamed.path}")

        tree.shutdown()
        print("All ok.")
