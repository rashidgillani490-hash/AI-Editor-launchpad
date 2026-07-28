"""Async wrappers for file_io operations (use asyncio.to_thread)."""

from __future__ import annotations

import asyncio
from typing import Optional

from ._read import _read_text, open_file
from ._write import _write_text, save_file, save_file_as
from ._create import create_file, create_folder
from ._tree import list_directory_tree
from ._result import FileOperationResult


async def open_file_async(path: str, encoding: str = "utf-8", *,
                          large_file_threshold: int = None) -> FileOperationResult:
    """Asynchronous variant of open_file that offloads work to a thread.

    This uses asyncio.to_thread and therefore does not require optional
    dependencies. It preserves the same returned FileOperationResult.
    """
    # Maintain original signature: default for large_file_threshold if not provided
    if large_file_threshold is None:
        # import the constant from encoding module to avoid import cycle; _read_text will use its default
        return await asyncio.to_thread(_read_text, path, encoding)
    return await asyncio.to_thread(_read_text, path, encoding, large_file_threshold)


async def save_file_async(path: str, content: str, encoding: str = "utf-8", *,
                          backup: bool = False, backup_suffix: str = ".bak") -> FileOperationResult:
    """Asynchronous variant of save_file that offloads work to a thread."""
    return await asyncio.to_thread(_write_text, path, content, encoding, True, backup, backup_suffix)


async def save_file_as_async(new_path: str, content: str, encoding: str = "utf-8", *,
                             backup: bool = False, backup_suffix: str = ".bak") -> FileOperationResult:
    """Asynchronous variant of save_file_as that offloads work to a thread."""
    return await asyncio.to_thread(_write_text, new_path, content, encoding, True, backup, backup_suffix)


async def create_file_async(path: str, content: str = "", encoding: str = "utf-8", *, exist_ok: bool = False) -> FileOperationResult:
    """Async variant of create_file."""
    return await asyncio.to_thread(create_file, path, content, encoding, exist_ok=exist_ok)


async def create_folder_async(path: str, *, exist_ok: bool = False) -> FileOperationResult:
    """Async variant of create_folder."""
    return await asyncio.to_thread(create_folder, path, exist_ok=exist_ok)


async def list_directory_tree_async(root_path: str, *, max_depth: Optional[int] = None, include_hidden: bool = False) -> FileOperationResult:
    """Async variant of list_directory_tree."""
    return await asyncio.to_thread(list_directory_tree, root_path, max_depth=max_depth, include_hidden=include_hidden)