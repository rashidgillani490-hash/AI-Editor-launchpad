"""
test_manager.py — Testing panel backend.

Auto-discovers pytest/unittest tests, runs them, and streams results
back per-test (not just a final summary). Supports running all tests,
a single test, a test file, or a folder of tests.

Usage (GUI hooks):
    from test_manager import TestManager

    tm = TestManager("/project")

    # Discovery
    tests = tm.discover_tests()

    # Run
    tm.run_all(on_result=my_callback, on_done=my_done_callback)
    tm.run_test("test_file.py::test_function", on_result=...)
    tm.stop()
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import threading
import json
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class TestNode:
    """A discovered test: function, class, or file container."""
    name: str
    test_id: str           # e.g. "file.py::TestClass::test_func"
    kind: str              # "file" | "class" | "function"
    children: List[TestNode] = field(default_factory=list)
    parent: Optional[TestNode] = None


@dataclass
class TestResult:
    """Result of running a single test."""
    test_id: str
    status: str            # "passed" | "failed" | "skipped" | "error"
    duration: float        # seconds
    error_message: Optional[str] = None
    output: str = ""


# ---------------------------------------------------------------------------
# TestManager
# ---------------------------------------------------------------------------

class TestManager:
    """Discover and run tests using pytest (primary) or unittest."""

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.abspath(project_root)
        self._process: Optional[subprocess.Popen] = None
        self._cancel_event: Optional[threading.Event] = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_tests(self) -> List[TestNode]:
        """Use pytest --collect-only to build a tree of test nodes."""
        nodes: List[TestNode] = []

        if self._has_pytest():
            nodes = self._discover_pytest()
        else:
            nodes = self._discover_unittest()

        return nodes

    def _has_pytest(self) -> bool:
        """Check if pytest is available."""
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _discover_pytest(self) -> List[TestNode]:
        """Run pytest --collect-only -q and parse output."""
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = r.stdout + r.stderr
        except Exception:
            return []

        # Each line looks like: "tests/test_foo.py::test_bar" or "tests/test_foo.py::TestClass::test_method"
        nodes: List[TestNode] = []
        seen_files: Dict[str, TestNode] = {}

        for line in output.splitlines():
            line = line.strip()
            if "::" not in line:
                continue
            # Parse
            parts = line.split("::")
            file_path = parts[0]

            if file_path not in seen_files:
                file_node = TestNode(
                    name=os.path.basename(file_path),
                    test_id=file_path,
                    kind="file",
                )
                seen_files[file_path] = file_node
                nodes.append(file_node)
            else:
                file_node = seen_files[file_path]

            if len(parts) == 2:
                # function-level
                func_node = TestNode(
                    name=parts[1],
                    test_id=line,
                    kind="function",
                    parent=file_node,
                )
                file_node.children.append(func_node)
            elif len(parts) == 3:
                # class::method
                class_id = f"{parts[0]}::{parts[1]}"
                # Find or create class node
                class_node = None
                for child in file_node.children:
                    if child.test_id == class_id and child.kind == "class":
                        class_node = child
                        break
                if class_node is None:
                    class_node = TestNode(
                        name=parts[1],
                        test_id=class_id,
                        kind="class",
                        parent=file_node,
                    )
                    file_node.children.append(class_node)
                method_node = TestNode(
                    name=parts[2],
                    test_id=line,
                    kind="function",
                    parent=class_node,
                )
                class_node.children.append(method_node)

        return nodes

    def _discover_unittest(self) -> List[TestNode]:
        """Basic unittest discovery by scanning for test_*.py files."""
        nodes: List[TestNode] = []
        for dirpath, _, filenames in os.walk(self.project_root):
            for fname in filenames:
                if fname.startswith("test_") and fname.endswith(".py"):
                    full = os.path.join(dirpath, fname)
                    file_node = TestNode(
                        name=fname,
                        test_id=full,
                        kind="file",
                    )
                    # Parse for test functions
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            content = f.read()
                        for m in re.finditer(
                            r"def\s+(test_\w+)\s*\(", content
                        ):
                            func_node = TestNode(
                                name=m.group(1),
                                test_id=f"{full}::{m.group(1)}",
                                kind="function",
                                parent=file_node,
                            )
                            file_node.children.append(func_node)
                    except OSError:
                        pass
                    nodes.append(file_node)
        return nodes

    # ------------------------------------------------------------------
    # Run tests
    # ------------------------------------------------------------------

    def run_all(
        self,
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Run all discovered tests."""
        self._run_pytest([], on_result, on_done)

    def run_test(
        self,
        test_id: str,
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Run a single test by its node ID."""
        self._run_pytest([test_id], on_result, on_done)

    def run_file(
        self,
        file_path: str,
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Run all tests in a file."""
        self._run_pytest([file_path], on_result, on_done)

    def run_folder(
        self,
        folder_path: str,
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Run all tests under a folder."""
        self._run_pytest([folder_path], on_result, on_done)

    def rerun_failed(
        self,
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Re-run only previously-failed tests."""
        self._run_pytest(["--lf"], on_result, on_done)

    def stop(self) -> None:
        """Stop the currently-running test suite."""
        if self._cancel_event:
            self._cancel_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_pytest(
        self,
        extra_args: List[str],
        on_result: Callable[[TestResult], None],
        on_done: Optional[Callable[[], None]],
    ) -> None:
        """Run pytest with JSON report for per-test streaming."""
        self._cancel_event = threading.Event()

        # Use pytest-json-report or fall back to parsing verbose output
        args = [
            sys.executable, "-m", "pytest",
            "-v",
            "--tb=short",
            "--no-header",
        ]
        # Try to use pytest-json-report if available
        try:
            r = subprocess.run(
                [sys.executable, "-c", "import pytest_jsonreport"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                report_path = os.path.join(
                    self.project_root, ".nexcore_test_report.json"
                )
                args.extend(["--json-report", f"--json-report-file={report_path}"])
        except Exception:
            pass

        args.extend(extra_args)

        self._process = subprocess.Popen(
            args,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        def _reader():
            if self._process is None:
                return
            # Parse pytest verbose output line by line
            # Lines look like:
            #   tests/test_foo.py::test_bar PASSED [ 50%]
            #   tests/test_foo.py::test_baz FAILED [100%]
            passed_re = re.compile(
                r"^(.+?::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            )
            if self._process.stdout:
                for line in self._process.stdout:
                    line = line.rstrip()
                    m = passed_re.match(line)
                    if m:
                        test_id = m.group(1)
                        status = m.group(2).lower()
                        on_result(TestResult(
                            test_id=test_id,
                            status=status,
                            duration=0.0,
                        ))
            if self._process:
                self._process.wait()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        # Wait for process in a separate thread, call on_done
        def _waiter():
            if self._process:
                self._process.wait()
            if on_done:
                on_done()

        threading.Thread(target=_waiter, daemon=True).start()


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        # Create a sample test file
        tests_dir = Path(tmp) / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").touch()
        (tests_dir / "test_sample.py").write_text(
            "def test_passes():\n    assert True\n\n"
            "def test_fails():\n    assert False\n\n"
            "def test_skipped():\n    import pytest; pytest.skip('nope')\n"
        )

        tm = TestManager(str(tmp))

        # Discovery
        nodes = tm.discover_tests()
        for n in nodes:
            print(f"File: {n.name}")
            for child in n.children:
                print(f"  {child.kind}: {child.name} ({child.test_id})")

        # Run
        results: List[TestResult] = []

        def on_result(r: TestResult) -> None:
            results.append(r)
            print(f"  {r.status.upper()}: {r.test_id}")

        done_flag = {"done": False}

        def on_done() -> None:
            done_flag["done"] = True
            print("All tests complete")

        tm.run_all(on_result=on_result, on_done=on_done)

        import time
        for _ in range(30):
            if done_flag["done"]:
                break
            time.sleep(0.2)

        print(f"Total results: {len(results)}")
