"""Directory tree listing: list_directory_tree."""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

from ._paths import validate_and_resolve_path
from ._result import FileOperationResult

logger = logging.getLogger(__name__)


def list_directory_tree(root_path: str, *, max_depth: Optional[int] = None, include_hidden: bool = False) -> FileOperationResult:
    """Build a nested directory tree for `root_path`.

    The returned structure is placed in FileOperationResult.tree as a JSON-
    serializable dict. Node format:
        {"name": <basename>, "path": <abs-path>, "is_dir": <bool>, "children": [...]}
    For files, "children" is omitted. If a directory could not be read due
    to permissions, its "children" will be None to indicate the subtree was
    skipped. The result.partial boolean will be True if any subtree was
    skipped.

    Args:
        root_path: Directory to list.
        max_depth: Maximum recursion depth (None = unlimited). Root is depth 0.
        include_hidden: If False (default), skip entries whose names start with '.'.

    Returns:
        FileOperationResult with .tree populated on success. On failure,
        success is False and error_type contains the exception type.
    """
    vr = validate_and_resolve_path(root_path)
    if not vr.success:
        return vr
    resolved_root = vr.path  # type: ignore

    if not os.path.exists(resolved_root):
        msg = f"Path not found: {resolved_root}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_root, error_type="FileNotFoundError")
    if not os.path.isdir(resolved_root):
        msg = f"Not a directory: {resolved_root}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_root, error_type="NotADirectoryError")

    partial = False
    visited: set = set()  # track (st_dev, st_ino) to avoid cycles

    def _node_for_path(path: str, depth: int) -> Dict:
        nonlocal partial, visited
        name = os.path.basename(path) or path
        node: Dict = {"name": name, "path": path, "is_dir": os.path.isdir(path)}
        # Protect against symlink loops by using stat info
        try:
            st = os.stat(path)
            identifier = (st.st_dev, st.st_ino)
            if identifier in visited:
                logger.debug("Detected cycle or repeated inode for %s; skipping children", path)
                node["children"] = None
                partial = True
                return node
            visited.add(identifier)
        except PermissionError:
            logger.debug("Permission denied stating %s while building tree", path)
            node["children"] = None if node["is_dir"] else None
            partial = True
            return node
        except OSError:
            # Stat failed for some other reason; mark as partial
            logger.debug("OS error stating %s while building tree", path, exc_info=True)
            node["children"] = None if node["is_dir"] else None
            partial = True
            return node

        if not node["is_dir"]:
            # File node; no children
            return node

        # Directory node; recurse if depth allows
        if max_depth is not None and depth >= max_depth:
            # Do not descend; present an empty children list to show it's a dir
            node["children"] = []
            return node

        entries: List[Tuple[str, bool]] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    entries.append((entry.path, entry.is_dir(follow_symlinks=False)))
        except PermissionError:
            logger.debug("Permission denied scanning %s", path)
            node["children"] = None
            partial = True
            return node
        except OSError as exc:
            logger.debug("OS error scanning %s: %s", path, exc, exc_info=True)
            node["children"] = None
            partial = True
            return node

        # Sort: directories first, then files; each group alphabetically case-insensitive
        dirs = [p for p, is_dir in entries if is_dir]
        files = [p for p, is_dir in entries if not is_dir]
        dirs.sort(key=lambda p: os.path.basename(p).casefold())
        files.sort(key=lambda p: os.path.basename(p).casefold())

        children: List[Dict] = []
        for child_path in dirs + files:
            child_node = _node_for_path(child_path, depth + 1)
            children.append(child_node)
        node["children"] = children
        return node

    try:
        tree = _node_for_path(resolved_root, 0)
        msg = f"Listed directory tree for {resolved_root}"
        logger.debug("%s (partial=%s)", msg, partial)
        return FileOperationResult(success=True, message=msg, path=resolved_root, tree=tree, partial=partial)
    except PermissionError:
        msg = f"Permission denied listing directory: {resolved_root}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_root, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error listing directory {resolved_root}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved_root, error_type="OSError")