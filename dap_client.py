"""
dap_client.py — Minimal Debug Adapter Protocol (DAP) client for debugpy.

Connects to a running debugpy server over TCP, handles DAP message
framing (Content-Length + JSON body), sends requests synchronously,
and runs a background listener thread that dispatches events to a
user-registered callback.

Usage:
    from dap_client import DAPClient

    dap = DAPClient(host="127.0.0.1", port=5678)

    # Register event handler BEFORE connecting
    dap.on_event = lambda event, body: print(f"[{event}]", body)

    dap.connect()
    dap.initialize()

    # Get call stack
    frames = dap.get_call_stack(thread_id=1)
    for f in frames:
        print(f["function_name"], f["file"], f["line"])

    # Step
    dap.step_next(thread_id=1)

    # Cleanup
    dap.disconnect()
"""

from __future__ import annotations

import socket
import json
import threading
import time
from typing import Optional, Callable, Dict, Any, List, Tuple


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DAPError(Exception):
    """Base exception for DAP client operations."""


class DAPConnectionError(DAPError):
    """Failed to connect to the debug adapter."""


class DAPRequestError(DAPError):
    """A DAP request returned an error response."""


class DAPTimeoutError(DAPError):
    """A DAP request timed out waiting for a response."""


# ---------------------------------------------------------------------------
# DAPClient
# ---------------------------------------------------------------------------

