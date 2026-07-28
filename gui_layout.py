"""gui_layout.py - NexCore IDE application shell.

This is the top-level assembly file: window setup, the three-column grid
layout (Explorer 20% / Editor 55% / AI Assistant 25%), the menu bar, the
Run/Stop toolbar, the status bar, and the ``NexCoreApp`` class that wires
all of it to the real backend modules (``editor_core.EditorWorkspace``,
``file_io``, ``execution_engine.ExecutionEngine``).

Everything reusable lives in ``ui_components.py`` (widget classes) and
``constants.py`` (colors/fonts/syntax-highlighting/sidebar filter) - this
file only assembles those pieces and contains the event handlers that
connect user actions to the backend.

Threading note
--------------
``ExecutionEngine`` invokes its callbacks from background reader/monitor
threads, and ``AIPanel`` makes its Anthropic API calls on a background
thread. Tkinter widgets may only be touched from the main thread, so both
of those subsystems only ever push messages onto a plain ``queue.Queue``;
a periodic ``after()`` poll on the main thread drains each queue and is
the only place that touches the corresponding widget.
"""

from __future__ import annotations

import os
import queue
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import tkinter.ttk as ttk
from typing import Callable, Dict, List, Optional, Tuple

import customtkinter

from constants import COLORS, SMALL_FONT, TOPBAR_FONT, UI_FONT, pick_monospace_font
from ui_components import (
    AIPanel,
    ActivityBar,
    BreadcrumbBar,
    CodeEditor,
    ConsolePanel,
    ExtensionsPanel,
    RunDebugPanel,
    SearchPanel,
    Sidebar,
    SourceControlPanel,
    StatusBar,
    TabButton,
    WelcomeScreen,
)
from editor_core import EditorTab, EditorWorkspace
from execution_engine import ExecutionEngine, ExecutionResult
import file_io

customtkinter.set_appearance_mode("dark")
customtkinter.set_default_color_theme("blue")


