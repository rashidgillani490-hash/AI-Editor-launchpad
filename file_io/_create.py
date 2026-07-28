"""Create helpers: create_file and create_folder."""

from __future__ import annotations

import logging
import os
from typing import Optional

from ._encoding import DEFAULT_ENCODING
from ._paths import validate_and_resolve_path
from ._result import FileOperationResult
from ._write import _write_text

logger = logging.getLogger(__name__)


def create_file(path: str, content: str = "", encoding: str = DEFAULT_ENCODING, *, exist_ok: bool = False) -> FileOperationResult:
    """Create a new file at `path`.

    This function:
        - Validates and resolves the provided path via validate_and_resolve_path().
        - Creates any missing parent directories automatically.
        - If the target already exists:
            * If it's a directory -> fail with error_type "IsADirectoryError".
            * If it's a file and exist_ok is False -> fail with error_type "FileExistsError" (no overwrite).
            * If it's a file and exist_ok is True -> no-op (file is left untouched) and
              a success result is returned. Note: when exist_ok=True we choose the
              "never touch existing content" semantics to make this idempotent and safe.
        - On success (file created), writes `content` using the existing _write_text()
          atomic path to avoid duplicating logic.

    Args:
        path: Destination file path to create.
        content: Optional initial content to write to the new file (default "").
        encoding: Encoding to use when writing.
        exist_ok: If True, do not treat an existing file as an error; return success
                  but do not overwrite existing content. If False (default), attempting
                  to create a file that already exists will fail.

    Returns:
        FileOperationResult describing the outcome. On success, result.path is the
        resolved path to the file.
    """
    vr = validate_and_resolve_path(path)
    if not vr.success:
        return vr
    resolved = vr.path  # type: ignore

    try:
        # If target exists
        if os.path.exists(resolved):
            if os.path.isdir(resolved):
                msg = f"Path exists and is a directory: {resolved}"
                logger.warning(msg)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="IsADirectoryError")
            # It's a file
            if not exist_ok:
                msg = f"File already exists: {resolved}"
                logger.warning(msg)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="FileExistsError")
            # exist_ok True -> leave existing file untouched (idempotent no-op)
            msg = f"File already exists and will not be modified (exist_ok=True): {resolved}"
            logger.debug(msg)
            return FileOperationResult(success=True, message=msg, path=resolved)
        # Ensure parent directories exist
        parent_dir = os.path.dirname(resolved)
        if parent_dir:
            try:
                os.makedirs(parent_dir, exist_ok=True)
                logger.debug("Created parent directories for %s", resolved)
            except PermissionError:
                msg = f"Permission denied creating parent directories for: {resolved}"
                logger.warning(msg)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="PermissionError")
            except OSError as exc:
                msg = f"OS error creating parent directories for {resolved}: {exc}"
                logger.error(msg, exc_info=True)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="OSError")

        # Use existing atomic writer (no backup) to create the file
        write_result = _write_text(resolved, content, encoding=encoding, atomic=True, backup=False)
        if not write_result.success:
            # propagate the failure but ensure path is present
            return FileOperationResult(success=False, message=write_result.message, path=resolved, error_type=write_result.error_type)
        # On success, ensure we return a friendly message
        msg = f"Created file {resolved}."
        logger.debug(msg)
        return FileOperationResult(success=True, message=msg, path=resolved)
    except PermissionError:
        msg = f"Permission denied while creating file: {resolved}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error while creating file {resolved}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="OSError")


def create_folder(path: str, *, exist_ok: bool = False) -> FileOperationResult:
    """Create a directory tree at `path`.

    This will create any missing intermediate directories. Behaviour on existing
    paths:
        - If `path` exists as a file -> fail with error_type "NotADirectoryError".
        - If `path` exists as a directory and exist_ok is False -> fail with
          error_type "FileExistsError".
        - If `path` exists as a directory and exist_ok is True -> success no-op.

    Args:
        path: Directory path to create.
        exist_ok: If True, tolerate an existing directory and return success.

    Returns:
        FileOperationResult describing the outcome.
    """
    vr = validate_and_resolve_path(path)
    if not vr.success:
        return vr
    resolved = vr.path  # type: ignore

    try:
        if os.path.exists(resolved):
            if os.path.isfile(resolved):
                msg = f"Path exists and is a file, cannot create directory: {resolved}"
                logger.warning(msg)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="NotADirectoryError")
            # It's a directory
            if not exist_ok:
                msg = f"Directory already exists: {resolved}"
                logger.warning(msg)
                return FileOperationResult(success=False, message=msg, path=resolved, error_type="FileExistsError")
            msg = f"Directory already exists and will not be modified (exist_ok=True): {resolved}"
            logger.debug(msg)
            return FileOperationResult(success=True, message=msg, path=resolved)
        # Attempt to create directory tree
        try:
            os.makedirs(resolved, exist_ok=exist_ok)
            msg = f"Created directory {resolved}"
            logger.debug(msg)
            return FileOperationResult(success=True, message=msg, path=resolved)
        except PermissionError:
            msg = f"Permission denied creating directory: {resolved}"
            logger.warning(msg)
            return FileOperationResult(success=False, message=msg, path=resolved, error_type="PermissionError")
        except OSError as exc:
            msg = f"OS error creating directory {resolved}: {exc}"
            logger.error(msg, exc_info=True)
            return FileOperationResult(success=False, message=msg, path=resolved, error_type="OSError")
    except OSError as exc:
        msg = f"OS error while inspecting path {resolved}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved, error_type="OSError")