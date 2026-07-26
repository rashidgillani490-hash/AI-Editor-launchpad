"""execution_engine.py - Asynchronous Code Compilation Subprocess Engine.

Runs a target Python file in its own OS process (via ``subprocess.Popen``)
on a background thread, streams its stdout/stderr back line-by-line via
callbacks, and supports forcibly killing a hung or infinite-looping run
on demand.

This module has no GUI dependency: it communicates entirely through
plain callables supplied by the caller (``on_stdout``, ``on_stderr``,
``on_finished``, ``on_error``). A CustomTkinter layer wires those
callbacks to append text into a console widget; a unit test can just
capture them into a list.

Threading model
----------------
``run()`` returns immediately after spawning the subprocess and starting
two reader threads (one for stdout, one for stderr) plus one monitor
thread that waits for process exit. None of this blocks the caller's
thread, so a GUI event loop calling ``run()`` from a button handler never
freezes, even while the target script runs indefinitely.

Because GUI toolkits are generally not thread-safe, callbacks fired from
the reader/monitor threads are handed raw strings/ints only - it is the
GUI layer's responsibility to marshal them onto its own main thread
(e.g. via a thread-safe queue polled with ``after()``) before touching
widgets directly. That marshalling is intentionally left to the GUI
layer since it is toolkit-specific.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass
class ExecutionResult:
    """Terminal outcome of one run, delivered via ``on_finished``.

    Attributes:
        return_code: The subprocess's exit code, or ``None`` if the
            process was forcibly killed before it could report one.
        was_killed: ``True`` if this run ended because :meth:`stop` was
            called, as opposed to the script finishing on its own.
        timed_out: ``True`` if this run ended because the auto-timeout
            expired, as opposed to a manual stop or natural completion.
        duration_seconds: Elapsed time in seconds from process start to
            exit (reaping). Useful for displaying "finished in 2.3s".
    """

    return_code: Optional[int]
    was_killed: bool
    timed_out: bool = False
    duration_seconds: float = 0.0

    def terminated_by_signal(self) -> Optional[int]:
        """Decode a POSIX signal number from a negative return code.

        On POSIX systems, if a process is terminated by a signal,
        ``wait()`` returns a negative value where ``-N`` means signal ``N``.
        This method extracts ``N`` if applicable, or returns ``None``
        if the return code is non-negative or not available.

        Returns:
            The signal number (e.g. ``signal.SIGTERM``) if the process was
            killed by a signal, or ``None`` otherwise.
        """
        if self.return_code is not None and self.return_code < 0:
            return -self.return_code
        return None


# Callback type aliases, purely for readability at call sites.
LineCallback = Callable[[str], None]
FinishedCallback = Callable[[ExecutionResult], None]
ErrorCallback = Callable[[str], None]


class ExecutionEngine:
    """Runs one Python script at a time in an isolated subprocess.

    Typical wiring from a GUI layer::

        engine = ExecutionEngine(
            on_stdout=lambda line: console.write(line),
            on_stderr=lambda line: console.write_error(line),
            on_finished=lambda result: console.write(
                f"Process exited with code {result.return_code}"
            ),
        )
        run_button.configure(command=lambda: engine.run(current_file_path))
        stop_button.configure(command=engine.stop)
    """

    def __init__(
        self,
        on_stdout: Optional[LineCallback] = None,
        on_stderr: Optional[LineCallback] = None,
        on_finished: Optional[FinishedCallback] = None,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        """Create an engine with the given callback hooks.

        Args:
            on_stdout: Called with each stdout line (newline stripped)
                as soon as it is produced by the running script.
            on_stderr: Called with each stderr line (newline stripped)
                as soon as it is produced by the running script.
            on_finished: Called exactly once per run, after the process
                has fully exited and been reaped, with an
                :class:`ExecutionResult`.
            on_error: Called if the engine itself fails to launch the
                subprocess (e.g. interpreter not found, permission
                denied) - distinct from the target script's own runtime
                errors, which surface via ``on_stderr`` instead.
        """
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._on_finished = on_finished
        self._on_error = on_error

        self._process: Optional[subprocess.Popen] = None
        # Guards all mutable state below so run()/stop()/is_running()
        # are safe to call from a GUI thread while reader/monitor
        # threads are concurrently active.
        self._lock = threading.Lock()
        self._reader_threads: List[threading.Thread] = []
        self._killed = False
        self._timed_out = False
        self._timeout_timer: Optional[threading.Timer] = None
        self._queued_run: Optional[tuple] = None
        self._process_start_time: Optional[float] = None

    # -- Public API -----------------------------------------------------

    def is_running(self) -> bool:
        """Return ``True`` if a subprocess is currently executing."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def run(
        self,
        script_path: str,
        args: Optional[List[str]] = None,
        python_executable: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        queue_if_busy: bool = False,
    ) -> bool:
        """Launch ``script_path`` asynchronously.

        Args:
            script_path: Path to the ``.py`` file to execute.
            args: Extra command-line arguments to pass to the script.
            python_executable: Interpreter to run with. Defaults to
                ``sys.executable`` (the same interpreter running
                NexCore IDE itself).
            timeout: Optional timeout in seconds. If the process has not
                exited by then, it is forcibly terminated and
                ``ExecutionResult.timed_out`` is set to ``True``.
                Defaults to ``None`` (no timeout).
            cwd: Working directory for the subprocess. Defaults to the
                script's own directory (``os.path.dirname(script_path)``)
                rather than the IDE's working directory, which matches
                typical user expectations.
            queue_if_busy: If ``True`` and a run is already in progress,
                queue this run to start after the current one finishes
                instead of returning ``False`` immediately. Defaults to
                ``False`` (drop the request, return ``False``).

        Returns:
            ``True`` if the subprocess was successfully launched or
            queued, ``False`` if a run was already in progress (with
            ``queue_if_busy=False``) or launching failed - in the latter
            case ``on_error`` is also invoked with details.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                # A process is already running.
                if queue_if_busy:
                    # Queue this run to start after the current one.
                    self._queued_run = (script_path, args, python_executable, timeout, cwd)
                    return True
                else:
                    # Drop the request.
                    if self._on_error is not None:
                        self._on_error("A script is already running. Stop it first.")
                    return False

            # Attempt to launch the process.
            success = self._launch_process(script_path, args, python_executable, timeout, cwd)
            return success

    def send_input(self, text: str) -> bool:
        """Send input to the running subprocess's stdin.

        This allows scripts that call ``input()`` to receive data.
        The text is written to stdin, flushed, and a newline is
        appended automatically if the text doesn't already end with one.

        Args:
            text: The input text to send. A newline is appended if
                ``text`` does not already end with one.

        Returns:
            ``True`` if the input was successfully written, ``False``
            if no process is running or stdin is not available.
        """
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            if process.stdin is None:
                return False

        try:
            if not text.endswith("\n"):
                text += "\n"
            process.stdin.write(text)
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            # Process may have exited or closed stdin - not an error
            # worth propagating; just report failure to the caller.
            return False

    def stop(self, timeout: float = 3.0) -> bool:
        """Forcibly terminate the currently running subprocess, if any.

        This is the "emergency stop" for a hung or infinite-looping
        script. Attempts a graceful ``terminate()`` first, then escalates
        to ``kill()`` if the process has not exited within ``timeout``
        seconds. On POSIX systems, terminates the entire process group
        (killing child processes as well). On Windows, attempts to use
        ``taskkill /T`` (with fallback to direct kill if unavailable).

        Args:
            timeout: Seconds to wait after ``terminate()`` before
                escalating to ``kill()``.

        Returns:
            ``True`` if a running process was found and a stop was
            requested, ``False`` if nothing was running.
        """
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._killed = True
            # Cancel any pending timeout timer.
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()

        try:
            self._terminate_process_tree(process, timeout)
        except OSError as exc:
            # Process may have exited in the gap between the poll()
            # check above and terminate()/kill() being called; anything
            # else is reported via on_error rather than raised.
            if self._on_error is not None:
                self._on_error(f"Error while stopping process: {exc}")
            return False

        return True

    # -- Internal helpers -------------------------------------------------

    def _launch_process(
        self,
        script_path: str,
        args: Optional[List[str]] = None,
        python_executable: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
    ) -> bool:
        """Attempt to launch the process; assumes the lock is held.

        Returns:
            ``True`` on success, ``False`` on failure (and calls on_error).
        """
        command = [python_executable or sys.executable, script_path, *(args or [])]

        # Resolve working directory: default to script's directory.
        if cwd is None:
            cwd = os.path.dirname(os.path.abspath(script_path))
            if not cwd:  # Empty if script is in cwd root
                cwd = None

        # Prepare environment: inject PYTHONUNBUFFERED if not present.
        env = os.environ.copy()
        if "PYTHONUNBUFFERED" not in env:
            env["PYTHONUNBUFFERED"] = "1"

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # Line-buffered, required for real-time streaming.
                cwd=cwd,
                env=env,
                **self._get_process_kwargs(),
            )
        except (OSError, ValueError) as exc:
            # OSError covers "interpreter/script not found",
            # "permission denied", etc. ValueError covers a handful
            # of Popen argument-validation edge cases.
            if self._on_error is not None:
                self._on_error(f"Failed to start process: {exc}")
            return False

        self._process = process
        self._process_start_time = time.monotonic()
        self._killed = False
        self._timed_out = False

        # Stream stdout and stderr concurrently so a script that
        # writes heavily to one doesn't delay lines from the other.
        stdout_thread = threading.Thread(
            target=self._stream_reader,
            args=(process.stdout, self._on_stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_reader,
            args=(process.stderr, self._on_stderr),
            daemon=True,
        )
        monitor_thread = threading.Thread(
            target=self._monitor_process,
            args=(process, stdout_thread, stderr_thread),
            daemon=True,
        )

        self._reader_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()
        monitor_thread.start()

        # Set up auto-timeout if requested.
        if timeout is not None and timeout > 0:
            self._timeout_timer = threading.Timer(
                timeout,
                self._on_timeout,
            )
            self._timeout_timer.daemon = True
            self._timeout_timer.start()

        return True

    def _get_process_kwargs(self) -> Dict:
        """Return platform-specific keyword arguments for Popen.

        On POSIX, sets ``preexec_fn`` to create a new process group
        so that ``stop()`` can kill the entire tree. On Windows,
        sets ``creationflags`` to create a new job object.

        Returns:
            Dictionary of kwargs to unpack into ``Popen(**kwargs)``.
        """
        kwargs = {}
        if platform.system() != "Windows":
            # POSIX: Create a new process group so we can kill the tree.
            kwargs["preexec_fn"] = os.setsid
        else:
            # Windows: Create a new process group.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        return kwargs

    def _terminate_process_tree(
        self,
        process: subprocess.Popen,
        timeout: float,
    ) -> None:
        """Terminate the entire process tree, not just the parent.

        On POSIX, sends SIGTERM to the entire process group. On Windows,
        attempts to use ``taskkill /T /F`` first, with fallback to direct kill.
        Escalates to SIGKILL / kill() if SIGTERM doesn't succeed within
        the timeout.

        Args:
            process: The subprocess.Popen object.
            timeout: Seconds to wait for graceful termination.
        """
        try:
            if platform.system() == "Windows":
                self._terminate_process_tree_windows(process, timeout)
            else:
                self._terminate_process_tree_posix(process, timeout)
        except OSError:
            # If the platform-specific method fails, fall back to
            # a simple kill of the immediate process (better than crashing).
            process.kill()
            process.wait(timeout=timeout)

    def _terminate_process_tree_posix(
        self,
        process: subprocess.Popen,
        timeout: float,
    ) -> None:
        """POSIX process tree termination via process group signals."""
        try:
            # Get the process group ID (should be the PID itself if
            # preexec_fn=os.setsid was honored).
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            # Fall back to killing just the process.
            process.terminate()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Graceful termination timed out; escalate to SIGKILL.
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                # Fall back to direct kill.
                process.kill()
            process.wait(timeout=timeout)

    def _terminate_process_tree_windows(
        self,
        process: subprocess.Popen,
        timeout: float,
    ) -> None:
        """Windows process tree termination via taskkill or fallback."""
        try:
            # Attempt to use taskkill /T /F to kill the tree.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                timeout=timeout,
                check=False,
                capture_output=True,
            )
            # Give it a moment to reap, then check if it's gone.
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # taskkill didn't work; fall through to direct kill.
                process.kill()
                process.wait(timeout=timeout)
        except (OSError, FileNotFoundError):
            # taskkill not found or failed; fall back to direct kill.
            process.kill()
            process.wait(timeout=timeout)

    def _on_timeout(self) -> None:
        """Called when the auto-timeout expires; forcibly stops the process."""
        with self._lock:
            self._timed_out = True
        self.stop(timeout=1.0)

    def _stream_reader(self, pipe, callback: Optional[LineCallback]) -> None:
        """Read ``pipe`` line-by-line, forwarding each line to ``callback``.

        Runs on a dedicated background thread. Exits naturally when the
        subprocess closes the pipe (i.e. when it exits).
        """
        if pipe is None:
            return
        try:
            for line in pipe:
                if callback is not None:
                    callback(line.rstrip("\n"))
        except (OSError, ValueError):
            # Pipe was closed out from under us (e.g. process killed
            # mid-read) - not an error worth surfacing, just stop reading.
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _monitor_process(
        self,
        process: subprocess.Popen,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
    ) -> None:
        """Wait for ``process`` to exit, then reap it and report completion.

        Runs on a dedicated background thread. Waiting for the reader
        threads to finish first guarantees every line already produced
        by the process has been delivered to the callbacks before
        ``on_finished`` fires, and calling ``process.wait()`` ensures the
        OS process table entry is cleaned up (no zombie processes).
        """
        return_code = process.wait()

        # Make sure every buffered line has been flushed to the
        # callbacks before announcing completion.
        stdout_thread.join()
        stderr_thread.join()

        with self._lock:
            was_killed = self._killed
            timed_out = self._timed_out
            start_time = self._process_start_time
            if self._process is process:
                self._process = None
            # Cancel any lingering timeout timer.
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()
                self._timeout_timer = None

        # Compute elapsed time.
        elapsed = 0.0
        if start_time is not None:
            elapsed = time.monotonic() - start_time

        result = ExecutionResult(
            return_code=return_code,
            was_killed=was_killed,
            timed_out=timed_out,
            duration_seconds=elapsed,
        )

        if self._on_finished is not None:
            self._on_finished(result)

        # If a run was queued, start it now.
        if self._queued_run is not None:
            script_path, args, python_executable, timeout, cwd = self._queued_run
            self._queued_run = None
            with self._lock:
                self._launch_process(script_path, args, python_executable, timeout, cwd)


if __name__ == "__main__":
    # Minimal, self-contained demonstration of the public API: write a
    # small temporary script, run it, stream its output to stdout, then
    # demonstrate an emergency stop on a script that would otherwise
    # loop forever.
    import tempfile

    demo_dir = tempfile.mkdtemp(prefix="nexcore_exec_demo_")

    finished_event = threading.Event()

    def handle_stdout(line: str) -> None:
        print(f"[stdout] {line}")

    def handle_stderr(line: str) -> None:
        print(f"[stderr] {line}")

    def handle_finished(result: ExecutionResult) -> None:
        msg = f"[finished] return_code={result.return_code} was_killed={result.was_killed}"
        if result.timed_out:
            msg += " timed_out=True"
        msg += f" duration={result.duration_seconds:.2f}s"
        print(msg)
        finished_event.set()

    def handle_error(message: str) -> None:
        print(f"[error] {message}")
        finished_event.set()

    engine = ExecutionEngine(
        on_stdout=handle_stdout,
        on_stderr=handle_stderr,
        on_finished=handle_finished,
        on_error=handle_error,
    )

    print("== Running a short-lived script ==")
    quick_script = os.path.join(demo_dir, "quick.py")
    with open(quick_script, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            "print('hello from the subprocess')\n"
            "print('a warning line', file=sys.stderr)\n"
        )
    engine.run(quick_script)
    finished_event.wait(timeout=10)

    print("\n== Running and then emergency-stopping an infinite loop ==")
    finished_event.clear()
    infinite_script = os.path.join(demo_dir, "infinite.py")
    with open(infinite_script, "w", encoding="utf-8") as fh:
        fh.write("while True:\n    pass\n")
    engine.run(infinite_script)
    time.sleep(1)  # Give it a moment to actually start looping.
    print("is_running:", engine.is_running())
    engine.stop()
    finished_event.wait(timeout=10)
    print("is_running after stop:", engine.is_running())
