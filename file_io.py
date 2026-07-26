"""file_io.py - Persistent I/O Transaction Layer for NexCore IDE.

This module owns every interaction with the filesystem that the editor
needs: opening a file into memory, saving an already-known file, and
"save as" (writing to a brand-new path). It has zero GUI dependencies -
every public function returns a plain, inspectable result object instead
of raising, so a GUI layer (CustomTkinter or otherwise) can react to
failures with a dialog box instead of crashing on an uncaught exception.
- Atomic writes: saves are written to a temp file in the same directory
  and atomically moved into place with os.replace().
- Async variants: async versions of open/save functions using
  asyncio.to_thread so GUI event loops stay responsive.
- Large file handling: files larger than a configurable threshold are
  read in streaming chunks. A flag in FileOperationResult indicates this.
- Encoding detection/fallback: on decode failures the module attempts
  to detect encoding via optional charset_normalizer or chardet packages
  and falls back to latin-1 if necessary.
- Debounced autosave: DebouncedSaver helper avoids hammering disk during
  frequent edits.
- Backup-before-overwrite: optional backup of existing files before
  overwriting (configurable, opt-in).
- File change detection: has_external_changes helper uses mtime checks.
- Logging: uses the standard logging module for diagnostics.
- Path validation helper: normalize/resolve paths and reject invalid ones.

Design notes
------------
* Backwards-compatible public API: open_file, save_file, save_file_as keep
  the same signatures and return FileOperationResult. New optional keyword
  args are added for opt-in features (backup, backup_suffix).
* FileOperationResult remains a plain dataclass, extended with a few
  non-breaking fields so callers can opt-in to richer information.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Default text encoding used for all read/write operations. Centralised
# here so it can be changed in one place if NexCore ever needs to support
# alternate encodings (e.g. reading legacy latin-1 source files).
DEFAULT_ENCODING = "utf-8"

# Threshold (in bytes) at which to treat a file as "large" and stream it.
LARGE_FILE_THRESHOLD = 5 * 1024 * 1024  # 5 MB default; configurable by callers if needed

# Size of read chunks when streaming (binary read then incremental decode).
_READ_CHUNK_SIZE = 64 * 1024  # 64 KB

logger = logging.getLogger(__name__)

# Optional encoding detectors (try preferred ones first). If not installed,
# we'll fall back to latin-1 for a best-effort decode.
_charset_detector = None
try:
    # Preferred: charset-normalizer (PyPI: charset-normalizer)
    from charset_normalizer import from_bytes as _cn_from_bytes  # type: ignore

    def _detect_encoding_with_charset_normalizer(sample: bytes) -> Optional[str]:
        try:
            results = _cn_from_bytes(sample)
            if results:
                best = results.best()
                if best and best.encoding:
                    return best.encoding
        except Exception:
            logger.debug("charset_normalizer detection failed", exc_info=True)
        return None

    _charset_detector = _detect_encoding_with_charset_normalizer
    logger.debug("Using charset_normalizer for encoding detection")
except Exception:
    try:
        # Fallback: chardet
        import chardet  # type: ignore

        def _detect_encoding_with_chardet(sample: bytes) -> Optional[str]:
            try:
                info = chardet.detect(sample)
                encoding = info.get("encoding")
                return encoding
            except Exception:
                logger.debug("chardet detection failed", exc_info=True)
                return None

        _charset_detector = _detect_encoding_with_chardet
        logger.debug("Using chardet for encoding detection")
    except Exception:
        logger.debug("No optional encoding detectors available; will fall back to latin-1 when necessary")


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


def _detect_encoding(sample: bytes, preferred: Optional[str] = None) -> Optional[str]:
    """Detect encoding for a byte sample.

    If a preferred encoding was supplied, we return it first (caller uses
    that before invoking this). Otherwise, use available detectors. May
    return None if detection fails.
    """
    if preferred:
        return preferred
    if _charset_detector:
        try:
            detected = _charset_detector(sample)
            if detected:
                logger.debug("Detected encoding %r from sample", detected)
                return detected
        except Exception:
            logger.debug("Encoding detector raised", exc_info=True)
    return None


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
                if _charset_detector:
                    alt_encoding = _charset_detector(sample)
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


# Async variants ----------------------------------------------------------

async def open_file_async(path: str, encoding: str = DEFAULT_ENCODING, *,
                          large_file_threshold: int = LARGE_FILE_THRESHOLD) -> FileOperationResult:
    """Asynchronous variant of open_file that offloads work to a thread.

    This uses asyncio.to_thread and therefore does not require optional
    dependencies. It preserves the same returned FileOperationResult.
    """
    return await asyncio.to_thread(_read_text, path, encoding, large_file_threshold)


async def save_file_async(path: str, content: str, encoding: str = DEFAULT_ENCODING, *,
                          backup: bool = False, backup_suffix: str = ".bak") -> FileOperationResult:
    """Asynchronous variant of save_file that offloads work to a thread."""
    return await asyncio.to_thread(_write_text, path, content, encoding, True, backup, backup_suffix)


async def save_file_as_async(new_path: str, content: str, encoding: str = DEFAULT_ENCODING, *,
                             backup: bool = False, backup_suffix: str = ".bak") -> FileOperationResult:
    """Asynchronous variant of save_file_as that offloads work to a thread."""
    return await asyncio.to_thread(_write_text, new_path, content, encoding, True, backup, backup_suffix)


# File change detection ----------------------------------------------------

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


# Debounced autosave helper ------------------------------------------------

class DebouncedSaver:
    """DebouncedSaver(callable) -> helper that writes to disk only after a
    period of inactivity.

    Usage:
        saver = DebouncedSaver(save_callback, delay=2.0, max_interval=30.0)

        # Called on each keystroke (or buffer change):
        saver.request(path, content)

        # Force immediate write (e.g. on explicit Save or shutdown)
        await saver.flush()        # if using async loop
        saver.stop()               # cancel any outstanding timers

    Arguments:
        save_callback: callable(path: str, content: str) -> FileOperationResult
                       or coroutine that performs the actual save.
                       This function will be run off the main thread if it
                       is synchronous.
        delay: Seconds of inactivity before performing the save.
        max_interval: Maximum time in seconds to force a save since first
                      request even if activity continues; prevents never
                      saving for long-lived typing sessions.
    Notes:
        - The helper is GUI-agnostic and does not import any GUI libs.
        - The callback may be sync or async; synchronous callbacks are
          executed in a background thread via asyncio.to_thread if an
          asyncio loop is running, otherwise via threading.Thread.
    """

    def __init__(self, save_callback: Callable[[str, str], Any], delay: float = 2.0, max_interval: float = 30.0):
        self._save_callback = save_callback
        self._delay = float(delay)
        self._max_interval = float(max_interval)
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._first_request_time: Optional[float] = None
        self._last_request_content: Optional[str] = None
        self._last_request_path: Optional[str] = None
        self._stopped = False

    def request(self, path: str, content: str) -> None:
        """Request that the content be saved. Actual save is debounced."""
        with self._lock:
            if self._stopped:
                logger.debug("DebouncedSaver.request called after stop()")
                return
            now = time.time()
            if self._first_request_time is None:
                self._first_request_time = now
            self._last_request_content = content
            self._last_request_path = path

            # Cancel prior timer
            if self._timer:
                self._timer.cancel()

            # If we've surpassed max_interval, save immediately in background
            if now - (self._first_request_time or now) >= self._max_interval:
                logger.debug("DebouncedSaver: max_interval exceeded, saving immediately")
                self._schedule_immediate_save()
                self._first_request_time = None
            else:
                # Schedule delayed save
                self._timer = threading.Timer(self._delay, self._perform_save_in_thread)
                self._timer.daemon = True
                self._timer.start()
                logger.debug("DebouncedSaver: scheduled save in %s seconds", self._delay)

    def _schedule_immediate_save(self) -> None:
        # Fire off save without waiting
        t = threading.Thread(target=self._perform_save_in_thread, daemon=True)
        t.start()

    def _perform_save_in_thread(self) -> None:
        """Perform the configured save callback in a background context."""
        with self._lock:
            path = self._last_request_path
            content = self._last_request_content
            self._last_request_content = None
            self._last_request_path = None
            self._timer = None
            self._first_request_time = None

        if not path:
            logger.debug("DebouncedSaver: no path supplied at save time")
            return

        try:
            # If save_callback is coroutine function and there's a running loop, run in it
            if asyncio.iscoroutinefunction(self._save_callback):
                try:
                    loop = asyncio.get_running_loop()
                    # Schedule coroutine on running loop via create_task
                    coroutine = self._save_callback(path, content)
                    loop.call_soon_threadsafe(asyncio.create_task, coroutine)
                    logger.debug("DebouncedSaver: scheduled async save callback on running loop")
                except RuntimeError:
                    # No event loop in this thread; run coroutine via new loop
                    asyncio.run(self._save_callback(path, content))
                    logger.debug("DebouncedSaver: executed async save callback via asyncio.run")
            else:
                # Synchronous callback - try to offload to a thread-friendly mechanism
                try:
                    loop = asyncio.get_running_loop()
                    # If called from an asyncio loop, use to_thread to avoid blocking
                    asyncio.run_coroutine_threadsafe(asyncio.to_thread(self._save_callback, path, content), loop)
                    logger.debug("DebouncedSaver: offloaded sync save callback to thread via asyncio.to_thread")
                except RuntimeError:
                    # No asyncio loop available; call directly (it will run in this background thread)
                    self._save_callback(path, content)
                    logger.debug("DebouncedSaver: executed sync save callback in background thread")
        except Exception:
            logger.exception("DebouncedSaver: exception during save")

    async def flush(self) -> None:
        """Force any pending save to happen and wait for it to complete.

        If the configured callback is synchronous, it will be executed in
        a worker thread via asyncio.to_thread.
        """
        with self._lock:
            # Cancel timer and capture latest content
            if self._timer:
                self._timer.cancel()
                self._timer = None
            path = self._last_request_path
            content = self._last_request_content
            self._last_request_content = None
            self._last_request_path = None
            self._first_request_time = None

        if not path:
            return

        if asyncio.iscoroutinefunction(self._save_callback):
            await self._save_callback(path, content or "")
        else:
            # Run the sync callback in a thread to avoid blocking
            await asyncio.to_thread(self._save_callback, path, content or "")

    def stop(self) -> None:
        """Cancel any pending saves and stop the debouncer."""
        with self._lock:
            self._stopped = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._first_request_time = None
            self._last_request_content = None
            self._last_request_path = None
        logger.debug("DebouncedSaver stopped")


# Minimal self-contained demonstration of the public API (still GUI-agnostic)
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

    demo_dir = tempfile.mkdtemp(prefix="nexcore_file_io_demo_")
    demo_path = os.path.join(demo_dir, "demo.py")

    print("== save_file_as: creating a new file ==")
    result = save_file_as(demo_path, "print('hello from NexCore IDE')\n")
    print(result)

    print("\n== open_file: reading it back ==")
    result = open_file(demo_path)
    print(result)

    print("\n== save_file: overwriting the same file with a backup ==")
    result = save_file(demo_path, "print('updated content')\n", backup=True)
    print(result)

    print("\n== open_file: reading a file that does not exist ==")
    result = open_file(os.path.join(demo_dir, "missing.py"))
    print(result)

    print("\n== save_file: writing into a directory that does not exist ==")
    result = save_file(os.path.join(demo_dir, "nowhere", "file.py"), "x = 1\n")
    print(result)

    # Demonstrate DebouncedSaver with a trivial save callback
    print("\n== DebouncedSaver demo ==")

    def demo_save_cb(path, content):
        print(f"demo_save_cb invoked for {path!r} with content length {len(content)}")
        return save_file(path, content)

    saver = DebouncedSaver(demo_save_cb, delay=1.0, max_interval=5.0)
    # Simulate keystrokes
    saver.request(demo_path, "a = 1\n")
    saver.request(demo_path, "a = 2\n")
    time.sleep(2.0)  # allow saver to fire
    saver.stop()

    print("\n== has_external_changes demo ==")
    vr = has_external_changes(demo_path, last_known_mtime=0.0)
    print(vr)