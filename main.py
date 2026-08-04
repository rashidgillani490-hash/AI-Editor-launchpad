"""NexCore pywebview desktop shell.

The UI lives in ``web/`` while this module owns the native window and the
bridge to the existing NexCore backend managers.
"""

from __future__ import annotations

import json
import importlib.util
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import webview


ROOT = Path(__file__).resolve().parent


def _fit_current_window_to_work_area() -> None:
    """Correct WebView2's frameless maximize bounds on Windows."""
    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", Rect),
            ("rcWork", Rect),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.windll.user32
    process_id = os.getpid()
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def find_process_window(hwnd: int, _lparam: int) -> bool:
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == process_id and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
            return False
        return True

    user32.EnumWindows(find_process_window, 0)
    if not handles:
        return

    hwnd = handles[0]
    monitor = user32.MonitorFromWindow(hwnd, 2)
    info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return

    work = info.rcWork
    user32.SetWindowPos(
        hwnd,
        0,
        work.left,
        work.top,
        work.right - work.left,
        work.bottom - work.top,
        0x0004 | 0x0010 | 0x0020,
    )


def _configure_backend_path() -> None:
    """Make the separately-owned NexCore backend modules importable."""
    candidates = [
        os.environ.get("NEXCORE_BACKEND_PATH", ""),
        str(ROOT),
        str(ROOT / "backend"),
        str(Path(tempfile.gettempdir()) / "nexcore-backend-runtime"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate, "tabs_manager.py").is_file():
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return


_configure_backend_path()

from file_tree import FileTree  # noqa: E402
from run_manager import RunManager  # noqa: E402
from tabs_manager import TabsManager  # noqa: E402
from terminal_manager import TerminalManager  # noqa: E402
from workspace_services import (  # noqa: E402
    APP_DIR,
    DEFAULT_SETTINGS,
    JsonStore,
    RecoveryManager,
    analyze_python_file as analyze_python_path,
    get_git_status as read_git_status,
    inspect_file as inspect_file_path,
    search_project as search_project_files,
)


class NexCoreAPI:
    """Methods exposed as ``window.pywebview.api`` in JavaScript."""

    def __init__(self) -> None:
        # Keep the native object private so pywebview does not recursively
        # inspect its accessibility graph while exposing this API to JS.
        self._window: Optional[webview.Window] = None
        self.tabs = TabsManager()
        self.terminals = TerminalManager()
        self.terminals.set_callbacks(self._on_terminal_output, self._on_terminal_state)
        self.runner = RunManager(
            self.tabs,
            self.terminals,
            on_state_change=self._on_run_state,
        )
        self.file_tree: Optional[FileTree] = None
        self._maximized = True
        self._restore_bounds = (1200, 760, 80, 60)
        self.settings = JsonStore("settings.json", DEFAULT_SETTINGS)
        self.recovery = RecoveryManager()

    @staticmethod
    def _tab_payload(tab: Any) -> dict[str, Any]:
        return {
            "tab_id": tab.tab_id,
            "path": tab.path,
            "filename": tab.filename,
            "content": tab.content,
            "is_modified": tab.is_modified,
            "is_untitled": tab.is_untitled,
            "encoding": tab.encoding,
        }

    @staticmethod
    def _node_payload(node: Any) -> dict[str, Any]:
        return {
            "name": node.name,
            "path": node.path,
            "is_dir": node.is_dir,
            "extension": node.extension,
        }

    def _emit(self, event_name: str, payload: Any) -> None:
        if self._window is None:
            return
        script = (
            "window.NexCoreEvents && "
            f"window.NexCoreEvents.dispatch({json.dumps(event_name)}, "
            f"{json.dumps(payload, default=str)});"
        )
        try:
            self._window.evaluate_js(script)
        except Exception:
            # The webview may be closing while a terminal worker finishes.
            pass

    def app_info(self) -> dict[str, str]:
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
        return {
            "name": "NexCore",
            "app_name": "NexCore Orbital Engine",
            "version": "0.1.0",
            "shell": "pywebview",
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "git_branch": branch,
            "user_name": os.environ.get("NEXCORE_USER_NAME", ""),
            "startup_file": os.environ.get("NEXCORE_STARTUP_FILE", ""),
        }

    def _active_project_path(self, candidate: str) -> Path:
        if not self.file_tree:
            raise ValueError("Open a project folder first.")
        root = Path(self.file_tree.root_path).resolve(strict=True)
        target = Path(candidate or root).expanduser().resolve(strict=True)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Path is outside the active project.") from exc
        return target

    def get_settings(self) -> dict[str, Any]:
        return {"ok": True, "settings": self.settings.load()}

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, "settings": self.settings.save(settings)}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def validate_path(self, path: str, expected_type: str = "any") -> dict[str, Any]:
        try:
            target = Path(path).expanduser()
            if not target.is_absolute():
                return {"ok": False, "valid": False, "error": "Path must be absolute."}
            target = target.resolve(strict=True)
            valid = (
                expected_type == "any"
                or (expected_type == "directory" and target.is_dir())
                or (expected_type == "file" and target.is_file())
            )
            return {
                "ok": True,
                "valid": valid,
                "path": str(target),
                "type": "directory" if target.is_dir() else "file" if target.is_file() else "other",
            }
        except (OSError, ValueError) as exc:
            return {"ok": True, "valid": False, "error": str(exc)}

    def export_settings(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        selected = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename="nexcore-settings.json",
            file_types=("JSON files (*.json)",),
        )
        if not selected:
            return {"ok": False, "cancelled": True}
        try:
            Path(selected[0]).write_text(json.dumps(self.settings.load(), indent=2), encoding="utf-8")
            return {"ok": True, "path": selected[0]}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def import_settings(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        selected = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=("JSON files (*.json)",))
        if not selected:
            return {"ok": False, "cancelled": True}
        try:
            payload = json.loads(Path(selected[0]).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Settings file must contain a JSON object.")
            return {"ok": True, "settings": self.settings.save(payload)}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def save_workspace_state(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            target = APP_DIR / "workspace.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "state": state}, indent=2), encoding="utf-8")
            temporary.replace(target)
            return {"ok": True}
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def load_workspace_state(self) -> dict[str, Any]:
        try:
            payload = json.loads((APP_DIR / "workspace.json").read_text(encoding="utf-8"))
            return {"ok": True, "state": payload.get("state", {}) if isinstance(payload, dict) else {}}
        except FileNotFoundError:
            return {"ok": True, "state": {}}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "state": {}, "error": str(exc)}

    def search_project(self, root_path: str, query: str, options: dict[str, Any]) -> dict[str, Any]:
        try:
            root = self._active_project_path(root_path)
            if root != Path(self.file_tree.root_path).resolve(strict=True):
                raise ValueError("Search root must be the active project root.")
            return search_project_files(str(root), query, options or {})
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def analyze_python_file(self, path: str) -> dict[str, Any]:
        try:
            target = self._active_project_path(path)
            return analyze_python_path(str(target))
        except (OSError, ValueError) as exc:
            return {"ok": False, "problems": [], "error": str(exc)}

    def get_git_status(self, root_path: str) -> dict[str, Any]:
        try:
            root = self._active_project_path(root_path)
            return read_git_status(str(root))
        except (OSError, ValueError) as exc:
            return {"ok": False, "is_repo": False, "branch": "", "files": {}, "error": str(exc)}

    def get_git_diff(self, path: str) -> dict[str, Any]:
        try:
            target = self._active_project_path(path)
            root = Path(self.file_tree.root_path).resolve(strict=True)
            relative = str(target.relative_to(root))
            result = subprocess.run(
                ["git", "diff", "--", relative], cwd=root, capture_output=True,
                text=True, timeout=6, check=False,
            )
            return {"ok": result.returncode == 0, "diff": result.stdout[:40_000], "truncated": len(result.stdout) > 40_000}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"ok": False, "diff": "", "error": str(exc)}

    def inspect_file(self, path: str, max_size: int = 2_000_000) -> dict[str, Any]:
        try:
            target = self._active_project_path(path)
            return inspect_file_path(str(target), max_size)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def read_file_snapshot(self, path: str) -> dict[str, Any]:
        try:
            target = self._active_project_path(path)
            metadata = inspect_file_path(str(target), int(self.settings.load()["files"]["max_file_size"]))
            if metadata.get("binary"):
                return {"ok": False, "binary": True, "metadata": metadata, "error": "This file cannot be displayed as text."}
            return {"ok": True, "content": target.read_text(encoding="utf-8"), "metadata": metadata}
        except (OSError, UnicodeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def save_recovery_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            path = str(payload.get("path", ""))
            if path:
                target = Path(path).expanduser().resolve(strict=False)
                open_paths = {
                    Path(tab.path).expanduser().resolve(strict=False)
                    for tab in self.tabs.tabs if getattr(tab, "path", "")
                }
                if target not in open_paths:
                    raise ValueError("Recovery is limited to files open in NexCore.")
            return self.recovery.save(payload)
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def list_recovery_snapshots(self) -> dict[str, Any]:
        return {"ok": True, "records": self.recovery.list()}

    def restore_recovery_snapshot(self, record_id: str) -> dict[str, Any]:
        record = self.recovery.get(record_id)
        return {"ok": bool(record), "record": record, "error": "" if record else "Recovery record was not found."}

    def discard_recovery_snapshot(self, record_id: str) -> dict[str, Any]:
        return {"ok": self.recovery.discard(record_id)}

    def new_file(self) -> dict[str, Any]:
        """Create a real untitled tab managed by TabsManager."""
        try:
            tab = self.tabs.create_untitled("Untitled.py")
            return {"ok": True, "cancelled": False, "tab": self._tab_payload(tab)}
        except Exception as exc:
            return {"ok": False, "cancelled": False, "error": str(exc)}

    def _creation_target(self, parent_path: str, name: str) -> tuple[Path, Path]:
        if self.file_tree is None:
            raise ValueError("Open a project folder first.")
        clean_name = str(name or "").strip()
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        if (
            not clean_name
            or clean_name in {".", ".."}
            or clean_name.endswith((" ", "."))
            or any(char in clean_name for char in '<>:"/\\|?*')
            or Path(clean_name).name != clean_name
            or clean_name.split(".", 1)[0].upper() in reserved
        ):
            raise ValueError("Enter a valid name without path characters.")
        parent = Path(parent_path).expanduser()
        if not parent.is_absolute():
            raise ValueError("Parent path must be absolute.")
        parent = parent.resolve(strict=True)
        root = Path(self.file_tree.root_path).resolve(strict=True)
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("Target folder is outside the active project.") from exc
        if not parent.is_dir():
            raise ValueError("The target parent is not a directory.")
        target = parent / clean_name
        # The name validation above guarantees a direct child; retain this
        # explicit containment check as defense in depth.
        if target.parent.resolve() != parent:
            raise ValueError("The target path escapes the selected folder.")
        return parent, target

    def create_file(self, parent_path: str, filename: str) -> dict[str, Any]:
        """Create a real file inside the active project and open it."""
        try:
            _parent, target = self._creation_target(parent_path, filename)
            with target.open("x", encoding="utf-8"):
                pass
            tab = self.tabs.open_file(str(target))
            return {
                "ok": True,
                "path": str(target.resolve()),
                "tab": self._tab_payload(tab),
            }
        except FileExistsError:
            return {"ok": False, "error": "A file or folder with that name already exists."}
        except PermissionError:
            return {"ok": False, "error": "Permission denied while creating the file."}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def create_folder(self, parent_path: str, folder_name: str) -> dict[str, Any]:
        """Create a real child directory inside the active project."""
        try:
            _parent, target = self._creation_target(parent_path, folder_name)
            target.mkdir()
            return {
                "ok": True,
                "path": str(target.resolve()),
                "name": target.name,
            }
        except FileExistsError:
            return {"ok": False, "error": "A file or folder with that name already exists."}
        except PermissionError:
            return {"ok": False, "error": "Permission denied while creating the folder."}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def list_tabs(self) -> dict[str, Any]:
        """Return the manager's real open-tab state for the web tab strip."""
        return {
            "ok": True,
            "active_tab_id": self.tabs.active_tab_id,
            "tabs": [self._tab_payload(tab) for tab in self.tabs.tabs],
        }

    def activate_tab(self, tab_id: str) -> dict[str, Any]:
        self.tabs.set_active_tab(tab_id)
        tab = self.tabs.get_tab(tab_id)
        return {
            "ok": tab is not None,
            "tab": self._tab_payload(tab) if tab else None,
        }

    def close_tab(self, tab_id: str, force: bool = False) -> dict[str, Any]:
        try:
            tab = self.tabs.get_tab(tab_id)
            if tab is None:
                return {"ok": False, "error": "Tab not found"}
            if tab.is_modified and not force:
                return {"ok": False, "needs_confirmation": True}
            self.tabs.close_tab(tab_id, force=force)
            active = self.tabs.active_tab
            return {
                "ok": True,
                "active_tab": self._tab_payload(active) if active else None,
                "tabs": [self._tab_payload(item) for item in self.tabs.tabs],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_file_as(self, tab_id: str, content: str) -> dict[str, Any]:
        """Always open the native Save As picker for an existing tab."""
        if self._window is None:
            return {"ok": False, "cancelled": False, "error": "No native window"}
        tab = self.tabs.get_tab(tab_id)
        if tab is None:
            return {"ok": False, "cancelled": False, "error": "Tab not found"}
        selected = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=tab.filename,
            file_types=("All files (*.*)",),
        )
        if not selected:
            return {"ok": False, "cancelled": True}
        return self.save_file(tab_id, content, selected[0])

    def open_file(self, path: str = "", open_anyway: bool = False) -> dict[str, Any]:
        """Open a native picker when no path is supplied, then load a real tab."""
        if not path:
            if self._window is None:
                return {"ok": False, "cancelled": True}
            selected = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Code files (*.py;*.js;*.ts;*.html;*.css;*.json;*.md)", "All files (*.*)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected[0]
        try:
            target = Path(path).expanduser().resolve(strict=True)
            metadata = inspect_file_path(str(target), int(self.settings.load()["files"]["max_file_size"]))
            if metadata.get("binary"):
                return {
                    "ok": False, "cancelled": False, "binary": True,
                    "metadata": metadata, "error": "This file cannot be displayed as text.",
                }
            if metadata.get("large") and not open_anyway:
                return {
                    "ok": False, "cancelled": False, "requires_confirmation": True,
                    "metadata": metadata, "error": "This file is larger than the configured safe limit.",
                }
            tab = self.tabs.open_file(path)
            return {"ok": True, "cancelled": False, "tab": self._tab_payload(tab), "metadata": metadata}
        except Exception as exc:
            return {"ok": False, "cancelled": False, "error": str(exc)}

    def update_file(self, tab_id: str, content: str) -> dict[str, Any]:
        self.tabs.update_content(tab_id, content)
        tab = self.tabs.get_tab(tab_id)
        return {"ok": tab is not None, "tab": self._tab_payload(tab) if tab else None}

    def save_file(self, tab_id: str, content: str, path: str = "") -> dict[str, Any]:
        try:
            self.tabs.update_content(tab_id, content)
            tab = self.tabs.get_tab(tab_id)
            if tab is None:
                return {"ok": False, "error": "Tab not found"}
            if path:
                tab = self.tabs.save_as(tab_id, path)
            elif tab.is_untitled:
                if self._window is None:
                    return {"ok": False, "error": "No native window"}
                selected = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=tab.filename,
                    file_types=("All files (*.*)",),
                )
                if not selected:
                    return {"ok": False, "cancelled": True}
                tab = self.tabs.save_as(tab_id, selected[0])
            else:
                tab = self.tabs.save(tab_id)
            return {"ok": True, "cancelled": False, "tab": self._tab_payload(tab)}
        except Exception as exc:
            return {"ok": False, "cancelled": False, "error": str(exc)}

    def choose_directory(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "No native window"}
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not selected:
            return {"ok": False, "cancelled": True}
        return self.list_directory(selected[0])

    def list_directory(self, path: str) -> dict[str, Any]:
        try:
            requested = Path(path).expanduser()
            if not requested.is_absolute():
                return {
                    "ok": False,
                    "cancelled": False,
                    "error": "Folder path must be absolute.",
                    "children": [],
                }
            requested = requested.resolve(strict=True)
            if not requested.is_dir():
                return {
                    "ok": False,
                    "cancelled": False,
                    "error": "The selected path is not a directory.",
                    "children": [],
                }
            # Force one real read here so FileTree cannot turn a permission
            # error into a misleading successful empty directory.
            list(requested.iterdir())
            if self.file_tree is not None:
                self.file_tree.shutdown()
            self.file_tree = FileTree(str(requested))
            root = self.file_tree.load_root()
            return {
                "ok": True,
                "cancelled": False,
                "root": self._node_payload(root),
                "path": root.path,
                "name": root.name,
                "children": [self._node_payload(node) for node in root.children],
            }
        except PermissionError:
            return {
                "ok": False,
                "cancelled": False,
                "error": "Permission denied while reading this folder.",
                "children": [],
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "cancelled": False,
                "error": "The selected folder no longer exists.",
                "children": [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "cancelled": False,
                "error": str(exc),
                "children": [],
            }

    def list_children(self, path: str) -> dict[str, Any]:
        """Return one real directory level with a stable bridge payload."""
        try:
            if self.file_tree is None:
                return {
                    "ok": False,
                    "error": "Open a workspace folder first.",
                    "children": [],
                }
            requested = Path(path).expanduser()
            if not requested.is_absolute():
                return {
                    "ok": False,
                    "error": "Folder path must be absolute.",
                    "children": [],
                }
            requested = requested.resolve(strict=True)
            root = Path(self.file_tree.root_path).resolve(strict=True)
            try:
                requested.relative_to(root)
            except ValueError:
                return {
                    "ok": False,
                    "error": "Folder is outside the active project.",
                    "children": [],
                }
            if not requested.is_dir():
                return {
                    "ok": False,
                    "error": "The selected path is not a directory.",
                    "children": [],
                }
            children = []
            for child in requested.iterdir():
                is_dir = child.is_dir()
                children.append(
                    {
                        "name": child.name,
                        "path": str(child.resolve()),
                        "is_dir": is_dir,
                        "extension": "" if is_dir else child.suffix.lower(),
                        "hidden": child.name.startswith("."),
                    }
                )
            children.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
            return {
                "ok": True,
                "path": str(requested),
                "name": requested.name or str(requested),
                "children": children,
            }
        except PermissionError:
            return {
                "ok": False,
                "error": "Permission denied while reading this folder.",
                "children": [],
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "The selected folder no longer exists.",
                "children": [],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "children": []}

    def ai_status(self) -> dict[str, Any]:
        """Report whether the existing Anthropic-backed assistant is usable."""
        package_available = importlib.util.find_spec("anthropic") is not None
        key_configured = bool(os.environ.get("ANTHROPIC_API_KEY"))
        return {
            "ok": True,
            "available": package_available and key_configured,
            "provider": "Anthropic" if package_available else "",
            "model": os.environ.get("NEXCORE_AI_MODEL", "claude-opus-4-8"),
            "package_available": package_available,
            "key_configured": key_configured,
        }

    def ai_chat(
        self,
        message: str,
        filename: str = "",
        content: str = "",
        include_context: bool = False,
        history: Optional[list[dict[str, str]]] = None,
    ) -> dict[str, Any]:
        """Send a real AI request without exposing credentials to JavaScript."""
        status = self.ai_status()
        if not status["available"]:
            return {
                "ok": False,
                "configured": False,
                "error": "NexCore AI is not configured yet.",
            }
        prompt = str(message or "").strip()
        if not prompt:
            return {"ok": False, "configured": True, "error": "Message cannot be empty."}
        try:
            import anthropic

            messages: list[dict[str, str]] = []
            for item in (history or [])[-12:]:
                role = item.get("role")
                text = str(item.get("content", ""))[:12000]
                if role in {"user", "assistant"} and text:
                    messages.append({"role": role, "content": text})
            if include_context and content:
                context = str(content)[:8000]
                prompt = (
                    f"Active file: {filename or 'untitled'}\n"
                    f"```\n{context}\n```\n\n{prompt}"
                )
            messages.append({"role": "user", "content": prompt})
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            response = client.messages.create(
                model=status["model"],
                max_tokens=1024,
                system=(
                    "You are the AI Assistant in NexCore IDE. Give concise, "
                    "accurate programming help. Never claim to modify a file "
                    "unless the user explicitly applies a proposed change."
                ),
                messages=messages,
            )
            reply = "".join(
                block.text for block in response.content if block.type == "text"
            )
            return {
                "ok": True,
                "configured": True,
                "reply": reply or "(Empty response)",
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "error": str(exc),
            }

    def run_file(self, path: str = "") -> dict[str, Any]:
        try:
            terminal_id = self.runner.run_file(path) if path else self.runner.run_active_file()
            if terminal_id is None:
                return {"ok": False, "error": "Save or open a runnable file first."}
            return {"ok": True, "terminal_id": terminal_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_run(self) -> dict[str, Any]:
        self.runner.stop()
        return {"ok": True}

    def window_minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def window_toggle_maximize(
        self,
        available_width: int = 0,
        available_height: int = 0,
        screen_x: int = 0,
        screen_y: int = 0,
    ) -> None:
        if not self._window:
            return
        if self._maximized:
            if available_width > 0 and available_height > 0:
                restore_width = min(
                    available_width, max(720, int(available_width * 0.82))
                )
                restore_height = min(
                    available_height, max(520, int(available_height * 0.82))
                )
                restore_x = screen_x + max(0, (available_width - restore_width) // 2)
                restore_y = screen_y + max(0, (available_height - restore_height) // 2)
                self._restore_bounds = (
                    restore_width,
                    restore_height,
                    restore_x,
                    restore_y,
                )
            self._window.restore()
            width, height, x, y = self._restore_bounds
            self._window.resize(width, height)
            self._window.move(x, y)
        else:
            self._window.maximize()
            _fit_current_window_to_work_area()
        self._maximized = not self._maximized

    def window_close(self) -> None:
        if self._window:
            self.terminals.shutdown()
            if self.file_tree:
                self.file_tree.shutdown()
            self._window.destroy()

    def _on_terminal_output(self, terminal_id: str, line: Any) -> None:
        self._emit(
            "terminal-output",
            {
                "terminal_id": terminal_id,
                "text": getattr(line, "text", str(line)),
                "kind": getattr(line, "kind", "stdout"),
            },
        )

    def _on_terminal_state(self, terminal_id: str, state: Any) -> None:
        self._emit(
            "terminal-state",
            {
                "terminal_id": terminal_id,
                "is_running": getattr(state, "is_running", False),
                "exit_code": getattr(state, "exit_code", None),
            },
        )

    def _on_run_state(self, running: bool, result: Any) -> None:
        self._emit(
            "run-state",
            {
                "running": running,
                "exit_code": getattr(result, "exit_code", None) if result else None,
            },
        )


def main() -> None:
    api = NexCoreAPI()
    primary_screen = webview.screens[0]
    restore_width = min(
        primary_screen.width, max(720, int(primary_screen.width * 0.82))
    )
    restore_height = min(
        primary_screen.height, max(520, int(primary_screen.height * 0.82))
    )
    restore_x = primary_screen.x + max(0, (primary_screen.width - restore_width) // 2)
    restore_y = primary_screen.y + max(0, (primary_screen.height - restore_height) // 2)
    api._restore_bounds = (restore_width, restore_height, restore_x, restore_y)
    window = webview.create_window(
        "NexCore",
        url=(ROOT / "web" / "index.html").as_uri(),
        js_api=api,
        width=restore_width,
        height=restore_height,
        x=restore_x,
        y=restore_y,
        screen=primary_screen,
        min_size=(
            min(primary_screen.width, max(640, int(primary_screen.width * 0.68))),
            min(primary_screen.height, max(480, int(primary_screen.height * 0.68))),
        ),
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#07101f",
        maximized=True,
    )
    api._window = window

    def maximize_after_show() -> None:
        # WebView2 can ignore the create-time maximized flag for a frameless
        # window. Re-applying it after the native HWND exists is reliable and
        # lets Windows use the monitor's real available work area.
        window.maximize()
        _fit_current_window_to_work_area()
        api._maximized = True

    window.events.shown += maximize_after_show
    webview.start(debug=False)


if __name__ == "__main__":
    main()
