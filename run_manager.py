"""
run_manager.py — Run / Play button backend logic.

Orchestrates saving the active file (optional auto-save before run),
detecting the correct Python interpreter, and launching the script
via terminal_manager. Tracks a running/stopped state so the GUI can
swap Play ↔ Stop icons.

Usage (GUI hooks):
    from run_manager import RunManager

    rm = RunManager(tabs_mgr, terminal_mgr)
    rm.run_active_file()           # Play button / F5
    rm.run_active_file_with_args("--verbose")
    rm.stop()                      # Stop button
    rm.is_running                  # bool
"""

from __future__ import annotations

import os
from typing import Optional, Callable, List
from dataclasses import dataclass

from tabs_manager import TabsManager, TabInfo
from terminal_manager import TerminalManager, TerminalInstance


@dataclass
class RunResult:
    """Result of a script execution."""
    exit_code: int
    execution_time: float
    file_path: str


class RunManager:
    """Handles the Run button and its associated state machine."""

    def __init__(
        self,
        tabs_manager: TabsManager,
        terminal_manager: TerminalManager,
        on_state_change: Optional[Callable[[bool, Optional[RunResult]], None]] = None,
        # on_state_change(is_running, result_or_none)
    ) -> None:
        self._tabs = tabs_manager
        self._terminal = terminal_manager
        self.on_state_change = on_state_change

        self._active_terminal_id: Optional[str] = None
        self._base_state_callbacks: dict[str, Optional[Callable]] = {}
        self._is_running = False
        self._last_result: Optional[RunResult] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._last_result

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run_active_file(
        self,
        args: Optional[List[str]] = None,
        auto_save: bool = True,
    ) -> Optional[str]:
        """Run the currently-active tab's file.
        Returns the terminal ID where output will appear, or None if
        nothing to run.
        """
        tab = self._tabs.active_tab
        if tab is None:
            return None

        if tab.is_untitled:
            return None  # GUI should prompt Save As first

        # Auto-save if dirty
        if tab.is_modified and auto_save:
            self._tabs.save(tab.tab_id)

        # Check file still exists
        if not os.path.isfile(tab.path):
            return None

        return self._launch(tab.path, args)

    def run_file(
        self,
        file_path: str,
        args: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Run an arbitrary Python file (not necessarily the active tab)."""
        if not os.path.isfile(file_path):
            return None
        # If this file is open in a tab and dirty, auto-save it
        tab = self._tabs.get_tab_by_path(file_path)
        if tab and tab.is_modified:
            self._tabs.save(tab.tab_id)
        return self._launch(file_path, args)

    def _launch(self, file_path: str, args: Optional[List[str]] = None) -> str:
        """Internal: create or reuse a terminal instance and run the file."""
        # Reuse existing terminal if available, otherwise create
        inst: TerminalInstance
        if self._active_terminal_id:
            inst = self._terminal.get_terminal(self._active_terminal_id)
            if inst is None:
                inst = self._terminal.create_terminal(cwd=os.path.dirname(file_path))
                self._active_terminal_id = inst.terminal_id
        else:
            inst = self._terminal.create_terminal(cwd=os.path.dirname(file_path))
            self._active_terminal_id = inst.terminal_id

        # Wire completion callback
        # Reused terminals already carry the wrapper installed by the
        # previous run. Preserve the original TerminalManager callback
        # once so repeated runs do not build a callback chain and emit
        # duplicate completion events.
        original_state_cb = self._base_state_callbacks.setdefault(
            inst.terminal_id, inst.on_state_change,
        )
        completion_sent = False

        def _on_state(state):
            nonlocal completion_sent
            if not state.is_running and state.exit_code is not None and not completion_sent:
                completion_sent = True
                self._is_running = False
                self._last_result = RunResult(
                    exit_code=state.exit_code,
                    execution_time=state.execution_time or 0.0,
                    file_path=file_path,
                )
                if self.on_state_change:
                    self.on_state_change(False, self._last_result)
            if original_state_cb:
                original_state_cb(state)

        inst.on_state_change = _on_state

        self._is_running = True
        self._last_result = None
        if self.on_state_change:
            self.on_state_change(True, None)

        inst.run_python_file(file_path, args=args)
        return inst.terminal_id

    def run_with_args(self, arg_string: str) -> Optional[str]:
        """Run the active file with CLI arguments (space-separated string)."""
        import shlex
        args = shlex.split(arg_string) if arg_string else None
        return self.run_active_file(args=args)

    def stop(self) -> None:
        """Stop the currently-running process."""
        if self._active_terminal_id:
            inst = self._terminal.get_terminal(self._active_terminal_id)
            if inst:
                inst.kill()
        self._is_running = False

    def restart(self) -> Optional[str]:
        """Stop and re-run the last file."""
        self.stop()
        if self._last_result:
            return self.run_file(self._last_result.file_path)
        return self.run_active_file()


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import time
    from pathlib import Path
    from tabs_manager import TabsManager
    from terminal_manager import TerminalManager

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "demo.py"
        script.write_text("import time; print('Running...'); time.sleep(0.3); print('OK')")

        tabs = TabsManager()
        tm = TerminalManager()
        rm = RunManager(tabs, tm)

        tab = tabs.open_file(str(script))
        tid = rm.run_active_file()
        print(f"Running in terminal: {tid}")

        while rm.is_running:
            if tid:
                inst = tm.get_terminal(tid)
                if inst:
                    for line in inst.read_all_output():
                        print(f"[{line.stream}] {line.text}", end="")
            time.sleep(0.1)

        print(f"Done. Exit code: {rm.last_result.exit_code if rm.last_result else '?'}")
        tm.shutdown()
