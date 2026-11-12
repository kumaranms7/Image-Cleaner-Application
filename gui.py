"""
gui.py
======

Tkinter desktop interface for Image Cleaner.

Wires up folder selection, configuration editing, and threaded workflows for
scanning a website, generating CSV reports, and moving unused images to
backup / restoring them. Long-running work always happens on a background
thread so the UI stays responsive; results are marshaled back to the main
thread via ``Tk.after``.

Visual design (colors, fonts, ttk styling) lives in :mod:`theme` and is
applied once at startup and again on demand when the user toggles light/dark
mode; this module owns layout and behavior only.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Dict, List, Optional, Tuple

import theme
from backup_manager import BackupEntry, BackupManager, BackupRunResult, RestoreRunResult
from comparison_engine import ComparisonEngine, ComparisonReport, ImageStatus
from config import AppConfig, ConfigManager
from duplicate_detector import DuplicateDetector, DuplicateGroup, build_report_rows
from liquidweb_importer import LiquidWebImporter
from reference_parser import ReferenceParser
from report_generator import LiquidWebComparisonRow, ReportGenerator
from scanner import ScanResult, WebsiteScanner
from utils import get_icon_path, human_readable_size

logger = logging.getLogger(__name__)

OUTER_PAD = 14
CARD_PAD = 12


class QueueLogHandler(logging.Handler):
    """A logging handler that pushes ``(levelname, formatted_message)`` pairs
    onto a thread-safe queue.

    The GUI polls this queue from the main thread (via ``Tk.after``) so log
    messages produced on background worker threads can be displayed safely,
    with the level preserved so each line can be color-coded.
    """

    def __init__(self, log_queue: "queue.Queue[Tuple[str, str]]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put_nowait((record.levelname, self.format(record)))
        except queue.Full:
            pass


class SettingsDialog(tk.Toplevel):
    """Modal dialog allowing the user to edit :class:`AppConfig` fields."""

    def __init__(
        self, parent: tk.Widget, config: AppConfig, palette: Dict[str, str], on_save
    ) -> None:
        super().__init__(parent)
        self.title("Image Cleaner Settings")
        self.geometry("580x640")
        self.minsize(480, 360)
        self.resizable(True, True)
        self.transient(parent)
        self.configure(background=palette["bg"])
        self.grab_set()

        self._config = config
        self._palette = palette
        self._on_save = on_save
        self._text_widgets: List[tk.Text] = []

        self._backup_enabled = tk.BooleanVar(value=config.backup_enabled)
        self._detect_duplicates = tk.BooleanVar(value=config.detect_duplicates)
        self._detect_missing = tk.BooleanVar(value=config.detect_missing_images)
        self._detect_broken = tk.BooleanVar(value=config.detect_broken_references)
        self._recent_days = tk.IntVar(value=config.recent_file_days)

        self._build_widgets()

    def _build_widgets(self) -> None:
        ttk.Label(self, text="Settings", style="Header.TLabel").pack(
            anchor="w", padx=OUTER_PAD, pady=(OUTER_PAD, 0)
        )
        ttk.Label(
            self,
            text="Scan exclusion rules and feature toggles. Changes apply on the next scan.",
            style="Subheader.TLabel",
        ).pack(anchor="w", padx=OUTER_PAD, pady=(0, 10))

        # Everything between the header and the Save/Cancel row lives inside a
        # scrollable canvas — with five multi-line list fields plus the option
        # toggles, the content is taller than the dialog on smaller screens,
        # and there was previously no way to reach what got clipped at the
        # bottom. The button row itself stays outside the canvas so Save/
        # Cancel are always visible without scrolling.
        scroll_area = ttk.Frame(self)
        scroll_area.pack(fill="both", expand=True, padx=OUTER_PAD, pady=(0, 8))

        canvas = tk.Canvas(
            scroll_area, background=self._palette["bg"], highlightthickness=0, bd=0
        )
        scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def sync_content_size(_event=None) -> None:
            # Stretch the embedded content to fill the canvas's full viewport
            # whenever there's room (so the list fields actually grow when
            # the dialog is resized or maximized, instead of staying pinned
            # to their minimal natural size with the extra space wasted as
            # blank scroll area) — falling back to content's natural size,
            # which re-enables scrolling, whenever it's taller than the
            # available viewport.
            canvas_width = canvas.winfo_width()
            canvas_height = canvas.winfo_height()
            content_height = content.winfo_reqheight()
            target_height = max(content_height, canvas_height)
            canvas.itemconfigure(content_window, width=canvas_width, height=target_height)
            canvas.configure(scrollregion=(0, 0, canvas_width, target_height))

        content.bind("<Configure>", sync_content_size)
        canvas.bind("<Configure>", sync_content_size)

        def on_mousewheel(event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def bind_mousewheel(_event=None) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_mousewheel(_event=None) -> None:
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", bind_mousewheel)
        canvas.bind("<Leave>", unbind_mousewheel)
        self.bind("<Destroy>", lambda e: unbind_mousewheel() if e.widget is self else None)

        list_frame = ttk.LabelFrame(content, text="LISTS  (one entry per line, wildcards allowed)")
        list_frame.pack(fill="both", expand=True, pady=(0, 8))

        self._folders_text = self._add_list_field(
            list_frame, "Excluded Folders", self._config.excluded_folders
        )
        self._filenames_text = self._add_list_field(
            list_frame, "Excluded Filenames", self._config.excluded_filenames
        )
        self._excluded_ext_text = self._add_list_field(
            list_frame, "Excluded Extensions", self._config.excluded_extensions
        )
        self._image_ext_text = self._add_list_field(
            list_frame, "Supported Image Extensions", self._config.supported_image_extensions
        )
        self._scan_ext_text = self._add_list_field(
            list_frame, "Supported Scan Extensions", self._config.supported_scan_extensions
        )

        options_frame = ttk.LabelFrame(content, text="OPTIONS")
        options_frame.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(options_frame, style="Panel.TFrame")
        row.pack(fill="x", padx=10, pady=(8, 6))
        ttk.Label(
            row, text="Ignore files modified in the last N days (0 = disabled):", style="Panel.TLabel"
        ).pack(side="left")
        ttk.Spinbox(row, from_=0, to=3650, textvariable=self._recent_days, width=6).pack(
            side="left", padx=8
        )

        for text, var in (
            ("Enable backup when moving unused images", self._backup_enabled),
            ("Detect duplicate images", self._detect_duplicates),
            ("Detect missing/broken image references", self._detect_missing),
            ("Detect broken references generally", self._detect_broken),
        ):
            ttk.Checkbutton(options_frame, text=text, variable=var).pack(
                anchor="w", padx=10, pady=3
            )
        ttk.Frame(options_frame, style="Panel.TFrame", height=6).pack()

        button_row = ttk.Frame(self)
        button_row.pack(fill="x", padx=OUTER_PAD, pady=OUTER_PAD)
        ttk.Button(button_row, text="Save", style="Primary.TButton", command=self._save).pack(
            side="right"
        )
        ttk.Button(button_row, text="Cancel", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=8
        )

    def _add_list_field(self, parent: tk.Widget, label: str, values) -> tk.Text:
        # fill="both", expand=True on both the row and the Text widget (not
        # just fill="x") is what lets these boxes actually grow taller when
        # the dialog is resized/maximized — every row sharing expand=True
        # under the same parent divides up whatever extra vertical space
        # becomes available equally between them.
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        ttk.Label(frame, text=label, width=26, style="Panel.TLabel").pack(side="left", anchor="n")
        text_widget = tk.Text(
            frame,
            height=4,
            width=48,
            bg=self._palette["bg"],
            fg=self._palette["text"],
            insertbackground=self._palette["text"],
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            font=(theme.FONT_FAMILY, 10),
        )
        text_widget.insert("1.0", "\n".join(values))
        text_widget.pack(side="left", fill="both", expand=True)
        self._text_widgets.append(text_widget)
        return text_widget

    @staticmethod
    def _lines(widget: tk.Text) -> list:
        raw = widget.get("1.0", "end").strip()
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _save(self) -> None:
        self._config.excluded_folders = self._lines(self._folders_text)
        self._config.excluded_filenames = self._lines(self._filenames_text)
        self._config.excluded_extensions = self._lines(self._excluded_ext_text)
        self._config.supported_image_extensions = self._lines(self._image_ext_text)
        self._config.supported_scan_extensions = self._lines(self._scan_ext_text)
        self._config.recent_file_days = self._recent_days.get()
        self._config.backup_enabled = self._backup_enabled.get()
        self._config.detect_duplicates = self._detect_duplicates.get()
        self._config.detect_missing_images = self._detect_missing.get()
        self._config.detect_broken_references = self._detect_broken.get()
        self._on_save(self._config)
        self.destroy()


class RestoreDialog(tk.Toplevel):
    """Modal dialog listing restorable backup entries, with select-or-all restore."""

    def __init__(
        self,
        parent: tk.Widget,
        entries: List[BackupEntry],
        palette: Dict[str, str],
        on_restore,
    ) -> None:
        super().__init__(parent)
        self.title("Restore Backup")
        self.geometry("660x440")
        self.transient(parent)
        self.configure(background=palette["bg"])
        self.grab_set()

        self._entries = entries
        self._on_restore = on_restore

        ttk.Label(self, text="Restore Backup", style="Header.TLabel").pack(
            anchor="w", padx=OUTER_PAD, pady=(OUTER_PAD, 0)
        )
        ttk.Label(
            self,
            text=f"{len(entries)} file(s) available to restore. "
            "Select one or more (Ctrl/Shift-click), or Restore All.",
            style="Subheader.TLabel",
            wraplength=620,
        ).pack(anchor="w", padx=OUTER_PAD, pady=(2, 10))

        list_frame = ttk.Frame(self, style="Card.TFrame")
        list_frame.pack(fill="both", expand=True, padx=OUTER_PAD, pady=4)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(
            list_frame,
            selectmode="extended",
            yscrollcommand=scrollbar.set,
            bg=palette["panel"],
            fg=palette["text"],
            selectbackground=palette["accent"],
            selectforeground=palette["on_accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=palette["border"],
            font=(theme.FONT_FAMILY, 10),
            activestyle="none",
        )
        scrollbar.configure(command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True, padx=1, pady=1)

        for entry in entries:
            size_str = human_readable_size(entry.file_size)
            self.listbox.insert("end", f"  [{entry.session_date}]   {entry.original_relative_path}   ({size_str})")

        button_row = ttk.Frame(self)
        button_row.pack(fill="x", padx=OUTER_PAD, pady=OUTER_PAD)
        ttk.Button(
            button_row, text="Restore Selected", style="Primary.TButton", command=self._restore_selected
        ).pack(side="left")
        ttk.Button(
            button_row, text="Restore All", style="Secondary.TButton", command=self._restore_all
        ).pack(side="left", padx=8)
        ttk.Button(button_row, text="Cancel", style="Ghost.TButton", command=self.destroy).pack(
            side="right"
        )

    def _restore_selected(self) -> None:
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showinfo("No Selection", "Please select at least one file to restore.")
            return
        selected = [self._entries[i] for i in indices]
        self.destroy()
        self._on_restore(selected)

    def _restore_all(self) -> None:
        entries = list(self._entries)
        self.destroy()
        self._on_restore(entries)


class ImageCleanerApp(tk.Tk):
    """Main application window for Image Cleaner."""

    STAT_DEFS: List[Tuple[str, str, str]] = [
        # (key, caption, palette color key)
        ("referenced", "Referenced", "success"),
        ("unused", "Unused", "danger"),
        ("protected", "Protected", "info"),
        ("missing", "Missing", "warning"),
        ("duplicates", "Duplicate Groups", "accent"),
        ("savings", "Space Reclaimable", "warning"),
    ]

    def __init__(self, config_manager: ConfigManager) -> None:
        super().__init__()
        self.title("Image Cleaner")
        self.geometry("1060x760")
        self.minsize(880, 600)
        self._apply_icon()

        self.config_manager = config_manager
        self.config: AppConfig = config_manager.load()

        self.style = ttk.Style(self)
        self.palette: Dict[str, str] = theme.configure_style(self, self.style, self.config.theme)
        self._theme_refresh_callbacks: List[Callable[[Dict[str, str]], None]] = []

        self.website_folder = tk.StringVar()
        self.liquidweb_report = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.backup_folder = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready.")
        self.progress_phase = tk.StringVar(value="Ready")
        self.progress_percent = tk.StringVar(value="")
        self._stat_vars: Dict[str, tk.StringVar] = {
            key: tk.StringVar(value="—") for key, _, _ in self.STAT_DEFS
        }

        self._log_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._scan_result: Optional[ScanResult] = None
        self._comparison_report: Optional[ComparisonReport] = None
        self._duplicate_groups: Optional[List[DuplicateGroup]] = None
        self._liquidweb_rows: Optional[List[LiquidWebComparisonRow]] = None
        self._dashboard_path: Optional[Path] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._report_thread: Optional[threading.Thread] = None
        self._backup_thread: Optional[threading.Thread] = None

        self._build_menu()
        self._build_widgets()
        self._attach_log_handler()
        self.after(150, self._poll_log_queue)

        # A previously generated report is tied to the output folder it was
        # written to; if the user points somewhere else, "Open Report" no
        # longer refers to anything meaningful, so fall back to regenerating.
        self.output_folder.trace_add("write", lambda *_args: self._reset_report_button())

        self.protocol("WM_DELETE_WINDOW", self._on_exit)

    # ------------------------------------------------------------------ #
    # Theming helpers
    # ------------------------------------------------------------------ #
    def _register_theme_refresh(self, callback: Callable[[Dict[str, str]], None]) -> None:
        """Register a widget-coloring closure and apply it immediately.

        ttk widgets pick up style changes automatically; plain ``tk``
        widgets (Frame borders, Text, Listbox, canvases) don't, so each one
        registers a small callback here that re-applies the current
        palette. On theme toggle, every registered callback re-runs with
        the new palette.
        """
        self._theme_refresh_callbacks.append(callback)
        callback(self.palette)

    def _make_card(self, parent: tk.Widget) -> tk.Frame:
        """A bordered "card" container matching the HTML dashboard's panel style."""
        frame = tk.Frame(parent, bd=0, highlightthickness=1)

        def refresh(palette: Dict[str, str]) -> None:
            frame.configure(bg=palette["panel"], highlightbackground=palette["border"])

        self._register_theme_refresh(refresh)
        return frame

    def _toggle_theme(self) -> None:
        self.config.theme = "dark" if self.config.theme == "light" else "light"
        self.config_manager.save(self.config)
        self.palette = theme.configure_style(self, self.style, self.config.theme)
        for callback in self._theme_refresh_callbacks:
            callback(self.palette)
        self.theme_toggle_btn.configure(
            text="Light Mode" if self.config.theme == "dark" else "Dark Mode"
        )
        logger.info("Switched to %s theme.", self.config.theme)

    def _apply_icon(self) -> None:
        """Set the window icon for this window and, via ``default=``, every
        Toplevel dialog created afterward (Settings, Restore Backup) — so it
        shows in the title bar, the taskbar, and Alt-Tab consistently across
        the whole app, not just the main window.
        """
        icon_path = get_icon_path()
        if not icon_path:
            return
        try:
            self.iconbitmap(default=str(icon_path))
        except tk.TclError as exc:
            logger.warning("Could not set application icon (%s): %s", icon_path, exc)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        settings_menu = tk.Menu(menu_bar, tearoff=False)
        settings_menu.add_command(label="Edit Settings...", command=self._open_settings)
        menu_bar.add_cascade(label="Settings", menu=settings_menu)
        self.config_menu = menu_bar
        self.configure(menu=menu_bar)

    def _build_widgets(self) -> None:
        self._build_header()

        # Tk's pack geometry manager carves cavity in call order: a widget
        # packed with expand=True greedily claims whatever cavity remains
        # *at the time it's packed*, regardless of what's packed after it.
        # So every fixed-height widget that should anchor to an edge must be
        # packed before the one expanding widget (the log console) claims
        # the rest — status bar and action bar are packed with side="bottom"
        # first, and the log console (fill=both, expand=True) is packed last.
        self._build_status_bar()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=OUTER_PAD, pady=(0, OUTER_PAD))

        self._build_folders_card(body)
        self._build_stats_row(body)
        self._build_progress_row(body)
        self._build_action_bar(body)
        self._build_log_card(body)

    def _build_header(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=OUTER_PAD, pady=(OUTER_PAD, 10))

        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        ttk.Label(title_box, text="Image Cleaner", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="Find and safely retire unused images from a static website.",
            style="Subheader.TLabel",
        ).pack(anchor="w")

        actions = ttk.Frame(header)
        actions.pack(side="right")
        ttk.Button(
            actions, text="Settings", style="Secondary.TButton", command=self._open_settings
        ).pack(side="right")
        self.theme_toggle_btn = ttk.Button(
            actions,
            text="Light Mode" if self.config.theme == "dark" else "Dark Mode",
            style="Ghost.TButton",
            command=self._toggle_theme,
        )
        self.theme_toggle_btn.pack(side="right", padx=(0, 8))

    def _build_folders_card(self, parent: tk.Widget) -> None:
        card = self._make_card(parent)
        card.pack(fill="x", pady=(0, CARD_PAD))

        inner = ttk.Frame(card, style="Panel.TFrame")
        inner.pack(fill="x", padx=CARD_PAD, pady=CARD_PAD)
        inner.columnconfigure(1, weight=1)

        ttk.Label(inner, text="FOLDERS", style="Muted.TLabel", font=(theme.FONT_FAMILY, 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        self._add_path_row(inner, 1, "Website Folder", self.website_folder, is_dir=True)
        self._add_path_row(
            inner, 2, "Liquid Web Orphan Report (optional)", self.liquidweb_report, is_dir=False
        )
        self._add_path_row(inner, 3, "Output Folder", self.output_folder, is_dir=True)
        self._add_path_row(inner, 4, "Backup Folder", self.backup_folder, is_dir=True)

    def _add_path_row(
        self, parent: tk.Widget, row: int, label: str, variable: tk.StringVar, is_dir: bool
    ) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=3
        )
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(
            parent,
            text="Browse",
            style="Secondary.TButton",
            command=lambda: self._browse(variable, is_dir),
        ).grid(row=row, column=2, pady=3)

    def _browse(self, variable: tk.StringVar, is_dir: bool) -> None:
        if is_dir:
            selected = filedialog.askdirectory(title="Select Folder")
        else:
            selected = filedialog.askopenfilename(
                title="Select Liquid Web Orphan Report",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
        if selected:
            variable.set(selected)

    def _build_stats_row(self, parent: tk.Widget) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, CARD_PAD))
        for index, (key, caption, color_key) in enumerate(self.STAT_DEFS):
            row.columnconfigure(index, weight=1)
            self._build_stat_card(row, index, key, caption, color_key)

    def _build_stat_card(
        self, parent: tk.Widget, column: int, key: str, caption: str, color_key: str
    ) -> None:
        card = self._make_card(parent)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))

        accent = tk.Frame(card, height=3, bd=0, highlightthickness=0)
        accent.pack(fill="x", side="top")

        def refresh_accent(palette: Dict[str, str]) -> None:
            color = palette["accent"] if color_key == "accent" else palette.get(color_key, palette["text"])
            accent.configure(bg=color)

        self._register_theme_refresh(refresh_accent)

        inner = ttk.Frame(card, style="Panel.TFrame")
        inner.pack(fill="both", expand=True, padx=12, pady=8)
        ttk.Label(inner, textvariable=self._stat_vars[key], style="CardValue.TLabel").pack(anchor="w")
        ttk.Label(inner, text=caption.upper(), style="CardLabel.TLabel").pack(anchor="w")

    def _build_progress_row(self, parent: tk.Widget) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, CARD_PAD))

        label_row = ttk.Frame(row)
        label_row.pack(fill="x", pady=(0, 4))
        ttk.Label(label_row, textvariable=self.progress_phase, style="MutedOnBg.TLabel").pack(
            side="left"
        )
        ttk.Label(
            label_row,
            textvariable=self.progress_percent,
            style="MutedOnBg.TLabel",
            font=(theme.FONT_FAMILY, 9, "bold"),
        ).pack(side="right")

        self.progress_bar = ttk.Progressbar(row, mode="determinate")
        self.progress_bar.pack(fill="x")

    def _build_log_card(self, parent: tk.Widget) -> None:
        card = self._make_card(parent)
        card.pack(fill="both", expand=True, pady=(0, CARD_PAD))

        inner = ttk.Frame(card, style="Panel.TFrame")
        inner.pack(fill="both", expand=True, padx=CARD_PAD, pady=CARD_PAD)

        header_row = ttk.Frame(inner, style="Panel.TFrame")
        header_row.pack(fill="x", pady=(0, 6))
        ttk.Label(
            header_row, text="ACTIVITY LOG", style="Muted.TLabel", font=(theme.FONT_FAMILY, 9, "bold")
        ).pack(side="left")
        ttk.Button(
            header_row, text="Clear Log", style="Ghost.TButton", command=self._clear_log
        ).pack(side="right")

        self.log_widget = ScrolledText(
            inner, height=8, state="disabled", wrap="word", relief="flat", borderwidth=0
        )
        self.log_widget.pack(fill="both", expand=True)

        def refresh_log(palette: Dict[str, str]) -> None:
            self.log_widget.configure(
                bg=palette["panel_alt"],
                fg=palette["text"],
                insertbackground=palette["text"],
                font=(theme.MONO_FONT_FAMILY, 10),
                highlightthickness=1,
                highlightbackground=palette["border"],
                highlightcolor=palette["border"],
            )
            self.log_widget.tag_configure("INFO", foreground=palette["text"])
            self.log_widget.tag_configure("WARNING", foreground=palette["warning"])
            self.log_widget.tag_configure("ERROR", foreground=palette["danger"])
            self.log_widget.tag_configure("CRITICAL", foreground=palette["danger"])
            self.log_widget.tag_configure("DEBUG", foreground=palette["text_muted"])

        self._register_theme_refresh(refresh_log)

    def _build_action_bar(self, parent: tk.Widget) -> None:
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill="x", side="bottom", pady=(8, 0))

        self.start_scan_btn = ttk.Button(
            button_frame, text="Start Scan", style="Primary.TButton", command=self._start_scan
        )
        self.start_scan_btn.pack(side="left")

        self.generate_reports_btn = ttk.Button(
            button_frame, text="Generate Reports", style="Secondary.TButton", command=self._generate_reports
        )
        self.generate_reports_btn.pack(side="left", padx=8)

        self.move_unused_btn = ttk.Button(
            button_frame,
            text="Move Unused Images",
            style="Secondary.TButton",
            command=self._move_unused_images,
        )
        self.move_unused_btn.pack(side="left")

        self.restore_backup_btn = ttk.Button(
            button_frame, text="Restore Backup", style="Secondary.TButton", command=self._restore_backup
        )
        self.restore_backup_btn.pack(side="left", padx=8)

        ttk.Button(button_frame, text="Exit", style="Ghost.TButton", command=self._on_exit).pack(
            side="right"
        )

    def _build_status_bar(self) -> None:
        bar = self._make_card(self)
        bar.pack(fill="x", side="bottom")

        inner = ttk.Frame(bar, style="Panel.TFrame")
        inner.pack(fill="x", padx=OUTER_PAD, pady=6)

        self.status_dot = tk.Label(inner, text="●", font=(theme.FONT_FAMILY, 10))
        self.status_dot.pack(side="left", padx=(0, 6))

        def refresh_dot(palette: Dict[str, str]) -> None:
            self.status_dot.configure(bg=palette["panel"], fg=palette["text_muted"])

        self._register_theme_refresh(refresh_dot)

        ttk.Label(inner, textvariable=self.status_text, style="Muted.TLabel").pack(
            side="left", fill="x", expand=True
        )
        self._set_status_state("idle")

    def _set_status_state(self, state: str) -> None:
        """Recolor the status-bar dot to reflect idle/busy/success/error."""
        color_key = {"idle": "text_muted", "busy": "accent", "success": "success", "error": "danger"}.get(
            state, "text_muted"
        )
        self.status_dot.configure(fg=self.palette[color_key])

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _attach_log_handler(self) -> None:
        handler = QueueLogHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                level, message = self._log_queue.get_nowait()
                self._append_log(level, message)
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    def _append_log(self, level: str, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message + "\n", (level,))
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        SettingsDialog(self, self.config, self.palette, self._on_settings_saved)

    def _on_settings_saved(self, updated_config: AppConfig) -> None:
        self.config = updated_config
        self.config_manager.save(self.config)
        self.status_text.set("Settings saved.")
        logger.info("Configuration updated and saved to %s", self.config_manager.config_path)

    # ------------------------------------------------------------------ #
    # Stats dashboard
    # ------------------------------------------------------------------ #
    def _update_stats(
        self,
        comparison_report: ComparisonReport,
        duplicate_groups: Optional[List[DuplicateGroup]],
    ) -> None:
        summary = comparison_report.summary
        unused_bytes = sum(
            r.image.size_bytes for r in comparison_report.results if r.status is ImageStatus.UNUSED
        )
        self._stat_vars["referenced"].set(str(summary.referenced_count))
        self._stat_vars["unused"].set(str(summary.unused_count))
        self._stat_vars["protected"].set(str(summary.protected_count))
        self._stat_vars["missing"].set(str(summary.missing_count))
        self._stat_vars["duplicates"].set(str(len(duplicate_groups)) if duplicate_groups else "0")
        self._stat_vars["savings"].set(human_readable_size(unused_bytes))

    # ------------------------------------------------------------------ #
    # Actions: Scan
    # ------------------------------------------------------------------ #
    def _start_scan(self) -> None:
        website_folder = self.website_folder.get().strip()
        if not website_folder:
            messagebox.showwarning("Website Folder Required", "Please select a website folder first.")
            return
        if not Path(website_folder).is_dir():
            messagebox.showerror("Invalid Folder", f"'{website_folder}' is not a valid folder.")
            return

        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showinfo("Scan in Progress", "A scan is already running.")
            return

        self.start_scan_btn.configure(state="disabled")
        self.progress_bar.configure(value=0, maximum=100)
        self.status_text.set("Scanning...")
        self._set_progress_phase("Scanning")
        self._set_status_state("busy")

        self._scan_thread = threading.Thread(
            target=self._run_scan_worker, args=(website_folder,), daemon=True
        )
        self._scan_thread.start()

    def _run_scan_worker(self, website_folder: str) -> None:
        try:
            scanner = WebsiteScanner(
                root_folder=Path(website_folder),
                config=self.config,
                compute_hashes=self.config.detect_duplicates,
            )

            def on_scan_progress(done: int, total: int, current_path: str) -> None:
                self.after(0, self._update_progress, done, total, "Scanning")

            scan_result = scanner.scan(progress_callback=on_scan_progress)

            self.after(0, self.status_text.set, "Parsing image references...")
            self.after(0, self._set_progress_phase, "Parsing references")
            parser = ReferenceParser(self.config.supported_image_extensions)

            def on_parse_progress(done: int, total: int, current_path: str) -> None:
                self.after(0, self._update_progress, done, total, "Parsing references")

            raw_references = parser.parse_all(
                scan_result.website_files, progress_callback=on_parse_progress
            )

            self.after(0, self.status_text.set, "Comparing images against references...")
            self.after(0, self._set_progress_phase, "Comparing")
            engine = ComparisonEngine(self.config)
            comparison_report = engine.compare(scan_result, raw_references)

            duplicate_groups: Optional[List[DuplicateGroup]] = None
            if self.config.detect_duplicates:
                self.after(0, self.status_text.set, "Detecting duplicate images...")
                self.after(0, self._set_progress_phase, "Detecting duplicates")
                duplicate_groups = DuplicateDetector().find_duplicates(scan_result, comparison_report)

            liquidweb_rows: Optional[List[LiquidWebComparisonRow]] = None
            liquidweb_path = self.liquidweb_report.get().strip()
            if liquidweb_path:
                self.after(0, self.status_text.set, "Comparing against Liquid Web orphan report...")
                self.after(0, self._set_progress_phase, "Comparing Liquid Web report")
                try:
                    importer = LiquidWebImporter()
                    lw_entries = importer.parse_report(Path(liquidweb_path))
                    liquidweb_rows = importer.compare(lw_entries, comparison_report)
                except Exception as exc:  # noqa: BLE001 - a bad LW report must not abort the scan
                    logger.warning("Liquid Web report import failed: %s", exc)
                    self.after(
                        0, self.status_text.set, f"Liquid Web report import failed: {exc}"
                    )

            self._scan_result = scan_result
            self._comparison_report = comparison_report
            self._duplicate_groups = duplicate_groups
            self._liquidweb_rows = liquidweb_rows
            self.after(
                0, self._on_scan_complete, scan_result, comparison_report, duplicate_groups, liquidweb_rows
            )
        except Exception as exc:  # noqa: BLE001 - surface any scan error to the user
            logger.exception("Scan failed")
            self.after(0, self._on_scan_failed, str(exc))

    def _update_progress(self, done: int, total: int, label: str = "Working") -> None:
        """Update the progress bar plus its phase/percentage labels from a
        concrete (done, total) count — used for scanning, parsing, moving,
        and restoring, all of which report incremental progress."""
        maximum = max(total, 1)
        percent = int((done / maximum) * 100) if maximum else 0
        if self.progress_bar.cget("mode") != "determinate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
        self.progress_bar.configure(maximum=maximum, value=done)
        self.progress_phase.set(label)
        self.progress_percent.set(f"{percent}%")
        self.status_text.set(f"{label}... {done}/{total} files")

    def _set_progress_phase(self, label: str) -> None:
        """Update just the phase label for a step with no granular progress
        count (comparing, detecting duplicates, generating reports, ...),
        switching the bar to an animated indeterminate mode so it still
        shows visible activity rather than sitting frozen."""
        self.progress_phase.set(label)
        self.progress_percent.set("")
        if self.progress_bar.cget("mode") != "indeterminate":
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start(12)

    def _finish_progress(self, label: str = "Complete") -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.configure(value=self.progress_bar["maximum"])
        self.progress_phase.set(label)
        self.progress_percent.set("100%")

    def _on_scan_complete(
        self,
        result: ScanResult,
        comparison_report: ComparisonReport,
        duplicate_groups: Optional[List[DuplicateGroup]],
        liquidweb_rows: Optional[List[LiquidWebComparisonRow]],
    ) -> None:
        self.start_scan_btn.configure(state="normal")
        self.move_unused_btn.configure(state="normal")
        self._finish_progress("Scan complete")
        self._update_stats(comparison_report, duplicate_groups)
        self._set_status_state("success")
        # Any previously generated report reflects the old scan; require a
        # fresh "Generate Reports" click before "Open Report" is offered again.
        self._reset_report_button()

        summary = comparison_report.summary
        duplicate_suffix = (
            f", {len(duplicate_groups)} duplicate group(s)" if duplicate_groups else ""
        )
        liquidweb_suffix = ""
        if liquidweb_rows is not None:
            disagreements = sum(1 for r in liquidweb_rows if not r.agreement)
            liquidweb_suffix = f", {disagreements} Liquid Web disagreement(s)"
        self.status_text.set(
            f"Scan complete: {summary.referenced_count} referenced, "
            f"{summary.unused_count} unused, {summary.protected_count} protected, "
            f"{summary.missing_count} missing{duplicate_suffix}{liquidweb_suffix} "
            f"({result.stats.duration_seconds:.2f}s)."
        )
        logger.info(
            "Scan finished. Images=%d WebsiteFiles=%d Errors=%d | "
            "Referenced=%d Unused=%d Protected=%d Missing=%d DuplicateGroups=%d LiquidWebEntries=%s",
            result.stats.images_found,
            result.stats.website_files_found,
            result.stats.errors,
            summary.referenced_count,
            summary.unused_count,
            summary.protected_count,
            summary.missing_count,
            len(duplicate_groups) if duplicate_groups else 0,
            len(liquidweb_rows) if liquidweb_rows is not None else "N/A",
        )

    def _on_scan_failed(self, error_message: str) -> None:
        self.start_scan_btn.configure(state="normal")
        self.status_text.set("Scan failed. See log for details.")
        self._finish_progress("Failed")
        self._set_status_state("error")
        messagebox.showerror("Scan Failed", error_message)

    # ------------------------------------------------------------------ #
    # Actions: Reports
    # ------------------------------------------------------------------ #
    def _generate_reports(self) -> None:
        if not self._scan_result or not self._comparison_report:
            messagebox.showinfo("No Scan Data", "Please run a scan first.")
            return

        output_folder = self.output_folder.get().strip()
        if not output_folder:
            messagebox.showwarning("Output Folder Required", "Please select an output folder first.")
            return

        if self._report_thread and self._report_thread.is_alive():
            messagebox.showinfo("Reports in Progress", "Reports are already being generated.")
            return

        self.generate_reports_btn.configure(state="disabled")
        self.status_text.set("Generating reports...")
        self._set_progress_phase("Generating reports")
        self._set_status_state("busy")

        self._report_thread = threading.Thread(
            target=self._run_report_worker, args=(output_folder,), daemon=True
        )
        self._report_thread.start()

    def _run_report_worker(self, output_folder: str) -> None:
        try:
            generator = ReportGenerator(Path(output_folder))
            duplicate_rows = build_report_rows(self._duplicate_groups) if self._duplicate_groups else None
            generated = generator.generate_all(
                self._comparison_report,
                self._scan_result,
                duplicate_groups=duplicate_rows,
                liquidweb_rows=self._liquidweb_rows,
            )
            self.after(0, self._on_reports_complete, generated)
        except Exception as exc:  # noqa: BLE001 - surface any report error to the user
            logger.exception("Report generation failed")
            self.after(0, self._on_reports_failed, str(exc))

    def _on_reports_complete(self, generated: Dict[str, Path]) -> None:
        self.generate_reports_btn.configure(state="normal")
        self.status_text.set(f"Reports generated: {len(generated)} file(s).")
        self._finish_progress("Reports generated")
        self._set_status_state("success")
        for name, path in generated.items():
            logger.info("Report '%s' written to %s", name, path)

        dashboard_path = generated.get("dashboard")
        if dashboard_path is not None:
            self._switch_to_open_report_mode(dashboard_path)
            messagebox.showinfo(
                "Reports Generated",
                f"{len(generated)} report file(s) written to:\n{self.output_folder.get()}\n\n"
                "Click 'Open Report' to view the dashboard in your browser.",
            )
        else:
            messagebox.showinfo(
                "Reports Generated",
                f"{len(generated)} report file(s) written to:\n{self.output_folder.get()}",
            )

    def _on_reports_failed(self, error_message: str) -> None:
        self.generate_reports_btn.configure(state="normal")
        self.status_text.set("Report generation failed. See log for details.")
        self._finish_progress("Failed")
        self._set_status_state("error")
        messagebox.showerror("Report Generation Failed", error_message)

    def _switch_to_open_report_mode(self, dashboard_path: Path) -> None:
        self._dashboard_path = dashboard_path
        self.generate_reports_btn.configure(text="Open Report", command=self._open_report)

    def _reset_report_button(self) -> None:
        self._dashboard_path = None
        self.generate_reports_btn.configure(text="Generate Reports", command=self._generate_reports)

    def _open_report(self) -> None:
        if not self._dashboard_path or not self._dashboard_path.exists():
            messagebox.showwarning(
                "Report Not Found",
                "The generated report could not be found (it may have been moved or deleted). "
                "Please generate it again.",
            )
            self._reset_report_button()
            return

        try:
            webbrowser.open(self._dashboard_path.resolve().as_uri())
            logger.info("Opened report in browser: %s", self._dashboard_path)
        except Exception as exc:  # noqa: BLE001 - surface any browser-launch error to the user
            logger.exception("Failed to open report in browser")
            messagebox.showerror("Could Not Open Report", str(exc))

    # ------------------------------------------------------------------ #
    # Actions: Move to backup
    # ------------------------------------------------------------------ #
    def _move_unused_images(self) -> None:
        if not self._comparison_report:
            messagebox.showinfo("No Scan Data", "Please run a scan first.")
            return

        backup_folder = self.backup_folder.get().strip()
        if not backup_folder:
            messagebox.showwarning("Backup Folder Required", "Please select a backup folder first.")
            return

        unused_images = [
            r.image for r in self._comparison_report.results if r.status is ImageStatus.UNUSED
        ]
        if not unused_images:
            messagebox.showinfo("Nothing to Move", "No unused images were found in the last scan.")
            return

        total_size = human_readable_size(sum(i.size_bytes for i in unused_images))
        if not messagebox.askyesno(
            "Confirm Move",
            f"Move {len(unused_images)} unused image(s) ({total_size}) into the backup folder?\n\n"
            "Files are moved, never deleted, and can be restored later with 'Restore Backup'.",
        ):
            return

        if self._backup_thread and self._backup_thread.is_alive():
            messagebox.showinfo("Operation in Progress", "A backup/restore operation is already running.")
            return

        self.move_unused_btn.configure(state="disabled")
        self.status_text.set("Moving unused images to backup...")
        self._set_progress_phase("Moving to backup")
        self._set_status_state("busy")

        self._backup_thread = threading.Thread(
            target=self._run_move_worker, args=(backup_folder, unused_images), daemon=True
        )
        self._backup_thread.start()

    def _run_move_worker(self, backup_folder: str, images: list) -> None:
        try:
            manager = BackupManager(Path(backup_folder))

            def on_progress(done: int, total: int, current_path: str) -> None:
                self.after(0, self._update_progress, done, total, "Moving to backup")

            result = manager.move_unused_images(images, progress_callback=on_progress)
            self.after(0, self._on_move_complete, result)
        except Exception as exc:  # noqa: BLE001 - surface any backup error to the user
            logger.exception("Move to backup failed")
            self.after(0, self._on_move_failed, str(exc))

    def _on_move_complete(self, result: BackupRunResult) -> None:
        self.status_text.set(
            f"Moved {len(result.moved)} image(s) to backup "
            f"({human_readable_size(result.total_bytes_moved)}); {len(result.failed)} failed."
        )
        self._finish_progress("Move complete")
        self._set_status_state("success" if not result.failed else "error")
        logger.info(
            "Backup complete. Session=%s Moved=%d Failed=%d TotalBytes=%d",
            result.session.date_label,
            len(result.moved),
            len(result.failed),
            result.total_bytes_moved,
        )
        for outcome in result.failed:
            logger.warning("Failed to move %s: %s", outcome.image.relative_path, outcome.error)
        messagebox.showinfo(
            "Move Complete",
            f"Moved {len(result.moved)} image(s) to:\n{result.session.session_folder}\n\n"
            f"Failed: {len(result.failed)}\n\nPlease run Start Scan again for an up-to-date report.",
        )
        # The in-memory comparison report is now stale for the moved images;
        # keep Move Unused Images disabled until a fresh scan confirms the new state.

    def _on_move_failed(self, error_message: str) -> None:
        self.move_unused_btn.configure(state="normal")
        self.status_text.set("Move to backup failed. See log for details.")
        self._finish_progress("Failed")
        self._set_status_state("error")
        messagebox.showerror("Move Failed", error_message)

    # ------------------------------------------------------------------ #
    # Actions: Restore
    # ------------------------------------------------------------------ #
    def _restore_backup(self) -> None:
        backup_folder = self.backup_folder.get().strip()
        website_folder = self.website_folder.get().strip()
        if not backup_folder:
            messagebox.showwarning("Backup Folder Required", "Please select a backup folder first.")
            return
        if not website_folder:
            messagebox.showwarning(
                "Website Folder Required", "Please select the website folder to restore into."
            )
            return

        manager = BackupManager(Path(backup_folder))
        entries = manager.list_backup_entries(only_pending=True)
        if not entries:
            messagebox.showinfo("Nothing to Restore", "No backed-up images are available to restore.")
            return

        RestoreDialog(
            self,
            entries,
            self.palette,
            lambda selected: self._start_restore(website_folder, backup_folder, selected),
        )

    def _start_restore(self, website_folder: str, backup_folder: str, entries: List[BackupEntry]) -> None:
        if not messagebox.askyesno(
            "Confirm Restore", f"Restore {len(entries)} image(s) back into the website folder?"
        ):
            return
        if self._backup_thread and self._backup_thread.is_alive():
            messagebox.showinfo("Operation in Progress", "A backup/restore operation is already running.")
            return

        self.restore_backup_btn.configure(state="disabled")
        self.status_text.set("Restoring images from backup...")
        self._set_progress_phase("Restoring")
        self._set_status_state("busy")

        self._backup_thread = threading.Thread(
            target=self._run_restore_worker, args=(website_folder, backup_folder, entries), daemon=True
        )
        self._backup_thread.start()

    def _run_restore_worker(
        self, website_folder: str, backup_folder: str, entries: List[BackupEntry]
    ) -> None:
        try:
            manager = BackupManager(Path(backup_folder))

            def on_progress(done: int, total: int, current_path: str) -> None:
                self.after(0, self._update_progress, done, total, "Restoring")

            result = manager.restore_entries(entries, Path(website_folder), progress_callback=on_progress)
            self.after(0, self._on_restore_complete, result)
        except Exception as exc:  # noqa: BLE001 - surface any restore error to the user
            logger.exception("Restore failed")
            self.after(0, self._on_restore_failed, str(exc))

    def _on_restore_complete(self, result: RestoreRunResult) -> None:
        self.restore_backup_btn.configure(state="normal")
        self.status_text.set(f"Restored {len(result.restored)} image(s); {len(result.failed)} failed.")
        self._finish_progress("Restore complete")
        self._set_status_state("success" if not result.failed else "error")
        for outcome in result.failed:
            logger.warning("Failed to restore %s: %s", outcome.entry.original_relative_path, outcome.error)
        messagebox.showinfo(
            "Restore Complete",
            f"Restored {len(result.restored)} image(s).\nFailed: {len(result.failed)}\n\n"
            "Please run Start Scan again for an up-to-date report.",
        )

    def _on_restore_failed(self, error_message: str) -> None:
        self.restore_backup_btn.configure(state="normal")
        self.status_text.set("Restore failed. See log for details.")
        self._finish_progress("Failed")
        self._set_status_state("error")
        messagebox.showerror("Restore Failed", error_message)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    def _on_exit(self) -> None:
        busy = (
            (self._scan_thread and self._scan_thread.is_alive())
            or (self._report_thread and self._report_thread.is_alive())
            or (self._backup_thread and self._backup_thread.is_alive())
        )
        if busy:
            if not messagebox.askyesno(
                "Work in Progress", "A scan, report, or backup/restore operation is still running. Exit anyway?"
            ):
                return
        self.destroy()
