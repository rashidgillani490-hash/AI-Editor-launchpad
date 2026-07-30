"""
extension_manager.py — Local plugin system + event bus.

Scans a local `extensions/` folder for installable plugins.
Each plugin is a folder with a `manifest.json` and a Python
entry-point module. Supports load/enable/disable/unload at runtime.

Also provides a lightweight event bus that plugins (and core IDE)
can hook into via `on()` / `emit()`.

Usage (GUI hooks):
    from extension_manager import ExtensionManager

    em = ExtensionManager(extensions_dir="./extensions")

    # Scan and load
    em.scan()
    em.load_all_enabled()

    # Manage
    em.enable_plugin("my-plugin")
    em.disable_plugin("my-plugin")

    # Event bus
    em.emit("on_file_saved", {"path": "/foo.py"})
"""

from __future__ import annotations

import os
import json
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class PluginManifest:
    name: str
    version: str
    description: str
    entry_point: str        # Python module path relative to plugin folder
    enabled: bool = True


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    module: Any             # the loaded Python module
    plugin_path: str        # absolute path to the plugin folder


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Simple publish-subscribe event bus."""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable]] = {}

    def on(self, event: str, callback: Callable) -> None:
        """Register *callback* to be called when *event* is emitted."""
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        if event in self._handlers:
            try:
                self._handlers[event].remove(callback)
            except ValueError:
                pass

    def emit(self, event: str, data: Any = None) -> None:
        """Fire an event, calling all registered handlers with *data*."""
        for handler in self._handlers.get(event, []):
            try:
                handler(data)
            except Exception as exc:
                print(f"[EventBus] Error in handler for '{event}': {exc}", file=sys.stderr)

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()


# ---------------------------------------------------------------------------
# ExtensionManager
# ---------------------------------------------------------------------------

class ExtensionManager:
    """Local plugin loader and lifecycle manager, plus event bus."""

    def __init__(self, extensions_dir: str) -> None:
        self._extensions_dir = os.path.abspath(extensions_dir)
        self._plugins: Dict[str, LoadedPlugin] = {}  # name -> LoadedPlugin
        self._manifests: Dict[str, PluginManifest] = {}  # name -> manifest
        self.events = EventBus()

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(self) -> List[PluginManifest]:
        """Scan the extensions/ directory for plugin folders with manifest.json.
        Returns all discovered manifests.
        """
        self._manifests.clear()

        if not os.path.isdir(self._extensions_dir):
            return []

        for entry in os.listdir(self._extensions_dir):
            plugin_dir = os.path.join(self._extensions_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue
            manifest_path = os.path.join(plugin_dir, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                manifest = PluginManifest(
                    name=data.get("name", entry),
                    version=data.get("version", "0.0.0"),
                    description=data.get("description", ""),
                    entry_point=data.get("entry_point", ""),
                    enabled=data.get("enabled", True),
                )
                self._manifests[manifest.name] = manifest
            except (json.JSONDecodeError, OSError):
                continue

        return self.get_manifests()

    # ------------------------------------------------------------------
    # Load / unload
    # ------------------------------------------------------------------

    def load_plugin(self, name: str) -> Optional[LoadedPlugin]:
        """Load a plugin by name. Returns the LoadedPlugin on success."""
        manifest = self._manifests.get(name)
        if manifest is None:
            # Try scanning
            self.scan()
            manifest = self._manifests.get(name)
        if manifest is None:
            return None

        plugin_path = os.path.join(self._extensions_dir, name)
        entry_module_path = os.path.join(plugin_dir := plugin_path, manifest.entry_point)
        if not os.path.isfile(entry_module_path):
            # entry_point might be a dotted module name, resolve it
            entry_module_path = os.path.join(
                plugin_path, manifest.entry_point.replace(".", os.sep) + ".py"
            )
            if not os.path.isfile(entry_module_path):
                return None

        # Dynamically import the module
        spec = importlib.util.spec_from_file_location(
            f"nexcore_ext_{name}", entry_module_path
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"[ExtensionManager] Failed to load '{name}': {exc}", file=sys.stderr)
            return None

        loaded = LoadedPlugin(
            manifest=manifest,
            module=module,
            plugin_path=plugin_path,
        )
        self._plugins[name] = loaded

        # Call activate() if the module exposes it
        if hasattr(module, "activate"):
            try:
                module.activate(self.events)
            except Exception as exc:
                print(f"[ExtensionManager] activate() failed for '{name}': {exc}", file=sys.stderr)

        return loaded

    def load_all_enabled(self) -> List[LoadedPlugin]:
        """Load every enabled plugin."""
        loaded: List[LoadedPlugin] = []
        for name, manifest in self._manifests.items():
            if manifest.enabled:
                result = self.load_plugin(name)
                if result:
                    loaded.append(result)
        return loaded

    def unload_plugin(self, name: str) -> None:
        """Unload a plugin (calls deactivate() and removes the module)."""
        loaded = self._plugins.pop(name, None)
        if loaded is None:
            return
        if hasattr(loaded.module, "deactivate"):
            try:
                loaded.module.deactivate()
            except Exception as exc:
                print(f"[ExtensionManager] deactivate() failed for '{name}': {exc}", file=sys.stderr)
        # Remove from sys.modules
        mod_name = f"nexcore_ext_{name}"
        sys.modules.pop(mod_name, None)

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable_plugin(self, name: str) -> bool:
        """Enable a plugin (updates manifest and loads it)."""
        manifest = self._manifests.get(name)
        if manifest is None:
            return False
        manifest.enabled = True
        self._save_manifest(name, manifest)
        return self.load_plugin(name) is not None

    def disable_plugin(self, name: str) -> bool:
        """Disable a plugin (unloads it and updates manifest)."""
        self.unload_plugin(name)
        manifest = self._manifests.get(name)
        if manifest is None:
            return False
        manifest.enabled = False
        self._save_manifest(name, manifest)
        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_manifests(self) -> List[PluginManifest]:
        return list(self._manifests.values())

    def get_loaded_plugins(self) -> List[LoadedPlugin]:
        return list(self._plugins.values())

    def is_loaded(self, name: str) -> bool:
        return name in self._plugins

    def is_enabled(self, name: str) -> bool:
        manifest = self._manifests.get(name)
        return manifest is not None and manifest.enabled

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_manifest(self, name: str, manifest: PluginManifest) -> None:
        plugin_dir = os.path.join(self._extensions_dir, name)
        manifest_path = os.path.join(plugin_dir, "manifest.json")
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "entry_point": manifest.entry_point,
                    "enabled": manifest.enabled,
                }, f, indent=2)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ext_dir = os.path.join(tmp, "extensions")
        plugin_dir = os.path.join(ext_dir, "hello-plugin")
        os.makedirs(plugin_dir)

        # manifest.json
        manifest = {
            "name": "hello-plugin",
            "version": "1.0.0",
            "description": "A test plugin",
            "entry_point": "main.py",
            "enabled": True,
        }
        with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        # entry point
        with open(os.path.join(plugin_dir, "main.py"), "w") as f:
            f.write(
                "def activate(events):\n"
                '    print("Hello from plugin!")\n'
                '    events.on("on_file_saved", lambda d: print(f"File saved: {d}"))\n'
                "def deactivate():\n"
                '    print("Plugin deactivated")\n'
            )

        em = ExtensionManager(ext_dir)
        manifests = em.scan()
        print(f"Found {len(manifests)} plugin(s)")

        loaded = em.load_all_enabled()
        print(f"Loaded: {[p.manifest.name for p in loaded]}")

        # Test event bus
        em.events.emit("on_file_saved", {"path": "/tmp/test.py"})

        em.unload_plugin("hello-plugin")
        print("Unloaded")
