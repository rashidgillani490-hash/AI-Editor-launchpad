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

from ._result import FileOperationResult
from ._encoding import DEFAULT_ENCODING, LARGE_FILE_THRESHOLD
from ._paths import validate_and_resolve_path
from ._read import open_file
from ._write import save_file, save_file_as
from ._create import create_file, create_folder
from ._tree import list_directory_tree
from ._watch import has_external_changes
from ._autosave import DebouncedSaver
from ._async import (
    open_file_async,
    save_file_async,
    save_file_as_async,
    create_file_async,
    create_folder_async,
    list_directory_tree_async,
)

__all__ = [
    "FileOperationResult",
    "DEFAULT_ENCODING",
    "LARGE_FILE_THRESHOLD",
    "validate_and_resolve_path",
    "open_file",
    "save_file",
    "save_file_as",
    "create_file",
    "create_folder",
    "list_directory_tree",
    "has_external_changes",
    "DebouncedSaver",
    "open_file_async",
    "save_file_async",
    "save_file_as_async",
    "create_file_async",
    "create_folder_async",
    "list_directory_tree_async",
]