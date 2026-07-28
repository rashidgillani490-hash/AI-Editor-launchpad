"""ui_components.py - Reusable UI widgets for the NexCore IDE GUI.

Every class here is a self-contained, importable Tkinter widget that
``gui_layout.py`` assembles into the full application window. None of
these classes talk to each other directly - each one takes plain
callables (``on_select``, ``on_file_selected``, ``get_context``, ...) as
constructor arguments, so ``gui_layout.py`` is the only place that wires
them to ``editor_core``/``file_io``/``execution_engine``.

Contents
--------
- :class:`CodeEditor` - line-numbered, syntax-highlighted text editor.
  Implements the ``get``/``insert``/``delete``/``bind`` surface that
  ``editor_core.TextWidgetLike`` expects, so ``EditorWorkspace`` can use
  it as a drop-in widget factory.
- :class:`TabButton` - a single flat editor tab with a hover-to-reveal
  close "x".
- :class:`Sidebar` - a recursive, lazily-expanding file explorer built on
  ``ttk.Treeview`` (real expand/collapse arrows and nested children, not
  a flat single-level list).
- :class:`ActivityBar` - switches the left sidebar between Explorer,
  Search, Source Control, and Extensions views.
- :class:`SearchPanel` - a VS Code-style mock search/replace results view.
- :class:`SourceControlPanel` - a mock branch, changes, and commit view.
- :class:`ExtensionsPanel` - a mock extension marketplace list.
- :class:`ConsolePanel` - the collapsible "OUTPUT" terminal panel.
- :class:`StatusBar` - the bottom VS Code-style blue status bar.
- :class:`WelcomeScreen` - the two-column Welcome page shown when no
  tabs are open.
- :class:`AIPanel` - the right-column "AI Assistant" chat panel, wired
  to the real Anthropic API on a background thread.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from typing import Callable, Dict, List, Optional, Tuple

import customtkinter

from constants import (
    AI_LABEL_FONT_SIZE,
    AI_MESSAGE_FONT_SIZE,
    CODE_FONT_SIZE,
    CODE_GUTTER_FONT_SIZE,
    CODE_LINE_SPACING,
    COLORS,
    HEADER_FONT,
    SMALL_FONT,
    SYNTAX_TAGS,
    UI_FONT,
    UI_FONT_BOLD,
    WELCOME_LINK_FONT_SIZE,
    WELCOME_TAGLINE_FONT_SIZE,
    WELCOME_TITLE_FONT_SIZE,
    apply_python_syntax_highlighting,
    is_ignored_entry,
    letter_spaced,
)


class _PlaceholderEntry(tk.Entry):
    """Dark themed entry with placeholder text, used by mock sidebar views."""

    def __init__(self, parent: tk.Misc, placeholder: str, **kwargs) -> None:
        self.placeholder = placeholder
        self._placeholder_visible = False
        super().__init__(
            parent,
            bg=COLORS["bg"],
            fg=COLORS["text_fg"],
            insertbackground=COLORS["text_fg"],
            selectbackground=COLORS["selection_bg"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=UI_FONT,
            **kwargs,
        )
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not self.get():
            self._placeholder_visible = True
            self.configure(fg=COLORS["tab_inactive_fg"])
            self.insert(0, self.placeholder)

    def _on_focus_in(self, _event=None) -> None:
        if self._placeholder_visible:
            self.delete(0, "end")
            self.configure(fg=COLORS["text_fg"])
            self._placeholder_visible = False

    def _on_focus_out(self, _event=None) -> None:
        self._show_placeholder()


class _SidebarView(tk.Frame):
    """Shared fixed-size-safe shell for views hosted beside the Activity Bar."""

    def __init__(self, parent: tk.Misc, title: str, width: int = 1) -> None:
        super().__init__(parent, bg=COLORS["sidebar_bg"], width=width)
        self.pack_propagate(False)
        header = tk.Frame(self, bg=COLORS["sidebar_bg"])
        header.pack(fill="x", padx=12, pady=(12, 8))
        tk.Label(
            header,
            text=letter_spaced(title.upper()),
            bg=COLORS["sidebar_bg"],
            fg=COLORS["sidebar_header_fg"],
            font=HEADER_FONT,
            anchor="w",
        ).pack(side="left")


class _InlineToggleEntry(tk.Frame):
    """Entry shell with compact visual-only toggle controls on its right."""

    def __init__(self, parent: tk.Misc, placeholder: str, toggles: Tuple[str, ...]) -> None:
        super().__init__(
            parent, bg=COLORS["bg"], highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
        )
        self.entry = _PlaceholderEntry(self, placeholder)
        self.entry.configure(highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.toggle_labels: List[tk.Label] = []
        for text in toggles:
            label = tk.Label(
                self, text=text, bg=COLORS["bg"], fg=COLORS["tab_inactive_fg"],
                font=(UI_FONT[0], 10, "bold"), padx=4, pady=3, cursor="hand2",
            )
            label.pack(side="left", padx=1)
            label._toggle_active = False
            label.bind("<Button-1>", lambda _event, item=label: self._toggle(item))
            self.toggle_labels.append(label)

    @staticmethod
    def _toggle(label: tk.Label) -> None:
        label._toggle_active = not label._toggle_active
        label.configure(
            bg=COLORS["selection_bg"] if label._toggle_active else COLORS["bg"],
            fg=COLORS["activity_icon_active_fg"] if label._toggle_active else COLORS["tab_inactive_fg"],
        )


class ActivityBar(tk.Frame):
    """VS Code-style icon rail that selects exactly one left sidebar view."""

    ITEMS = (
        ("explorer", "▱", "Explorer"),
        ("search", "⌕", "Search"),
        ("source_control", "⑂", "Source Control"),
        ("run_debug", "▷", "Run and Debug"),
        ("extensions", "▦", "Extensions"),
    )

    def __init__(self, parent: tk.Misc, on_select: Callable[[str], None]) -> None:
        super().__init__(parent, bg=COLORS["activity_bar_bg"], width=48)
        self.grid_propagate(False)
        self._on_select = on_select
        self._buttons: Dict[str, Tuple[tk.Frame, tk.Frame, tk.Canvas]] = {}
        self._active = "explorer"

        for key, _glyph, _tooltip in self.ITEMS:
            row = tk.Frame(self, bg=COLORS["activity_bar_bg"], height=50, cursor="hand2")
            row.pack(fill="x")
            row.pack_propagate(False)
            accent = tk.Frame(row, bg=COLORS["activity_bar_bg"], width=2)
            accent.pack(side="left", fill="y")
            icon = tk.Canvas(
                row,
                bg=COLORS["activity_bar_bg"],
                width=42,
                height=48,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            icon.pack(fill="both", expand=True)
            self._draw_icon(icon, key, COLORS["activity_icon_fg"])
            for widget in (row, accent, icon):
                widget.bind("<Button-1>", lambda _event, item=key: self.select(item))
            icon.bind(
                "<Enter>",
                lambda _event, canvas=icon: self._color_icon(canvas, COLORS["text_fg"]),
            )
            icon.bind("<Leave>", lambda _event, item=key: self._restyle_icon(item))
            self._buttons[key] = (row, accent, icon)
        self._utility_canvases: Dict[str, tk.Canvas] = {}
        self._make_utility_button("settings").pack(side="bottom", fill="x")
        self._make_utility_button("accounts").pack(side="bottom", fill="x")
        self._refresh()

    def _make_utility_button(self, key: str) -> tk.Frame:
        row = tk.Frame(self, bg=COLORS["activity_bar_bg"], height=48, cursor="hand2")
        row.pack_propagate(False)
        canvas = tk.Canvas(
            row, bg=COLORS["activity_bar_bg"], width=46, height=46,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        canvas.pack(fill="both", expand=True)
        self._draw_icon(canvas, key, COLORS["activity_icon_fg"])
        for widget in (row, canvas):
            widget.bind("<Button-1>", lambda event, item=key: self._show_utility_popup(event, item))
        canvas.bind("<Enter>", lambda _event, item=canvas: self._color_icon(item, COLORS["text_fg"]))
        canvas.bind("<Leave>", lambda _event, item=canvas: self._color_icon(item, COLORS["activity_icon_fg"]))
        self._utility_canvases[key] = canvas
        return row

    def _show_utility_popup(self, event: tk.Event, key: str) -> None:
        menu = tk.Menu(
            self, tearoff=0, bg=COLORS["menu_bg"], fg=COLORS["text_fg"],
            activebackground=COLORS["menu_hover"], activeforeground="#ffffff",
            relief="flat", bd=0, font=UI_FONT,
        )
        text = "Sign in to sync settings" if key == "accounts" else "Settings (not implemented in this preview)"
        menu.add_command(label=text, command=lambda: None)
        menu.tk_popup(event.x_root + 8, event.y_root - 8)

    @staticmethod
    def _draw_icon(canvas: tk.Canvas, key: str, color: str) -> None:
        """Draw simple vector icons so rendering never depends on glyph fonts."""
        common = {"fill": color, "outline": color, "width": 1.8, "tags": "activity_icon"}
        if key == "explorer":
            canvas.create_rectangle(13, 10, 28, 29, **common)
            canvas.create_rectangle(18, 15, 33, 34, fill="", outline=color, width=1.8, tags="activity_icon")
        elif key == "search":
            canvas.create_oval(10, 9, 28, 27, fill="", outline=color, width=2.2, tags="activity_icon")
            canvas.create_line(25, 25, 34, 34, fill=color, width=2.2, tags="activity_icon")
        elif key == "source_control":
            canvas.create_line(14, 11, 14, 31, 29, 24, 29, 12, fill=color, width=1.8, tags="activity_icon")
            for x, y in ((14, 10), (14, 32), (29, 11), (29, 24)):
                canvas.create_oval(
                    x - 3, y - 3, x + 3, y + 3, fill=COLORS["activity_bar_bg"],
                    outline=color, width=1.8, tags="activity_icon",
                )
        elif key == "run_debug":
            canvas.create_polygon(
                10, 10, 10, 31, 27, 20, fill="", outline=color, width=1.8, tags="activity_icon",
            )
            canvas.create_oval(22, 22, 34, 34, fill="", outline=color, width=1.8, tags="activity_icon")
        elif key == "extensions":
            for x, y in ((11, 10), (23, 10), (11, 22), (23, 22)):
                canvas.create_rectangle(x, y, x + 9, y + 9, **common)
        elif key == "accounts":
            canvas.create_oval(17, 8, 29, 20, fill="", outline=color, width=1.8, tags="activity_icon")
            canvas.create_arc(
                11, 18, 35, 40, start=15, extent=150, style="arc",
                outline=color, width=1.8, tags="activity_icon",
            )
        else:
            canvas.create_oval(14, 11, 32, 29, fill="", outline=color, width=1.8, tags="activity_icon")
            canvas.create_oval(20, 17, 26, 23, fill="", outline=color, width=1.5, tags="activity_icon")
            for x1, y1, x2, y2 in (
                (23, 7, 23, 12), (23, 28, 23, 33), (10, 20, 15, 20), (31, 20, 36, 20),
                (14, 11, 17, 14), (29, 26, 32, 29), (32, 11, 29, 14), (17, 26, 14, 29),
            ):
                canvas.create_line(x1, y1, x2, y2, fill=color, width=2, tags="activity_icon")

    @staticmethod
    def _color_icon(canvas: tk.Canvas, color: str) -> None:
        for item in canvas.find_withtag("activity_icon"):
            item_type = canvas.type(item)
            if item_type in {"line", "polygon"}:
                canvas.itemconfigure(item, fill=color)
            elif item_type in {"rectangle", "oval", "arc"}:
                canvas.itemconfigure(item, outline=color)
                if canvas.itemcget(item, "fill"):
                    canvas.itemconfigure(item, fill=color)

    def select(self, key: str) -> None:
        if key not in self._buttons:
            return
        self._active = key
        self._refresh()
        self._on_select(key)

    def _restyle_icon(self, key: str) -> None:
        _row, _accent, icon = self._buttons[key]
        self._color_icon(
            icon,
            COLORS["activity_icon_active_fg"] if key == self._active else COLORS["activity_icon_fg"],
        )

    def _refresh(self) -> None:
        for key, (row, accent, icon) in self._buttons.items():
            active = key == self._active
            bg = COLORS["activity_active_bg"] if active else COLORS["activity_bar_bg"]
            row.configure(bg=bg)
            accent.configure(bg=COLORS["accent"] if active else COLORS["activity_bar_bg"])
            icon.configure(bg=bg)
            self._color_icon(
                icon,
                COLORS["activity_icon_active_fg"] if active else COLORS["activity_icon_fg"],
            )


class SearchPanel(_SidebarView):
    """Mock Search sidebar; input text intentionally does not filter results."""

    MOCK_MATCH_TERM = "mock"
    MOCK_RESULTS = (
        ("example_module.py", (("12", "def load_mock_project():"), ("27", "mock_status = \"ready\""))),
        ("sample_utils.py", (("8", "return build_mock_result(data)"), ("19", "mock_items.append(item)"))),
        ("demo_screen.py", (("31", "title = \"Mock Preview\""),)),
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, "Search")

        actions = tk.Frame(self, bg=COLORS["sidebar_bg"])
        actions.pack(fill="x", padx=9, pady=(0, 7))
        for glyph in reversed(("↻", "×", "≡", "▤", "⇈")):
            button = tk.Label(
                actions, text=glyph, bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
                font=("Segoe UI Symbol", 15), padx=4, pady=2, cursor="hand2",
            )
            button.pack(side="right", padx=2)
            button.bind("<Enter>", lambda event: event.widget.configure(fg=COLORS["text_fg"]))
            button.bind("<Leave>", lambda event: event.widget.configure(fg=COLORS["sidebar_header_fg"]))

        search_row = tk.Frame(self, bg=COLORS["sidebar_bg"])
        self.search_row = search_row
        search_row.pack(fill="x", padx=10)
        self.replace_toggle = tk.Label(
            search_row, text="▸", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=UI_FONT_BOLD, cursor="hand2",
        )
        self.replace_toggle.pack(side="left", padx=(0, 5))
        self.search_box = _InlineToggleEntry(search_row, "Search", ("Aa", "ab", ".*"))
        self.search_box.pack(side="left", fill="x", expand=True)
        self.search_entry = self.search_box.entry
        self.replace_toggle.bind("<Button-1>", lambda _event: self._toggle_replace())

        self.replace_row = tk.Frame(self, bg=COLORS["sidebar_bg"])
        replace_shell = tk.Frame(self.replace_row, bg=COLORS["sidebar_bg"])
        replace_shell.pack(fill="x", padx=(26, 10), pady=(6, 0))
        self.replace_box = _InlineToggleEntry(replace_shell, "Replace", ("Aa", "AB"))
        self.replace_box.pack(side="left", fill="x", expand=True)
        self.replace_entry = self.replace_box.entry
        replace_all = tk.Label(
            replace_shell, text="⇄", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=("Segoe UI Symbol", 15), padx=5, cursor="hand2",
        )
        replace_all.pack(side="right", padx=(4, 0))
        replace_all.bind("<Enter>", lambda event: event.widget.configure(fg=COLORS["text_fg"]))
        replace_all.bind("<Leave>", lambda event: event.widget.configure(fg=COLORS["sidebar_header_fg"]))
        self._replace_visible = False

        summary = tk.Label(
            self, text="3 files · 5 results", bg=COLORS["sidebar_bg"], fg=COLORS["tab_inactive_fg"],
            font=SMALL_FONT, anchor="w",
        )
        summary.pack(fill="x", padx=12, pady=(12, 4))

        results = tk.Frame(self, bg=COLORS["sidebar_bg"])
        results.pack(fill="both", expand=True)
        for filename, matches in self.MOCK_RESULTS:
            file_row = tk.Frame(results, bg=COLORS["sidebar_bg"])
            file_row.pack(fill="x", padx=8, pady=(5, 1))
            tk.Label(
                file_row, text="▾  📄", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
                font=SMALL_FONT,
            ).pack(side="left")
            tk.Label(
                file_row, text=filename, bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
                font=UI_FONT_BOLD, anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Label(
                file_row, text=str(len(matches)), bg=COLORS["badge_bg"], fg=COLORS["text_fg"],
                font=SMALL_FONT, padx=6,
            ).pack(side="right")

            for line_no, snippet in matches:
                row = tk.Frame(results, bg=COLORS["sidebar_bg"])
                row.pack(fill="x", padx=(28, 8), pady=1)
                tk.Label(
                    row, text=line_no, width=4, anchor="e", bg=COLORS["sidebar_bg"],
                    fg=COLORS["gutter_fg"], font=SMALL_FONT,
                ).pack(side="left", padx=(0, 6))
                text = tk.Text(
                    row, height=1, wrap="none", bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
                    font=SMALL_FONT, relief="flat", bd=0, highlightthickness=0, cursor="arrow",
                )
                text.pack(side="left", fill="x", expand=True)
                text.insert("1.0", snippet)
                start = snippet.lower().find(self.MOCK_MATCH_TERM)
                if start >= 0:
                    text.tag_add("match", f"1.{start}", f"1.{start + len(self.MOCK_MATCH_TERM)}")
                text.tag_configure(
                    "match", background=COLORS["search_match_bg"], foreground=COLORS["search_match_fg"],
                )
                text.configure(state="disabled")

    def _toggle_replace(self) -> None:
        self._replace_visible = not self._replace_visible
        self.replace_toggle.configure(text="▾" if self._replace_visible else "▸")
        if self._replace_visible:
            self.replace_row.pack(fill="x", after=self.search_row)
        else:
            self.replace_row.pack_forget()


class SourceControlPanel(_SidebarView):
    """Visual-only source-control view populated with mock changes."""

    MOCK_CHANGES = (
        ("M", "example_module.py"),
        ("M", "sample_utils.py"),
        ("U", "demo_notes.txt"),
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, "Source Control")
        branch = tk.Frame(self, bg=COLORS["sidebar_bg"])
        branch.pack(fill="x", padx=12, pady=(0, 9))
        tk.Label(
            branch, text="⑂", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=("Segoe UI Symbol", 16),
        ).pack(side="left")
        tk.Label(
            branch, text="main", bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
            font=UI_FONT_BOLD,
        ).pack(side="left", padx=6)

        self.message_entry = _PlaceholderEntry(self, "Message (press Ctrl+Enter to commit)")
        self.message_entry.pack(fill="x", padx=10, ipady=7)
        commit = tk.Button(
            self, text="Commit", bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["menu_hover"], activeforeground="#ffffff",
            relief="flat", bd=0, font=UI_FONT, cursor="hand2",
        )
        commit.pack(fill="x", padx=10, pady=(7, 12), ipady=4)

        section = tk.Frame(self, bg=COLORS["sidebar_bg"])
        section.pack(fill="x", padx=8, pady=(2, 4))
        tk.Label(
            section, text="▾  CHANGES", bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
            font=UI_FONT_BOLD,
        ).pack(side="left")
        tk.Label(
            section, text=str(len(self.MOCK_CHANGES)), bg=COLORS["badge_bg"], fg=COLORS["text_fg"],
            font=SMALL_FONT, padx=6,
        ).pack(side="right")

        for status, filename in self.MOCK_CHANGES:
            row = tk.Frame(self, bg=COLORS["sidebar_bg"])
            row.pack(fill="x", padx=(24, 12), pady=3)
            tk.Label(
                row, text=f"📄  {filename}", bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
                font=UI_FONT, anchor="w",
            ).pack(side="left", fill="x", expand=True)
            color = COLORS["scm_modified"] if status == "M" else COLORS["scm_untracked"]
            tk.Label(
                row, text=status, bg=COLORS["sidebar_bg"], fg=color, font=UI_FONT_BOLD,
            ).pack(side="right")


class RunDebugPanel(_SidebarView):
    """Visual-only Run and Debug landing view with placeholder actions."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, "Run and Debug")
        content = tk.Frame(self, bg=COLORS["sidebar_bg"])
        content.pack(fill="both", expand=True, padx=14, pady=(18, 12))

        opening = tk.Frame(content, bg=COLORS["sidebar_bg"])
        opening.pack(fill="x", pady=(0, 14))
        tk.Label(
            opening, text="Open a file", bg=COLORS["sidebar_bg"], fg=COLORS["link"],
            font=UI_FONT, cursor="hand2", anchor="w",
        ).pack(anchor="w")
        tk.Label(
            opening, text="which can be debugged or run.", bg=COLORS["sidebar_bg"],
            fg=COLORS["text_fg"], font=UI_FONT, anchor="w",
        ).pack(anchor="w")

        self._accent_button(content, "Run and Debug").pack(fill="x", pady=(0, 18))

        self._linked_sentence(
            content, "To customize Run and Debug ", "create a launch.json file.",
        ).pack(fill="x", pady=(0, 16))
        debug_links = tk.Frame(content, bg=COLORS["sidebar_bg"])
        debug_links.pack(fill="x", pady=(0, 16))
        tk.Label(
            debug_links, text="Debug using a", bg=COLORS["sidebar_bg"],
            fg=COLORS["text_fg"], font=SMALL_FONT, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            debug_links, text="terminal command", bg=COLORS["sidebar_bg"],
            fg=COLORS["link"], font=SMALL_FONT, anchor="w", cursor="hand2",
        ).pack(anchor="w")
        tk.Label(
            debug_links, text="or in an", bg=COLORS["sidebar_bg"],
            fg=COLORS["text_fg"], font=SMALL_FONT, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            debug_links, text="interactive chat.", bg=COLORS["sidebar_bg"],
            fg=COLORS["link"], font=SMALL_FONT, anchor="w", cursor="hand2",
        ).pack(anchor="w")

        self._accent_button(content, "Show automatic Python configurations").pack(fill="x")

    @staticmethod
    def _accent_button(parent: tk.Misc, text: str) -> customtkinter.CTkButton:
        return customtkinter.CTkButton(
            parent, text=text, height=34, corner_radius=6,
            fg_color=COLORS["accent"], hover_color=COLORS["menu_hover"],
            text_color="#ffffff", font=UI_FONT,
        )

    @staticmethod
    def _linked_sentence(parent: tk.Misc, prefix: str, link: str, suffix: str = "") -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["sidebar_bg"])
        tk.Label(
            row, text=prefix, bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
            font=SMALL_FONT, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            row, text=link, bg=COLORS["sidebar_bg"], fg=COLORS["link"],
            font=SMALL_FONT, anchor="w", cursor="hand2",
        ).pack(anchor="w")
        if suffix:
            tk.Label(
                row, text=suffix, bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
                font=SMALL_FONT, anchor="w",
            ).pack(anchor="w")
        return row


