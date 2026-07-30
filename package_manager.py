"""
package_manager.py — Backend logic for managing Python packages from inside the IDE.

Integrates with the existing `terminal_manager.py` to run pip commands
and stream live output. Provides structured result dicts for every
operation so the GUI never sees a raw stack trace.

Usage (GUI hooks):
    from package_manager import PackageManager
    from terminal_manager import TerminalManager

    tm = TerminalManager()
    pm = PackageManager(project_root="/path/to/project", terminal_manager=tm)

    # Install with live streaming
    pm.install_package("requests", on_output=gui_output_callback,
                       on_done=gui_done_callback)

    # Uninstall
    result = pm.uninstall_package("requests")
    # → {"success": True, "message": "...", "data": None}

    # List installed
    result = pm.list_installed_packages()
    # → {"success": True, "data": [{"name": "requests", "version": "2.31.0"}, ...]}

    # Check outdated
    result = pm.check_outdated_packages()
    # → {"success": True, "data": [{"name":"pip","current":"23.0","latest":"24.0"}, ...]}

    # Requirements file
    pm.generate_requirements_file("requirements.txt")

    # Detect if a failed import is a missing package
    result = pm.detect_missing_import("numpy")
    # → {"success": True, "data": True}   # True = missing package
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import json
import shutil
import time
import threading
from pathlib import Path
from typing import (
    Optional, Callable, List, Dict, Any, Union,
)

# ---------------------------------------------------------------------------
# Try to import the local terminal_manager for type hints + venv detection.
# If unavailable, the module still works standalone using subprocess.
# ---------------------------------------------------------------------------
try:
    from terminal_manager import (
        TerminalManager,
        TerminalInstance,
        OutputLine,
    )
    _HAS_TERMINAL_MANAGER = True
except ImportError:
    _HAS_TERMINAL_MANAGER = False
    TerminalManager = None                                           # type: ignore[assignment,misc]
    TerminalInstance = None                                          # type: ignore[assignment,misc]
    OutputLine = None                                                # type: ignore[assignment,misc]

    # Minimal stand-in for OutputLine when terminal_manager is absent.
    from collections import namedtuple
    _OutputLine = namedtuple("OutputLine", ["text", "stream", "timestamp"])  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Structured result type
# ---------------------------------------------------------------------------

# Every public method returns a dict with at minimum:
#   {"success": bool, "message": str}
# Query-style methods also include:
#   {"data": ...}


# ---------------------------------------------------------------------------
# Known PyPI name mappings for common import-name ≠ package-name cases
# ---------------------------------------------------------------------------

_IMPORT_TO_PACKAGE: Dict[str, str] = {
    # Scientific / data
    "PIL":           "Pillow",
    "cv2":           "opencv-python",
    "sklearn":       "scikit-learn",
    "skimage":       "scikit-image",
    "yaml":          "PyYAML",
    "bs4":           "beautifulsoup4",
    "lxml":          "lxml",
    "Crypto":        "pycryptodome",
    "MySQLdb":       "mysqlclient",
    "psycopg2":      "psycopg2-binary",
    # Web
    "dotenv":        "python-dotenv",
    "jinja2":        "Jinja2",
    "markdown":      "Markdown",
    # Tools / dev
    "pytest":        "pytest",
    "black":         "black",
    "isort":         "isort",
    "mypy":          "mypy",
    "flake8":        "flake8",
    "pylint":        "pylint",
    "coverage":      "coverage",
    "sphinx":        "Sphinx",
    "tox":           "tox",
    "pre_commit":    "pre-commit",
    # GUI
    "tkinter":       "tk",
    "PyQt5":         "PyQt5",
    "PyQt6":         "PyQt6",
    "wx":            "wxPython",
    # Misc
    "dateutil":      "python-dateutil",
    "pytz":          "pytz",
    "tzdata":        "tzdata",
    "sqlalchemy":    "SQLAlchemy",
    "redis":         "redis",
    "celery":        "celery",
    "google":        "google-api-python-client",
    "boto3":         "boto3",
    "botocore":      "botocore",
}

# Stand-alone stdlib module names that should never trigger a "missing package" flag.
_STDLIB_MODULES: frozenset = frozenset({
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
    "asyncore", "atexit", "audioop", "base64", "bdb", "binascii",
    "binhex", "bisect", "builtins", "bz2", "calendar", "cgi",
    "cgitb", "chunk", "cmath", "cmd", "code", "codecs", "codeop",
    "collections", "colorsys", "compileall", "concurrent", "configparser",
    "contextlib", "contextvars", "copy", "copyreg", "cProfile",
    "crypt", "csv", "ctypes", "curses", "dataclasses", "datetime",
    "dbm", "decimal", "difflib", "dis", "distutils", "doctest",
    "email", "encodings", "enum", "errno", "faulthandler", "fcntl",
    "filecmp", "fileinput", "fnmatch", "formatter", "fractions",
    "ftplib", "functools", "gc", "getopt", "getpass", "gettext",
    "glob", "grp", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "idlelib", "imaplib", "imghdr", "imp", "importlib",
    "inspect", "io", "ipaddress", "itertools", "json", "keyword",
    "lib2to3", "linecache", "locale", "logging", "lzma", "mailbox",
    "mailcap", "marshal", "math", "mimetypes", "mmap", "modulefinder",
    "multiprocessing", "netrc", "nis", "nntplib", "numbers", "operator",
    "optparse", "os", "ossaudiodev", "parser", "pathlib", "pdb",
    "pickle", "pickletools", "pipes", "pkgutil", "platform", "plistlib",
    "poplib", "posix", "posixpath", "pprint", "profile", "pstats",
    "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
    "random", "re", "readline", "reprlib", "resource", "rlcompleter",
    "runpy", "sched", "secrets", "select", "selectors", "shelve",
    "shlex", "shutil", "signal", "site", "smtpd", "smtplib",
    "sndhdr", "socket", "socketserver", "sqlite3", "ssl", "stat",
    "statistics", "string", "stringprep", "struct", "subprocess",
    "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
    "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
    "threading", "time", "timeit", "tkinter", "token", "tokenize",
    "trace", "traceback", "tracemalloc", "tty", "turtle", "turtledemo",
    "types", "typing", "unicodedata", "unittest", "urllib", "uu",
    "uuid", "venv", "warnings", "wave", "weakref", "webbrowser",
    "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc",
    "zipapp", "zipfile", "zipimport", "zlib",
    # Tkinter aliases (stdlib on most Python builds)
    "_tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
})


# ---------------------------------------------------------------------------
# PackageManager
# ---------------------------------------------------------------------------

class PackageManager:
    """Manages pip operations with venv detection and live output streaming."""

    def __init__(
        self,
        project_root: str,
        terminal_manager: Optional[TerminalManager] = None,
        use_system_python: bool = False,
    ) -> None:
        """Initialise the package manager.

        Args:
            project_root: Absolute path to the project directory.
            terminal_manager: A TerminalManager instance for streaming
                output.  If None, a new one is created internally.
            use_system_python: If True, always use the system Python
                interpreter and ignore any venv in the project root.
        """
        self._project_root = os.path.abspath(project_root)
        self._use_system_python = use_system_python
        self._terminal_mgr: TerminalManager

        if terminal_manager is None and _HAS_TERMINAL_MANAGER:
            self._terminal_mgr = TerminalManager()
        elif terminal_manager is not None:
            self._terminal_mgr = terminal_manager
        else:
            self._terminal_mgr = None  # type: ignore[assignment]

        # Cached installed-package list (refreshed lazily)
        self._installed_cache: Optional[Dict[str, str]] = None
        self._cache_lock = threading.Lock()

    # ==================================================================
    # Venv / interpreter resolution
    # ==================================================================

    def get_python_interpreter(self) -> str:
        """Return the Python interpreter path, respecting venv detection.

        Returns:
            Absolute path to the python executable (or "python3"/"python"
            as a system-level fallback).
        """
        if self._use_system_python:
            return sys.executable

        # Walk up from project root looking for venv / .venv
        search_dir = self._project_root
        while True:
            for venv_name in ("venv", ".venv"):
                for sub in ("bin", "Scripts"):
                    candidate = os.path.join(search_dir, venv_name, sub, "python")
                    if os.path.isfile(candidate):
                        return candidate
                    candidate_exe = candidate + ".exe"
                    if os.path.isfile(candidate_exe):
                        return candidate_exe
            parent = os.path.dirname(search_dir)
            if parent == search_dir:  # reached filesystem root
                break
            search_dir = parent

        return sys.executable

    def get_pip_command(self) -> List[str]:
        """Return the pip invocation as a list of CLI tokens.

        Uses `<python> -m pip` for reliability across platforms.
        """
        return [self.get_python_interpreter(), "-m", "pip"]

    def force_system_python(self, enabled: bool = True) -> None:
        """Toggle whether to force the system Python interpreter."""
        self._use_system_python = enabled

    # ==================================================================
    # Low-level: run pip and capture output (non-streaming)
    # ==================================================================

    def _run_pip_capture(
        self,
        args: List[str],
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess:
        """Run a pip subprocess and capture stdout/stderr.

        Args:
            args: Pip CLI arguments (without the leading "pip").
            timeout: Maximum seconds to wait.

        Returns:
            CompletedProcess with .stdout, .stderr, .returncode.
        """
        cmd = self.get_pip_command() + args
        return subprocess.run(
            cmd,
            cwd=self._project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

    # ==================================================================
    # Low-level: run pip with streaming (via TerminalManager)
    # ==================================================================

    def _run_pip_streaming(
        self,
        args: List[str],
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[int, float], None]] = None,
        timeout: float = 300.0,
    ) -> None:
        """Run a pip command and stream output live via the terminal manager.

        Args:
            args: Pip CLI arguments.
            on_output(line_text, stream_name): Called for each output line.
                stream_name is "stdout", "stderr", or "system".
            on_done(exit_code, elapsed_sec): Called when the process finishes.
            timeout: Max seconds before force-kill.
        """
        if not _HAS_TERMINAL_MANAGER or self._terminal_mgr is None:
            # Fallback: run synchronously and batch-deliver output
            self._run_pip_streaming_fallback(args, on_output, on_done, timeout)
            return

        cmd = self.get_pip_command() + args
        cmd_str = " ".join(cmd)

        term = self._terminal_mgr.create_terminal(cwd=self._project_root)

        def _wrap_output(line: Any) -> None:
            if on_output:
                stream = getattr(line, "stream", "stdout")
                text = getattr(line, "text", str(line))
                on_output(text, stream)

        def _wrap_state(state: Any) -> None:
            if state is not None and not getattr(state, "is_running", True):
                if on_done:
                    exit_code = getattr(state, "exit_code", -1) or -1
                    elapsed = getattr(state, "execution_time", 0.0) or 0.0
                    on_done(exit_code, elapsed)
                self._terminal_mgr.remove_terminal(term.terminal_id)

        term.on_output = _wrap_output
        term.on_state_change = _wrap_state
        term.run_command(cmd_str, cwd=self._project_root)

    def _run_pip_streaming_fallback(
        self,
        args: List[str],
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[int, float], None]] = None,
        timeout: float = 300.0,
    ) -> None:
        """Fallback streaming when terminal_manager is unavailable."""
        cmd = self.get_pip_command() + args
        start = time.time()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self._project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            if on_output:
                on_output(f"Failed to launch pip: {exc}", "stderr")
            if on_done:
                on_done(-1, 0.0)
            return

        def _read(stream, label: str) -> None:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                if on_output:
                    on_output(line, label)

        t_out = threading.Thread(target=_read, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=_read, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        elapsed = time.time() - start
        if on_done:
            on_done(proc.returncode, elapsed)

    # ==================================================================
    # Public: install
    # ==================================================================

    def install_package(
        self,
        name: str,
        version: Optional[str] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """Install a package, streaming live output.

        Args:
            name: Package name (PyPI name, e.g. "requests").
            version: If given, pins to this version ("2.31.0").
            on_output(line_text, stream_name): Live output callback.
            on_done(success, message): Completion callback.
        """
        if not name or not name.strip():
            if on_done:
                on_done(False, "Package name is empty.")
            return

        target = f"{name.strip()}=={version.strip()}" if version else name.strip()

        def _on_done(exit_code: int, elapsed: float) -> None:
            success = exit_code == 0
            msg = (f"Installed {target} ({elapsed:.1f}s)"
                   if success
                   else f"Install failed (exit code {exit_code})")
            if on_done:
                on_done(success, msg)
            # Invalidate cache
            self._invalidate_cache()

        self._run_pip_streaming(
            ["install", target],
            on_output=on_output,
            on_done=_on_done,
        )

    # ==================================================================
    # Public: uninstall
    # ==================================================================

    def uninstall_package(
        self,
        name: str,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> Dict[str, Any]:
        """Uninstall a package, streaming live output.

        Args:
            name: Package name.
            on_output: Live output callback.
            on_done: Completion callback.

        Returns:
            A preliminary result dict (the final result is delivered
            via *on_done*).  For synchronous usage, check the callback.
        """
        if not name or not name.strip():
            return {"success": False, "message": "Package name is empty.", "data": None}

        def _on_done(exit_code: int, elapsed: float) -> None:
            success = exit_code == 0
            msg = (f"Uninstalled {name}"
                   if success
                   else f"Uninstall failed (exit code {exit_code})")
            if on_done:
                on_done(success, msg)
            self._invalidate_cache()

        self._run_pip_streaming(
            ["uninstall", name.strip(), "-y"],
            on_output=on_output,
            on_done=_on_done,
        )
        return {"success": True, "message": "Uninstall started.", "data": None}

    # ==================================================================
    # Public: list installed
    # ==================================================================

    def list_installed_packages(self) -> Dict[str, Any]:
        """Return a list of installed packages.

        Returns:
            dict with keys:
                success (bool)
                message (str)
                data (List[Dict] | None) — [{"name": str, "version": str}, ...]
        """
        try:
            r = self._run_pip_capture(["list", "--format=json"], timeout=30)
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Pip list timed out.", "data": None}
        except FileNotFoundError:
            return {"success": False,
                    "message": "pip not found. Is Python installed correctly?",
                    "data": None}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": None}

        if r.returncode != 0:
            return {"success": False,
                    "message": f"pip list failed:\n{r.stderr.strip()}",
                    "data": None}

        try:
            packages: List[Dict[str, str]] = json.loads(r.stdout)
        except json.JSONDecodeError:
            # Fallback: parse human-readable output
            packages = self._parse_pip_list_human(r.stdout)

        # Update cache
        with self._cache_lock:
            self._installed_cache = {p["name"].lower(): p["version"] for p in packages}

        return {"success": True,
                "message": f"{len(packages)} package(s) installed.",
                "data": packages}

    # ==================================================================
    # Public: check outdated
    # ==================================================================

    def check_outdated_packages(self) -> Dict[str, Any]:
        """Return outdated packages.

        Returns:
            dict with:
                success (bool)
                message (str)
                data (List[Dict] | None):
                    [{"name": str, "current_version": str, "latest_version": str}, ...]
        """
        try:
            r = self._run_pip_capture(
                ["list", "--outdated", "--format=json"],
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"success": False,
                    "message": "Checking for outdated packages timed out.",
                    "data": None}
        except FileNotFoundError:
            return {"success": False,
                    "message": "pip not found.",
                    "data": None}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": None}

        if r.returncode != 0:
            return {"success": False,
                    "message": f"pip list --outdated failed:\n{r.stderr.strip()}",
                    "data": None}

        try:
            raw: List[Dict[str, str]] = json.loads(r.stdout)
        except json.JSONDecodeError:
            raw = self._parse_pip_outdated_human(r.stdout)

        outdated: List[Dict[str, str]] = []
        for entry in raw:
            outdated.append({
                "name": entry.get("name", ""),
                "current_version": entry.get("version", ""),
                "latest_version": entry.get("latest_version",
                                           entry.get("latest_file_version", "")),
            })

        return {"success": True,
                "message": f"{len(outdated)} package(s) outdated.",
                "data": outdated}

    # ==================================================================
    # Public: requirements.txt
    # ==================================================================

    def install_from_requirements(
        self,
        path: str = "requirements.txt",
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> Dict[str, Any]:
        """Install all packages listed in a requirements file.

        Args:
            path: Relative or absolute path to the requirements file.
            on_output: Live output callback.
            on_done(success, message): Completion callback.

        Returns:
            Preliminary result dict.
        """
        abs_path = path if os.path.isabs(path) else os.path.join(self._project_root, path)

        if not os.path.isfile(abs_path):
            msg = f"Requirements file not found: {abs_path}"
            if on_done:
                on_done(False, msg)
            return {"success": False, "message": msg, "data": None}

        def _on_done(exit_code: int, elapsed: float) -> None:
            success = exit_code == 0
            msg = (f"Installed all requirements from {path} ({elapsed:.1f}s)"
                   if success
                   else f"Requirements install failed (exit code {exit_code})")
            if on_done:
                on_done(success, msg)
            self._invalidate_cache()

        self._run_pip_streaming(
            ["install", "-r", abs_path],
            on_output=on_output,
            on_done=_on_done,
        )
        return {"success": True,
                "message": f"Installing from {path} ...",
                "data": None}

    def generate_requirements_file(self, path: str = "requirements.txt") -> Dict[str, Any]:
        """Run `pip freeze` and write the output to *path*.

        Args:
            path: Destination file (relative or absolute).

        Returns:
            Result dict with success/message/data.
        """
        abs_path = path if os.path.isabs(path) else os.path.join(self._project_root, path)

        try:
            r = subprocess.run(
                self.get_pip_command() + ["freeze"],
                cwd=self._project_root,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "pip freeze timed out.", "data": None}
        except FileNotFoundError:
            return {"success": False,
                    "message": "pip not found.",
                    "data": None}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": None}

        if r.returncode != 0:
            return {"success": False,
                    "message": f"pip freeze failed:\n{r.stderr.strip()}",
                    "data": None}

        try:
            # Ensure parent dir exists
            os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(r.stdout)
        except OSError as exc:
            return {"success": False,
                    "message": f"Cannot write {abs_path}: {exc}",
                    "data": None}

        pkgs = [line for line in r.stdout.splitlines() if line.strip()]
        return {"success": True,
                "message": f"Wrote {len(pkgs)} package(s) to {path}.",
                "data": {"path": abs_path, "count": len(pkgs)}}

    # ==================================================================
    # Public: detect missing import
    # ==================================================================

    def detect_missing_import(self, module_name: str) -> Dict[str, Any]:
        """Check whether a failed import is a missing package vs. a typo.

        Heuristics used (in order):
        1. If the name matches a stdlib module → it's NOT missing.
        2. If the name matches an already-installed package → NOT missing.
        3. Check a known mapping of import-name → PyPI-name and verify
           with a lightweight pip call.
        4. As a last resort, query `pip index versions <name>` to see if
           the name exists on PyPI at all.

        Args:
            module_name: The top-level module name that failed to import
                (e.g. "numpy", "requests").

        Returns:
            dict with:
                success (bool)
                message (str)
                data (bool | None):
                    True  → package is genuinely missing (installable)
                    False → package is already installed OR is stdlib
                    None  → could not determine (network error, etc.)
        """
        name = module_name.strip()

        # 0. Empty / garbage
        if not name:
            return {"success": False,
                    "message": "Module name is empty.",
                    "data": None}

        # 1. Stdlib check
        if name in _STDLIB_MODULES or name.split(".")[0] in _STDLIB_MODULES:
            return {"success": True,
                    "message": f"'{name}' is a standard-library module.",
                    "data": False}

        # 2. Already installed?
        installed = self._get_installed_cache()
        if name.lower() in installed:
            return {"success": True,
                    "message": f"'{name}' is already installed (v{installed[name.lower()]}).",
                    "data": False}
        # Also check the mapped name
        mapped = _IMPORT_TO_PACKAGE.get(name, name).lower()
        if mapped in installed:
            return {"success": True,
                    "message": f"'{mapped}' (for import '{name}') is already installed.",
                    "data": False}

        # 3. Check PyPI: does this package exist?
        exists = self._package_exists_on_pypi(mapped)
        if exists is True:
            return {"success": True,
                    "message": f"'{name}' → '{mapped}' is available on PyPI but not installed.",
                    "data": True}
        if exists is False:
            # Not found on PyPI — could be a typo or a non-public package
            return {"success": True,
                    "message": (f"'{name}' was not found on PyPI. "
                                "It may be a typo, a local module, "
                                "or a non-public package."),
                    "data": None}

        # exists is None → network error or indeterminate
        return {"success": False,
                "message": (f"Could not determine whether '{name}' "
                            "exists on PyPI (network or timeout)."),
                "data": None}

    # ==================================================================
    # Public: convenience helpers
    # ==================================================================

    def upgrade_package(
        self,
        name: str,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """Upgrade a package to its latest version (streaming)."""
        self.install_package(name, version=None, on_output=on_output, on_done=on_done)
        # pip install --upgrade <name> is more explicit:
        def _on_done(exit_code: int, elapsed: float) -> None:
            success = exit_code == 0
            msg = (f"Upgraded {name} ({elapsed:.1f}s)"
                   if success
                   else f"Upgrade failed (exit code {exit_code})")
            if on_done:
                on_done(success, msg)
            self._invalidate_cache()

        # Replace with explicit --upgrade call
        self._run_pip_streaming(
            ["install", "--upgrade", name.strip()],
            on_output=on_output,
            on_done=_on_done,
        )

    def upgrade_all_outdated(
        self,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """Upgrade every outdated package in a single batch."""
        result = self.check_outdated_packages()
        if not result["success"] or not result["data"]:
            if on_done:
                on_done(False, "No outdated packages to upgrade, or check failed.")
            return

        names = [p["name"] for p in result["data"]]
        if not names:
            if on_done:
                on_done(True, "All packages are up to date.")
            return

        def _on_done(exit_code: int, elapsed: float) -> None:
            success = exit_code == 0
            msg = (f"Upgraded {len(names)} package(s) ({elapsed:.1f}s)"
                   if success
                   else f"Batch upgrade failed (exit code {exit_code})")
            if on_done:
                on_done(success, msg)
            self._invalidate_cache()

        self._run_pip_streaming(
            ["install", "--upgrade"] + names,
            on_output=on_output,
            on_done=_on_done,
        )

    def show_package_info(self, name: str) -> Dict[str, Any]:
        """Return detailed metadata about an installed package via `pip show`.

        Returns:
            dict with success/message/data.  data is a dict of metadata
            fields (Name, Version, Summary, Home-page, Requires, etc.).
        """
        if not name or not name.strip():
            return {"success": False, "message": "Package name is empty.", "data": None}

        try:
            r = self._run_pip_capture(["show", name.strip()], timeout=30)
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "pip show timed out.", "data": None}
        except FileNotFoundError:
            return {"success": False, "message": "pip not found.", "data": None}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": None}

        if r.returncode != 0:
            return {"success": False,
                    "message": f"Package '{name}' not found.",
                    "data": None}

        info: Dict[str, str] = {}
        for line in r.stdout.splitlines():
            if ": " in line:
                key, val = line.split(": ", 1)
                info[key.strip()] = val.strip()

        return {"success": True,
                "message": f"Details for {name}",
                "data": info}

    def is_pip_installed(self) -> bool:
        """Check whether pip is available at all."""
        try:
            r = subprocess.run(
                self.get_pip_command() + ["--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ==================================================================
    # Internals: parsing helpers
    # ==================================================================

    @staticmethod
    def _parse_pip_list_human(text: str) -> List[Dict[str, str]]:
        """Parse human-readable `pip list` output."""
        packages: List[Dict[str, str]] = []
        # Lines look like: "Package          Version"
        #                  "requests         2.31.0"
        header_seen = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("package") and "version" in line.lower():
                header_seen = True
                continue
            if not header_seen:
                # Try parsing anyway
                pass
            # Split on 2+ spaces
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 2:
                packages.append({"name": parts[0].strip(), "version": parts[1].strip()})
        return packages

    @staticmethod
    def _parse_pip_outdated_human(text: str) -> List[Dict[str, str]]:
        """Parse human-readable `pip list --outdated` output."""
        packages: List[Dict[str, str]] = []
        header_seen = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("package"):
                header_seen = True
                continue
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 3:
                packages.append({
                    "name": parts[0].strip(),
                    "version": parts[1].strip(),
                    "latest_version": parts[2].strip(),
                })
        return packages

    # ==================================================================
    # Internals: cache
    # ==================================================================

    def _get_installed_cache(self) -> Dict[str, str]:
        """Return a dict of {name_lower: version} from cache or pip list."""
        with self._cache_lock:
            if self._installed_cache is None:
                result = self.list_installed_packages()
                if result["success"] and result["data"]:
                    self._installed_cache = {
                        p["name"].lower(): p["version"] for p in result["data"]
                    }
                else:
                    self._installed_cache = {}
            return dict(self._installed_cache)

    def _invalidate_cache(self) -> None:
        """Clear the installed-package cache."""
        with self._cache_lock:
            self._installed_cache = None

    # ==================================================================
    # Internals: PyPI existence check
    # ==================================================================

    @staticmethod
    def _package_exists_on_pypi(package_name: str) -> Optional[bool]:
        """Check whether *package_name* exists on PyPI.

        Returns:
            True   — found
            False  — not found
            None   — could not determine (network error / timeout)
        """
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "index", "versions", package_name],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            # pip index versions outputs the available versions if the
            # package exists and exits 0; exits non-zero or prints an
            # error if not found.
            if r.returncode == 0 and "Available versions:" in r.stdout:
                return True
            if "No matching distribution found" in r.stderr:
                return False
            if r.returncode != 0:
                return False
            return None
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    # ==================================================================
    # Cleanup
    # ==================================================================

    def shutdown(self) -> None:
        """Release resources managed by the package manager."""
        if _HAS_TERMINAL_MANAGER and self._terminal_mgr is not None:
            self._terminal_mgr.shutdown()
        self._invalidate_cache()


# ---------------------------------------------------------------------------
# Example / quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("PackageManager — module-level test")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        pm = PackageManager(project_root=tmp)

        # ---- pip available? ----
        print(f"\n1. pip available: {pm.is_pip_installed()}")
        print(f"   Using python: {pm.get_python_interpreter()}")

        # ---- list installed ----
        print("\n2. list_installed_packages():")
        result = pm.list_installed_packages()
        print(f"   success={result['success']}, {result['message']}")
        if result["data"]:
            for pkg in result["data"][:5]:
                print(f"      {pkg['name']}=={pkg['version']}")

        # ---- check outdated ----
        print("\n3. check_outdated_packages():")
        result = pm.check_outdated_packages()
        print(f"   success={result['success']}, {result['message']}")
        if result["data"]:
            for pkg in result["data"][:5]:
                print(f"      {pkg['name']}: {pkg['current_version']} → {pkg['latest_version']}")

        # ---- detect missing import ----
        print("\n4. detect_missing_import():")
        for mod in ("numpy", "os", "typo_module_xyz", "sklearn"):
            r = pm.detect_missing_import(mod)
            status = {True: "MISSING", False: "OK (stdlib/installed)", None: "UNKNOWN"}
            print(f"   '{mod}' → {status.get(r['data'], '?')} — {r['message']}")

        # ---- show package info ----
        print("\n5. show_package_info('pip'):")
        r = pm.show_package_info("pip")
        if r["success"] and r["data"]:
            for k, v in r["data"].items():
                print(f"   {k}: {v}")

        # ---- generate requirements (to temp) ----
        print("\n6. generate_requirements_file():")
        req_path = os.path.join(tmp, "requirements.txt")
        r = pm.generate_requirements_file(req_path)
        print(f"   success={r['success']}, {r['message']}")
        if r["success"] and os.path.isfile(req_path):
            lines = Path(req_path).read_text().strip().splitlines()
            print(f"   First 5 lines: {lines[:5]}")

        pm.shutdown()

    print("\nAll tests complete.")
