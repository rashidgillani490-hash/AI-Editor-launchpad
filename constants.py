"""constants.py - Shared styling and lightweight helpers for the NexCore IDE GUI.

Acts as the project's "assets" module: no image/icon asset files are used
(the UI is built from Unicode glyphs), so this file is the single source of
truth for the color palette, fonts, Python syntax highlighting, and the
Explorer sidebar's hidden-file filter. ``gui_layout.py`` and
``ui_components.py`` both import from here so retuning the theme never
requires touching widget code.
"""

from __future__ import annotations

import fnmatch
import keyword as keyword_module
import re
import tkinter as tk
import tkinter.font as tkfont

# ============================================================================
# VS Code-inspired dark color palette. Every widget pulls its colors from
# here so the theme stays consistent and easy to retune in one place.
# ============================================================================
COLORS = {
    "bg": "#1e1e1e",                 # Editor / main canvas background.
    "topbar_bg": "#3c3c3c",          # Top menu bar - deliberately lighter than the sidebar.
    "sidebar_bg": "#252526",
    "sidebar_header_fg": "#bbbbbb",
    "activity_bar_bg": "#181818",
    "activity_active_bg": "#242424",
    "activity_icon_fg": "#858585",
    "activity_icon_active_fg": "#ffffff",
    "tab_active_bg": "#1e1e1e",
    "tab_inactive_bg": "#2d2d2d",
    "tab_active_fg": "#ffffff",
    "tab_inactive_fg": "#969696",
    "accent": "#007acc",
    "border": "#3c3c3c",
    "gutter_bg": "#1e1e1e",
    "gutter_fg": "#858585",
    "gutter_fg_active": "#c6c6c6",
    "text_fg": "#d4d4d4",
    "selection_bg": "#264f78",
    "hover_bg": "#2a2d2e",
    "menu_bg": "#25252b",
    "menu_hover": "#094771",
    "floating_bg": "#25252b",
    "floating_border": "#4a4a57",
    "floating_overlay": "#111116",
    "floating_glow": "#30304a",
    "console_bg": "#1e1e1e",
    "console_stdout": "#d4d4d4",
    "console_stderr": "#f48771",
    "console_info": "#3794ff",
    "statusbar_bg": "#007acc",
    "statusbar_fg": "#ffffff",
    "run_green": "#2ea043",
    "run_green_hover": "#3fb950",
    "stop_red": "#f14c4c",
    "stop_red_hover": "#ff6b6b",
    "action_toolbar_bg": "#252526",
    "syntax_comment": "#6a9955",
    "syntax_string": "#ce9178",
    "syntax_keyword": "#569cd6",
    "syntax_number": "#b5cea8",
    "link": "#3794ff",
    "link_hover": "#5babff",
    "welcome_heading": "#cccccc",
    "welcome_muted": "#9d9d9d",
    "card_hover_bg": "#383838",
    "icon_disabled_fg": "#5a5a5a",
    "badge_bg": "#3c3c3c",
    "search_match_bg": "#613214",
    "search_match_fg": "#ffffff",
    "breadcrumb_fg": "#9d9d9d",
    "breadcrumb_active_fg": "#d4d4d4",
    "breadcrumb_hover_bg": "#2a2d2e",
    "problem_warning": "#cca700",
    "problem_error": "#f14c4c",
    "split_focus": "#3794ff",
    "scm_modified": "#e2c08d",
    "scm_untracked": "#73c991",
    "extension_icon_bg": "#3776ab",
    # AI Assistant panel (right column).
    "ai_user_label": "#3794ff",
    "ai_assistant_label": "#2ea043",
    "ai_error_label": "#f48771",
    "ai_user_bubble": "#264f78",     # Subtle blue tint for the user's own messages.
    "ai_assistant_bubble": "#2d2d2d",  # Slightly lighter than the panel bg (#252526).
    "ai_error_bubble": "#4b2020",    # Subtle dark red tint for error messages.
}

# ============================================================================
# Fonts. Every element gets its own named size per the design spec rather
# than one blanket UI_FONT, though several elements deliberately land on
# the same 13px baseline (tab labels, Explorer names, menu items, Welcome
# section headers) - UI_FONT/UI_FONT_BOLD cover that shared baseline.
# ============================================================================
UI_FONT = ("Segoe UI", 13)
UI_FONT_BOLD = ("Segoe UI", 13, "bold")
TOPBAR_FONT = ("Segoe UI", 13)
SMALL_FONT = ("Segoe UI", 12)                # Status bar, dialog hint text.
HEADER_FONT = ("Segoe UI", 12, "bold")        # EXPLORER / OUTPUT / AI ASSISTANT section headers.
ICON_FONT_FAMILY = "Segoe MDL2 Assets"
ICON_FONT = (ICON_FONT_FAMILY, 14)
ICON_FONT_LARGE = (ICON_FONT_FAMILY, 20)