class ExtensionsPanel(_SidebarView):
    """Visual-only extension marketplace populated with mock cards."""

    MOCK_EXTENSIONS = (
        ("🐍", "Python", "Python language support and tools"),
        ("P", "Pylance", "Fast, feature-rich Python support"),
        ("G", "GitLens", "Supercharge Git inside the editor"),
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, "Extensions")
        search = _PlaceholderEntry(self, "Search Extensions in Marketplace")
        search.pack(fill="x", padx=10, pady=(0, 10), ipady=6)

        for icon_text, name, description in self.MOCK_EXTENSIONS:
            card = tk.Frame(self, bg=COLORS["sidebar_bg"], cursor="hand2")
            card.pack(fill="x", padx=7, pady=3)
            icon = tk.Label(
                card, text=icon_text, width=3, height=2, bg=COLORS["extension_icon_bg"],
                fg="#ffffff", font=(UI_FONT[0], 18, "bold"),
            )
            icon.pack(side="left", padx=(3, 8), pady=6)

            install = tk.Button(
                card, text="Install", bg=COLORS["accent"], fg="#ffffff",
                activebackground=COLORS["menu_hover"], activeforeground="#ffffff",
                relief="flat", bd=0, font=SMALL_FONT, padx=8, pady=2, cursor="hand2",
            )
            install.pack(side="right", padx=(4, 6))

            details = tk.Frame(card, bg=COLORS["sidebar_bg"])
            details.pack(side="left", fill="both", expand=True, pady=5)
            tk.Label(
                details, text=name, bg=COLORS["sidebar_bg"], fg=COLORS["text_fg"],
                font=UI_FONT_BOLD, anchor="w",
            ).pack(fill="x")
            tk.Label(
                details, text=description, bg=COLORS["sidebar_bg"], fg=COLORS["tab_inactive_fg"],
                font=SMALL_FONT, anchor="w", justify="left", wraplength=125,
            ).pack(fill="x")


