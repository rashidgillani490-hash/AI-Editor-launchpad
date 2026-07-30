"""
remote_manager.py — Remote Explorer backend (SSH/SFTP).

Connects to a remote host via paramiko, exposes a FileNode-based
directory tree for remote browsing, and supports reading/writing
files over SFTP.  Stores connection profiles in a local config file
(never plaintext passwords).

Usage (GUI hooks):
    from remote_manager import RemoteManager

    rm = RemoteManager()

    # Set up a profile
    rm.add_profile("myserver", host="192.168.1.1", username="user", key_path="~/.ssh/id_rsa")

    # Connect
    rm.connect("myserver")

    # Browse
    root = rm.list_directory("/home/user")
    for node in root.children:
        print(node.name)

    # Read a file
    content = rm.read_file("/home/user/script.py")

    # Write
    rm.write_file("/home/user/test.txt", "hello")

    # Disconnect
    rm.disconnect()
"""

from __future__ import annotations

import os
import json
import stat as stat_module
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field

from file_tree import FileNode


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class SSHProfile:
    """Saved connection profile."""
    name: str
    host: str
    port: int = 22
    username: str = ""
    key_path: str = ""          # path to private key; empty = prompt for password


# ---------------------------------------------------------------------------
# RemoteManager
# ---------------------------------------------------------------------------

