"""
terminal_manager.py — Subprocess runner for the Integrated Terminal panel.

Supports multiple concurrent terminal instances, each with its own
subprocess, working directory, and output buffer.  Streams stdout/stderr
in real time, accepts stdin input, and can run arbitrary shell commands
or specific Python scripts.

Usage (GUI hooks):
    from terminal_manager import TerminalManager, TerminalInstance

    tm = TerminalManager()

    # Create a terminal tab
    term = tm.create_terminal(cwd="/project")

    # Run the active Python file
    term.run_python_file("/project/main.py", args=["--verbose"])

    # Run an arbitrary command
    term.run_command("ls -la")

    # Send input to the running process
    term.send_input("some input\\n")

    # Stop
    term.kill()

    # Read output
    while line := term.read_output_line():
        gui_append(line)

    tm.shutdown()
"""

from __future__ import annotations

import os
import sys
import time
import signal
import shlex
import threading
import subprocess
import uuid
from queue import Queue, Empty
from typing import (
    Optional, Callable, List, Dict, Any, Tuple, Literal,
)
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class OutputLine:
    """A single line of output from a running process."""
    text: str
    stream: Literal["stdout", "stderr", "system"]
    timestamp: float = field(default_factory=time.time)


@dataclass
class TerminalState:
    """Snapshot of terminal instance state for GUI to render."""
    terminal_id: str
    is_running: bool
    pid: Optional[int]
    cwd: str
    exit_code: Optional[int]
    execution_time: Optional[float]  # seconds


# ---------------------------------------------------------------------------
# TerminalInstance
# ---------------------------------------------------------------------------

class TerminalInstance:
    """One terminal tab / instance with its own subprocess and working directory."""

    def __init__(
        self,
        terminal_id: str,
        cwd: str,
        on_output: Optional[Callable[[OutputLine], None]] = None,
        on_state_change: Optional[Callable[[TerminalState], None]] = None,
    ) -> None:
        self.terminal_id = terminal_id
        self.cwd = os.path.abspath(cwd)
        self.on_output = on_output
        self.on_state_change = on_state_change

        self._process: Optional[subprocess.Popen] = None
        self._output_queue: Queue[Optional[OutputLine]] = Queue()
        self._threads: List[threading.Thread] = []
        self._running = False
        self._exit_code: Optional[int] = None
        self._start_time: float = 0.0
        self._pid: Optional[int] = None

    # -- properties ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._pid

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def get_state(self) -> TerminalState:
        elapsed = (time.time() - self._start_time) if self._start_time > 0 else None
        if self._exit_code is not None and elapsed is not None:
            elapsed = min(elapsed, time.time() - self._start_time)
        return TerminalState(
            terminal_id=self.terminal_id,
            is_running=self.is_running,
            pid=self._pid,
            cwd=self.cwd,
            exit_code=self._exit_code,
            execution_time=elapsed,
        )

    # -- python detection ----------------------------------------------------

    @staticmethod
    def detect_python_interpreter(cwd: str) -> str:
        """Auto-detect the best Python interpreter for *cwd*.
        Checks for venv/.venv first, then falls back to sys.executable.
        """
        for venv_name in ("venv", ".venv"):
            for sub in ("bin", "Scripts"):
                candidate = os.path.join(cwd, venv_name, sub, "python")
                if os.path.isfile(candidate):
                    return candidate
                candidate += ".exe"
                if os.path.isfile(candidate):
                    return candidate
        # Walk up looking for venvs
        parent = os.path.dirname(cwd)
        while parent and parent != cwd:
            for venv_name in ("venv", ".venv"):
                for sub in ("bin", "Scripts"):
                    candidate = os.path.join(parent, venv_name, sub, "python")
                    if os.path.isfile(candidate):
                        return candidate
                    candidate += ".exe"
                    if os.path.isfile(candidate):
                        return candidate
            cwd = parent
            parent = os.path.dirname(cwd)
        return sys.executable

    # -- run Python file -----------------------------------------------------

    def run_python_file(
        self,
        file_path: str,
        args: Optional[List[str]] = None,
        python_path: Optional[str] = None,
    ) -> None:
        """Execute a Python script. Output streams to the terminal."""
        if self.is_running:
            self.kill()
            time.sleep(0.1)

        interpreter = python_path or self.detect_python_interpreter(self.cwd)
        cmd = [interpreter, "-u", file_path]  # -u = unbuffered
        if args:
            cmd.extend(args)
        self._launch(cmd, cwd=os.path.dirname(file_path))

    # -- run shell command ---------------------------------------------------

    def run_command(self, cmd_str: str, cwd: Optional[str] = None) -> None:
        """Run an arbitrary shell command string.
        If the command is `cd <dir>`, update cwd directly instead of spawning.
        """
        stripped = cmd_str.strip()
        if not stripped:
            return

        # Handle cd internally
        if stripped.startswith("cd ") or stripped == "cd":
            parts = shlex.split(stripped)
            if len(parts) == 1:
                new_dir = os.path.expanduser("~")
            else:
                new_dir = os.path.expanduser(parts[1])
            if not os.path.isabs(new_dir):
                new_dir = os.path.join(self.cwd, new_dir)
            new_dir = os.path.abspath(new_dir)
            if os.path.isdir(new_dir):
                self.cwd = new_dir
                self._emit_output(OutputLine(f"cd → {self.cwd}\n", "system"))
                self._emit_state()
            else:
                self._emit_output(OutputLine(
                    f"cd: no such directory: {new_dir}\n", "stderr"
                ))
            return

        if self.is_running:
            self.kill()
            time.sleep(0.1)

        target_cwd = cwd or self.cwd

        # Use shell on non-Windows for convenience; subprocess on Windows
        if os.name == "nt":
            self._launch(["cmd", "/c", cmd_str], cwd=target_cwd)
        else:
            self._launch(["bash", "-c", cmd_str], cwd=target_cwd)

    # -- control -------------------------------------------------------------

    def send_input(self, text: str) -> None:
        """Write *text* to the running subprocess's stdin."""
        if self._process and self._process.stdin and not self._process.stdin.closed:
            try:
                self._process.stdin.write(text)
                self._process.stdin.flush()
            except (OSError, BrokenPipeError, ValueError):
                pass

    def kill(self, force_timeout: float = 5.0) -> None:
        """Terminate the running process (SIGTERM then SIGKILL after timeout)."""
        if self._process is None:
            return
        proc = self._process
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=force_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._running = False
        self._emit_state()

    def clear_output(self) -> None:
        """Drain the output queue."""
        while not self._output_queue.empty():
            try:
                self._output_queue.get_nowait()
            except Empty:
                break

    def read_output_line(self, block: bool = False, timeout: float = 0.05) -> Optional[OutputLine]:
        """Read one output line from the queue. Non-blocking by default."""
        try:
            return self._output_queue.get(block=block, timeout=timeout)
        except Empty:
            return None

    def read_all_output(self) -> List[OutputLine]:
        """Drain and return all available output lines."""
        lines: List[OutputLine] = []
        while True:
            line = self.read_output_line(block=False)
            if line is None:
                break
            lines.append(line)
        return lines

    # -- internals -----------------------------------------------------------

    def _launch(self, cmd: List[str], cwd: str) -> None:
        self._running = True
        self._exit_code = None
        self._start_time = time.time()

        try:
            self._process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                # Create in new process group so we can kill the whole tree
                start_new_session=True if os.name != "nt" else False,
            )
            self._pid = self._process.pid
            self._emit_state()

            # Reader threads
            t_out = threading.Thread(
                target=self._read_stream,
                args=(self._process.stdout, "stdout"),
                daemon=True,
            )
            t_err = threading.Thread(
                target=self._read_stream,
                args=(self._process.stderr, "stderr"),
                daemon=True,
            )
            t_wait = threading.Thread(target=self._wait_process, daemon=True)

            self._threads = [t_out, t_err, t_wait]
            t_out.start()
            t_err.start()
            t_wait.start()

        except Exception as exc:
            self._emit_output(OutputLine(f"Launch error: {exc}\n", "stderr"))
            self._running = False
            self._exit_code = -1
            self._emit_state()

    def _read_stream(self, stream, label: Literal["stdout", "stderr"]) -> None:
        """Read lines from *stream* and push them into the queue."""
        try:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                if not self._running:
                    break
                self._emit_output(OutputLine(line, label))
        except (ValueError, OSError):
            pass

    def _wait_process(self) -> None:
        """Wait for process to finish, then record exit code."""
        if self._process is None:
            return
        try:
            self._process.wait()
        except Exception:
            pass
        self._exit_code = self._process.returncode
        self._running = False
        self._pid = None
        self._emit_state()
        # Signal end of output
        self._output_queue.put(None)  # sentinel

    def _emit_output(self, line: OutputLine) -> None:
        self._output_queue.put(line)
        if self.on_output:
            self.on_output(line)

    def _emit_state(self) -> None:
        if self.on_state_change:
            self.on_state_change(self.get_state())