# ============================================================================
# The code editor: a line-number gutter + a syntax-highlighted text buffer,
# scroll-synced together. Implements the get/insert/delete/bind surface
# that editor_core.TextWidgetLike expects, so EditorWorkspace can use a
# factory that returns one of these exactly like it would a plain
# tkinter.Text or a CTkTextbox.
# ============================================================================
class CodeEditor(tk.Frame):
    """VS Code-style editor pane: line numbers + syntax-highlighted text."""

    def __init__(
        self,
        parent: tk.Misc,
        font_family: str,
        font_size: int = CODE_FONT_SIZE,
        gutter_font_size: int = CODE_GUTTER_FONT_SIZE,
    ) -> None:
        super().__init__(parent, bg=COLORS["bg"])
        self._font = (font_family, font_size)
        self._gutter_font = (font_family, gutter_font_size)
        measuring_font = tkfont.Font(family=font_family, size=font_size)

        # spacing1/spacing3 (pixels above/below each line) approximate a
        # CSS line-height of ~1.5 - tkinter.Text has no literal line-height
        # control. Must be identical, in pixels, on both Text widgets
        # below or the gutter's line numbers drift out of vertical
        # alignment with the code as the file grows.
        self.linenumbers = tk.Text(
            self,
            width=4,
            padx=6,
            pady=4,
            bg=COLORS["gutter_bg"],
            fg=COLORS["gutter_fg"],
            bd=0,
            highlightthickness=0,
            font=self._gutter_font,
            spacing1=CODE_LINE_SPACING,
            spacing3=CODE_LINE_SPACING,
            state="disabled",
            takefocus=0,
            cursor="arrow",
        )
        self.linenumbers.pack(side="left", fill="y")
        self.linenumbers.tag_configure("right", justify="right")
        self.linenumbers.tag_configure("current", foreground=COLORS["gutter_fg_active"])

        tk.Frame(self, bg=COLORS["border"], width=1).pack(side="left", fill="y")

        self.text = tk.Text(
            self,
            bg=COLORS["bg"],
            fg=COLORS["text_fg"],
            insertbackground="#ffffff",
            selectbackground=COLORS["selection_bg"],
            selectforeground="#ffffff",
            bd=0,
            highlightthickness=0,
            font=self._font,
            spacing1=CODE_LINE_SPACING,
            spacing3=CODE_LINE_SPACING,
            wrap="none",
            undo=True,
            padx=6,
            pady=4,
            tabs=(measuring_font.measure(" " * 4),),
        )
        self.text.pack(side="left", fill="both", expand=True)
        for tag in SYNTAX_TAGS:
            self.text.tag_configure(tag, foreground=COLORS[f"syntax_{tag}"])

        self.vscroll = ttk.Scrollbar(self, orient="vertical", command=self._on_scrollbar)
        self.vscroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=self._on_text_scroll)

        # Internal bindings are added first (before any external caller
        # calls .bind()), and CodeEditor.bind() always adds rather than
        # replaces, so both these and an app-level "notify_change" hook
        # can coexist on the same event - mirroring how CTkTextbox itself
        # delegates bind() with add=True.
        self.text.bind("<KeyRelease>", self._on_internal_change)
        self.text.bind("<ButtonRelease-1>", self._on_cursor_move)
        self.linenumbers.bind("<MouseWheel>", self._forward_scroll_to_text)
        self.linenumbers.bind("<Button-4>", self._forward_scroll_to_text)
        self.linenumbers.bind("<Button-5>", self._forward_scroll_to_text)

        self._update_line_numbers()

    # -- TextWidgetLike protocol (matches editor_core.TextWidgetLike) -----

    def get(self, start: str, end: Optional[str] = None) -> str:
        return self.text.get(start, end)

    def insert(self, index: str, text: str, tags=None) -> None:
        self.text.insert(index, text, tags)

    def delete(self, start: str, end: Optional[str] = None) -> None:
        self.text.delete(start, end)

    def bind(self, sequence: Optional[str] = None, command: Optional[Callable] = None, add=True):
        return self.text.bind(sequence, command, add=True)

    # -- Extras used only by the GUI layer (not part of the core protocol) -

    def focus_editor(self) -> None:
        self.text.focus_set()

    def refresh(self) -> None:
        """Recompute gutter + highlighting after a programmatic content
        change (e.g. ``EditorTab.set_content`` loading a file), which
        bypasses the <KeyRelease> binding used for interactive typing.
        """
        self._update_line_numbers()
        apply_python_syntax_highlighting(self.text)

    def highlight_current_line_number(self) -> None:
        """Public wrapper so gui_layout (e.g. "Go to Line") can refresh
        the gutter's current-line highlight after moving the cursor
        programmatically."""
        self._highlight_current_line_number()

    # -- Internal scroll-sync / gutter / highlight plumbing ------------------

    def _on_text_scroll(self, first: str, last: str) -> None:
        self.vscroll.set(first, last)
        self.linenumbers.yview_moveto(first)

    def _on_scrollbar(self, *args) -> None:
        self.text.yview(*args)
        self.linenumbers.yview(*args)

    def _forward_scroll_to_text(self, event: tk.Event) -> str:
        self.text.event_generate("<MouseWheel>", delta=getattr(event, "delta", 0))
        return "break"

    def _on_internal_change(self, _event=None) -> None:
        self._update_line_numbers()
        apply_python_syntax_highlighting(self.text)

    def _on_cursor_move(self, _event=None) -> None:
        self._highlight_current_line_number()

    def _update_line_numbers(self) -> None:
        line_count = int(self.text.index("end-1c").split(".")[0])
        numbers = "\n".join(str(i) for i in range(1, line_count + 1))
        self.linenumbers.configure(state="normal")
        self.linenumbers.delete("1.0", "end")
        self.linenumbers.insert("1.0", numbers)
        self.linenumbers.tag_add("right", "1.0", "end")
        self.linenumbers.configure(state="disabled")
        self._highlight_current_line_number()

    def _highlight_current_line_number(self) -> None:
        current_line = int(self.text.index("insert").split(".")[0])
        self.linenumbers.tag_remove("current", "1.0", "end")
        self.linenumbers.tag_add("current", f"{current_line}.0", f"{current_line}.end")


