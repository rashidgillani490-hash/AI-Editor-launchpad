"""
Nexcore IDE — Backend Modules Package

This package provides all backend logic for the Nexcore IDE.
Each module is UI-framework-agnostic — no Tkinter imports — so
they can be wired into any GUI toolkit.

Modules:
    file_tree          — Lazy-loading file-explorer tree + CRUD
    tabs_manager       — Editor tab lifecycle + dirty/save state
    selection_manager  — Multi-selection (single/Ctrl/Shift-click)
    context_menu_actions — Right-click menu handlers
    fs_watcher         — File-system watcher (watchdog or polling)
    terminal_manager   — Subprocess runner, multi-terminal, stream output
    menu_actions       — Menu bar backend handlers (File/Edit/View/…)
    run_manager        — Play/Run button logic
    pane_manager       — Split-screen multi-pane layout tree
    search_manager     — Async project-wide find/replace
    git_manager        — Git CLI wrapper (status/diff/commit/branch)
    debug_manager      — Debug adapter (debugpy-based)
    test_manager       — pytest/unittest discovery + execution
    extension_manager  — Local plugin loader + event bus
    remote_manager     — SSH/SFTP remote file browser
    syntax_highlighter — Python tokenizer + color classification
    editor_intelligence — Auto-indent, brackets, code folding
    autocomplete_manager — Jedi-based IntelliSense
    lint_manager       — Background linting (pyflakes/flake8/pylint)
"""

__version__ = "0.1.0"
