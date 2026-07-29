"""
lint_manager.py — Background linting and error/warning reporting.

Runs pyflakes (fast, syntax + undefined-name errors) and optionally
flake8/pylint in a background thread with a debounce mechanism.
Returns diagnostics as {line, column, severity, message} so the GUI
can draw squiggly underlines and populate a "Problems" panel.

Usage (GUI hooks):
    from lint_manager import LintManager

    lm = LintManager()

    # Schedule a lint (debounced)
    lm.schedule_lint(source_code, file_path="/project/main.py", debounce_ms=500)

    # Or lint immediately
    diagnostics = lm.lint(source_code, file_path="/project/main.py")

    # Listen for results
    lm.on_diagnostics = lambda diags: gui.show_errors(diags)

    # Shutdown
    lm.shutdown()
"""

from __future__ import annotations

import os
import sys
import re
import subprocess
import threading
import tempfile
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Diagnostic:
    """A linting error/warning/info for the Problems panel and squiggly lines."""
    line: int            # 1-based
    column: int          # 0-based (or -1 if unknown)
    end_column: int      # 0-based (or -1)
    severity: str        # "error", "warning", "info"
    message: str
    source: str          # "pyflakes", "flake8", "pylint", "syntax"
    code: str = ""       # e.g. "F821", "E302"


# ---------------------------------------------------------------------------
# LintManager
# ---------------------------------------------------------------------------

