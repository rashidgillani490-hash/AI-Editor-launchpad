"""File change detection: has_external_changes."""

from __future__ import annotations

import logging

from ._paths import validate_and_resolve_path
from ._result import FileOperationResult

logger = logging.getLogger(__name__)


def has_external_changes(path: str, last_known_mtime: float) -> FileOperationResult:
    """Check whether `path` has changed on disk since `last_known_mtime`.

    Args:
        path: Path to check.
        last_known_mtime: Previously observed mtime (e.g. from a prior open).

    Returns:
        FileOperationResult with external_changed True/False in
        result.external_changed and current_mtime set. On error, success
        is False and a descriptive message is provided.
    """
    vr = validate_and_resolve_path(path)
    if not vr.success:
        return vr
    resolved = vr.path  # type: ignore
    try:
        import os

        st = os.stat(resolved)
        current = st.st_mtime
        changed = current != last_known_mtime
        logger.debug("has_external_changes(%s): last=%s current=%s changed=%s", resolved, last_known_mtime, current, changed)
        return FileOperationResult(success=True, message="Checked mtime", path=resolved, external_changed=changed, current_mtime=current)
    except FileNotFoundError:
        msg = f"Path not found: {resolved}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="FileNotFoundError")
    except PermissionError:
        msg = f"Permission denied checking: {resolved}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error checking mtime for {resolved}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="OSError")