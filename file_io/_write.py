"""Writing helpers: _write_text, save_file, save_file_as."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Optional

from ._encoding import DEFAULT_ENCODING
from ._paths import validate_and_resolve_path
from ._result import FileOperationResult

logger = logging.getLogger(__name__)


def _write_text(path: str, content: str, encoding: str = DEFAULT_ENCODING, *,
                atomic: bool = True, backup: bool = False, backup_suffix: str = ".bak") -> FileOperationResult:
    """Write ``content`` to ``path``, overwriting any existing file.

    Args:
        path: Destination filesystem path.
        content: Full text to write.
        encoding: Text encoding to encode the file with.
        atomic: If True (default), perform atomic write via temp file + os.replace.
        backup: If True, create a backup copy of the existing file before overwrite.
        backup_suffix: Suffix appended to original filename for backup copy.

    Returns:
        A :class:`FileOperationResult` describing the outcome.
    """
    vr = validate_and_resolve_path(path)
    if not vr.success:
        return vr
    resolved_path = vr.path  # type: ignore

    parent_dir = os.path.dirname(resolved_path)
    if parent_dir and not os.path.isdir(parent_dir):
        msg = f"Directory does not exist: {parent_dir}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="FileNotFoundError")

    # If backup requested and file exists, make a copy first (opt-in)
    backup_path = None
    try:
        if backup and os.path.exists(resolved_path):
            backup_path = resolved_path + backup_suffix
            # Copy with metadata preserved
            shutil.copy2(resolved_path, backup_path)
            logger.debug("Created backup %s for %s", backup_path, resolved_path)

        # Write to a temp file in same dir to ensure atomic replace on same filesystem
        if atomic:
            fd = None
            tmp_path = None
            try:
                # NamedTemporaryFile(delete=False) is used to get a stable path we can replace.
                with tempfile.NamedTemporaryFile("wb", delete=False, dir=parent_dir) as tmp:
                    tmp_path = tmp.name
                    # Encode content with requested encoding. If encoding fails, surface error.
                    try:
                        encoded = content.encode(encoding)
                        tmp.write(encoded)
                    except UnicodeEncodeError as exc:
                        # Surface encoding error in a friendly way
                        msg = f"Could not encode content as {encoding}: {exc}"
                        logger.error(msg, exc_info=True)
                        # Clean up temp file
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="UnicodeEncodeError")
                    # Ensure data hits disk
                    tmp.flush()
                    os.fsync(tmp.fileno())
                # Replace existing file atomically
                os.replace(tmp_path, resolved_path)
                logger.debug("Atomically replaced %s with %s", resolved_path, tmp_path)
            finally:
                # If something failed and temp file remains, try to remove it
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    logger.debug("Couldn't remove temporary file %s", tmp_path, exc_info=True)
        else:
            # Non-atomic write path; open and overwrite
            with open(resolved_path, "w", encoding=encoding) as fh:
                fh.write(content)
            logger.debug("Wrote %s non-atomically with encoding=%s", resolved_path, encoding)
    except FileNotFoundError:
        msg = f"Path not found while saving: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="FileNotFoundError")
    except PermissionError:
        msg = f"Permission denied while saving: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error while saving {resolved_path}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="OSError")

    msg = f"Saved {resolved_path} successfully."
    logger.debug(msg)
    return FileOperationResult(success=True, message=msg, path=resolved_path, backup_path=backup_path)


def save_file(path: str, content: str, encoding: str = DEFAULT_ENCODING, *, backup: bool = False,
              backup_suffix: str = ".bak") -> FileOperationResult:
    """Save ``content`` to an already-known file path.

    Contract:
        * Never raises. All failures are reported via the returned
          ``FileOperationResult.success == False``.
        * Intended for the "Save" action, where the target path is
          already associated with the tab (i.e. the file was previously
          opened or already saved once).

    New optional keyword-only args:
        backup: If True, create a backup copy before overwriting.
        backup_suffix: Suffix to use for backup file (default ".bak").

    Args:
        path: Destination path to overwrite.
        content: Current text buffer contents to persist.
        encoding: Text encoding to use when writing the file.

    Returns:
        A :class:`FileOperationResult` describing the outcome.
    """
    return _write_text(path, content, encoding=encoding, atomic=True, backup=backup, backup_suffix=backup_suffix)


def save_file_as(new_path: str, content: str, encoding: str = DEFAULT_ENCODING, *, backup: bool = False,
                 backup_suffix: str = ".bak") -> FileOperationResult:
    """Save ``content`` to a new file path ("Save As" semantics).

    Contract:
        * Never raises. All failures are reported via the returned
          ``FileOperationResult.success == False``.
        * Functionally this performs the same write as :func:`save_file`;
          it exists as a distinct, explicitly-named entry point so GUI
          code that wires up "Save As..." (which first prompts the user
          for a new path via a file dialog) has a call target whose name
          matches its intent, and so the tab's associated path can be
          updated to ``new_path`` on success.

    New optional keyword-only args:
        backup: If True, create a backup copy before overwriting.
        backup_suffix: Suffix to use for backup file (default ".bak").

    Args:
        new_path: The newly chosen destination path.
        content: Current text buffer contents to persist.
        encoding: Text encoding to use when writing the file.

    Returns:
        A :class:`FileOperationResult` describing the outcome. On
        success, callers should update the owning tab's tracked file
        path to ``result.path``.
    """
    return _write_text(new_path, content, encoding=encoding, atomic=True, backup=backup, backup_suffix=backup_suffix)