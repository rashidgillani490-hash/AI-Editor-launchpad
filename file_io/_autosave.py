"""Debounced autosave helper (DebouncedSaver)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


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