class NexCoreApp(customtkinter.CTk):
    """VS Code-styled NexCore IDE window wiring the GUI to the core backend."""

    def __init__(self) -> None:
        super().__init__()
        self.title("NexCore IDE")
        self.geometry("1500x820")
        self.configure(fg_color=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._configure_ttk_styles()
        self.editor_font_family = pick_monospace_font(self)

        # -- Core, GUI-agnostic objects --------------------------------------
        # The factory closes over `_pending_frame`, pointed at whichever
        # tab-content frame is about to receive a new CodeEditor right
        # before EditorWorkspace.add_tab() is called.
        self._pending_frame: Optional[tk.Frame] = None
        self.workspace = EditorWorkspace(widget_factory=self._create_editor_widget)

        self._output_queue: "queue.Queue[tuple]" = queue.Queue()
        self.engine = ExecutionEngine(
            on_stdout=lambda line: self._output_queue.put(("stdout", line)),
            on_stderr=lambda line: self._output_queue.put(("stderr", line)),
            on_finished=lambda result: self._output_queue.put(("finished", result)),
            on_error=lambda message: self._output_queue.put(("error", message)),
        )

        self._tab_frames: Dict[int, tk.Frame] = {}
        self._tab_buttons: Dict[int, TabButton] = {}
        self._untitled_counter = 0
        # Session-only "Recent" list for the Welcome page - (display_name,
        # full_path, kind) tuples, most-recently-opened first, capped at 3.
        # No disk persistence; resets on every launch.
        self._recent_items: List[Tuple[str, str, str]] = []

        self._build_topbar()
        self._build_action_toolbar()
        self._divider(self, "x")
        self.status_bar = StatusBar(self)
        self.status_bar.pack(side="bottom", fill="x")
        self._divider(self, "x", side="bottom")

        self._build_body()

        self.welcome_screen = WelcomeScreen(
            self.editor_stack,
            editor_font_family=self.editor_font_family,
            on_new_file=lambda: self._new_tab(),
            on_open_file=self._open_file_dialog,
            on_open_folder=self._choose_workspace_folder,
            on_open_recent=self._open_recent,
            on_show_about=self._show_about,
            on_not_implemented=self._not_implemented,
            get_recent_items=lambda: self._recent_items,
        )
        self.welcome_screen.grid(row=0, column=0, sticky="nsew")
        self._show_empty_state()

        self.after(50, self._drain_output_queue)

    # -- One-time styling ---------------------------------------------------

    def _configure_ttk_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Vertical.TScrollbar", background=COLORS["sidebar_bg"], troughcolor=COLORS["bg"],
            bordercolor=COLORS["bg"], arrowcolor=COLORS["gutter_fg"], relief="flat",
        )
        # The Explorer's recursive file tree (ttk.Treeview), styled to
        # match the dark theme - background/foreground/selection colors
        # plus a comfortable row height for the Unicode folder/file icons.
        style.configure(
            "Explorer.Treeview", background=COLORS["sidebar_bg"], fieldbackground=COLORS["sidebar_bg"],
            foreground=COLORS["text_fg"], borderwidth=0, rowheight=28, font=UI_FONT,
        )
        style.map(
            "Explorer.Treeview",
            background=[("selected", COLORS["selection_bg"])],
            foreground=[("selected", "#ffffff")],
        )
        style.layout("Explorer.Treeview", [("Explorer.Treeview.treearea", {"sticky": "nswe"})])

    def _divider(self, parent: tk.Misc, axis: str, side: str = "top") -> tk.Frame:
        """A thin 1px separator line between panes (used for the
        remaining pack()-managed dividers - the three main columns use
        grid()-managed dividers instead, see _build_body)."""
        if axis == "x":
            line = tk.Frame(parent, bg=COLORS["border"], height=1)
            line.pack(side=side, fill="x")
        else:
            line = tk.Frame(parent, bg=COLORS["border"], width=1)
            line.pack(side="left", fill="y")
        return line

    # -- Three-column body: Explorer 20% / Editor 55% / AI Assistant 25% ----

    def _build_body(self) -> None:
        """Lay out the three main columns with grid() column weights so
        the 20/55/25 proportions hold (approximately) across resizes -
        pack() has no equivalent of a proportional weight, which is why
        this region uses grid() while the rest of the window still uses
        pack() for its simpler top-to-bottom stacking.
        """
        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(side="top", fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=20)  # Explorer sidebar.
        body.grid_columnconfigure(1, weight=0)   # 1px divider (fixed width).
        body.grid_columnconfigure(2, weight=55)  # Editor area.
        body.grid_columnconfigure(3, weight=0)   # 1px divider (fixed width).
        body.grid_columnconfigure(4, weight=25)  # AI Assistant panel.

        # The Activity Bar and its four mutually-exclusive views share the
        # existing 20% left column, so the overall 20/55/25 proportions do
        # not change when a different view is selected.
        self.left_sidebar_area = tk.Frame(body, bg=COLORS["sidebar_bg"], width=1, height=1)
        self.left_sidebar_area.grid(row=0, column=0, sticky="nsew")
        self.left_sidebar_area.grid_propagate(False)
        self.left_sidebar_area.grid_rowconfigure(0, weight=1)
        self.left_sidebar_area.grid_columnconfigure(0, weight=0, minsize=48)
        self.left_sidebar_area.grid_columnconfigure(1, weight=1)

        self.sidebar = Sidebar(
            self.left_sidebar_area, on_open_folder=self._choose_workspace_folder, on_file_selected=self._open_path,
            on_open_file=self._open_file_dialog,
        )
        self.search_panel = SearchPanel(self.left_sidebar_area)
        self.source_control_panel = SourceControlPanel(self.left_sidebar_area)
        self.run_debug_panel = RunDebugPanel(self.left_sidebar_area)
        self.extensions_panel = ExtensionsPanel(self.left_sidebar_area)
        self._activity_panels = {
            "explorer": self.sidebar,
            "search": self.search_panel,
            "source_control": self.source_control_panel,
            "run_debug": self.run_debug_panel,
            "extensions": self.extensions_panel,
        }
        for panel in self._activity_panels.values():
            panel.grid(row=0, column=1, sticky="nsew")
            panel.grid_remove()
        self.sidebar.grid()

        self.activity_bar = ActivityBar(self.left_sidebar_area, on_select=self._show_activity_panel)
        self.activity_bar.grid(row=0, column=0, sticky="nsew")

        self.sidebar_divider = tk.Frame(body, bg=COLORS["border"], width=1)
        self.sidebar_divider.grid(row=0, column=1, sticky="ns")

        # Frozen requested size (see Sidebar for the same pattern): without
        # this, the tk.Text widgets and the Welcome screen's padding
        # packed inside main_area later would inflate its *requested*
        # width well past its fair 55% share, which grid treats as a
        # minimum floor - throwing off the 20/55/25 proportions.
        self.main_area = tk.Frame(body, bg=COLORS["bg"], width=1, height=1)
        self.main_area.pack_propagate(False)
        self.main_area.grid(row=0, column=2, sticky="nsew")
        self._build_main_area(self.main_area)

        self.ai_divider = tk.Frame(body, bg=COLORS["border"], width=1)
        self.ai_divider.grid(row=0, column=3, sticky="ns")

        self.ai_panel = AIPanel(body, self.editor_font_family, get_context=self._get_ai_context)
        self.ai_panel.grid(row=0, column=4, sticky="nsew")

    def _show_activity_panel(self, panel_name: str) -> None:
        """Switch the one visible view beside the Activity Bar."""
        for name, panel in self._activity_panels.items():
            if name == panel_name:
                panel.grid()
            else:
                panel.grid_remove()

    def _build_main_area(self, main_area: tk.Frame) -> None:
        """The center column's own internal top-to-bottom layout: tab
        strip, editor stack, and the collapsible output console. This
        part is unrelated to the 20/55/25 proportions, so it keeps the
        simpler pack()-based layout used everywhere else in the window.
        """
        self.tab_bar = tk.Frame(main_area, bg=COLORS["tab_inactive_bg"], height=34)
        self.tab_bar.pack_propagate(False)
        self.tab_bar.pack(side="top", fill="x")
        self._divider(main_area, "x")

        self.breadcrumb = BreadcrumbBar(main_area)
        self.breadcrumb.pack(side="top", fill="x")
        self.breadcrumb.hide()

        self.console_divider = self._divider(main_area, "x", side="bottom")
        self.console = ConsolePanel(main_area, font=(self.editor_font_family, 13))
        self.console.pack(side="bottom", fill="x")

        self.editor_stack = tk.Frame(main_area, bg=COLORS["bg"])
        self.editor_stack.pack(side="top", fill="both", expand=True)
        self.editor_stack.grid_rowconfigure(0, weight=1)
        self.editor_stack.grid_columnconfigure(0, weight=1)

    # -- Top bar: seven VS Code-style dropdown menus -------------------------

    def _build_topbar(self) -> None:
        topbar = tk.Frame(self, bg=COLORS["topbar_bg"], height=30)
        topbar.pack_propagate(False)
        topbar.pack(side="top", fill="x")

        def make_menu() -> tk.Menu:
            return tk.Menu(
                self, tearoff=0, bg=COLORS["menu_bg"], fg="#f0f0f0", activebackground=COLORS["menu_hover"],
                activeforeground="#ffffff", relief="flat", bd=0, font=UI_FONT,
            )

        # -- File: fully wired to editor_core / file_io -----------------
        file_menu = make_menu()
        file_menu.add_command(label="New File", command=lambda: self._new_tab(), accelerator="Ctrl+N")
        file_menu.add_command(label="Open File...", command=self._open_file_dialog, accelerator="Ctrl+O")
        file_menu.add_command(label="Open Folder...", command=self._choose_workspace_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Save", command=self._save_current, accelerator="Ctrl+S")
        file_menu.add_command(label="Save As...", command=self._save_current_as, accelerator="Ctrl+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Close Tab", command=self._close_active_tab, accelerator="Ctrl+W")

        # -- Edit: Undo/Redo grouped separately from Cut/Copy/Paste,
        # which is grouped separately from Find/Replace - matching the
        # visual grouping VS Code uses in its own Edit menu.
        edit_menu = make_menu()
        edit_menu.add_command(label="Undo", command=self._edit_undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=self._edit_redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", command=self._edit_cut, accelerator="Ctrl+X")
        edit_menu.add_command(label="Copy", command=self._edit_copy, accelerator="Ctrl+C")
        edit_menu.add_command(label="Paste", command=self._edit_paste, accelerator="Ctrl+V")
        edit_menu.add_separator()
        edit_menu.add_command(label="Find", command=lambda: self._show_find_replace(replace_mode=False),
                               accelerator="Ctrl+F")
        edit_menu.add_command(label="Replace", command=lambda: self._show_find_replace(replace_mode=True),
                               accelerator="Ctrl+H")

        # -- Selection: Select All + Duplicate are real; multi-cursor
        # editing isn't something a plain tkinter.Text supports, so those
        # stay visible-but-inert placeholders.
        selection_menu = make_menu()
        selection_menu.add_command(label="Select All", command=self._selection_select_all, accelerator="Ctrl+A")
        selection_menu.add_command(label="Duplicate Selection", command=self._selection_duplicate)
        selection_menu.add_separator()
        selection_menu.add_command(label="Add Cursor Above", command=lambda: self._not_implemented("Add Cursor Above"))
        selection_menu.add_command(label="Add Cursor Below", command=lambda: self._not_implemented("Add Cursor Below"))

        # -- View: Explorer/Terminal/AI Assistant toggle real panels;
        # Problems/Output panels don't exist in this harness yet.
        view_menu = make_menu()
        view_menu.add_command(label="Explorer", command=self._view_toggle_explorer, accelerator="Ctrl+Shift+E")
        view_menu.add_command(label="Terminal", command=self._terminal_focus_output, accelerator="Ctrl+`")
        view_menu.add_command(label="AI Assistant", command=self._view_toggle_ai_panel)
        view_menu.add_separator()
        view_menu.add_command(label="Problems", command=lambda: self._not_implemented("Problems Panel"))
        view_menu.add_command(label="Output", command=lambda: self._not_implemented("Output Panel"))

        # -- Go: Go to Line is real; file/symbol navigation is out of
        # scope for this file explorer.
        go_menu = make_menu()
        go_menu.add_command(label="Go to Line...", command=self._show_go_to_line, accelerator="Ctrl+G")
        go_menu.add_separator()
        go_menu.add_command(label="Back", command=lambda: self._not_implemented("Go Back"))
        go_menu.add_command(label="Forward", command=lambda: self._not_implemented("Go Forward"))
        go_menu.add_command(label="Go to File...", command=lambda: self._not_implemented("Go to File"))

        terminal_menu = make_menu()
        terminal_menu.add_command(label="New Terminal", command=self._terminal_focus_output)
        terminal_menu.add_command(label="Run Active File", command=self._run_current, accelerator="F5")
        terminal_menu.add_separator()
        terminal_menu.add_command(label="Clear Output", command=lambda: self.console.clear())

        help_menu = make_menu()
        help_menu.add_command(label="About NexCore IDE", command=self._show_about)

        self._add_menu_label(topbar, "File", file_menu)
        self._add_menu_label(topbar, "Edit", edit_menu)
        self._add_menu_label(topbar, "Selection", selection_menu)
        self._add_menu_label(topbar, "View", view_menu)
        self._add_menu_label(topbar, "Go", go_menu)
        self._add_menu_label(topbar, "Terminal", terminal_menu)
        self._add_menu_label(topbar, "Help", help_menu)

    def _build_action_toolbar(self) -> None:
        """A dedicated row for large, unmistakable Run/Stop controls.

        Kept separate from the thin menu bar above it so these two
        actions - the ones reached for constantly - stay big and visible
        instead of competing for space with the seven text menus.
        """
        toolbar = tk.Frame(self, bg=COLORS["action_toolbar_bg"], height=52)
        toolbar.pack_propagate(False)
        toolbar.pack(side="top", fill="x")

        button_font = (self.editor_font_family, 13, "bold")

        self.run_button = customtkinter.CTkButton(
            toolbar, text="▶  Run", width=100, height=38, corner_radius=8,
            fg_color=COLORS["run_green"], hover_color=COLORS["run_green_hover"], text_color="#ffffff",
            font=button_font, command=self._run_current,
        )
        self.stop_button = customtkinter.CTkButton(
            toolbar, text="■  Stop", width=100, height=38, corner_radius=8,
            fg_color=COLORS["stop_red"], hover_color=COLORS["stop_red_hover"], text_color="#ffffff",
            font=button_font, command=self._stop_running, state="disabled",
        )
        # Packed right-to-left so Run still reads before Stop, left to
        # right, while the pair as a whole sits flush against the right
        # edge of the toolbar row.
        self.stop_button.pack(side="right", padx=(6, 12), pady=7)
        self.run_button.pack(side="right", padx=6, pady=7)

    def _add_menu_label(self, parent: tk.Misc, text: str, menu: tk.Menu) -> None:
        label = tk.Label(
            parent, text=f"  {text}  ", bg=COLORS["topbar_bg"], fg="#cccccc", font=TOPBAR_FONT, cursor="hand2",
        )
        label.pack(side="left")

        def on_click(event: tk.Event) -> None:
            menu.tk_popup(event.x_root, event.y_root)

        def on_enter(_e=None):
            label.configure(bg=COLORS["hover_bg"])

        def on_leave(_e=None):
            label.configure(bg=COLORS["topbar_bg"])

        label.bind("<Button-1>", on_click)
        label.bind("<Enter>", on_enter)
        label.bind("<Leave>", on_leave)

    # -- Widget factory used by EditorWorkspace ------------------------------

    def _create_editor_widget(self) -> CodeEditor:
        """Build the CodeEditor backing a new tab.

        Called by ``EditorWorkspace.add_tab`` with no arguments; the
        parent frame it should live in is picked up from
        ``self._pending_frame``, set immediately before ``add_tab`` runs.
        """
        editor = CodeEditor(self._pending_frame, self.editor_font_family)
        editor.pack(fill="both", expand=True)
        return editor

    # -- Tab management -------------------------------------------------------

    def _new_tab(self, file_path: Optional[str] = None, content: str = "") -> None:
        self._show_output_panel()
        # Only burn an Untitled number when actually creating a blank
        # buffer - opening a real file must never advance this counter,
        # otherwise "Untitled-N" labels jump ahead every time a file is
        # opened, which looks exactly like phantom tab creation.
        if file_path:
            title = os.path.basename(file_path)
        else:
            self._untitled_counter += 1
            title = f"Untitled-{self._untitled_counter}"

        content_frame = tk.Frame(self.editor_stack, bg=COLORS["bg"])
        content_frame.grid(row=0, column=0, sticky="nsew")

        self._pending_frame = content_frame
        tab = self.workspace.add_tab(file_path=file_path, content=content, title=title)
        self._pending_frame = None

        # Programmatic content load bypasses <KeyRelease>, so force a
        # gutter/highlight refresh once up front.
        tab.widget.refresh()
        tab.widget.bind("<KeyRelease>", lambda _e, tid=tab.tab_id: self._on_text_changed(tid))

        self._tab_frames[tab.tab_id] = content_frame

        button = TabButton(
            self.tab_bar, title=tab.display_title(),
            on_select=lambda tid=tab.tab_id: self.switch_to_tab(tid),
            on_close=lambda tid=tab.tab_id: self.request_close_tab(tid),
        )
        button.pack(side="left")
        self._tab_buttons[tab.tab_id] = button

        self.switch_to_tab(tab.tab_id)

    def switch_to_tab(self, tab_id: int) -> None:
        tab = self.workspace.switch_tab(tab_id)
        if tab is None:
            return
        self._tab_frames[tab_id].tkraise()
        for tid, button in self._tab_buttons.items():
            button.set_active(tid == tab_id)
        tab.widget.focus_editor()
        self._update_breadcrumb(tab)
        self._update_status_bar()

    def _on_text_changed(self, tab_id: int) -> None:
        self._mark_tab_edited(tab_id)

    def _refresh_tab_label(self, tab_id: int) -> None:
        tab = self.workspace.get_tab(tab_id)
        button = self._tab_buttons.get(tab_id)
        if tab is not None and button is not None:
            button.set_title(tab.display_title())

    def _active_tab(self) -> Optional[EditorTab]:
        return self.workspace.get_active_tab()

    def _close_active_tab(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            self.request_close_tab(tab.tab_id)

    # -- Edit menu: delegates straight to the active tab's tk.Text widget --

    def _edit_undo(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        try:
            tab.widget.text.edit_undo()
        except tk.TclError:
            pass  # Nothing left to undo.

    def _edit_redo(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        try:
            tab.widget.text.edit_redo()
        except tk.TclError:
            pass  # Nothing left to redo.

    def _edit_cut(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            tab.widget.text.event_generate("<<Cut>>")

    def _edit_copy(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            tab.widget.text.event_generate("<<Copy>>")

    def _edit_paste(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            tab.widget.text.event_generate("<<Paste>>")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About NexCore IDE",
            "NexCore IDE\n\n"
            "A CustomTkinter/Tkinter desktop IDE shell wired to\n"
            "editor_core.EditorWorkspace, file_io, and\n"
            "execution_engine.ExecutionEngine, with an AI Assistant\n"
            "panel backed by the Anthropic API.",
        )

    def _not_implemented(self, feature: str) -> None:
        """Placeholder for menu items that look real but aren't wired up.

        Used for the menu entries the design spec explicitly calls out
        as "visible but inert" (multi-cursor editing, Problems/Output
        panels, history navigation) - things a real IDE offers but this
        backend has no equivalent for yet.
        """
        messagebox.showinfo(feature, f"'{feature}' is not implemented yet.")

    # -- Selection menu -----------------------------------------------------

    def _selection_select_all(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        text_widget = tab.widget.text
        text_widget.tag_add("sel", "1.0", "end-1c")
        text_widget.mark_set("insert", "end-1c")
        text_widget.see("insert")

    def _selection_duplicate(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        text_widget = tab.widget.text
        selection = text_widget.tag_ranges("sel")
        if not selection:
            messagebox.showinfo("Duplicate Selection", "Select some text first.")
            return
        start, end = selection
        text_widget.insert(end, text_widget.get(start, end))
        self._mark_tab_edited(tab.tab_id)

    # -- View menu ------------------------------------------------------

    def _view_toggle_explorer(self) -> None:
        """Show/hide the complete Activity Bar/sidebar region and divider.

        grid_remove() hides a widget while remembering its grid options,
        so a bare grid() call restores it at exactly the same cell -
        much simpler than the pack(before=...) dance the previous
        pack()-based layout needed for the same toggle.
        """
        if self.left_sidebar_area.winfo_manager():
            self.left_sidebar_area.grid_remove()
            self.sidebar_divider.grid_remove()
        else:
            self.left_sidebar_area.grid()
            self.sidebar_divider.grid()

    def _view_toggle_ai_panel(self) -> None:
        """Show/hide the AI Assistant panel and its divider (same
        grid_remove()/grid() pattern as _view_toggle_explorer)."""
        if self.ai_panel.winfo_manager():
            self.ai_panel.grid_remove()
            self.ai_divider.grid_remove()
        else:
            self.ai_panel.grid()
            self.ai_divider.grid()

    # -- Go menu ----------------------------------------------------------

    def _show_go_to_line(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        text_widget = tab.widget.text

        dialog = tk.Toplevel(self)
        dialog.title("Go to Line")
        dialog.configure(bg=COLORS["menu_bg"])
        dialog.transient(self)
        dialog.resizable(False, False)

        line_count = int(text_widget.index("end-1c").split(".")[0])
        tk.Label(
            dialog, text=f"Line number (1-{line_count}):", bg=COLORS["menu_bg"], fg="#f0f0f0", font=UI_FONT,
        ).pack(padx=12, pady=(12, 4), anchor="w")
        entry = tk.Entry(dialog, bg=COLORS["bg"], fg=COLORS["text_fg"], insertbackground="#ffffff", relief="flat")
        entry.pack(padx=12, fill="x")
        entry.focus_set()

        status = tk.Label(dialog, text="", bg=COLORS["menu_bg"], fg=COLORS["console_stderr"], font=SMALL_FONT)
        status.pack(padx=12, pady=(2, 0), anchor="w")

        def do_go(_event=None) -> None:
            value = entry.get().strip()
            if not value.isdigit():
                status.configure(text="Enter a valid line number.")
                return
            line = max(1, min(int(value), line_count))
            text_widget.mark_set("insert", f"{line}.0")
            text_widget.see(f"{line}.0")
            tab.widget.focus_editor()
            tab.widget.highlight_current_line_number()
            dialog.destroy()

        entry.bind("<Return>", do_go)
        button_row = tk.Frame(dialog, bg=COLORS["menu_bg"])
        button_row.pack(padx=12, pady=12, anchor="e")
        self._dialog_button(button_row, "Go", do_go).pack(side="left")

    # -- Edit menu: Find / Replace ------------------------------------------

    def _show_find_replace(self, replace_mode: bool) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        text_widget = tab.widget.text
        text_widget.tag_configure("search_highlight", background=COLORS["accent"], foreground="#ffffff")

        dialog = tk.Toplevel(self)
        dialog.title("Replace" if replace_mode else "Find")
        dialog.configure(bg=COLORS["menu_bg"])
        dialog.transient(self)
        dialog.resizable(False, False)

        tk.Label(dialog, text="Find:", bg=COLORS["menu_bg"], fg="#f0f0f0", font=UI_FONT).grid(
            row=0, column=0, padx=(12, 6), pady=(12, 4), sticky="w",
        )
        find_entry = tk.Entry(dialog, width=28, bg=COLORS["bg"], fg=COLORS["text_fg"], insertbackground="#ffffff",
                               relief="flat")
        find_entry.grid(row=0, column=1, padx=(0, 12), pady=(12, 4))
        find_entry.focus_set()

        replace_entry = None
        if replace_mode:
            tk.Label(dialog, text="Replace:", bg=COLORS["menu_bg"], fg="#f0f0f0", font=UI_FONT).grid(
                row=1, column=0, padx=(12, 6), pady=4, sticky="w",
            )
            replace_entry = tk.Entry(dialog, width=28, bg=COLORS["bg"], fg=COLORS["text_fg"],
                                      insertbackground="#ffffff", relief="flat")
            replace_entry.grid(row=1, column=1, padx=(0, 12), pady=4)

        status = tk.Label(dialog, text="", bg=COLORS["menu_bg"], fg=COLORS["tab_inactive_fg"], font=SMALL_FONT)
        status.grid(row=2, column=0, columnspan=2, sticky="w", padx=12)

        def find_next(_event=None) -> None:
            text_widget.tag_remove("search_highlight", "1.0", "end")
            query = find_entry.get()
            if not query:
                return
            pos = text_widget.search(query, text_widget.index("insert"), stopindex="end")
            if not pos:
                pos = text_widget.search(query, "1.0", stopindex="end")  # Wrap around.
            if not pos:
                status.configure(text="No matches found.")
                return
            end_pos = f"{pos}+{len(query)}c"
            text_widget.tag_add("search_highlight", pos, end_pos)
            text_widget.mark_set("insert", end_pos)
            text_widget.see(pos)
            status.configure(text="")

        def replace_one() -> None:
            if replace_entry is None:
                return
            ranges = text_widget.tag_ranges("search_highlight")
            if ranges:
                text_widget.delete(ranges[0], ranges[1])
                text_widget.insert(ranges[0], replace_entry.get())
                self._mark_tab_edited(tab.tab_id)
            find_next()

        def replace_all() -> None:
            if replace_entry is None:
                return
            query, replacement = find_entry.get(), replace_entry.get()
            if not query:
                return
            content = text_widget.get("1.0", "end-1c")
            count = content.count(query)
            if count == 0:
                status.configure(text="No matches found.")
                return
            text_widget.delete("1.0", "end")
            text_widget.insert("1.0", content.replace(query, replacement))
            self._mark_tab_edited(tab.tab_id)
            status.configure(text=f"Replaced {count} occurrence(s).")

        find_entry.bind("<Return>", find_next)
        button_row = tk.Frame(dialog, bg=COLORS["menu_bg"])
        button_row.grid(row=3, column=0, columnspan=2, padx=12, pady=(8, 12), sticky="e")
        self._dialog_button(button_row, "Find Next", find_next).pack(side="left", padx=(0, 6))
        if replace_mode:
            self._dialog_button(button_row, "Replace", replace_one).pack(side="left", padx=(0, 6))
            self._dialog_button(button_row, "Replace All", replace_all).pack(side="left")

    def _dialog_button(self, parent: tk.Misc, text: str, command: Callable) -> tk.Button:
        """Small flat dark button shared by the Find/Replace and Go-to-Line dialogs."""
        return tk.Button(
            parent, text=text, command=command, bg=COLORS["menu_hover"], fg="#ffffff",
            activebackground=COLORS["accent"], activeforeground="#ffffff", relief="flat",
            font=UI_FONT, padx=10, pady=4, cursor="hand2", bd=0,
        )

    # -- Terminal menu ---------------------------------------------------

    def _terminal_focus_output(self) -> None:
        """Stand-in for "New Terminal": there's only ever one output
        panel in this harness, so this just ensures it's visible and
        gives it focus.
        """
        self._show_output_panel()
        if not self.console._visible:
            self.console.toggle()
        self.console.text.focus_set()

    def _show_output_panel(self) -> None:
        """Show the complete Output region, including its divider."""
        if not self.console_divider.winfo_manager():
            self.console_divider.pack(side="bottom", fill="x", before=self.editor_stack)
        if not self.console.winfo_manager():
            self.console.pack(side="bottom", fill="x", before=self.editor_stack)

    def _hide_output_panel(self) -> None:
        """Remove every visible part of Output from the center column."""
        self.console.pack_forget()
        self.console_divider.pack_forget()

    def _mark_tab_edited(self, tab_id: int) -> None:
        """Shared bookkeeping for programmatic (non-keystroke) edits.

        Menu-driven text changes (Replace All, Duplicate Selection) skip
        the <KeyRelease> binding that normally drives modified-tracking
        and gutter/highlight refresh, so they call this directly instead.
        """
        tab = self.workspace.get_tab(tab_id)
        if tab is None:
            return
        tab.notify_change()
        tab.widget.refresh()
        self._refresh_tab_label(tab_id)
        self._update_status_bar()

    def request_close_tab(self, tab_id: int) -> None:
        tab = self.workspace.get_tab(tab_id)
        if tab is None:
            return

        if tab.is_modified:
            response = messagebox.askyesnocancel(
                "Unsaved Changes", f"'{tab.title}' has unsaved changes. Save before closing?",
            )
            if response is None:
                return
            if response and not self._save_tab(tab):
                return

        frame = self._tab_frames.pop(tab_id, None)
        button = self._tab_buttons.pop(tab_id, None)
        self.workspace.close_tab(tab_id)
        if frame is not None:
            frame.destroy()
        if button is not None:
            button.destroy()

        remaining = self.workspace.list_tabs()
        if not remaining:
            self._show_empty_state()
        else:
            active = self.workspace.get_active_tab()
            self.switch_to_tab(active.tab_id)

    def _show_empty_state(self) -> None:
        """Raise the Welcome page when zero tabs are open (also refreshes
        its "Recent" list, since it may have changed since last shown)."""
        self._hide_output_panel()
        self.breadcrumb.hide()
        self.welcome_screen.refresh()
        self.welcome_screen.tkraise()
        self._update_status_bar()

    def _open_recent(self, path: str, kind: str) -> None:
        if kind == "folder":
            self.sidebar.show_directory(path, is_root=True)
            self._show_output_panel()
        else:
            self._open_path(path)

    def _record_recent(self, display_name: str, full_path: str, kind: str) -> None:
        """Track the last few opened folders/files for the Welcome page's
        "Recent" section. Session-only (in-memory) - intentionally not
        persisted to disk.
        """
        self._recent_items = [item for item in self._recent_items if item[1] != full_path]
        self._recent_items.insert(0, (display_name, full_path, kind))
        self._recent_items = self._recent_items[:3]

    # -- AI Assistant wiring --------------------------------------------------

    def _get_ai_context(self) -> Optional[Tuple[str, str]]:
        """Give the AI panel (filename, content) for the active tab, or
        None with no tab open - it injects this as context on every
        request so answers can be grounded in the open file.
        """
        tab = self._active_tab()
        if tab is None:
            return None
        return (tab.file_path or tab.title, tab.get_content())

    # -- File I/O wiring --------------------------------------------------

    def _open_path(self, path: str) -> None:
        for tab in self.workspace.list_tabs():
            if tab.file_path and os.path.normcase(os.path.abspath(tab.file_path)) == os.path.normcase(
                os.path.abspath(path)
            ):
                self.switch_to_tab(tab.tab_id)
                self._record_recent(os.path.basename(tab.file_path), tab.file_path, "file")
                return

        result = file_io.open_file(path)
        if not result.success:
            messagebox.showerror("Open File Failed", result.message)
            self.status_bar.set_status(result.message)
            return

        self._new_tab(file_path=result.path, content=result.content)
        self._record_recent(os.path.basename(result.path), result.path, "file")
        self.status_bar.set_status(result.message)

    def _open_file_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Open File",
            filetypes=[("Python Files", "*.py"), ("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if path:
            self._open_path(path)

    def _choose_workspace_folder(self) -> None:
        path = filedialog.askdirectory(title="Open Folder")
        if path:
            self.sidebar.show_directory(path, is_root=True)
            self._show_output_panel()
            self._record_recent(os.path.basename(path.rstrip("/\\")) or path, path, "folder")

    def _save_tab(self, tab: EditorTab) -> bool:
        """Save ``tab`` to disk, prompting for a path if it has none.

        Returns ``True`` if the tab ended up saved, ``False`` if the
        user cancelled a required "Save As" dialog or the write failed.
        """
        if tab.file_path is None:
            return self._save_tab_as(tab)

        result = file_io.save_file(tab.file_path, tab.get_content())
        if not result.success:
            messagebox.showerror("Save Failed", result.message)
            self.status_bar.set_status(result.message)
            return False

        tab.mark_saved()
        self._refresh_tab_label(tab.tab_id)
        self._update_status_bar()
        self.status_bar.set_status(result.message)
        return True

    def _save_tab_as(self, tab: EditorTab) -> bool:
        path = filedialog.asksaveasfilename(
            title="Save As", defaultextension=".py",
            filetypes=[("Python Files", "*.py"), ("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return False

        result = file_io.save_file_as(path, tab.get_content())
        if not result.success:
            messagebox.showerror("Save As Failed", result.message)
            self.status_bar.set_status(result.message)
            return False

        tab.file_path = result.path
        tab.title = os.path.basename(result.path)
        tab.mark_saved()
        self._refresh_tab_label(tab.tab_id)
        self._update_status_bar()
        self.status_bar.set_status(result.message)
        return True

    def _save_current(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            self._save_tab(tab)

    def _save_current_as(self) -> None:
        tab = self._active_tab()
        if tab is not None:
            self._save_tab_as(tab)

    # -- Execution engine wiring --------------------------------------------

    def _run_current(self) -> None:
        self._show_output_panel()
        tab = self._active_tab()
        if tab is None:
            return

        if self.engine.is_running():
            messagebox.showwarning("Already Running", "A script is already running.")
            return

        if tab.file_path is None or tab.is_modified:
            if not self._save_tab(tab):
                self.status_bar.set_status("Run cancelled: file was not saved.")
                return

        self.console.clear()
        self.console.append(f"Running {tab.file_path} ...\n", kind="info")

        if self.engine.run(tab.file_path):
            self.run_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status_bar.set_status("Running...")

    def _stop_running(self) -> None:
        if self.engine.stop():
            self.status_bar.set_status("Stop requested...")
        else:
            self.status_bar.set_status("No running process to stop.")

    def _drain_output_queue(self) -> None:
        """Move any pending execution-engine messages onto the console.

        The only method that touches ``self.console`` on behalf of the
        engine, keeping Tkinter access on the main thread even though
        the engine's callbacks fire from worker threads.
        """
        try:
            while True:
                kind, payload = self._output_queue.get_nowait()
                if kind == "stdout":
                    self.console.append(f"{payload}\n", kind="stdout")
                elif kind == "stderr":
                    self.console.append(f"{payload}\n", kind="stderr")
                elif kind == "finished":
                    result: ExecutionResult = payload
                    if result.was_killed:
                        status = "Killed"
                    else:
                        status = f"Exit Code: {result.return_code}"
                    self.console.append(f"\n[Process finished - {status}]\n", kind="info")
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status_bar.set_status(status)
                elif kind == "error":
                    self.console.append(f"\n[Error: {payload}]\n", kind="stderr")
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status_bar.set_status(f"Error: {payload}")
        except queue.Empty:
            pass
        finally:
            self.after(50, self._drain_output_queue)

    # -- Small helpers -----------------------------------------------------

    def _update_breadcrumb(self, tab: EditorTab) -> None:
        self.breadcrumb.set_path(tab.file_path, fallback_title=tab.title)
        if not self.breadcrumb.winfo_manager():
            self.breadcrumb.pack(side="top", fill="x", before=self.editor_stack)

    def _update_status_bar(self) -> None:
        tab = self._active_tab()
        if tab is None:
            self.status_bar.set_file("No file open")
            self.status_bar.set_editor_info("Ln 1, Col 1", "Plain Text")
            return
        name = tab.file_path or f"{tab.title} (unsaved)"
        marker = "  ●" if tab.is_modified else ""
        self.status_bar.set_file(f"{name}{marker}")
        line, column = tab.widget.text.index("insert").split(".")
        suffix = os.path.splitext(tab.file_path or tab.title)[1].lower()
        language = {
            ".py": "Python",
            ".md": "Markdown",
            ".json": "JSON",
        }.get(suffix, "Plain Text")
        self.status_bar.set_editor_info(f"Ln {line}, Col {int(column) + 1}", language)

    def _on_close(self) -> None:
        if self.workspace.has_unsaved_changes():
            if not messagebox.askyesno("Unsaved Changes", "Some tabs have unsaved changes. Quit anyway?"):
                return
        if self.engine.is_running():
            self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    app = NexCoreApp()
    app.mainloop()
