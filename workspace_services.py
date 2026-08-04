"""Focused persistence, search, diagnostics, Git, and recovery services."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "NexCore"
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 2,
    "general": {
        "confirm_exit": True,
        "show_welcome": True,
    },
    "editor": {
        "font_size": 14,
        "line_height": 1.55,
        "tab_size": 4,
        "word_wrap": False,
        "line_numbers": True,
        "current_line": True,
        "indent_guides": True,
        "autosave_mode": "off",
        "autosave_delay": 1000,
        "format_on_save": False,
    },
    "files": {
        "max_file_size": 2_000_000,
        "recovery_interval": 10,
        "show_hidden": False,
    },
    "terminal": {"font_size": 11, "auto_scroll": True, "clear_before_run": True},
    "appearance": {"panel_opacity": 0.91, "glow": 1.0, "reduced_motion": False},
    "workspace": {
        "restore_on_startup": "never",
        "restore_open_files": True,
        "restore_layout": True,
    },
    "git": {"show_indicators": True, "show_branch": True, "refresh_interval": 30},
    "ai": {"confirm_whole_file": True, "confirm_apply": True, "max_context": 40_000},
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}


def _merge_defaults(defaults: dict[str, Any], incoming: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    incoming = incoming if isinstance(incoming, dict) else {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            result[key] = _merge_defaults(value, incoming.get(key))
            continue
        candidate = incoming.get(key, value)
        if isinstance(value, bool):
            result[key] = candidate if isinstance(candidate, bool) else value
        elif isinstance(value, int):
            result[key] = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else value
        elif isinstance(value, float):
            result[key] = float(candidate) if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) else value
        elif isinstance(value, str):
            result[key] = candidate if isinstance(candidate, str) else value
        else:
            result[key] = value
    return result


class JsonStore:
    def __init__(self, filename: str, defaults: dict[str, Any]) -> None:
        self.path = APP_DIR / filename
        self.defaults = defaults

    def load(self) -> dict[str, Any]:
        try:
            incoming = json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.name == "settings.json":
                incoming = self._migrate_settings(incoming)
            return _merge_defaults(self.defaults, incoming)
        except (OSError, ValueError, TypeError):
            return json.loads(json.dumps(self.defaults))

    def save(self, payload: Any) -> dict[str, Any]:
        if self.path.name == "settings.json":
            payload = self._migrate_settings(payload)
        data = _merge_defaults(self.defaults, payload)
        if self.path.name == "settings.json":
            data["editor"]["font_size"] = min(24, max(11, data["editor"]["font_size"]))
            data["editor"]["line_height"] = min(2.0, max(1.2, data["editor"]["line_height"]))
            data["editor"]["tab_size"] = min(8, max(2, data["editor"]["tab_size"]))
            data["editor"]["autosave_delay"] = data["editor"]["autosave_delay"] if data["editor"]["autosave_delay"] in {500, 1000, 2000, 5000} else 1000
            if data["editor"]["autosave_mode"] not in {"off", "delay", "focus", "window"}:
                data["editor"]["autosave_mode"] = "off"
            if data["workspace"]["restore_on_startup"] not in {"never", "ask", "always"}:
                data["workspace"]["restore_on_startup"] = "never"
        APP_DIR.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return data

    @staticmethod
    def _migrate_settings(payload: Any) -> dict[str, Any]:
        incoming = json.loads(json.dumps(payload)) if isinstance(payload, dict) else {}
        general = incoming.get("general") if isinstance(incoming.get("general"), dict) else {}
        workspace = incoming.get("workspace") if isinstance(incoming.get("workspace"), dict) else {}
        if "restore_on_startup" not in workspace:
            legacy = general.get("restore_workspace")
            # Preserve an explicit legacy Always choice. Legacy Ask was the old
            # default, so migrate it to the new non-prompting default.
            workspace["restore_on_startup"] = "always" if legacy == "always" else "never"
        if workspace.get("restore_on_startup") not in {"never", "ask", "always"}:
            workspace["restore_on_startup"] = "never"
        general.pop("restore_workspace", None)
        incoming["general"] = general
        incoming["workspace"] = workspace
        incoming["version"] = 2
        return incoming


class RecoveryManager:
    def __init__(self) -> None:
        self.directory = APP_DIR / "recovery"

    @staticmethod
    def _record_id(path: str, tab_id: str) -> str:
        return hashlib.sha256(f"{path}|{tab_id}".encode()).hexdigest()[:24]

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content", ""))
        path = str(payload.get("path", ""))
        tab_id = str(payload.get("tab_id", "untitled"))
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "id": self._record_id(path, tab_id),
            "tab_id": tab_id,
            "path": path,
            "filename": str(payload.get("filename") or Path(path).name or "Untitled"),
            "project_root": str(payload.get("project_root", "")),
            "content": content,
            "saved_hash": str(payload.get("saved_hash", "")),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "encoding": str(payload.get("encoding", "utf-8")),
            "cursor": max(0, int(payload.get("cursor", 0))),
            "scroll_top": max(0, int(payload.get("scroll_top", 0))),
            "timestamp": time.time(),
        }
        if not record["saved_hash"] and path and Path(path).is_file():
            try:
                record["saved_hash"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                pass
        target = self.directory / f"{record['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(target)
        return {"ok": True, "record": record}

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.directory.exists():
            return records
        for target in self.directory.glob("*.json"):
            try:
                record = json.loads(target.read_text(encoding="utf-8"))
                disk = Path(str(record.get("path", "")))
                record["file_exists"] = disk.is_file()
                if disk.is_file():
                    disk_hash = hashlib.sha256(disk.read_bytes()).hexdigest()
                    record["disk_changed"] = bool(record.get("saved_hash") and disk_hash != record["saved_hash"])
                else:
                    record["disk_changed"] = False
                record.pop("content", None)
                records.append(record)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda item: float(item.get("timestamp", 0)), reverse=True)

    def get(self, record_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{24}", record_id):
            return None
        try:
            return json.loads((self.directory / f"{record_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def discard(self, record_id: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{24}", record_id):
            return False
        try:
            (self.directory / f"{record_id}.json").unlink(missing_ok=True)
            return True
        except OSError:
            return False


def safe_project_root(candidate: str) -> Path:
    root = Path(candidate).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project root is not a directory.")
    return root


def search_project(root_path: str, query: str, options: dict[str, Any]) -> dict[str, Any]:
    try:
        root = safe_project_root(root_path)
        if not query:
            return {"ok": True, "query": "", "total_matches": 0, "files": [], "truncated": False}
        case = bool(options.get("case_sensitive"))
        whole = bool(options.get("whole_word"))
        use_regex = bool(options.get("regex"))
        max_results = min(5000, max(1, int(options.get("max_results", 1000))))
        includes = [p.strip() for p in str(options.get("include_patterns", "")).split(",") if p.strip()]
        excludes = set(SKIP_DIRS)
        excludes.update(p.strip().strip("/\\") for p in str(options.get("exclude_patterns", "")).split(",") if p.strip())
        expression = query if use_regex else re.escape(query)
        if whole:
            expression = rf"\b(?:{expression})\b"
        pattern = re.compile(expression, 0 if case else re.IGNORECASE)
    except (OSError, ValueError, re.error) as exc:
        return {"ok": False, "error": f"Invalid search: {exc}"}

    grouped: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excludes and not name.startswith(".nexcore")]
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(root)
            relative_text = str(relative)
            if includes and not any(fnmatch.fnmatch(filename, item) or fnmatch.fnmatch(relative_text, item) for item in includes):
                continue
            if any(part in excludes for part in relative.parts):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw[:8192]:
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            matches: list[dict[str, Any]] = []
            for line_number, line in enumerate(text.splitlines(), 1):
                for found in pattern.finditer(line):
                    matches.append({
                        "line": line_number,
                        "column": found.start() + 1,
                        "text": line[:500],
                        "match_start": found.start(),
                        "match_end": found.end(),
                    })
                    total += 1
                    if total >= max_results:
                        truncated = True
                        break
                if truncated:
                    break
            if matches:
                grouped.append({"path": str(path.resolve()), "relative_path": relative_text, "matches": matches})
            if truncated:
                break
        if truncated:
            break
    return {"ok": True, "query": query, "total_matches": total, "files": grouped, "truncated": truncated}


def analyze_python_file(path_text: str) -> dict[str, Any]:
    path = Path(path_text).expanduser()
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        return {"ok": True, "problems": []}
    except SyntaxError as exc:
        return {"ok": True, "problems": [{
            "severity": "error",
            "path": str(path),
            "line": exc.lineno or 1,
            "column": exc.offset or 1,
            "message": exc.msg,
            "source": "Python",
        }]}
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "error": str(exc), "problems": []}


def get_git_status(root_path: str) -> dict[str, Any]:
    try:
        root = safe_project_root(root_path)
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=root, capture_output=True,
            text=True, timeout=4, check=False,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root,
            capture_output=True, text=True, timeout=8, check=False,
        )
        if status_result.returncode != 0:
            return {"ok": True, "is_repo": False, "branch": "", "files": {}}
        files: dict[str, str] = {}
        for line in status_result.stdout.splitlines():
            if len(line) < 4:
                continue
            code, name = line[:2], line[3:]
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            status = "C" if "U" in code or code in {"AA", "DD"} else "U" if code == "??" else "A" if "A" in code else "D" if "D" in code else "R" if "R" in code else "M"
            files[name.replace("/", os.sep)] = status
        return {"ok": True, "is_repo": True, "branch": branch_result.stdout.strip(), "files": files}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"ok": False, "is_repo": False, "branch": "", "files": {}, "error": str(exc)}


def inspect_file(path_text: str, max_size: int) -> dict[str, Any]:
    try:
        path = Path(path_text).expanduser().resolve(strict=True)
        size = path.stat().st_size
        raw = path.read_bytes()[:8192]
        return {
            "ok": True, "path": str(path), "name": path.name, "size": size,
            "binary": b"\x00" in raw, "large": size > max(100_000, max_size),
            "mtime": path.stat().st_mtime_ns,
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