# Microsoft Segoe Fluent Icons glyphs, available with Windows.  Keeping
# codepoints here prevents individual widgets from mixing emoji, text
# symbols, and hand-drawn approximations.
ICONS = {
    "back": "\ue72b",
    "forward": "\ue72a",
    "search": "\ue721",
    "chevron_down": "\ue70d",
    "close": "\ue711",
    "minimize": "\ue921",
    "maximize": "\ue922",
    "restore": "\ue923",
    "add": "\ue710",
    "delete": "\ue74d",
    "more": "\ue712",
    "settings": "\ue713",
    "account": "\ue77b",
    "folder": "\ue8b7",
    "folder_open": "\ue838",
    "file": "\ue8a5",
    "explorer": "\ue8b7",
    "source_control": "\ue8a7",
    "run": "\ue768",
    "stop": "\ue71a",
    "debug": "\ue7a7",
    "extensions": "\ue71b",
    "testing": "\ue9d9",
    "sparkle": "\ue735",
    "panel_left": "\ue700",
    "panel_right": "\ue89f",
    "panel_bottom": "\ue90c",
    "split": "\ue8a9",
    "terminal": "\ue756",
    "warning": "\ue7ba",
    "error": "\ue783",
    "branch": "\ue8ad",
    "bell": "\ue7ed",
    "clear": "\ue894",
    "fullscreen": "\ue740",
    "copy": "\ue8c8",
    "send": "\ue724",
    "chat": "\ue8bd",
    "save": "\ue74e",
    "python": "\ue943",
    "app": "\ue7c5",
}

CODE_FONT_SIZE = 15
CODE_GUTTER_FONT_SIZE = 13
# tkinter.Text has no literal CSS "line-height" - spacing1/spacing3 (px
# added above/below each line) is the closest approximation, and must be
# applied identically, in pixels, to both the gutter and the code Text
# widgets or their line numbers drift out of vertical alignment with the
# code as the file grows.
CODE_LINE_SPACING = 4

WELCOME_TITLE_FONT_SIZE = 42
WELCOME_TAGLINE_FONT_SIZE = 15
WELCOME_LINK_FONT_SIZE = 14

AI_MESSAGE_FONT_SIZE = 14
AI_LABEL_FONT_SIZE = 11


def letter_spaced(text: str, sep: str = " ") -> str:
    """Approximate CSS letter-spacing for small-caps section headers.

    Tkinter has no character-tracking control, so this inserts a thin
    space (U+2009) between characters - subtle, unlike a full space,
    which would look overly gappy on a short all-caps word like
    "EXPLORER".
    """
    return sep.join(text)


def pick_monospace_font(root: tk.Misc) -> str:
    """Return the best available monospace font for the code editor.

    Tries a few popular coding fonts in order of preference and falls
    back to a font guaranteed to exist on Windows.
    """
    preferred = ("JetBrains Mono", "Fira Code", "Cascadia Code", "Consolas")
    available = set(tkfont.families(root))
    for name in preferred:
        if name in available:
            return name
    return "Courier New"


# ============================================================================
# Regex-based Python syntax highlighting (not a real parser - a single
# combined regex scanned left-to-right, which is enough for a test harness
# and correctly avoids re-tagging keywords/comments that fall inside an
# already-matched string, since matches never overlap).
# ============================================================================
_PY_KEYWORDS = sorted(
    {re.escape(w) for w in set(keyword_module.kwlist) | set(getattr(keyword_module, "softkwlist", []))},
    key=len,
    reverse=True,
)
_TOKEN_REGEX = re.compile(
    r"(?P<comment>#[^\n]*)"
    r"|(?P<string>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')"
    r"|(?P<keyword>\b(?:" + "|".join(_PY_KEYWORDS) + r")\b)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
)
SYNTAX_TAGS = ("comment", "string", "keyword", "number")


def apply_python_syntax_highlighting(text_widget: tk.Text) -> None:
    """Re-tag the full buffer of ``text_widget`` with basic Python colors.

    Clears the previous highlight tags and reapplies them from a fresh
    scan of the current content. Simple and O(n) per keystroke, which is
    plenty fast for the small demo files this harness is meant for.
    """
    content = text_widget.get("1.0", "end-1c")
    for tag in SYNTAX_TAGS:
        text_widget.tag_remove(tag, "1.0", "end")
    for match in _TOKEN_REGEX.finditer(content):
        tag = match.lastgroup
        text_widget.tag_add(tag, f"1.0+{match.start()}c", f"1.0+{match.end()}c")


# ============================================================================
# Entries the Explorer sidebar should never surface - tool/VCS metadata
# directories and generated files a user never wants to open as "source".
# ============================================================================
SIDEBAR_IGNORE_NAMES = {".claude", "__pycache__", ".git", ".vscode", ".idea", ".pytest_cache", ".mypy_cache"}
SIDEBAR_IGNORE_GLOBS = ("*.pyc", "*.pyo")


def is_ignored_entry(name: str) -> bool:
    """Return True if `name` should be hidden from the Explorer sidebar.

    Hides dotfiles/dotfolders (``.claude``, ``.git``, ...), known
    tool-cache directories (``__pycache__``), and generated bytecode
    files (``*.pyc``/``*.pyo``) so only real project source is listed.
    """
    if name.startswith("."):
        return True
    if name in SIDEBAR_IGNORE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in SIDEBAR_IGNORE_GLOBS)