# ============================================================================
# A single flat editor tab: title + a close "x" that is invisible until the
# tab is hovered or active, matching VS Code's tab affordance.
# ============================================================================
class TabButton(tk.Frame):
    def __init__(self, parent: tk.Misc, title: str, on_select: Callable, on_close: Callable) -> None:
        super().__init__(parent, bg=COLORS["tab_inactive_bg"])
        self.on_select = on_select
        self.on_close = on_close
        self._active = False
        self._hover = False

        self.accent = tk.Frame(self, bg=COLORS["tab_inactive_bg"], height=2)
        self.accent.pack(side="top", fill="x")

        self.inner = tk.Frame(self, bg=COLORS["tab_inactive_bg"])
        self.inner.pack(side="top", fill="both", expand=True)

        self.title_label = tk.Label(
            self.inner, text=self._label_text(title), bg=COLORS["tab_inactive_bg"], fg=COLORS["tab_inactive_fg"],
            font=UI_FONT, padx=6, pady=6,
        )
        self.title_label.pack(side="left")

        self.close_label = tk.Label(
            self.inner, text="×", bg=COLORS["tab_inactive_bg"], fg=COLORS["tab_inactive_bg"],
            font=("Segoe UI", 13), padx=6, cursor="hand2",
        )
        self.close_label.pack(side="left")

        tk.Frame(self, bg=COLORS["border"], width=1).pack(side="right", fill="y")

        for widget in (self, self.inner, self.title_label):
            widget.bind("<Button-1>", lambda _e: self.on_select())
        self.close_label.bind("<Button-1>", lambda _e: self.on_close())

        for widget in (self, self.inner, self.title_label, self.close_label):
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

        self._restyle()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._restyle()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._restyle()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._restyle()

    def set_title(self, title: str) -> None:
        self.title_label.configure(text=self._label_text(title))

    @staticmethod
    def _label_text(title: str) -> str:
        """Prefix the tab's filename with a small file-type icon - a
        Python-specific glyph for ``.py`` files, a generic document icon
        otherwise. Strips a trailing modified-marker "*" (added by
        ``EditorTab.display_title()``) before checking the extension so
        the icon doesn't flicker between file-type and "unknown" as a
        tab's modified state toggles.
        """
        base = title[:-1] if title.endswith("*") else title
        icon = "\U0001F40D" if base.endswith(".py") else "\U0001F4C4"
        return f"{icon}  {title}"

    def _restyle(self) -> None:
        if self._active:
            bg, fg, accent, close_fg = (
                COLORS["tab_active_bg"], COLORS["tab_active_fg"], COLORS["accent"], COLORS["tab_active_fg"],
            )
        elif self._hover:
            bg, fg, accent, close_fg = (
                COLORS["hover_bg"], COLORS["tab_active_fg"], COLORS["tab_inactive_bg"], COLORS["tab_active_fg"],
            )
        else:
            bg, fg, accent = COLORS["tab_inactive_bg"], COLORS["tab_inactive_fg"], COLORS["tab_inactive_bg"]
            close_fg = bg  # Invisible until hovered/active.

        self.configure(bg=bg)
        self.inner.configure(bg=bg)
        self.accent.configure(bg=accent)
        self.title_label.configure(bg=bg, fg=fg)
        self.close_label.configure(bg=bg, fg=close_fg)


# ============================================================================
# Left sidebar: a recursive, lazily-expanding file explorer built on
# ttk.Treeview. Folders show a real disclosure arrow; expanding one loads
# its children on demand (a single "dummy" placeholder child is replaced
# with the real listing the first time it's opened), so opening a huge
# project directory doesn't eagerly walk the whole tree up front. Clicking
# a file opens it via the existing on_file_selected callback.
# ============================================================================
class Sidebar(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        on_open_folder: Callable,
        on_file_selected: Callable[[str], None],
        on_open_file: Optional[Callable] = None,
        width: int = 1,
    ) -> None:
        # `width` is deliberately tiny (not a real target width): this
        # frame is always grid()-managed with sticky="nsew" by a parent
        # that assigns it a proportional column weight (see
        # gui_layout.py's 20/55/25 body layout), and grid's minimum
        # column size includes each cell widget's *requested* size. A
        # large frozen width here (e.g. 250) would set a large minimum
        # floor for the column and skew the weighted 20% share - see
        # pack_propagate(False) below for why it's still frozen at all.
        super().__init__(parent, bg=COLORS["sidebar_bg"], width=width)
        # Freezes the frame's *requested* size so long filenames packed
        # inside can't balloon it and fight the grid weighting above;
        # the frame's actual *rendered* width still comes entirely from
        # the parent grid's column allocation, since geometry managers
        # (grid) resize a widget independently of its own propagate
        # setting when sticky covers both axes.
        self.pack_propagate(False)
        self.on_open_folder = on_open_folder
        self.on_file_selected = on_file_selected
        self.on_open_file = on_open_file
        self.root_dir: Optional[str] = None
        self._expanded = True
        self._item_paths: Dict[str, str] = {}
        self._item_is_dir: Dict[str, bool] = {}

        header = tk.Frame(self, bg=COLORS["sidebar_bg"], height=40)
        header.pack(fill="x", padx=8, pady=(8, 5))
        header.pack_propagate(False)
        tk.Label(
            header, text=letter_spaced("EXPLORER"), bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=HEADER_FONT, anchor="w",
        ).pack(side="left")

        # Header icon row (right-aligned). Open Folder and Open File stay
        # available even after a workspace is loaded; Refresh/Collapse All
        # are dimmed and inert only while no folder is open.
        header_icon_font = ("Segoe UI Symbol", 18)
        self.open_folder_icon = tk.Label(
            header, text="\U0001F4C2", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=header_icon_font, padx=3, pady=3, cursor="hand2",
        )
        self.open_folder_icon.pack(side="right", padx=2)
        self.open_folder_icon.bind("<Button-1>", lambda _e: self.on_open_folder())

        self.open_file_icon = tk.Label(
            header, text="\U0001F4C4", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=header_icon_font, padx=3, pady=3, cursor="hand2",
        )
        self.open_file_icon.pack(side="right", padx=2)
        if self.on_open_file:
            self.open_file_icon.bind("<Button-1>", lambda _e: self.on_open_file())

        self.collapse_icon = tk.Label(
            header, text="▤", bg=COLORS["sidebar_bg"], fg=COLORS["icon_disabled_fg"],
        )
        self.collapse_icon.configure(font=header_icon_font, padx=3, pady=3)
        self.collapse_icon.pack(side="right", padx=2)
        self.collapse_icon.bind("<Button-1>", self._on_collapse_all_click)

        self.refresh_icon = tk.Label(
            header, text="↻", bg=COLORS["sidebar_bg"], fg=COLORS["icon_disabled_fg"],
        )
        self.refresh_icon.configure(font=header_icon_font, padx=3, pady=3)
        self.refresh_icon.pack(side="right", padx=2)
        self.refresh_icon.bind("<Button-1>", self._on_refresh_click)

        for icon in (self.refresh_icon, self.collapse_icon):
            icon.bind("<Enter>", self._on_header_icon_enter)
            icon.bind("<Leave>", self._on_header_icon_leave)
        for icon in (self.open_folder_icon, self.open_file_icon):
            icon.bind("<Enter>", lambda event: event.widget.configure(fg=COLORS["tab_active_fg"]))
            icon.bind("<Leave>", lambda event: event.widget.configure(fg=COLORS["sidebar_header_fg"]))

        # Bold, clickable project-folder row - click the chevron/name to
        # collapse or expand the tree below it, like VS Code's
        # workspace-folder header in the Explorer panel.
        folder_row = tk.Frame(self, bg=COLORS["sidebar_bg"], cursor="hand2")
        folder_row.pack(fill="x", padx=6, pady=(6, 4))

        self.chevron_label = tk.Label(
            folder_row, text="▾", bg=COLORS["sidebar_bg"], fg=COLORS["tab_inactive_fg"], font=UI_FONT_BOLD,
        )
        self.chevron_label.pack(side="left", padx=(4, 2))

        self.folder_label = tk.Label(
            folder_row, text="NO FOLDER OPENED", bg=COLORS["sidebar_bg"], fg=COLORS["tab_active_fg"],
            font=UI_FONT_BOLD, anchor="w",
        )
        self.folder_label.pack(side="left", fill="x")

        for widget in (folder_row, self.chevron_label, self.folder_label):
            widget.bind("<Button-1>", lambda _e: self._toggle_expanded())

        # Shown only while no folder is open - VS Code's empty-Explorer
        # affordance. Hidden the moment a real folder is opened.
        self.no_folder_frame = tk.Frame(self, bg=COLORS["sidebar_bg"])
        self.no_folder_frame.pack(fill="x", padx=10, pady=(4, 10))
        self.open_folder_button = self._make_flat_button(
            self.no_folder_frame, "Open Folder", lambda: self.on_open_folder(),
        )
        self.open_folder_button.pack(fill="x", pady=(0, 4))
        self.open_file_button = self._make_flat_button(
            self.no_folder_frame, "Open File",
            (lambda: self.on_open_file()) if self.on_open_file else None,
        )
        self.open_file_button.pack(fill="x")

        # -- Recursive tree ---------------------------------------------
        self._tree_container = tk.Frame(self, bg=COLORS["sidebar_bg"])
        self._tree_container.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            self._tree_container, show="tree", selectmode="browse", style="Explorer.Treeview",
        )
        tree_scroll = ttk.Scrollbar(self._tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.tree.tag_configure("pyfile", foreground="#4B8BBE")  # Python-blue, like a language-icon accent.
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_close)
        # Deliberately bound to a real mouse click, not <<TreeviewSelect>>:
        # that virtual event also fires for keyboard navigation and
        # programmatic selection_set() calls, which would otherwise
        # spuriously toggle a folder's expand state just from arrowing
        # past it. <Button-1> only fires for an actual click.
        self.tree.bind("<Button-1>", self._on_tree_click)

        self._update_header_icon_states()  # Start dimmed: no folder is open yet.

    def _make_flat_button(self, parent: tk.Misc, text: str, command: Optional[Callable]) -> tk.Button:
        """A small flat VS Code-style sidebar button (dark gray, hover highlight)."""
        return tk.Button(
            parent, text=text, command=command, bg=COLORS["topbar_bg"], fg=COLORS["tab_active_fg"],
            activebackground=COLORS["hover_bg"], activeforeground=COLORS["tab_active_fg"],
            disabledforeground=COLORS["icon_disabled_fg"], relief="flat", bd=0, anchor="w",
            font=UI_FONT, padx=10, pady=6, cursor="hand2",
        )

    # -- Header icon row: Refresh / Collapse All -----------------------------

    def _on_header_icon_enter(self, event: tk.Event) -> None:
        if self.root_dir is not None:
            event.widget.configure(fg=COLORS["tab_active_fg"])

    def _on_header_icon_leave(self, event: tk.Event) -> None:
        if self.root_dir is not None:
            event.widget.configure(fg=COLORS["sidebar_header_fg"])

    def _on_refresh_click(self, _event: Optional[tk.Event] = None) -> None:
        """Re-read the opened folder from disk (rebuilding the whole tree,
        so any expand-state is reset). No-op if no folder is open."""
        if self.root_dir is not None:
            self.show_directory(self.root_dir, is_root=True)

    def _on_collapse_all_click(self, _event: Optional[tk.Event] = None) -> None:
        """Collapse every expanded node back to its closed state, without
        re-reading anything from disk. No-op if no folder is open."""
        if self.root_dir is not None:
            self._collapse_all_items()

    def _update_header_icon_states(self) -> None:
        enabled = self.root_dir is not None
        fg = COLORS["sidebar_header_fg"] if enabled else COLORS["icon_disabled_fg"]
        cursor = "hand2" if enabled else "arrow"
        for icon in (self.refresh_icon, self.collapse_icon):
            icon.configure(fg=fg, cursor=cursor)

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self.chevron_label.configure(text="▾")
            self._tree_container.pack(fill="both", expand=True)
        else:
            self.chevron_label.configure(text="▸")
            self._tree_container.pack_forget()

    # -- Recursive tree population -----------------------------------------

    def show_directory(self, path: str, is_root: bool = False) -> None:
        """Open (or re-open) ``path`` as the Explorer's root folder.

        Clears and rebuilds the whole tree - any previous folder's
        contents are fully replaced, matching VS Code's "Open Folder"
        behavior (never merges two folders' listings together).
        """
        if is_root:
            self.root_dir = path
        self.no_folder_frame.pack_forget()
        self._update_header_icon_states()

        display_name = os.path.basename(path.rstrip("/\\")) or path
        self.folder_label.configure(text=display_name.upper())

        self.tree.delete(*self.tree.get_children(""))
        self._item_paths.clear()
        self._item_is_dir.clear()
        self._insert_entries("", path)

    def _insert_entries(self, parent_item: str, dir_path: str) -> None:
        try:
            names = sorted(
                (n for n in os.listdir(dir_path) if not is_ignored_entry(n)),
                key=lambda n: (not os.path.isdir(os.path.join(dir_path, n)), n.lower()),
            )
        except OSError:
            names = []

        for name in names:
            full_path = os.path.join(dir_path, name)
            is_dir = os.path.isdir(full_path)
            icon = self._FOLDER_CLOSED_ICON if is_dir else "\U0001F4C4"
            tags = ("pyfile",) if (not is_dir and name.endswith(".py")) else ()
            item_id = self.tree.insert(parent_item, "end", text=f" {icon}  {name}", open=False, tags=tags)
            self._item_paths[item_id] = full_path
            self._item_is_dir[item_id] = is_dir
            if is_dir:
                # Lazy-load placeholder so the expand arrow appears
                # without eagerly recursing the whole tree up front.
                self.tree.insert(item_id, "end", text="", tags=("dummy",))

    # Distinct glyphs for a folder's closed vs. expanded state, matching
    # how a real file explorer swaps its folder icon on expand/collapse.
    _FOLDER_CLOSED_ICON = "\U0001F4C1"
    _FOLDER_OPEN_ICON = "\U0001F4C2"

    def _set_folder_icon(self, item: str, opened: bool) -> None:
        if not self._item_is_dir.get(item, False):
            return
        full_path = self._item_paths.get(item)
        if full_path is None:
            return
        name = os.path.basename(full_path.rstrip("/\\"))
        icon = self._FOLDER_OPEN_ICON if opened else self._FOLDER_CLOSED_ICON
        self.tree.item(item, text=f" {icon}  {name}")

    def _on_tree_open(self, _event=None) -> None:
        item = self.tree.focus()
        self._expand_item(item)
        self._set_folder_icon(item, opened=True)

    def _on_tree_close(self, _event=None) -> None:
        item = self.tree.focus()
        self._set_folder_icon(item, opened=False)

    def _expand_item(self, item: str) -> None:
        children = self.tree.get_children(item)
        if len(children) == 1 and "dummy" in self.tree.item(children[0], "tags"):
            self.tree.delete(children[0])
            self._insert_entries(item, self._item_paths[item])

    def _on_tree_click(self, event: tk.Event) -> None:
        """Handle a real mouse click on the tree: toggle a folder when
        its label (not the disclosure arrow) is clicked, or open a file.

        Clicking the disclosure arrow is left entirely to ttk's own
        native handling (which already toggles the row and fires
        <<TreeviewOpen>>/<<TreeviewClose>> - see _on_tree_open/
        _on_tree_close) - if this handler *also* toggled on an arrow
        click, the two would cancel each other out.
        """
        element = self.tree.identify_element(event.x, event.y)
        if "indicator" in element:
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        path = self._item_paths.get(item)
        if path is None:
            return
        if self._item_is_dir.get(item, False):
            is_open = bool(self.tree.item(item, "open"))
            new_open = not is_open
            self.tree.item(item, open=new_open)
            if new_open:
                # Don't rely on <<TreeviewOpen>> also firing for this
                # programmatic .item() call (that virtual event is only
                # guaranteed for a genuine arrow click, handled
                # separately by _on_tree_open) - lazy-load explicitly so
                # a label click expands the folder just as reliably as
                # clicking its arrow does.
                self._expand_item(item)
            self._set_folder_icon(item, opened=new_open)
        else:
            self.on_file_selected(path)

    def _collapse_all_items(self, item: str = "") -> None:
        for child in self.tree.get_children(item):
            self.tree.item(child, open=False)
            self._set_folder_icon(child, opened=False)
            self._collapse_all_items(child)


