"""Reading helpers: _read_text and open_file."""

from __future__ import annotations

import codecs
import logging
import os
from typing import Optional

from ._encoding import DEFAULT_ENCODING, LARGE_FILE_THRESHOLD, _READ_CHUNK_SIZE, _detect_encoding
from ._paths import validate_and_resolve_path
from ._result import FileOperationResult

logger = logging.getLogger(__name__)


def _read_text(path: str, encoding: str = DEFAULT_ENCODING, *,
               large_file_threshold: int = LARGE_FILE_THRESHOLD) -> FileOperationResult:
    """Read the full text contents of ``path``.

    This is the single choke point for read errors so ``open_file`` stays
    a thin, readable wrapper.

    Args:
        path: Filesystem path to read.
        encoding: Text encoding to decode the file with.
        large_file_threshold: If the file is bigger than this many bytes,
            read it in streaming chunks (but the returned content will
            still contain the full text).

    Returns:
        A :class:`FileOperationResult`. On success, ``content`` holds the
        file text. On failure, ``success`` is ``False`` and ``message``
        describes what went wrong.
    """
    vr = validate_and_resolve_path(path)
    if not vr.success:
        return vr

    resolved_path = vr.path  # type: ignore

    try:
        st = os.stat(resolved_path)
        file_size = st.st_size
        mtime = st.st_mtime
    except FileNotFoundError:
        msg = f"File not found: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="FileNotFoundError")
    except PermissionError:
        msg = f"Permission denied while accessing: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error while accessing {resolved_path}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="OSError")

    # If file is small, attempt a straightforward open/decode path first.
    large_file_mode = file_size > large_file_threshold

    try:
        if not large_file_mode:
            # Try the faster text-mode open first with the given encoding.
            try:
                with open(resolved_path, "r", encoding=encoding) as fh:
                    text = fh.read()
                logger.debug("Opened %s (size=%d) with encoding=%s", resolved_path, file_size, encoding)
                return FileOperationResult(
                    success=True,
                    message=f"Opened {resolved_path} successfully.",
                    path=resolved_path,
                    content=text,
                    large_file=False,
                    used_encoding=encoding,
                    current_mtime=mtime,
                )
            except UnicodeDecodeError as exc:
                # fall through to detection/fallback below
                logger.debug("UnicodeDecodeError reading %s with %s: %s", resolved_path, encoding, exc)
        # Large files or decode issues: read in binary and decode incrementally.
        with open(resolved_path, "rb") as fh:
            # Read a sample for detection
            sample = fh.read(16384)
            detected = _detect_encoding(sample, preferred=encoding)
            chosen_encoding = detected or encoding
            if chosen_encoding != encoding:
                logger.debug("Initial encoding %r for %s differs from requested %r", chosen_encoding, resolved_path, encoding)
            # Prepare decoder
            try:
                decoder_class = codecs.getincrementaldecoder(chosen_encoding)
                decoder = decoder_class(errors="strict")  # will raise on malformed input
            except Exception:
                # If chosen encoding isn't supported by codecs, fall back to latin-1
                logger.debug("Couldn't get incremental decoder for %r; falling back to latin-1", chosen_encoding, exc_info=True)
                chosen_encoding = "latin-1"
                decoder_class = codecs.getincrementaldecoder(chosen_encoding)
                decoder = decoder_class(errors="strict")

            # Decode the sample then stream the rest
            pieces = []
            try:
                pieces.append(decoder.decode(sample))
                if large_file_mode:
                    # We have already consumed sample; continue reading in chunks.
                    while True:
                        chunk = fh.read(_READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        pieces.append(decoder.decode(chunk))
                else:
                    # For non-large files we only read the rest once
                    rest = fh.read()
                    if rest:
                        pieces.append(decoder.decode(rest))
                # Finalize
                pieces.append(decoder.decode(b"", final=True))
                text = "".join(pieces)
                logger.debug("Stream-read %s (size=%d) using encoding=%s", resolved_path, file_size, chosen_encoding)
                return FileOperationResult(
                    success=True,
                    message=f"Opened {resolved_path} successfully.",
                    path=resolved_path,
                    content=text,
                    large_file=large_file_mode,
                    used_encoding=chosen_encoding,
                    current_mtime=mtime,
                )
            except UnicodeDecodeError as exc:
                # If decoding failed with the chosen encoding, try detectors or latin-1
                logger.debug("Incremental decode failed (%s); attempting fallback", exc, exc_info=True)
                # Try detector on the sample again more exhaustively
                alt_encoding = None
                if _detect_encoding is not None:
                    alt_encoding = _detect_encoding(sample)
                if not alt_encoding:
                    # as a last resort, use latin-1 which will always succeed
                    alt_encoding = "latin-1"
                logger.debug("Fallback encoding chosen: %r", alt_encoding)
                # Rewind and decode in a simpler way with the fallback
                fh.seek(0)
                raw = fh.read()
                try:
                    text = raw.decode(alt_encoding)
                    return FileOperationResult(
                        success=True,
                        message=f"Opened {resolved_path} (fallback encoding {alt_encoding}) successfully.",
                        path=resolved_path,
                        content=text,
                        large_file=large_file_mode,
                        used_encoding=alt_encoding,
                        current_mtime=mtime,
                    )
                except Exception as exc2:
                    msg = f"Could not decode {resolved_path}: {exc2}"
                    logger.error(msg, exc_info=True)
                    return FileOperationResult(
                        success=False,
                        message=msg,
                        path=resolved_path,
                        error_type="UnicodeDecodeError",
                    )
    except FileNotFoundError:
        msg = f"File not found: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="FileNotFoundError")
    except PermissionError:
        msg = f"Permission denied while reading: {resolved_path}"
        logger.warning(msg)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="PermissionError")
    except OSError as exc:
        msg = f"OS error while reading {resolved_path}: {exc}"
        logger.error(msg, exc_info=True)
        return FileOperationResult(success=False, message=msg, path=resolved_path, error_type="OSError")


def open_file(path: str, encoding: str = DEFAULT_ENCODING) -> FileOperationResult:
    """Open a file from disk and return its contents.

    Contract:
        * Never raises. All failures (missing file, permission denied,
          decode errors, generic OS errors) are reported via the returned
          ``FileOperationResult.success == False``.
        * On success, ``result.content`` contains the full file text and
          is ready to be loaded straight into an editor tab/text widget.

    Args:
        path: Path of the file to open.
        encoding: Text encoding to use when decoding the file.

    Returns:
        A :class:`FileOperationResult` describing the outcome.
    """
    return _read_text(path, encoding=encoding)