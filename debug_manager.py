"""
debug_manager.py — Run and Debug backend.

Wraps `debugpy` (the VS Code debug adapter) for breakpoints, stepping,
call stack inspection, variable views, and watch expressions.

If `debugpy` is not installed, falls back to a basic `pdb`-based
implementation for core stepping functionality.

Usage (GUI hooks):
    from debug_manager import DebugManager

    dm = DebugManager()

    # Set breakpoints
    dm.set_breakpoint("/file.py", 12)
    dm.toggle_breakpoint("/file.py", 5)

    # Start debugging a file
    dm.start("/project/main.py")

    # Stepping
    dm.step_over()
    dm.step_into()
    dm.step_out()
    dm.continue_execution()
    dm.pause()
    dm.stop()

    # When paused, inspect:
    frames = dm.get_call_stack()
    vars = dm.get_variables(frame_id=0)
    result = dm.evaluate("my_var + 1")
"""

from __future__ import annotations

import os
import sys
import socket
import time
import threading
import subprocess
from typing import Optional, Callable, List, Dict, Any, Set
from dataclasses import dataclass, field

from dap_client import DAPClient, DAPError, DAPConnectionError


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Breakpoint:
    file_path: str
    line_number: int
    enabled: bool = True
    condition: Optional[str] = None


@dataclass
class StackFrame:
    frame_id: int
    function_name: str
    file_path: str
    line_number: int


@dataclass
class Variable:
    name: str
    value: str
    type_name: str


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class DebugEvents:
    """Event type constants emitted by DebugManager."""
    BREAKPOINT_HIT = "breakpoint_hit"
    PAUSED = "paused"
    RESUMED = "resumed"
    PROCESS_ENDED = "process_ended"
    OUTPUT = "output"


# ---------------------------------------------------------------------------
# DebugManager
# ---------------------------------------------------------------------------