# ============================================================================
# Thin editor breadcrumb strip; navigation is visual-only.
# ============================================================================
class BreadcrumbBar(tk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=COLORS["bg"], height=24)
        self.pack_propagate(False)
        self._segments = tk.Frame(self, bg=COLORS["bg"])
        self._segments.pack(side="left", fill="y", padx=8)

    def set_path(self, path: Optional[str], fallback_title: str = "") -> None:
        for child in self._segments.winfo_children():
            child.destroy()
        parts = self._path_parts(path, fallback_title)
        if not parts:
            self.pack_forget()
            return
        for index, part in enumerate(parts):
            if index:
                tk.Label(
                    self._segments, text="›", bg=COLORS["bg"], fg=COLORS["gutter_fg"],
                    font=SMALL_FONT, padx=3,
                ).pack(side="left", fill="y")
            label = tk.Label(
                self._segments, text=part, bg=COLORS["bg"],
                fg=COLORS["breadcrumb_active_fg"] if index == len(parts) - 1 else COLORS["breadcrumb_fg"],
                font=SMALL_FONT, padx=3, cursor="hand2",
            )
            label.pack(side="left", fill="y")
            label.bind("<Enter>", lambda event: event.widget.configure(bg=COLORS["breadcrumb_hover_bg"]))
            label.bind("<Leave>", lambda event: event.widget.configure(bg=COLORS["bg"]))
    @staticmethod
    def _path_parts(path: Optional[str], fallback_title: str) -> List[str]:
        if not path:
            return [fallback_title] if fallback_title else []
        normalized = os.path.normpath(path)
        pieces = [piece for piece in normalized.replace("\\", "/").split("/") if piece]
        for index, piece in enumerate(pieces):
            if piece.lower() == "nexcore ide":
                return [pieces[index].upper(), *pieces[index + 1:]]
        # Keep the strip compact: project/folder/file is more useful than
        # showing an entire absolute Windows path.
        return pieces[-3:] if len(pieces) > 3 else pieces

    def hide(self) -> None:
        self.pack_forget()


