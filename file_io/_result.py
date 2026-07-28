"""FileOperationResult dataclass for file_io package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class FileOperationResult:
    """Outcome of a single file I/O transaction.

    Attributes:
        success: ``True`` if the operation completed without error.
        message: Human-readable status message, safe to show in the GUI
            (e.g. in a status bar or a message box).
        path: The absolute/relative path the operation was performed
            against, when known.
        content: The text content read from disk. Only populated by
            ``open_file`` on success; ``None`` otherwise.
        error_type: The exception class name (e.g. "PermissionError"),
            included so calling code can branch on failure category
            without re-parsing ``message`` strings.

    Extended optional fields (non-breaking additions):
        large_file: True if the read used streaming/chunking because the
            file exceeded the configured threshold.
        used_encoding: The encoding that was actually used to decode the file.
        backup_path: Path to a backup copy (if a backup was made before an overwrite).
        external_changed: Boolean used by has_external_changes helper to
            indicate whether the file changed since a supplied mtime.
        current_mtime: The mtime observed during operations that check it.
        tree: Optional nested directory tree returned by list_directory_tree.
            The structure is a plain JSON-serializable dict, e.g.:
                {"name": "root", "path": "/abs/path", "is_dir": True, "children": [...]}
            For files, "children" is omitted. For directories that could not be
            read due to permissions the "children" key will be present with value
            None to indicate a skipped subtree.
        partial: Optional boolean indicating whether the tree was partially
            generated because one or more subtrees were skipped (e.g. permission errors).
    """

    success: bool
    message: str
    path: Optional[str] = None
    content: Optional[str] = None
    error_type: Optional[str] = None

    # Extended (optional)
    large_file: bool = False
    used_encoding: Optional[str] = None
    backup_path: Optional[str] = None
    external_changed: Optional[bool] = None
    current_mtime: Optional[float] = None

    # Directory listing extensions
    tree: Optional[Dict] = None
    partial: Optional[bool] = None