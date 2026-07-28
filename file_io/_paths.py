"""Path validation and resolution helpers."""

from __future__ import annotations

import logging
import os
from typing import Optional

from ._result import FileOperationResult

logger = logging.getLogger(__name__)


def _is_valid_path_string(path: str) -> bool:
    """Quick sanity checks for obviously invalid path inputs."""
    if path is None:
        return False
    if not isinstance(path, str):
        return False
    if path.strip() == "":
        return False
    if "\x00" in path:  # null byte injection
        return False
    return True


def validate_and_resolve_path(path: str) -> FileOperationResult:
    """Normalize and resolve a path, rejecting clearly invalid inputs.

    Returns:
        FileOperationResult.success == True with result.path set to the
        resolved absolute path on success.
    """
    if not _is_valid_path_string(path):
        msg = "Invalid path string."
        logger.warning(msg + " Path was: %r", path)
        return FileOperationResult(success=False, message=msg, path=path, error_type="ValueError")

    try:
        # Use normpath + abspath + realpath to collapse redundant pieces and
        # resolve symlinks where possible.
        normalized = os.path.normpath(path)
        abs_path = os.path.abspath(normalized)
        real = os.path.realpath(abs_path)
        logger.debug("Validated path %r -> %r", path, real)
        return FileOperationResult(success=True, message="Path normalized", path=real)
    except Exception as exc:
        msg = f"Error resolving path {path}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=path, error_type=type(exc).__name__)