# ---------------------------------------------------------------------------
# TerminalManager
# ---------------------------------------------------------------------------

class TerminalManager:
    """Manages multiple TerminalInstance objects (one per terminal tab)."""

    def __init__(self) -> None:
        self._terminals: Dict[str, TerminalInstance] = {}
        self._on_instance_output: Optional[Callable[[str, OutputLine], None]] = None
        self._on_instance_state: Optional[Callable[[str, TerminalState], None]] = None

    def set_callbacks(
        self,
        on_output: Optional[Callable[[str, OutputLine], None]],
        on_state: Optional[Callable[[str, TerminalState], None]],
    ) -> None:
        self._on_instance_output = on_output
        self._on_instance_state = on_state

    def create_terminal(self, cwd: str = "") -> TerminalInstance:
        """Create a new terminal instance at *cwd* (default: project root)."""
        tid = str(uuid.uuid4())[:8]
        inst = TerminalInstance(
            terminal_id=tid,
            cwd=cwd or os.getcwd(),
            on_output=lambda line: self._on_instance_output(tid, line) if self._on_instance_output else None,
            on_state_change=lambda state: self._on_instance_state(tid, state) if self._on_instance_state else None,
        )
        self._terminals[tid] = inst
        return inst

    def get_terminal(self, terminal_id: str) -> Optional[TerminalInstance]:
        return self._terminals.get(terminal_id)

    def remove_terminal(self, terminal_id: str) -> None:
        inst = self._terminals.pop(terminal_id, None)
        if inst:
            inst.kill()

    def kill_all(self) -> None:
        for inst in self._terminals.values():
            inst.kill()

    def shutdown(self) -> None:
        self.kill_all()
        self._terminals.clear()

    @property
    def active_terminals(self) -> Dict[str, TerminalInstance]:
        return dict(self._terminals)


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tm = TerminalManager()

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "test_script.py"
        script.write_text(
            "import time\n"
            "print('Hello from script')\n"
            "time.sleep(0.3)\n"
            "print('Done.')\n"
        )

        inst = tm.create_terminal(cwd=tmp)
        inst.run_python_file(str(script))

        while inst.is_running or not inst._output_queue.empty():
            line = inst.read_output_line(timeout=0.1)
            if line:
                print(f"[{line.stream}] {line.text}", end="")
            else:
                time.sleep(0.05)

        print(f"Exit code: {inst.exit_code}")

    tm.shutdown()
