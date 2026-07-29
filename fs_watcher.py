"""
fs_watcher.py — Optional file-system watcher for the project root.

Uses `watchdog` if available; falls back to a lightweight polling loop.
Emits callbacks so the GUI file tree can refresh automatically.

Usage (GUI hooks):
    from fs_watcher import start_watching, stop_watching

    watcher = start_watching("/project", on_change=my_refresh_callback)
    # ...
    stop_watching(watcher)
"""

from __future__ import annotations

import os
import time
import threading
from pathlib import Path
from typing import Optional, Callable, Dict, Any, Set


# ---------------------------------------------------------------------------
# Event data
# ---------------------------------------------------------------------------

class FileChangeEvent:
    """Describes a detected file-system change."""
    __slots__ = ("event_type", "path", "is_directory")

    def __init__(self, event_type: str, path: str, is_directory: bool = False) -> None:
        self.event_type = event_type  # "created" | "deleted" | "modified" | "moved"
        self.path = path
        self.is_directory = is_directory

    def __repr__(self) -> str:
        return f"FileChangeEvent({self.event_type!r}, {self.path!r})"


ChangeCallback = Callable[[FileChangeEvent], None]

# ---------------------------------------------------------------------------
# Watchdog-based watcher
# ---------------------------------------------------------------------------

class _WatchdogWatcher:
    """Wrapper around watchdog.observers.Observer."""

    def __init__(self, root_path: str, on_change: ChangeCallback) -> None:
        self._root = root_path
        self._on_change = on_change
        self._observer = None
        self._running = False

    def start(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileSystemEvent
        except ImportError:
            raise RuntimeError("watchdog is not installed; use PollingWatcher instead")

        class _Handler(FileSystemEventHandler):
            def on_any_event(handler_self, event: FileSystemEvent) -> None:  # type: ignore[override]
                etype = event.event_type  # "created" / "deleted" / "modified" / "moved"
                self._on_change(FileChangeEvent(
                    event_type=etype,
                    path=event.src_path,
                    is_directory=event.is_directory,
                ))

        self._observer = Observer()
        self._observer.schedule(_Handler(), self._root, recursive=True)
        self._observer.start()
        self._running = True

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        self._running = False


# ---------------------------------------------------------------------------
# Polling fallback watcher
# ---------------------------------------------------------------------------

class _PollingWatcher:
    """Scans the file tree at regular intervals, comparing snapshots."""

    def __init__(
        self,
        root_path: str,
        on_change: ChangeCallback,
        interval: float = 2.0,
    ) -> None:
        self._root = root_path
        self._on_change = on_change
        self._interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._snapshot: Dict[str, Optional[float]] = {}  # path -> mtime

    def start(self) -> None:
        self._snapshot = self._scan()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._interval + 1)
            self._thread = None

    def _scan(self) -> Dict[str, Optional[float]]:
        """Walk the tree and return {path: mtime}."""
        snapshot: Dict[str, Optional[float]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self._root):
                # Skip hidden dirs to reduce noise
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in dirnames + filenames:
                    full = os.path.join(dirpath, name)
                    try:
                        snapshot[full] = os.path.getmtime(full)
                    except OSError:
                        snapshot[full] = None
        except Exception:
            pass
        return snapshot

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            try:
                new_snap = self._scan()
                old_paths = set(self._snapshot.keys())
                new_paths = set(new_snap.keys())

                created = new_paths - old_paths
                deleted = old_paths - new_paths
                common = old_paths & new_paths

                for p in created:
                    self._on_change(FileChangeEvent("created", p))
                for p in deleted:
                    self._on_change(FileChangeEvent("deleted", p))
                for p in common:
                    if self._snapshot[p] != new_snap[p]:
                        self._on_change(FileChangeEvent("modified", p))

                self._snapshot = new_snap
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_watching(
    root_path: str,
    on_change: ChangeCallback,
    prefer_watchdog: bool = True,
    poll_interval: float = 2.0,
) -> object:
    """Start a file-system watcher on *root_path*.

    Returns a watcher object you can pass to `stop_watching()`.
    Tries watchdog first; falls back to polling.
    """
    if prefer_watchdog:
        try:
            w = _WatchdogWatcher(root_path, on_change)
            w.start()
            return w
        except (ImportError, RuntimeError):
            pass

    w = _PollingWatcher(root_path, on_change, poll_interval)
    w.start()
    return w


def stop_watching(watcher: object) -> None:
    """Stop a watcher returned by `start_watching()`."""
    if hasattr(watcher, "stop"):
        watcher.stop()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    events: list = []

    def handler(e: FileChangeEvent) -> None:
        events.append(e)
        print(f"[watcher] {e}")

    with tempfile.TemporaryDirectory() as tmp:
        w = start_watching(tmp, handler, prefer_watchdog=False, poll_interval=0.5)
        time.sleep(0.6)  # let initial scan settle

        # Create a file
        Path(os.path.join(tmp, "new_file.py")).touch()
        time.sleep(0.6)

        # Modify it
        Path(os.path.join(tmp, "new_file.py")).write_text("hi")
        time.sleep(0.6)

        # Delete it
        os.unlink(os.path.join(tmp, "new_file.py"))
        time.sleep(0.6)

        stop_watching(w)
        print(f"Captured {len(events)} events.")