# ============================================================================
# Bottom panel: a collapsible, VS Code integrated-terminal-style output
# console with separate coloring for stdout / stderr / informational lines.
# ============================================================================
class ConsolePanel(tk.Frame):
    def __init__(self, parent: tk.Misc, font: tuple) -> None:
        super().__init__(parent, bg=COLORS["bg"])
        self._visible = True

        header = tk.Frame(self, bg=COLORS["sidebar_bg"], height=30)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self.toggle_label = tk.Label(
            header, text="▾", bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=HEADER_FONT, cursor="hand2", padx=6,
        )
        self.toggle_label.pack(side="left", padx=(5, 0))
        self.toggle_label.bind("<Button-1>", lambda _e: self.toggle())

        self.problems_tab = tk.Label(
            header, text=f"{letter_spaced('PROBLEMS')}  2 ⚠  1 ✕",
            bg=COLORS["sidebar_bg"], fg=COLORS["tab_inactive_fg"],
            font=HEADER_FONT, cursor="hand2", padx=8,
        )
        self.problems_tab.pack(side="left")
        self.problems_tab.bind("<Button-1>", lambda _e: self._select_tab("problems"))

        self.output_tab = tk.Label(
            header, text=letter_spaced("OUTPUT"), bg=COLORS["sidebar_bg"],
            fg=COLORS["text_fg"], font=HEADER_FONT, cursor="hand2", padx=8,
        )
        self.output_tab.pack(side="left")
        self.output_tab.bind("<Button-1>", lambda _e: self._select_tab("output"))

        clear_label = tk.Label(
            header, text="Clear", bg=COLORS["sidebar_bg"], fg=COLORS["tab_inactive_fg"],
            font=UI_FONT, cursor="hand2",
        )
        clear_label.pack(side="right", padx=10)
        clear_label.bind("<Button-1>", lambda _e: self.clear())

        self.body = tk.Frame(self, bg=COLORS["bg"])
        self.body.pack(fill="both", side="top")

        self.output_frame = tk.Frame(self.body, bg=COLORS["console_bg"])
        self.output_frame.pack(fill="both", expand=True)
        self.text = tk.Text(
            self.output_frame, bg=COLORS["console_bg"], fg=COLORS["console_stdout"], bd=0, highlightthickness=0,
            font=font, height=10, wrap="word", state="disabled",
        )
        self.text.pack(fill="both", expand=True, padx=8, pady=4)
        self.text.tag_configure("stdout", foreground=COLORS["console_stdout"])
        self.text.tag_configure("stderr", foreground=COLORS["console_stderr"])
        self.text.tag_configure("info", foreground=COLORS["console_info"])
        self.problems_frame = tk.Frame(self.body, bg=COLORS["console_bg"])
        mock_problems = (
            ("⚠", "constants.py:42", "unused import 'os'", COLORS["problem_warning"]),
            ("⚠", "gui_layout.py:76", "mock layout warning", COLORS["problem_warning"]),
            ("✕", "editor_core.py:118", "undefined variable 'tmp'", COLORS["problem_error"]),
        )
        for icon, location, message, color in mock_problems:
            row = tk.Frame(self.problems_frame, bg=COLORS["console_bg"])
            row.pack(fill="x", padx=12, pady=4)
            tk.Label(
                row, text=icon, bg=COLORS["console_bg"], fg=color,
                font=UI_FONT_BOLD, width=2,
            ).pack(side="left")
            tk.Label(
                row, text=location, bg=COLORS["console_bg"], fg=COLORS["text_fg"],
                font=UI_FONT_BOLD,
            ).pack(side="left", padx=(4, 8))
            tk.Label(
                row, text=f"— {message}", bg=COLORS["console_bg"],
                fg=COLORS["tab_inactive_fg"], font=UI_FONT,
            ).pack(side="left")
        self._active_tab = "output"

    @staticmethod
    def _header_text(expanded: bool) -> str:
        chevron = "▾" if expanded else "▸"
        return f"{chevron}  {letter_spaced('OUTPUT')}"

    def toggle(self) -> None:
        self._visible = not self._visible
        self.toggle_label.configure(text="▾" if self._visible else "▸")
        if self._visible:
            self.body.pack(fill="both", side="top")
        else:
            self.body.pack_forget()

    def _select_tab(self, name: str) -> None:
        self._active_tab = name
        if not self._visible:
            self.toggle()
        if name == "problems":
            self.output_frame.pack_forget()
            self.problems_frame.pack(fill="both", expand=True)
        else:
            self.problems_frame.pack_forget()
            self.output_frame.pack(fill="both", expand=True)
        self.problems_tab.configure(
            fg=COLORS["text_fg"] if name == "problems" else COLORS["tab_inactive_fg"],
        )
        self.output_tab.configure(
            fg=COLORS["text_fg"] if name == "output" else COLORS["tab_inactive_fg"],
        )

    def append(self, text: str, kind: str = "stdout") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text, kind)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# ============================================================================
# Bottom-of-window VS Code-style blue status bar.
# ============================================================================
class StatusBar(tk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=COLORS["statusbar_bg"], height=24)
        self.pack_propagate(False)

        # Icons live in their own small labels, separate from the text
        # labels that set_file()/set_status() update, so those two
        # methods' behavior/signature stays exactly as before.
        file_section = tk.Frame(self, bg=COLORS["statusbar_bg"])
        file_section.pack(side="left", padx=10)
        tk.Label(
            file_section, text="\U0001F4C4", bg=COLORS["statusbar_bg"], fg=COLORS["statusbar_fg"],
            font=(SMALL_FONT[0], 10),
        ).pack(side="left", padx=(0, 5))
        self.file_label = tk.Label(
            file_section, text="No file open", bg=COLORS["statusbar_bg"], fg=COLORS["statusbar_fg"],
            font=SMALL_FONT, anchor="w",
        )
        self.file_label.pack(side="left")

        self.problem_count = tk.Label(
            file_section, text="  2 ⚠   1 ✕", bg=COLORS["statusbar_bg"],
            fg=COLORS["statusbar_fg"], font=SMALL_FONT,
        )
        self.problem_count.pack(side="left", padx=(10, 0))

        status_section = tk.Frame(self, bg=COLORS["statusbar_bg"])
        status_section.pack(side="right", padx=10)
        self.status_label = tk.Label(
            status_section, text="", bg=COLORS["statusbar_bg"], fg=COLORS["statusbar_fg"], font=SMALL_FONT,
            anchor="e",
        )
        self.status_label.pack(side="right")
        tk.Label(
            status_section, text="⚙", bg=COLORS["statusbar_bg"], fg=COLORS["statusbar_fg"],
            font=(SMALL_FONT[0], 10),
        ).pack(side="right", padx=(0, 5))

        metadata = tk.Frame(self, bg=COLORS["statusbar_bg"])
        metadata.pack(side="right", padx=8)
        self.position_label = self._metadata_label(metadata, "Ln 1, Col 1")
        self.language_label = self._metadata_label(metadata, "Plain Text")
        self.encoding_label = self._metadata_label(metadata, "UTF-8")
        self.line_ending_label = self._metadata_label(metadata, "LF")

    @staticmethod
    def _metadata_label(parent: tk.Misc, text: str) -> tk.Label:
        label = tk.Label(
            parent, text=text, bg=COLORS["statusbar_bg"], fg=COLORS["statusbar_fg"],
            font=SMALL_FONT, padx=7,
        )
        label.pack(side="left")
        return label

    def set_file(self, text: str) -> None:
        self.file_label.configure(text=text)

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def set_editor_info(self, position: str, language: str) -> None:
        self.position_label.configure(text=position)
        self.language_label.configure(text=language)