class DebugManager:
    """Debug session manager wrapping debugpy."""

    def __init__(self) -> None:
        self._breakpoints: Dict[str, Set[int]] = {}  # file -> {lines}
        self._bp_objects: Dict[str, List[Breakpoint]] = {}

        self._is_running = False
        self._is_paused = False
        self._debugpy_available = self._check_debugpy()

        # DAP client — created when a debug session starts
        self._dap_client: Optional[DAPClient] = None
        self._stopped_thread_id: Optional[int] = None
        self._debugpy_port: int = 0
        self._debugpy_process: Optional[subprocess.Popen] = None

        self._listeners: Dict[str, List[Callable]] = {
            DebugEvents.BREAKPOINT_HIT: [],
            DebugEvents.PAUSED: [],
            DebugEvents.RESUMED: [],
            DebugEvents.PROCESS_ENDED: [],
            DebugEvents.OUTPUT: [],
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on(self, event: str, callback: Callable) -> None:
        """Register a callback for a debug event."""
        if event in self._listeners:
            self._listeners[event].append(callback)

    def _emit(self, event: str, data: Any = None) -> None:
        for cb in self._listeners.get(event, []):
            try:
                cb(data)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Breakpoints
    # ------------------------------------------------------------------

    def set_breakpoint(self, file_path: str, line_number: int) -> Breakpoint:
        """Add a breakpoint. Syncs to the debug adapter if a session is active.

        Returns the Breakpoint object.
        """
        abs_path = os.path.abspath(file_path)
        if abs_path not in self._breakpoints:
            self._breakpoints[abs_path] = set()
        self._breakpoints[abs_path].add(line_number)

        bp = Breakpoint(file_path=abs_path, line_number=line_number)
        if abs_path not in self._bp_objects:
            self._bp_objects[abs_path] = []
        self._bp_objects[abs_path].append(bp)

        # Sync to the debug adapter if a session is running
        if self._dap_client is not None:
            try:
                self._dap_client.set_breakpoints(
                    abs_path, sorted(self._breakpoints[abs_path])
                )
            except DAPError:
                pass
        return bp

    def remove_breakpoint(self, file_path: str, line_number: int) -> None:
        """Remove a breakpoint. Syncs to the debug adapter if a session is active."""
        abs_path = os.path.abspath(file_path)
        if abs_path in self._breakpoints:
            self._breakpoints[abs_path].discard(line_number)
        if abs_path in self._bp_objects:
            self._bp_objects[abs_path] = [
                b for b in self._bp_objects[abs_path]
                if b.line_number != line_number
            ]

        # Sync to the debug adapter if a session is running
        if self._dap_client is not None:
            try:
                remaining = sorted(self._breakpoints.get(abs_path, set()))
                self._dap_client.set_breakpoints(abs_path, remaining)
            except DAPError:
                pass

    def toggle_breakpoint(self, file_path: str, line_number: int) -> bool:
        """Toggle a breakpoint on/off. Returns new state (True=set)."""
        abs_path = os.path.abspath(file_path)
        lines = self._breakpoints.get(abs_path, set())
        if line_number in lines:
            self.remove_breakpoint(abs_path, line_number)
            return False
        self.set_breakpoint(abs_path, line_number)
        return True

    def get_breakpoints(self, file_path: Optional[str] = None) -> List[Breakpoint]:
        """Return all breakpoints, optionally filtered by file."""
        result: List[Breakpoint] = []
        if file_path:
            abs_path = os.path.abspath(file_path)
            return list(self._bp_objects.get(abs_path, []))
        for bps in self._bp_objects.values():
            result.extend(bps)
        return result

    def clear_all_breakpoints(self) -> None:
        """Clear all breakpoints and sync to the debug adapter."""
        # Collect files that had breakpoints before clearing
        files_to_clear = list(self._breakpoints.keys())
        self._breakpoints.clear()
        self._bp_objects.clear()

        # Sync to the debug adapter if a session is running
        if self._dap_client is not None:
            for abs_path in files_to_clear:
                try:
                    self._dap_client.set_breakpoints(abs_path, [])
                except DAPError:
                    pass

    # ------------------------------------------------------------------
    # Debug session (debugpy-based)
    # ------------------------------------------------------------------

    def start(self, file_path: str, args: Optional[List[str]] = None) -> None:
        """Launch *file_path* in debug mode."""
        if not self._debugpy_available:
            return self._start_pdb(file_path, args)
        self._start_debugpy(file_path, args)

    def _start_debugpy(self, file_path: str, args: Optional[List[str]] = None) -> None:
        """Launch *file_path* under debugpy and connect the DAP client."""
        self._is_running = True
        self._is_paused = False

        abs_path = os.path.abspath(file_path)

        # 1. Find a free port for debugpy
        self._debugpy_port = self._find_free_port()

        # 2. Launch debugpy as a subprocess on that port, waiting for a client
        cmd = [
            sys.executable, "-m", "debugpy",
            "--listen", str(self._debugpy_port),
            "--wait-for-client",
            abs_path,
        ]
        if args:
            cmd.extend(args)

        try:
            self._debugpy_process = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(abs_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            self._is_running = False
            self._emit(DebugEvents.OUTPUT, {
                "text": f"Failed to launch debugpy: {exc}\n",
                "stream": "stderr",
            })
            return

        # 3. Read debugpy stdout/stderr in background threads
        def _read_stream(stream, label):
            if stream is None:
                return
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    self._emit(DebugEvents.OUTPUT, {
                        "text": line,
                        "stream": label,
                    })
            except (ValueError, OSError):
                pass

        threading.Thread(
            target=_read_stream,
            args=(self._debugpy_process.stdout, "stdout"),
            daemon=True,
        ).start()
        threading.Thread(
            target=_read_stream,
            args=(self._debugpy_process.stderr, "stderr"),
            daemon=True,
        ).start()

        # 4. Give debugpy a moment to start listening
        time.sleep(0.4)

        # 5. Create and connect the DAP client
        self._dap_client = DAPClient(host="127.0.0.1", port=self._debugpy_port)

        # Route DAP events → DebugManager event system
        def _on_dap_event(event_name: str, body: Dict[str, Any]) -> None:
            self._handle_dap_event(event_name, body)

        self._dap_client.on_event = _on_dap_event

        try:
            self._dap_client.connect(timeout=5.0)
            self._dap_client.initialize()
        except DAPConnectionError as exc:
            self._is_running = False
            self._emit(DebugEvents.OUTPUT, {
                "text": f"DAP connection failed: {exc}\n",
                "stream": "stderr",
            })
            self._cleanup_dap()
            return

        # 6. Send all existing breakpoints to the debug adapter
        self._sync_breakpoints_to_adapter()

        # 7. Monitor subprocess completion in a background thread
        def _monitor_process():
            if self._debugpy_process is None:
                return
            exit_code = self._debugpy_process.wait()
            self._is_running = False
            self._is_paused = False
            self._emit(DebugEvents.PROCESS_ENDED, {"exit_code": exit_code})
            self._cleanup_dap()

        threading.Thread(target=_monitor_process, daemon=True).start()

    def _start_pdb(self, file_path: str, args: Optional[List[str]] = None) -> None:
        """Fallback: launch with pdb (limited functionality)."""
        import subprocess

        self._is_running = True
        self._is_paused = False

        abs_path = os.path.abspath(file_path)
        cmd = [sys.executable, "-m", "pdb", abs_path]
        if args:
            cmd.extend(args)

        def _run():
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=os.path.dirname(abs_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if proc.stdout:
                    for line in proc.stdout:
                        self._emit(DebugEvents.OUTPUT, {"text": line, "stream": "stdout"})
                proc.wait()
            except Exception:
                pass
            finally:
                self._is_running = False
                self._is_paused = False
                self._emit(DebugEvents.PROCESS_ENDED, {"exit_code": 0})

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def stop(self) -> None:
        """End the debug session. Disconnects DAP and kills the process."""
        self._is_running = False
        self._is_paused = False

        # Tell the adapter we're disconnecting
        if self._dap_client:
            try:
                self._dap_client.disconnect_dap()
            except Exception:
                pass

        # Kill the subprocess
        if self._debugpy_process is not None:
            try:
                self._debugpy_process.terminate()
                try:
                    self._debugpy_process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._debugpy_process.kill()
            except Exception:
                pass

        self._cleanup_dap()
        self._emit(DebugEvents.PROCESS_ENDED, {"exit_code": -1})

    # ------------------------------------------------------------------
    # Stepping
    # ------------------------------------------------------------------

    def step_over(self) -> None:
        """Step over current line."""
        self._send_debug_command("next")

    def step_into(self) -> None:
        """Step into function call."""
        self._send_debug_command("step")

    def step_out(self) -> None:
        """Step out of current function."""
        self._send_debug_command("return")

    def continue_execution(self) -> None:
        """Continue execution until next breakpoint."""
        self._send_debug_command("continue")

    def pause(self) -> None:
        """Pause execution (break into debugger)."""
        self._send_debug_command("pause")

    # ------------------------------------------------------------------
    # Inspection (called when paused)
    # ------------------------------------------------------------------

    def get_call_stack(self) -> List[StackFrame]:
        """Return the current call stack.  Frame 0 = current frame.

        Requires the debuggee to be in a paused state (breakpoint hit,
        step completed, etc.) so that ``_stopped_thread_id`` is set.
        """
        if self._dap_client is None:
            return []
        if self._stopped_thread_id is None:
            return []

        try:
            raw_frames = self._dap_client.get_call_stack(self._stopped_thread_id)
        except DAPError:
            return []

        frames: List[StackFrame] = []
        for f in raw_frames:
            frames.append(StackFrame(
                frame_id=f.get("frame_id", 0),
                function_name=f.get("function_name", "??"),
                file_path=f.get("file", ""),
                line_number=f.get("line", 0),
            ))
        return frames

    def get_variables(self, frame_id: int = 0, scope: str = "locals") -> List[Variable]:
        """Return variables in the given stack frame.

        *frame_id* is the DAP frame id (from ``get_call_stack()``).
        *scope* is one of ``"locals"``, ``"globals"``, or ``"builtins"``.
        """
        if self._dap_client is None:
            return []

        # 1. Get scopes for the frame
        try:
            scopes = self._dap_client.get_scopes(frame_id)
        except DAPError:
            return []

        # 2. Find the matching scope
        target = scope.lower()
        vars_ref = 0
        for s in scopes:
            if s.get("name", "").lower() == target:
                vars_ref = s.get("variables_reference", 0)
                break

        # Fallback: use the first scope if target not found
        if vars_ref == 0 and scopes:
            vars_ref = scopes[0].get("variables_reference", 0)

        if vars_ref == 0:
            return []

        # 3. Get variables for that reference
        try:
            raw_vars = self._dap_client.get_variables(vars_ref)
        except DAPError:
            return []

        variables: List[Variable] = []
        for v in raw_vars:
            variables.append(Variable(
                name=v.get("name", "??"),
                value=v.get("value", ""),
                type_name=v.get("type", ""),
            ))
        return variables

    def evaluate(self, expression: str, frame_id: int = 0) -> str:
        """Evaluate *expression* in the given frame's context.

        Returns the string representation of the result, or an error
        message prefixed with ``"Error: "``.
        """
        if self._dap_client is None:
            return "Error: not connected to debug adapter"

        try:
            result = self._dap_client.evaluate(
                expression,
                frame_id=frame_id if frame_id > 0 else None,
                context="repl",
            )
            return result.get("result", "")
        except DAPError as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_debugpy() -> bool:
        """Return True if debugpy is importable."""
        try:
            import debugpy  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _find_free_port() -> int:
        """Find an available TCP port on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _send_debug_command(self, command: str) -> None:
        """Send a stepping / control command to the debug adapter.

        Requires the debuggee to be paused so ``_stopped_thread_id``
        is set.  The ``pause`` command does not need a thread id.
        """
        if self._dap_client is None:
            return
        try:
            if command == "pause":
                self._dap_client.pause()
            elif self._stopped_thread_id is not None:
                if command == "next":
                    self._dap_client.step_next(self._stopped_thread_id)
                elif command == "step":
                    self._dap_client.step_in(self._stopped_thread_id)
                elif command == "return":
                    self._dap_client.step_out(self._stopped_thread_id)
                elif command == "continue":
                    self._dap_client.continue_(self._stopped_thread_id)
        except DAPError:
            pass

    def _handle_dap_event(self, event_name: str, body: Dict[str, Any]) -> None:
        """Translate a DAP event into the DebugManager event system."""
        if event_name == "stopped":
            reason = body.get("reason", "")
            self._stopped_thread_id = body.get("threadId")
            self._is_paused = True
            if reason == "breakpoint" or reason == "breakpoint_hit":
                self._emit(DebugEvents.BREAKPOINT_HIT, {
                    "thread_id": self._stopped_thread_id,
                    "reason": reason,
                })
            self._emit(DebugEvents.PAUSED, {
                "thread_id": self._stopped_thread_id,
                "reason": reason,
            })

        elif event_name == "continued":
            self._is_paused = False
            self._emit(DebugEvents.RESUMED, {
                "thread_id": body.get("threadId"),
            })

        elif event_name == "terminated":
            self._is_running = False
            self._is_paused = False
            self._emit(DebugEvents.PROCESS_ENDED, {
                "exit_code": body.get("exit_code", 0),
            })
            self._cleanup_dap()

        elif event_name == "output":
            self._emit(DebugEvents.OUTPUT, {
                "text": body.get("output", ""),
                "stream": body.get("category", "stdout"),
            })

        elif event_name == "thread":
            # Track thread start/exit; not surfaced to GUI yet
            pass

        elif event_name == "disconnected":
            self._is_running = False
            self._is_paused = False
            self._emit(DebugEvents.PROCESS_ENDED, {"exit_code": -1})
            self._cleanup_dap()

    def _sync_breakpoints_to_adapter(self) -> None:
        """Send all currently-set breakpoints to the debug adapter."""
        if self._dap_client is None:
            return
        for file_path, lines in self._breakpoints.items():
            if lines:
                try:
                    self._dap_client.set_breakpoints(file_path, sorted(lines))
                except DAPError:
                    pass

    def _cleanup_dap(self) -> None:
        """Disconnect the DAP client and reset session state."""
        if self._dap_client:
            try:
                self._dap_client.disconnect()
            except Exception:
                pass
            self._dap_client = None
        self._stopped_thread_id = None
        self._debugpy_port = 0

        if self._debugpy_process is not None:
            try:
                if self._debugpy_process.poll() is None:
                    self._debugpy_process.terminate()
            except Exception:
                pass
            self._debugpy_process = None


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dm = DebugManager()

    # Breakpoints
    bp = dm.set_breakpoint("/tmp/test.py", 10)
    print(f"Set breakpoint at line {bp.line_number}")

    dm.toggle_breakpoint("/tmp/test.py", 10)
    bps = dm.get_breakpoints("/tmp/test.py")
    print(f"After toggle: {len(bps)} breakpoints")

    dm.set_breakpoint("/tmp/test.py", 15)
    dm.set_breakpoint("/tmp/test.py", 20)
    print(f"All breakpoints: {len(dm.get_breakpoints())}")

    dm.clear_all_breakpoints()
    print(f"After clear: {len(dm.get_breakpoints())}")

    print(f"debugpy available: {dm._debugpy_available}")