class RemoteManager:
    """Manages SSH/SFTP connections and remote file browsing."""

    CONFIG_FILE = ".nexcore_remote_profiles.json"

    def __init__(
        self,
        config_dir: str = "",
        on_connection_change: Optional[Callable[[bool, str], None]] = None,
        # on_connection_change(connected, profile_name)
    ) -> None:
        self._config_dir = config_dir
        self._profiles: Dict[str, SSHProfile] = {}
        self._client = None      # paramiko.SSHClient
        self._sftp = None        # paramiko.SFTPClient
        self._connected_profile: Optional[str] = None
        self.on_connection_change = on_connection_change

        if config_dir:
            self._load_profiles()

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def add_profile(
        self,
        name: str,
        host: str,
        port: int = 22,
        username: str = "",
        key_path: str = "",
    ) -> SSHProfile:
        """Add or update a connection profile."""
        profile = SSHProfile(
            name=name,
            host=host,
            port=port,
            username=username,
            key_path=os.path.expanduser(key_path) if key_path else "",
        )
        self._profiles[name] = profile
        self._save_profiles()
        return profile

    def remove_profile(self, name: str) -> bool:
        if name in self._profiles:
            del self._profiles[name]
            self._save_profiles()
            return True
        return False

    def get_profile(self, name: str) -> Optional[SSHProfile]:
        return self._profiles.get(name)

    def list_profiles(self) -> List[SSHProfile]:
        return list(self._profiles.values())

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def connect(
        self,
        profile_name: str,
        password: Optional[str] = None,
    ) -> bool:
        """Connect to a remote host using the named profile.
        If the profile has no key_path, *password* is required.
        Returns True on success.
        """
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"Unknown profile: {profile_name!r}")

        try:
            import paramiko
        except ImportError:
            raise RuntimeError(
                "paramiko is required for remote connections. "
                "Install it with: pip install paramiko"
            )

        self.disconnect()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            if profile.key_path and os.path.isfile(profile.key_path):
                key = paramiko.RSAKey.from_private_key_file(profile.key_path)
                client.connect(
                    hostname=profile.host,
                    port=profile.port,
                    username=profile.username,
                    pkey=key,
                    timeout=10,
                )
            else:
                if password is None:
                    raise ValueError("Password required (no key file configured).")
                client.connect(
                    hostname=profile.host,
                    port=profile.port,
                    username=profile.username,
                    password=password,
                    timeout=10,
                )

            self._client = client
            self._sftp = client.open_sftp()
            self._connected_profile = profile_name

            if self.on_connection_change:
                self.on_connection_change(True, profile_name)
            return True

        except Exception as exc:
            client.close()
            raise ConnectionError(f"Failed to connect to {profile.host}: {exc}") from exc

    def disconnect(self) -> None:
        """Close the current connection."""
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        prev = self._connected_profile
        self._connected_profile = None
        if prev and self.on_connection_change:
            self.on_connection_change(False, prev)

    def reconnect(self) -> bool:
        """Reconnect using the last active profile."""
        if self._connected_profile:
            return self.connect(self._connected_profile)
        return False

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._sftp is not None

    @property
    def connected_profile_name(self) -> Optional[str]:
        return self._connected_profile

    # ------------------------------------------------------------------
    # Directory listing (returns FileNode tree)
    # ------------------------------------------------------------------

    def list_directory(self, remote_path: str) -> FileNode:
        """List a remote directory. Returns a FileNode with children.
        Raises an error if not connected.
        """
        if not self.is_connected or self._sftp is None:
            raise ConnectionError("Not connected to a remote host.")

        # Resolve home shorthand
        rpath = remote_path
        if rpath.startswith("~"):
            try:
                self._sftp.chdir(".")
                home = self._sftp.getcwd() or "/"
            except Exception:
                home = "/"
            rpath = rpath.replace("~", home, 1)

        node = FileNode(
            name=os.path.basename(rpath) or rpath,
            path=rpath,
            is_dir=True,
            is_expanded=True,
        )

        try:
            attrs = self._sftp.listdir_attr(rpath)
        except IOError as exc:
            raise FileNotFoundError(f"Cannot list {rpath!r}: {exc}") from exc

        children: List[FileNode] = []
        for attr in attrs:
            is_dir = stat_module.S_ISDIR(attr.st_mode) if attr.st_mode is not None else False
            child = FileNode(
                name=attr.filename,
                path=os.path.join(rpath, attr.filename).replace("\\", "/"),
                is_dir=is_dir,
                extension="" if is_dir else os.path.splitext(attr.filename)[1].lower(),
                parent=node,
            )
            children.append(child)

        # Sort folders first
        children.sort(key=lambda c: (not c.is_dir, c.name.lower()))
        node.children = children
        node.is_loaded = True
        return node

    # ------------------------------------------------------------------
    # Read / write files
    # ------------------------------------------------------------------

    def read_file(self, remote_path: str) -> str:
        """Read a remote file's content over SFTP."""
        if not self.is_connected or self._sftp is None:
            raise ConnectionError("Not connected.")
        try:
            with self._sftp.open(remote_path, "r") as f:
                return f.read()
        except IOError as exc:
            raise FileNotFoundError(f"Cannot read {remote_path!r}: {exc}") from exc

    def write_file(self, remote_path: str, content: str) -> None:
        """Write content to a remote file over SFTP."""
        if not self.is_connected or self._sftp is None:
            raise ConnectionError("Not connected.")
        try:
            with self._sftp.open(remote_path, "w") as f:
                f.write(content)
        except IOError as exc:
            raise OSError(f"Cannot write {remote_path!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # Remote shell channel
    # ------------------------------------------------------------------

    def open_shell(
        self,
        on_output: Optional[Callable[[str], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Open an interactive SSH shell channel.
        Returns the paramiko Channel for the GUI to read/write.
        """
        if not self.is_connected or self._client is None:
            raise ConnectionError("Not connected.")
        channel = self._client.invoke_shell()
        # The GUI will handle reading/writing from this channel
        return channel

    def exec_command(self, command: str) -> tuple:
        """Execute a command on the remote host and return (stdout, stderr, exit_code)."""
        if not self.is_connected or self._client is None:
            raise ConnectionError("Not connected.")
        stdin, stdout, stderr = self._client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode("utf-8", errors="replace"), \
            stderr.read().decode("utf-8", errors="replace"), \
            exit_code

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_profiles(self) -> None:
        config_path = os.path.join(self._config_dir, self.CONFIG_FILE)
        if not os.path.isfile(config_path):
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("profiles", []):
                profile = SSHProfile(
                    name=item["name"],
                    host=item["host"],
                    port=item.get("port", 22),
                    username=item.get("username", ""),
                    key_path=item.get("key_path", ""),
                )
                self._profiles[profile.name] = profile
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    def _save_profiles(self) -> None:
        if not self._config_dir:
            return
        config_path = os.path.join(self._config_dir, self.CONFIG_FILE)
        data = {
            "profiles": [
                {
                    "name": p.name,
                    "host": p.host,
                    "port": p.port,
                    "username": p.username,
                    "key_path": p.key_path,
                }
                for p in self._profiles.values()
            ]
        }
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def shutdown(self) -> None:
        self.disconnect()


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rm = RemoteManager()

    # Add a profile (no actual connection in this example)
    rm.add_profile(
        "example",
        host="192.168.1.100",
        username="user",
        key_path="~/.ssh/id_rsa",
    )

    profiles = rm.list_profiles()
    for p in profiles:
        print(f"Profile: {p.name} → {p.username}@{p.host}:{p.port}")

    print(f"Connected: {rm.is_connected}")
    rm.shutdown()