# ============================================================================
# The Welcome page shown whenever zero tabs are open (launch, or after
# closing the last tab). Left-aligned, top-aligned, two-column layout -
# not a centered splash - matching VS Code's own Welcome tab.
# ============================================================================
class WelcomeScreen(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        editor_font_family: str,
        on_new_file: Callable,
        on_open_file: Callable,
        on_open_folder: Callable,
        on_open_recent: Callable[[str, str], None],
        on_show_about: Callable,
        on_not_implemented: Callable[[str], None],
        get_recent_items: Callable[[], List[Tuple[str, str, str]]],
    ) -> None:
        super().__init__(parent, bg=COLORS["bg"])
        self.editor_font_family = editor_font_family
        self.on_new_file = on_new_file
        self.on_open_file = on_open_file
        self.on_open_folder = on_open_folder
        self.on_open_recent = on_open_recent
        self.on_show_about = on_show_about
        self.on_not_implemented = on_not_implemented
        self.get_recent_items = get_recent_items
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the page content - called every time it's shown so the
        "Recent" list reflects whatever's been opened so far this session.
        """
        for child in self.winfo_children():
            child.destroy()
        self._build_content()

    def _build_content(self) -> None:
        content = tk.Frame(self, bg=COLORS["bg"])
        content.pack(side="top", anchor="nw", padx=(180, 40), pady=(56, 20))

        left = tk.Frame(content, bg=COLORS["bg"])
        left.pack(side="left", anchor="n")
        right = tk.Frame(content, bg=COLORS["bg"])
        right.pack(side="left", anchor="n", padx=(64, 0))

        # -- Left column: title, tagline, Start actions, Recent ----------
        tk.Label(
            left, text="NexCore IDE", bg=COLORS["bg"], fg="#f3f3f3",
            font=(self.editor_font_family, WELCOME_TITLE_FONT_SIZE, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            left, text="Pakistan's First AI-Assisted Code Editor", bg=COLORS["bg"],
            fg=COLORS["welcome_muted"], font=(UI_FONT[0], WELCOME_TAGLINE_FONT_SIZE), anchor="w",
        ).pack(anchor="w", pady=(6, 28))

        tk.Label(
            left, text="START", bg=COLORS["bg"], fg=COLORS["welcome_heading"], font=UI_FONT_BOLD, anchor="w",
        ).pack(anchor="w", pady=(0, 8))
        self._make_action_link(left, "\U0001F4C4", "New File...", self.on_new_file)
        self._make_action_link(left, "\U0001F4C2", "Open File...", self.on_open_file)
        self._make_action_link(left, "\U0001F4C1", "Open Folder...", self.on_open_folder)

        tk.Label(
            left, text="RECENT", bg=COLORS["bg"], fg=COLORS["welcome_heading"], font=UI_FONT_BOLD, anchor="w",
        ).pack(anchor="w", pady=(28, 8))
        recent_items = self.get_recent_items()
        if recent_items:
            for display_name, full_path, kind in recent_items[:3]:
                self._make_recent_row(left, display_name, full_path, kind)
        else:
            tk.Label(
                left, text="No recent folders", bg=COLORS["bg"], fg=COLORS["welcome_muted"],
                font=UI_FONT, anchor="w",
            ).pack(anchor="w")

        # -- Right column: Get Started walkthrough cards ------------------
        tk.Label(
            right, text="GET STARTED", bg=COLORS["bg"], fg=COLORS["welcome_heading"], font=UI_FONT_BOLD, anchor="w",
        ).pack(anchor="w", pady=(0, 10))
        self._make_walkthrough_card(
            right, "Learn the Basics",
            "Familiarize yourself with the editor, tabs, and terminal.",
            lambda: self.on_not_implemented("Learn the Basics"),
        )
        self._make_walkthrough_card(
            right, "Python Setup Guide",
            "Configure your Python interpreter and run scripts.",
            lambda: self.on_not_implemented("Python Setup Guide"),
        )
        self._make_walkthrough_card(
            right, "About NexCore IDE",
            "See what's powering this editor under the hood.",
            self.on_show_about,
        )

    def _make_action_link(self, parent: tk.Misc, icon: str, text: str, command: Callable) -> None:
        """One "Start" row: a small icon + a blue clickable link, VS Code style."""
        link_font = (UI_FONT[0], WELCOME_LINK_FONT_SIZE)
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(anchor="w", pady=3)

        tk.Label(row, text=icon, bg=COLORS["bg"], fg=COLORS["welcome_muted"], font=link_font).pack(
            side="left", padx=(0, 8)
        )
        link = tk.Label(row, text=text, bg=COLORS["bg"], fg=COLORS["link"], font=link_font, cursor="hand2")
        link.pack(side="left")

        def on_enter(_e=None):
            link.configure(fg=COLORS["link_hover"], font=(link_font[0], link_font[1], "underline"))

        def on_leave(_e=None):
            link.configure(fg=COLORS["link"], font=link_font)

        link.bind("<Button-1>", lambda _e: command())
        link.bind("<Enter>", on_enter)
        link.bind("<Leave>", on_leave)

    def _make_recent_row(self, parent: tk.Misc, display_name: str, full_path: str, kind: str) -> None:
        """One "Recent" row: name in link-blue, full path dimmed beside it."""
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(anchor="w", pady=2, fill="x")

        icon = "\U0001F4C1" if kind == "folder" else "\U0001F4C4"
        tk.Label(row, text=icon, bg=COLORS["bg"], fg=COLORS["welcome_muted"], font=UI_FONT).pack(
            side="left", padx=(0, 8)
        )
        name_label = tk.Label(row, text=display_name, bg=COLORS["bg"], fg=COLORS["link"], font=UI_FONT,
                               cursor="hand2")
        name_label.pack(side="left")
        path_label = tk.Label(
            row, text=f"   {full_path}", bg=COLORS["bg"], fg=COLORS["welcome_muted"], font=("Segoe UI", 9),
            cursor="hand2",
        )
        path_label.pack(side="left")

        def on_click(_e=None):
            self.on_open_recent(full_path, kind)

        def on_enter(_e=None):
            name_label.configure(fg=COLORS["link_hover"])

        def on_leave(_e=None):
            name_label.configure(fg=COLORS["link"])

        for widget in (row, name_label, path_label):
            widget.bind("<Button-1>", on_click)
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)

    def _make_walkthrough_card(self, parent: tk.Misc, title: str, subtitle: str, command: Callable) -> None:
        """One flat "Get Started" card - dark gray, hover highlight, click to act."""
        card = tk.Frame(parent, bg=COLORS["tab_inactive_bg"], width=340, height=72, cursor="hand2")
        card.pack(anchor="w", pady=(0, 10))
        card.pack_propagate(False)

        inner = tk.Frame(card, bg=COLORS["tab_inactive_bg"])
        inner.pack(fill="both", expand=True, padx=14, pady=10)

        title_label = tk.Label(
            inner, text=title, bg=COLORS["tab_inactive_bg"], fg="#f3f3f3", font=UI_FONT_BOLD, anchor="w",
        )
        title_label.pack(anchor="w")
        subtitle_label = tk.Label(
            inner, text=subtitle, bg=COLORS["tab_inactive_bg"], fg=COLORS["welcome_muted"], font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=305,
        )
        subtitle_label.pack(anchor="w", pady=(2, 0))

        widgets = (card, inner, title_label, subtitle_label)

        def on_enter(_e=None):
            for widget in widgets:
                widget.configure(bg=COLORS["card_hover_bg"])

        def on_leave(_e=None):
            for widget in widgets:
                widget.configure(bg=COLORS["tab_inactive_bg"])

        for widget in widgets:
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            widget.bind("<Button-1>", lambda _e: command())


# ============================================================================
# Right column: "AI Assistant" chat panel wired to the real Anthropic API.
# Network calls run on a background thread (mirroring how ExecutionEngine
# streams subprocess output) so the UI never freezes while waiting on a
# response; a queue + periodic after() poll is the only bridge back to
# Tkinter, which may only be touched from the main thread.
#
# The chat log is a scrollable Canvas of individually-drawn message
# "bubbles" (rounded rectangles with an avatar + sender label above each)
# rather than a single Text widget - Tkinter has no native border-radius,
# so genuine rounded corners require drawing them on a Canvas. The input
# box uses the same rounded-rectangle technique for its border.
# ============================================================================
class AIPanel(tk.Frame):
    MODEL = "claude-opus-4-8"
    MAX_CONTEXT_CHARS = 8000  # Defensive cap on injected file content.
    SYSTEM_PROMPT = (
        "You are the AI Assistant built into NexCore IDE, a desktop Python "
        "code editor. Answer the user's question about their code clearly "
        "and concisely. When the currently open file's content is provided "
        "as context, ground your answer in it rather than guessing."
    )
    PLACEHOLDER_TEXT = "Ask NexCore AI..."
    BUBBLE_MAX_WIDTH = 250

    _AVATAR_BG = {"user": COLORS["ai_user_label"], "assistant": COLORS["ai_assistant_label"],
                  "error": COLORS["ai_error_label"]}
    _AVATAR_GLYPH = {"user": "Y", "assistant": "✦", "error": "!"}
    _BUBBLE_BG = {"user": COLORS["ai_user_bubble"], "assistant": COLORS["ai_assistant_bubble"],
                  "error": COLORS["ai_error_bubble"]}
    _SENDER_NAME = {"user": "You", "assistant": "NexCore AI", "error": "NexCore AI"}

    def __init__(
        self,
        parent: tk.Misc,
        editor_font_family: str,
        get_context: Optional[Callable[[], Optional[Tuple[str, str]]]] = None,
    ) -> None:
        # Frozen requested size (same pattern as Sidebar in this file):
        # widgets packed inside later would otherwise inflate this
        # panel's *requested* size well past its fair grid-column share.
        # Freezing it here lets the parent's column weight (see
        # gui_layout.py's 20/55/25 body layout) actually govern the
        # rendered width - pack() below still lays out children at the
        # panel's real, grid-allocated size, not this frozen 1x1 request.
        super().__init__(parent, bg=COLORS["sidebar_bg"], width=1, height=1)
        self.pack_propagate(False)
        self.get_context = get_context
        self._history: List[dict] = []
        self._response_queue: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._placeholder_active = True
        self._input_focused = False
        self._typing_row: Optional[tk.Frame] = None
        self._typing_canvas: Optional[tk.Canvas] = None
        self._typing_dots_id: Optional[int] = None
        self._typing_after_id: Optional[str] = None

        self._build_header()
        self._build_chat_area()
        self._build_input_area()

        self._append_hint(
            "Ask about your open file, or a general Python question. "
            "Set the ANTHROPIC_API_KEY environment variable to enable this panel."
        )

        self.after(80, self._drain_response_queue)

    # -- Layout construction --------------------------------------------------

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=COLORS["sidebar_bg"])
        header.pack(fill="x", padx=10, pady=(12, 8))
        tk.Label(
            header, text="✦", bg=COLORS["sidebar_bg"], fg=COLORS["link"], font=(UI_FONT[0], 12),
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            header, text=letter_spaced("AI ASSISTANT"), bg=COLORS["sidebar_bg"], fg=COLORS["sidebar_header_fg"],
            font=HEADER_FONT,
        ).pack(side="left")
        tk.Frame(self, bg=COLORS["border"], height=1).pack(fill="x")

    def _build_chat_area(self) -> None:
        chat_outer = tk.Frame(self, bg=COLORS["bg"])
        chat_outer.pack(fill="both", expand=True)

        self._chat_canvas = tk.Canvas(chat_outer, bg=COLORS["bg"], highlightthickness=0)
        chat_scroll = ttk.Scrollbar(chat_outer, orient="vertical", command=self._chat_canvas.yview)
        self._chat_canvas.configure(yscrollcommand=chat_scroll.set)
        self._chat_canvas.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")

        self._chat_inner = tk.Frame(self._chat_canvas, bg=COLORS["bg"])
        self._chat_window = self._chat_canvas.create_window((0, 0), window=self._chat_inner, anchor="nw")

        self._chat_inner.bind("<Configure>", self._on_chat_inner_configure)
        self._chat_canvas.bind("<Configure>", self._on_chat_canvas_configure)
        self._chat_canvas.bind("<Enter>", lambda _e: self._chat_canvas.bind_all("<MouseWheel>", self._on_chat_scroll))
        self._chat_canvas.bind("<Leave>", lambda _e: self._chat_canvas.unbind_all("<MouseWheel>"))

    def _build_input_area(self) -> None:
        input_outer = tk.Frame(self, bg=COLORS["sidebar_bg"])
        input_outer.pack(fill="x", padx=10, pady=(6, 12))

        # A rounded "capsule" drawn on a Canvas, with the real Text widget
        # and the send-icon Canvas embedded inside it via create_window -
        # the only way to get genuine rounded corners around interactive
        # widgets in Tkinter.
        self._input_canvas = tk.Canvas(input_outer, bg=COLORS["sidebar_bg"], highlightthickness=0, height=72)
        self._input_canvas.pack(fill="x")
        self._input_rect_id: Optional[int] = None

        self.input_box = tk.Text(
            self._input_canvas, height=3, bg=COLORS["bg"], fg=COLORS["welcome_muted"], insertbackground="#ffffff",
            bd=0, highlightthickness=0, font=(UI_FONT[0], 11), wrap="word", padx=8, pady=6,
        )
        self.input_box.insert("1.0", self.PLACEHOLDER_TEXT)
        self.input_box.bind("<FocusIn>", self._on_input_focus_in)
        self.input_box.bind("<FocusOut>", self._on_input_focus_out)
        self.input_box.bind("<KeyRelease>", self._on_input_key_release)
        self.input_box.bind("<Return>", self._on_return_key)

        self.send_button = tk.Canvas(
            self._input_canvas, width=30, height=30, bg=COLORS["bg"], highlightthickness=0, cursor="arrow",
        )
        self._send_icon_id = self.send_button.create_text(
            15, 15, text="➤", font=(UI_FONT[0], 13), fill=COLORS["icon_disabled_fg"],
        )
        self.send_button.bind("<Button-1>", lambda _e: self.send_message())

        self._input_window_id = self._input_canvas.create_window(0, 0, anchor="nw", window=self.input_box)
        self._send_window_id = self._input_canvas.create_window(0, 0, anchor="nw", window=self.send_button)
        self._input_canvas.bind("<Configure>", self._redraw_input_shell)

        self._update_send_button_state()

    # -- Rounded-rectangle drawing helper (shared by bubbles + input box) ----

    @staticmethod
    def _draw_rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    # -- Chat scroll region plumbing ------------------------------------------

    def _on_chat_inner_configure(self, _event=None) -> None:
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))

    def _on_chat_canvas_configure(self, event: tk.Event) -> None:
        self._chat_canvas.itemconfig(self._chat_window, width=event.width)

    def _on_chat_scroll(self, event: tk.Event) -> None:
        self._chat_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _scroll_chat_to_bottom(self) -> None:
        self._chat_canvas.update_idletasks()
        self._chat_canvas.yview_moveto(1.0)

    # -- Input box: rounded shell, placeholder, focus highlight --------------

    def _redraw_input_shell(self, _event=None) -> None:
        canvas = self._input_canvas
        w = max(canvas.winfo_width(), 60)
        h = max(canvas.winfo_height(), 40)
        if self._input_rect_id is not None:
            canvas.delete(self._input_rect_id)
        border_color = COLORS["accent"] if self._input_focused else COLORS["border"]
        self._input_rect_id = self._draw_rounded_rect(
            canvas, 1, 1, w - 1, h - 1, 12, fill=COLORS["bg"], outline=border_color, width=1.5,
        )
        canvas.tag_lower(self._input_rect_id)
        send_w = 30
        canvas.coords(self._input_window_id, 8, 6)
        canvas.itemconfig(self._input_window_id, width=w - send_w - 20, height=h - 12)
        canvas.coords(self._send_window_id, w - send_w - 8, (h - 30) // 2)

    def _on_input_focus_in(self, _event=None) -> None:
        self._input_focused = True
        self._redraw_input_shell()
        if self._placeholder_active:
            self.input_box.delete("1.0", "end")
            self.input_box.configure(fg=COLORS["text_fg"])
            self._placeholder_active = False
        self._update_send_button_state()

    def _on_input_focus_out(self, _event=None) -> None:
        self._input_focused = False
        self._redraw_input_shell()
        if not self.input_box.get("1.0", "end-1c").strip():
            self._show_placeholder()

    def _show_placeholder(self) -> None:
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", self.PLACEHOLDER_TEXT)
        self.input_box.configure(fg=COLORS["welcome_muted"])
        self._placeholder_active = True
        self._update_send_button_state()

    def _on_input_key_release(self, _event=None) -> None:
        self._update_send_button_state()

    def _update_send_button_state(self) -> None:
        has_text = (not self._placeholder_active) and bool(self.input_box.get("1.0", "end-1c").strip())
        enabled = has_text and not self._busy
        self.send_button.itemconfig(
            self._send_icon_id, fill=COLORS["link"] if enabled else COLORS["icon_disabled_fg"],
        )
        self.send_button.configure(cursor="hand2" if enabled else "arrow")

    # -- Input handling -------------------------------------------------------

    def _on_return_key(self, event: tk.Event):
        if event.state & 0x0001:  # Shift held -> allow a literal newline.
            return None
        self.send_message()
        return "break"

    def send_message(self) -> None:
        if self._busy or self._placeholder_active:
            return
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            return
        self.input_box.delete("1.0", "end")
        self._update_send_button_state()
        self._add_bubble(text, "user")

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._add_bubble(
                "No ANTHROPIC_API_KEY environment variable is set, so I can't reach the "
                "Claude API. Set ANTHROPIC_API_KEY and try again.",
                "error",
            )
            return

        try:
            import anthropic  # noqa: F401 - import-checked here so the panel degrades gracefully.
        except ImportError:
            self._add_bubble(
                "The 'anthropic' Python package isn't installed. Run 'pip install anthropic' "
                "to enable the AI Assistant.",
                "error",
            )
            return

        user_content = self._build_user_content(text)
        self._history.append({"role": "user", "content": user_content})
        self._set_busy(True)

        thread = threading.Thread(target=self._call_api, args=(api_key,), daemon=True)
        thread.start()

    def _build_user_content(self, question: str) -> str:
        """Prefix the user's question with the currently open file's
        content, if the caller wired up a context source. Truncated
        defensively so a huge open file doesn't blow past a reasonable
        request size for a chat sidebar.
        """
        context = self.get_context() if self.get_context else None
        if not context:
            return question
        filename, file_content = context
        if not file_content:
            return question
        truncated = file_content[: self.MAX_CONTEXT_CHARS]
        note = "" if len(file_content) <= self.MAX_CONTEXT_CHARS else "\n...(truncated)"
        return f"Context - currently open file `{filename}`:\n```python\n{truncated}{note}\n```\n\n{question}"

    # -- Background API call (runs off the Tk main thread) -------------------

    def _call_api(self, api_key: str) -> None:
        import anthropic

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=self.MODEL,
                max_tokens=1024,
                system=self.SYSTEM_PROMPT,
                messages=list(self._history),
            )
            reply = "".join(block.text for block in response.content if block.type == "text")
            self._response_queue.put(("ok", reply))
        except anthropic.AuthenticationError:
            self._response_queue.put(("error", "Authentication failed - check that ANTHROPIC_API_KEY is valid."))
        except anthropic.RateLimitError:
            self._response_queue.put(("error", "Rate limited by the Anthropic API. Try again shortly."))
        except anthropic.APIConnectionError:
            self._response_queue.put(("error", "Could not reach the Anthropic API - check your network connection."))
        except anthropic.APIStatusError as exc:
            self._response_queue.put(("error", f"Anthropic API error ({exc.status_code}): {exc.message}"))
        except Exception as exc:  # noqa: BLE001 - last resort: never let a background thread crash the app.
            self._response_queue.put(("error", str(exc)))

    def _drain_response_queue(self) -> None:
        try:
            while True:
                kind, payload = self._response_queue.get_nowait()
                self._set_busy(False)
                if kind == "ok":
                    self._history.append({"role": "assistant", "content": payload})
                    self._add_bubble(payload or "(empty response)", "assistant")
                else:
                    # Drop the dangling user turn so a failed request
                    # doesn't leave an unanswered message at the end of
                    # history for the next request to build on.
                    if self._history and self._history[-1]["role"] == "user":
                        self._history.pop()
                    self._add_bubble(payload, "error")
        except queue.Empty:
            pass
        finally:
            self.after(80, self._drain_response_queue)

    # -- Busy state: send-button styling + animated typing indicator ---------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_send_button_state()
        if busy:
            self._show_typing_indicator()
        else:
            self._hide_typing_indicator()

    def _show_typing_indicator(self) -> None:
        if self._typing_row is not None:
            return
        row, canvas = self._start_bubble_row("assistant")
        canvas.configure(width=46, height=26)
        rect_id = self._draw_rounded_rect(
            canvas, 0, 0, 46, 26, 12, fill=COLORS["ai_assistant_bubble"], outline=COLORS["ai_assistant_bubble"],
        )
        self._typing_dots_id = canvas.create_text(23, 13, text=".", fill=COLORS["text_fg"], font=(UI_FONT[0], 12))
        canvas.tag_lower(rect_id, self._typing_dots_id)
        self._typing_row = row
        self._typing_canvas = canvas
        self._animate_typing_dots(0)
        self._scroll_chat_to_bottom()

    def _animate_typing_dots(self, frame: int) -> None:
        if self._typing_row is None or self._typing_canvas is None:
            return
        dots = "." * ((frame % 3) + 1)
        self._typing_canvas.itemconfig(self._typing_dots_id, text=dots)
        self._typing_after_id = self.after(400, lambda: self._animate_typing_dots(frame + 1))

    def _hide_typing_indicator(self) -> None:
        if self._typing_after_id is not None:
            self.after_cancel(self._typing_after_id)
            self._typing_after_id = None
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None
        self._typing_canvas = None
        self._typing_dots_id = None

    # -- Chat rendering: avatars + rounded bubbles ----------------------------

    def _start_bubble_row(self, kind: str):
        """Build the avatar + sender-label header row for one message and
        an empty bubble Canvas beneath it, aligned right for the user's
        own messages and left for anything from NexCore AI. Returns
        ``(row_frame, bubble_canvas)`` so callers finish drawing the
        bubble's content and sizing it.
        """
        is_user = kind == "user"

        row = tk.Frame(self._chat_inner, bg=COLORS["bg"])
        row.pack(fill="x", padx=8, pady=(6, 0))

        col = tk.Frame(row, bg=COLORS["bg"])
        col.pack(side="right" if is_user else "left")

        head = tk.Frame(col, bg=COLORS["bg"])
        head.pack(fill="x")

        avatar = tk.Canvas(head, width=20, height=20, bg=COLORS["bg"], highlightthickness=0)
        avatar_bg = self._AVATAR_BG[kind]
        avatar.create_oval(1, 1, 19, 19, fill=avatar_bg, outline=avatar_bg)
        avatar.create_text(10, 10, text=self._AVATAR_GLYPH[kind], fill="#ffffff", font=(UI_FONT[0], 9, "bold"))

        sender_label = tk.Label(
            head, text=self._SENDER_NAME[kind], bg=COLORS["bg"], fg=avatar_bg,
            font=(UI_FONT[0], AI_LABEL_FONT_SIZE, "bold"),
        )
        if is_user:
            sender_label.pack(side="right", padx=(0, 6))
            avatar.pack(side="right")
        else:
            avatar.pack(side="left")
            sender_label.pack(side="left", padx=(6, 0))

        bubble_canvas = tk.Canvas(col, bg=COLORS["bg"], highlightthickness=0)
        bubble_canvas.pack(anchor="e" if is_user else "w", pady=(3, 2))

        return row, bubble_canvas

    def _add_bubble(self, text: str, kind: str) -> None:
        bubble_bg = self._BUBBLE_BG[kind]
        text_fg = "#ffffff" if kind == "user" else COLORS["text_fg"]

        _row, canvas = self._start_bubble_row(kind)
        padding = 10
        text_id = canvas.create_text(
            padding, padding, anchor="nw", text=text, fill=text_fg,
            font=(UI_FONT[0], AI_MESSAGE_FONT_SIZE), width=self.BUBBLE_MAX_WIDTH,
        )
        canvas.update_idletasks()
        bbox = canvas.bbox(text_id) or (padding, padding, padding + 10, padding + 10)
        _x1, _y1, x2, y2 = bbox
        rect_w = (x2 - padding) + padding * 2
        rect_h = (y2 - padding) + padding * 2
        canvas.configure(width=rect_w, height=rect_h)
        rect_id = self._draw_rounded_rect(canvas, 0, 0, rect_w, rect_h, 10, fill=bubble_bg, outline=bubble_bg)
        canvas.tag_lower(rect_id, text_id)
        canvas.coords(text_id, padding, padding)

        self._scroll_chat_to_bottom()

    def _append_hint(self, text: str) -> None:
        tk.Label(
            self._chat_inner, text=text, bg=COLORS["bg"], fg=COLORS["welcome_muted"], font=(UI_FONT[0], 11),
            wraplength=self.BUBBLE_MAX_WIDTH + 40, justify="left", anchor="w",
        ).pack(fill="x", padx=10, pady=(10, 4), anchor="w")