class DAPClient:
    """Minimal DAP client that speaks to a debugpy (or any DAP-compliant) server."""

    # How long to wait for a response to a request (seconds)
    DEFAULT_TIMEOUT = 10.0

    def __init__(self, host: str = "127.0.0.1", port: int = 5678) -> None:
        """Initialise the DAP client.

        Args:
            host: Hostname or IP of the debug adapter (usually localhost).
            port: TCP port the adapter is listening on.
        """
        self._host = host
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._file: Any = None  # socket.makefile() for line reading
        self._seq = 0
        self._lock = threading.Lock()
        self._running = False

        # Response routing: seq → condition variable + result
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        # Event callback: (event_name: str, body: dict) -> None
        self.on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None

        # Listener thread
        self._listener_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        """Connect to the debug adapter.

        Args:
            timeout: Connection timeout in seconds.

        Raises:
            DAPConnectionError: If the connection fails.
        """
        if self._socket is not None:
            return  # already connected

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self._host, self._port))
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            sock.close()
            raise DAPConnectionError(
                f"Could not connect to {self._host}:{self._port}: {exc}"
            ) from exc

        sock.settimeout(None)  # switch to blocking for reads
        self._socket = sock
        self._file = sock.makefile("rb", buffering=0)
        self._running = True

        # Start background listener
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name="dap-listener",
        )
        self._listener_thread.start()

    def disconnect(self) -> None:
        """Close the connection and stop the listener thread."""
        self._running = False
        try:
            if self._socket:
                self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self._socket:
                self._socket.close()
        except OSError:
            pass
        self._socket = None
        self._file = None

        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2.0)
        self._listener_thread = None

    @property
    def is_connected(self) -> bool:
        """Return True if the client is currently connected."""
        return self._socket is not None and self._running

    # ------------------------------------------------------------------
    # DAP wire protocol: message framing
    # ------------------------------------------------------------------

    def _send_message(self, msg: Dict[str, Any]) -> None:
        """Encode and send a DAP message over the wire.

        Format:
            Content-Length: <N>\\r\\n
            \\r\\n
            <JSON body of exactly N bytes>

        Args:
            msg: The JSON-serialisable message dict.
        """
        body = json.dumps(msg, ensure_ascii=False)
        body_bytes = body.encode("utf-8")
        header = f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("ascii")
        self._send_raw(header + body_bytes)

    def _send_raw(self, data: bytes) -> None:
        """Send raw bytes on the socket."""
        if self._socket is None:
            raise DAPConnectionError("Not connected.")
        try:
            self._socket.sendall(data)
        except OSError as exc:
            raise DAPConnectionError(f"Send failed: {exc}") from exc

    def _recv_message(self, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """Read and parse one complete DAP message from the socket.

        Returns the parsed JSON dict, or None on timeout / closed connection.

        Raises:
            DAPConnectionError: On framing or JSON errors.
        """
        if self._file is None:
            raise DAPConnectionError("Not connected.")

        # Read header lines until we find Content-Length
        content_length: Optional[int] = None
        while content_length is None:
            line = self._read_line(timeout)
            if line is None:
                return None  # EOF or timeout
            line = line.strip()
            if not line:
                # Empty line = end of headers; if we haven't seen
                # Content-Length yet, something is malformed
                if content_length is None:
                    raise DAPConnectionError(
                        "Malformed DAP message: no Content-Length header"
                    )
                break
            if line.lower().startswith(b"content-length:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except ValueError as exc:
                    raise DAPConnectionError(
                        f"Malformed Content-Length header: {line!r}"
                    ) from exc

        # Read exactly content_length bytes of JSON body
        body_bytes = self._read_exactly(content_length, timeout)
        if body_bytes is None:
            raise DAPConnectionError(
                f"Incomplete DAP message: expected {content_length} bytes"
            )

        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DAPConnectionError(f"Malformed JSON body: {exc}") from exc

    def _read_line(self, timeout: float) -> Optional[bytes]:
        """Read one line (terminated by \\n) from the socket. Returns None on EOF."""
        if self._file is None:
            return None

        buf = bytearray()
        while True:
            try:
                byte = self._file.read(1)
            except OSError:
                return None
            if not byte:
                return None  # EOF
            buf.append(byte[0] if isinstance(byte, bytes) else byte)
            if buf[-1] == 0x0A:  # \n
                return bytes(buf)

    def _read_exactly(self, n: int, timeout: float) -> Optional[bytes]:
        """Read exactly *n* bytes from the socket. Returns None on EOF."""
        if self._file is None:
            return None
        try:
            data = self._file.read(n)
        except OSError:
            return None
        if data is None or len(data) < n:
            return None
        return data

    # ------------------------------------------------------------------
    # Request/response lifecycle
    # ------------------------------------------------------------------

    def _send_request(
        self,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Send a DAP request. Returns the sequence number used.

        The caller should then call `_await_response(seq)` to get the result.
        """
        with self._lock:
            self._seq += 1
            seq = self._seq

        msg: Dict[str, Any] = {
            "seq": seq,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            msg["arguments"] = arguments

        self._send_message(msg)
        return seq

    def _await_response(self, seq: int, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """Block until the response for sequence number *seq* arrives.

        Returns the parsed response body dict.

        Raises:
            DAPRequestError: If the response contains an error.
            DAPTimeoutError: If no response arrives within *timeout* seconds.
        """
        cond = threading.Condition()
        result_container: Dict[str, Any] = {}

        with self._pending_lock:
            self._pending[seq] = {"cond": cond, "result": result_container}

        try:
            with cond:
                if not result_container:
                    cond.wait(timeout=timeout)
        finally:
            with self._pending_lock:
                self._pending.pop(seq, None)

        if not result_container:
            raise DAPTimeoutError(
                f"No response for request seq={seq} after {timeout:.1f}s"
            )

        response = result_container.get("response")
        if response is None:
            raise DAPRequestError(
                f"Request seq={seq} failed: no response payload"
            )

        if not response.get("success", False):
            error_msg = response.get("message", "Unknown DAP error")
            raise DAPRequestError(f"DAP error: {error_msg}")

        return response.get("body", {})

    # ------------------------------------------------------------------
    # Background listener
    # ------------------------------------------------------------------

    def _listener_loop(self) -> None:
        """Read incoming DAP messages and dispatch to response waiters
        or the event callback."""
        while self._running:
            try:
                msg = self._recv_message(timeout=1.0)
            except DAPConnectionError:
                if self._running:
                    # Unexpected disconnect
                    if self.on_event:
                        self.on_event("disconnected", {})
                    self._running = False
                break

            if msg is None:
                continue  # timeout, loop back

            msg_type = msg.get("type", "")

            if msg_type == "response":
                self._handle_response(msg)
            elif msg_type == "event":
                self._handle_event(msg)

    def _handle_response(self, msg: Dict[str, Any]) -> None:
        """Route a response to the waiter that sent the matching request."""
        request_seq = msg.get("request_seq")
        if request_seq is None:
            return
        with self._pending_lock:
            entry = self._pending.get(request_seq)
        if entry is None:
            return
        entry["result"]["response"] = msg
        with entry["cond"]:
            entry["cond"].notify_all()

    def _handle_event(self, msg: Dict[str, Any]) -> None:
        """Dispatch a DAP event to the user callback."""
        event_name = msg.get("event", "")
        body = msg.get("body", {})
        if self.on_event:
            try:
                self.on_event(event_name, body)
            except Exception:
                pass  # don't crash the listener on callback errors

    # ==================================================================
    # DAP request helpers — the public API
    # ==================================================================

    def initialize(self) -> Dict[str, Any]:
        """Send the DAP initialize request.

        Must be called first after connecting.

        Returns:
            The capabilities dict from the adapter.
        """
        seq = self._send_request("initialize", {
            "clientID": "nexcore-ide",
            "clientName": "Nexcore IDE",
            "adapterID": "debugpy",
            "locale": "en-US",
            "linesStartAt1": True,
            "columnsStartAt1": False,
            "pathFormat": "path",
        })
        return self._await_response(seq)

    def launch(
        self,
        program: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a launch request. (Usually debugpy is already attached;
        this is here for completeness.)"""
        args: Dict[str, Any] = dict(kwargs)
        if program:
            args["program"] = program
        seq = self._send_request("launch", args)
        return self._await_response(seq)

    def get_call_stack(self, thread_id: int) -> List[Dict[str, Any]]:
        """Retrieve the call stack for *thread_id*.

        Returns:
            List of frame dicts, each with keys:
                frame_id, function_name, file, line, column
        """
        seq = self._send_request("stackTrace", {
            "threadId": thread_id,
        })
        body = self._await_response(seq)

        frames: List[Dict[str, Any]] = []
        for f in body.get("stackFrames", []):
            source = f.get("source", {}) or {}
            frames.append({
                "frame_id": f.get("id"),
                "function_name": f.get("name", "??"),
                "file": source.get("path", source.get("name", "")),
                "line": f.get("line", 0),
                "column": f.get("column", 0),
            })
        return frames

    def get_scopes(self, frame_id: int) -> List[Dict[str, Any]]:
        """Retrieve the variable scopes for a stack frame.

        Returns:
            List of scope dicts, each with:
                name, variables_reference, expensive
            Typical scopes: "Locals", "Globals", "Builtins"
        """
        seq = self._send_request("scopes", {"frameId": frame_id})
        body = self._await_response(seq)
        return body.get("scopes", [])

    def get_variables(self, variables_reference: int) -> List[Dict[str, Any]]:
        """Retrieve the variables for a given variables reference.

        Args:
            variables_reference: From a scope or a structured variable.

        Returns:
            List of variable dicts:
                {name, value, type, variables_reference}
        """
        seq = self._send_request("variables", {
            "variablesReference": variables_reference,
        })
        body = self._await_response(seq)
        vars_list: List[Dict[str, Any]] = []
        for v in body.get("variables", []):
            vars_list.append({
                "name": v.get("name", "??"),
                "value": v.get("value", ""),
                "type": v.get("type", ""),
                "variables_reference": v.get("variablesReference", 0),
            })
        return vars_list

    def evaluate(
        self,
        expression: str,
        frame_id: Optional[int] = None,
        context: str = "repl",
    ) -> Dict[str, Any]:
        """Evaluate an expression in the context of *frame_id*.

        Args:
            expression: The Python expression to evaluate.
            frame_id: Stack frame to evaluate in. None = global context.
            context: One of "watch", "repl", "hover".

        Returns:
            {result, type, variables_reference}
        """
        args: Dict[str, Any] = {
            "expression": expression,
            "context": context,
        }
        if frame_id is not None:
            args["frameId"] = frame_id

        seq = self._send_request("evaluate", args)
        body = self._await_response(seq)
        return {
            "result": body.get("result", ""),
            "type": body.get("type", ""),
            "variables_reference": body.get("variablesReference", 0),
        }

    def set_breakpoints(
        self,
        file_path: str,
        lines: List[int],
    ) -> List[Dict[str, Any]]:
        """Set breakpoints in *file_path* at the given line numbers.

        Args:
            file_path: Absolute path to the source file.
            lines: List of 1-based line numbers.

        Returns:
            List of breakpoint result dicts from the adapter.
        """
        seq = self._send_request("setBreakpoints", {
            "source": {"path": file_path},
            "breakpoints": [{"line": ln} for ln in lines],
        })
        body = self._await_response(seq)
        return body.get("breakpoints", [])

    # -- Stepping -----------------------------------------------------------

    def step_next(self, thread_id: int) -> None:
        """Step over the current line."""
        self._send_request("next", {"threadId": thread_id})

    def step_in(self, thread_id: int) -> None:
        """Step into the current function call."""
        self._send_request("stepIn", {"threadId": thread_id})

    def step_out(self, thread_id: int) -> None:
        """Step out of the current function."""
        self._send_request("stepOut", {"threadId": thread_id})

    def continue_(self, thread_id: int) -> None:
        """Continue execution until the next breakpoint."""
        self._send_request("continue", {"threadId": thread_id})

    def pause(self) -> None:
        """Pause execution (break into the debugger)."""
        self._send_request("pause", {})

    def disconnect_dap(self, restart: bool = False) -> None:
        """Send a disconnect request to the debug adapter."""
        try:
            seq = self._send_request("disconnect", {"restart": restart})
            self._await_response(seq, timeout=3.0)
        except (DAPError, DAPConnectionError, DAPTimeoutError):
            pass


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import subprocess
    import tempfile
    from pathlib import Path

    print("=" * 60)
    print("DAPClient — end-to-end test with debugpy")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "target.py"
        script.write_text("""\
def add(a, b):
    result = a + b          # breakpoint here
    return result

def main():
    x = 10
    y = 20
    total = add(x, y)
    print(f"Total: {total}")

main()
""")

        # 1. Launch debugpy subprocess
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "debugpy",
                "--listen", "5679",
                "--wait-for-client",
                str(script),
            ],
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # 2. Connect DAP client
        dap = DAPClient(host="127.0.0.1", port=5679)

        events: List[tuple] = []

        def on_event(name: str, body: dict) -> None:
            events.append((name, body))
            print(f"  [event] {name}  body={ {k: v for k, v in body.items() if k != 'source'} }")

        dap.on_event = on_event

        try:
            dap.connect(timeout=5.0)
            print("Connected to debugpy.")

            caps = dap.initialize()
            print(f"Initialized. Adapter: {caps.get('supportsConfigurationDoneRequest', '?')}")

            # Set a breakpoint
            bp_results = dap.set_breakpoints(str(script), [3])
            print(f"Breakpoint set: {bp_results}")

            # Continue to let it hit the breakpoint
            dap.continue_(thread_id=0)  # fallback — we'll get the real threadId from the stopped event

            # Wait for 'stopped' event
            time.sleep(1.5)

            thread_id = 1
            for name, body in events:
                if name == "stopped":
                    thread_id = body.get("threadId", 1)
                    break

            print(f"Thread ID from event: {thread_id}")

            # Get call stack
            frames = dap.get_call_stack(thread_id)
            print(f"Call stack ({len(frames)} frames):")
            for f in frames:
                print(f"  {f['function_name']}() at {f['file']}:{f['line']}")

            # Get variables
            if frames:
                scopes = dap.get_scopes(frames[0]["frame_id"])
                for scope in scopes:
                    if scope["name"] == "Locals":
                        vars = dap.get_variables(scope["variables_reference"])
                        print(f"Locals ({scope['name']}):")
                        for v in vars:
                            print(f"  {v['name']} = {v['value']} ({v['type']})")
                        break

            # Evaluate
            result = dap.evaluate("a + b", frame_id=frames[0]["frame_id"])
            print(f"evaluate('a + b') → {result}")

            # Continue to finish
            dap.continue_(thread_id)

        except DAPError as exc:
            print(f"DAP error: {exc}")
        finally:
            dap.disconnect()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"Captured {len(events)} events.")
    print("Done.")