class LintManager:
    """Background linting with debounce and multiple checkers."""

    def __init__(self) -> None:
        self.on_diagnostics: Optional[Callable[[List[Diagnostic]], None]] = None

        self._debounce_timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._running = True

        # Check which linters are available
        self._pyflakes_available = self._check_pyflakes()
        self._flake8_available = self._check_flake8()
        self._pylint_available = self._check_pylint()

    # ------------------------------------------------------------------
    # Debounced lint
    # ------------------------------------------------------------------

    def schedule_lint(
        self,
        source: str,
        file_path: str = "",
        debounce_ms: int = 500,
        run_pyflakes: bool = True,
        run_flake8: bool = False,
        run_pylint: bool = False,
    ) -> None:
        """Schedule a debounced lint. Cancels any pending lint for the same file."""
        with self._lock:
            if file_path in self._debounce_timers:
                self._debounce_timers[file_path].cancel()

            timer = threading.Timer(
                debounce_ms / 1000.0,
                self._do_lint,
                args=[source, file_path, run_pyflakes, run_flake8, run_pylint],
            )
            timer.daemon = True
            self._debounce_timers[file_path] = timer
            timer.start()

    def lint(
        self,
        source: str,
        file_path: str = "",
        run_pyflakes: bool = True,
        run_flake8: bool = False,
        run_pylint: bool = False,
    ) -> List[Diagnostic]:
        """Run lint synchronously. Returns diagnostics immediately."""
        return self._run_lint(source, file_path, run_pyflakes, run_flake8, run_pylint)

    def lint_before_run(self, source: str, file_path: str = "") -> List[Diagnostic]:
        """Quick pre-run check for obvious syntax errors.
        Returns only error-level diagnostics.
        """
        diags = self._run_lint(source, file_path, run_pyflakes=True, run_flake8=False, run_pylint=False)
        return [d for d in diags if d.severity == "error"]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _do_lint(
        self,
        source: str,
        file_path: str,
        run_pyflakes: bool,
        run_flake8: bool,
        run_pylint: bool,
    ) -> None:
        if not self._running:
            return
        diags = self._run_lint(source, file_path, run_pyflakes, run_flake8, run_pylint)
        if self.on_diagnostics and self._running:
            self.on_diagnostics(diags)

    def _run_lint(
        self,
        source: str,
        file_path: str,
        run_pyflakes: bool,
        run_flake8: bool,
        run_pylint: bool,
    ) -> List[Diagnostic]:
        """Core lint logic: run available checkers and merge results."""
        all_diags: List[Diagnostic] = []

        # 1. Syntax check (always run, uses compile())
        syntax_errs = self._check_syntax(source, file_path)
        all_diags.extend(syntax_errs)

        # If there are syntax errors, skip semantic checkers (they'll crash)
        if syntax_errs:
            return all_diags

        # 2. Pyflakes
        if run_pyflakes and self._pyflakes_available:
            all_diags.extend(self._run_pyflakes(source, file_path))

        # 3. Flake8
        if run_flake8 and self._flake8_available:
            all_diags.extend(self._run_flake8(file_path))

        # 4. Pylint
        if run_pylint and self._pylint_available:
            all_diags.extend(self._run_pylint(file_path))

        return all_diags

    # ------------------------------------------------------------------
    # Syntax check (stdlib, zero-dependency)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_syntax(source: str, file_path: str) -> List[Diagnostic]:
        """Compile source to detect syntax errors."""
        try:
            compile(source, file_path or "<string>", "exec")
        except SyntaxError as exc:
            return [Diagnostic(
                line=exc.lineno or 1,
                column=(exc.offset or 1) - 1,
                end_column=exc.offset or 1,
                severity="error",
                message=f"SyntaxError: {exc.msg}",
                source="syntax",
                code="E999",
            )]
        return []

    # ------------------------------------------------------------------
    # Pyflakes
    # ------------------------------------------------------------------

    def _run_pyflakes(self, source: str, file_path: str) -> List[Diagnostic]:
        """Run pyflakes on the source."""
        try:
            import pyflakes.api
            import pyflakes.reporter
        except ImportError:
            return []

        diags: List[Diagnostic] = []

        class _Reporter(pyflakes.reporter.Reporter):
            def __init__(self):
                self._diags: List[Diagnostic] = []

            def unexpectedError(self, filename, msg):
                pass

            def syntaxError(self, filename, msg, lineno, offset, text):
                self._diags.append(Diagnostic(
                    line=lineno,
                    column=(offset or 1) - 1,
                    end_column=offset or 1,
                    severity="error",
                    message=f"Syntax: {msg}",
                    source="pyflakes",
                ))

            def flake(self, message):
                self._diags.append(Diagnostic(
                    line=message.lineno,
                    column=(message.col or 1) - 1,
                    end_column=-1,
                    severity="warning",
                    message=str(message),
                    source="pyflakes",
                    code=f"F{message.message_code}" if hasattr(message, "message_code") else "",
                ))

        reporter = _Reporter()
        try:
            pyflakes.api.check(source, file_path or "<string>", reporter=reporter)
        except Exception:
            pass

        return reporter._diags

    def _run_flake8(self, file_path: str) -> List[Diagnostic]:
        """Run flake8 via subprocess on a temp file."""
        if not file_path or not os.path.isfile(file_path):
            return []
        try:
            r = subprocess.run(
                [sys.executable, "-m", "flake8", "--format=default", file_path],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return []

        diags: List[Diagnostic] = []
        pattern = re.compile(r"^.+:(\d+):(\d+):\s+(\w+)\s+(.+)$")
        for line in r.stdout.splitlines() + r.stderr.splitlines():
            m = pattern.match(line.strip())
            if m:
                diags.append(Diagnostic(
                    line=int(m.group(1)),
                    column=int(m.group(2)) - 1,
                    end_column=-1,
                    severity="warning",
                    message=m.group(4),
                    source="flake8",
                    code=m.group(3),
                ))
        return diags

    def _run_pylint(self, file_path: str) -> List[Diagnostic]:
        """Run pylint via subprocess."""
        if not file_path or not os.path.isfile(file_path):
            return []
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pylint",
                 "--output-format=text", "--score=n", file_path],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            return []

        diags: List[Diagnostic] = []
        pattern = re.compile(
            r"^.+:(\d+):(\d+):\s+(\w+):\s+\((\w+)\)\s+(.*)$"
        )
        for line in r.stdout.splitlines():
            m = pattern.match(line.strip())
            if m:
                severity = m.group(3).lower()
                if severity == "error" or severity == "fatal":
                    severity = "error"
                elif severity == "warning":
                    severity = "warning"
                else:
                    severity = "info"
                diags.append(Diagnostic(
                    line=int(m.group(1)),
                    column=int(m.group(2)) - 1,
                    end_column=-1,
                    severity=severity,
                    message=m.group(5),
                    source="pylint",
                    code=m.group(4),
                ))
        return diags

    # ------------------------------------------------------------------
    # Availability checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_pyflakes() -> bool:
        try:
            import pyflakes  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _check_flake8() -> bool:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "flake8", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _check_pylint() -> bool:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pylint", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def shutdown(self) -> None:
        """Cancel pending timers and stop processing."""
        self._running = False
        with self._lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lm = LintManager()

    print(f"Pyflakes available: {lm._pyflakes_available}")
    print(f"Flake8 available: {lm._flake8_available}")
    print(f"Pylint available: {lm._pylint_available}")

    # Test with some bad code
    bad_code = '''\
def foo()
    x = 1
    y = undefined_name
    return x + y
'''

    diags = lm.lint(bad_code, file_path="test.py")
    for d in diags:
        print(f"  L{d.line}: [{d.severity}] {d.source}/{d.code}: {d.message}")

    # Pre-run check
    pre = lm.lint_before_run(bad_code, "test.py")
    print(f"\nPre-run errors: {len(pre)}")

    lm.shutdown()
