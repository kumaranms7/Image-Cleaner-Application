"""
theme.py
========

Visual design system for the Image Cleaner desktop UI: a light/dark color
palette and a single function that configures every ``ttk`` widget style the
GUI uses. Kept separate from :mod:`gui` so the look of the app can be tuned
in one place without touching layout code.

Classic ``tk`` widgets (``Text``, ``Listbox``) aren't covered by ``ttk.Style``
at all, so callers apply the returned palette to those manually — see
``gui.py``'s log console and the restore dialog's file list.

Colors intentionally mirror the palette already used in the HTML dashboard
(:mod:`report_generator`), so the desktop app and the reports it generates
read as one consistent product rather than two different tools bolted
together.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Dict

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

LIGHT: Dict[str, str] = {
    "bg": "#f5f6f8",
    "panel": "#ffffff",
    "panel_alt": "#f0f1f4",
    "text": "#1a1d23",
    "text_muted": "#6b7280",
    "border": "#e2e4e9",
    "accent": "#4f46e5",
    "accent_hover": "#4338ca",
    "accent_active": "#3730a3",
    "on_accent": "#ffffff",
    "success": "#16a34a",
    "danger": "#dc2626",
    "info": "#2563eb",
    "warning": "#d97706",
    "success_bg": "#e8f8ee",
    "danger_bg": "#fdecec",
    "info_bg": "#e9f0fe",
    "warning_bg": "#fdf3e3",
}

DARK: Dict[str, str] = {
    "bg": "#15171c",
    "panel": "#1e2128",
    "panel_alt": "#262a33",
    "text": "#e8eaed",
    "text_muted": "#9aa0a6",
    "border": "#31353f",
    "accent": "#818cf8",
    "accent_hover": "#9399f5",
    "accent_active": "#a5abf7",
    "on_accent": "#14161a",
    "success": "#4ade80",
    "danger": "#f87171",
    "info": "#60a5fa",
    "warning": "#fbbf24",
    "success_bg": "#183226",
    "danger_bg": "#341c1c",
    "info_bg": "#1b2a40",
    "warning_bg": "#332617",
}

PALETTES = {"light": LIGHT, "dark": DARK}


def get_palette(mode: str) -> Dict[str, str]:
    return PALETTES.get(mode, LIGHT)


def configure_style(root: tk.Misc, style: ttk.Style, mode: str) -> Dict[str, str]:
    """Configure every ttk style the app uses for the given mode ("light"/"dark").

    Returns the resolved color palette so callers can apply the same colors
    to plain ``tk`` widgets that ``ttk.Style`` doesn't reach.
    """
    palette = get_palette(mode)

    # 'clam' is the only bundled theme that reliably honors custom colors
    # for backgrounds/borders on Windows — the native themes largely ignore
    # ttk.Style color overrides in favor of OS chrome.
    style.theme_use("clam")

    base_font = (FONT_FAMILY, 10)
    header_font = (FONT_FAMILY, 18, "bold")
    subheader_font = (FONT_FAMILY, 10)
    section_font = (FONT_FAMILY, 11, "bold")
    card_value_font = (FONT_FAMILY, 22, "bold")
    card_label_font = (FONT_FAMILY, 9)
    mono_font = (MONO_FONT_FAMILY, 10)

    style.configure(
        ".",
        background=palette["bg"],
        foreground=palette["text"],
        font=base_font,
        bordercolor=palette["border"],
        focuscolor=palette["accent"],
    )

    # --- Frames -------------------------------------------------------- #
    style.configure("TFrame", background=palette["bg"])
    style.configure("Panel.TFrame", background=palette["panel"])
    style.configure("Card.TFrame", background=palette["panel"])

    # --- Labels ---------------------------------------------------------#
    style.configure("TLabel", background=palette["bg"], foreground=palette["text"], font=base_font)
    style.configure(
        "Panel.TLabel", background=palette["panel"], foreground=palette["text"], font=base_font
    )
    style.configure(
        "Header.TLabel", background=palette["bg"], foreground=palette["text"], font=header_font
    )
    style.configure(
        "Subheader.TLabel",
        background=palette["bg"],
        foreground=palette["text_muted"],
        font=subheader_font,
    )
    style.configure(
        "Section.TLabel", background=palette["bg"], foreground=palette["text"], font=section_font
    )
    style.configure(
        "Muted.TLabel", background=palette["panel"], foreground=palette["text_muted"], font=base_font
    )
    style.configure(
        "MutedOnBg.TLabel", background=palette["bg"], foreground=palette["text_muted"], font=base_font
    )
    style.configure(
        "CardValue.TLabel",
        background=palette["panel"],
        foreground=palette["text"],
        font=card_value_font,
    )
    style.configure(
        "CardLabel.TLabel",
        background=palette["panel"],
        foreground=palette["text_muted"],
        font=card_label_font,
    )
    style.configure(
        "StatusBar.TLabel",
        background=palette["panel_alt"],
        foreground=palette["text_muted"],
        font=base_font,
    )

    # --- Buttons ----------------------------------------------------- #
    style.configure(
        "TButton",
        background=palette["panel"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        lightcolor=palette["panel"],
        darkcolor=palette["panel"],
        focusthickness=0,
        padding=(14, 8),
        font=base_font,
    )
    style.map(
        "TButton",
        background=[("active", palette["panel_alt"]), ("disabled", palette["panel"])],
        foreground=[("disabled", palette["text_muted"])],
    )

    style.configure(
        "Primary.TButton",
        background=palette["accent"],
        foreground=palette["on_accent"],
        bordercolor=palette["accent"],
        lightcolor=palette["accent"],
        darkcolor=palette["accent"],
        padding=(16, 9),
        font=(FONT_FAMILY, 10, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", palette["border"]),
            ("pressed", palette["accent_active"]),
            ("active", palette["accent_hover"]),
        ],
        foreground=[("disabled", palette["text_muted"])],
    )

    style.configure(
        "Secondary.TButton",
        background=palette["panel"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        padding=(14, 8),
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", palette["panel"]), ("active", palette["panel_alt"])],
        foreground=[("disabled", palette["text_muted"])],
    )

    style.configure(
        "Ghost.TButton",
        background=palette["bg"],
        foreground=palette["text_muted"],
        bordercolor=palette["bg"],
        padding=(12, 8),
    )
    style.map(
        "Ghost.TButton",
        background=[("active", palette["panel_alt"])],
        foreground=[("active", palette["text"])],
    )

    # --- Entries / Spinbox --------------------------------------------- #
    style.configure(
        "TEntry",
        fieldbackground=palette["panel"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        insertcolor=palette["text"],
        padding=6,
    )
    style.map("TEntry", bordercolor=[("focus", palette["accent"])])

    style.configure(
        "TSpinbox",
        fieldbackground=palette["panel"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        arrowcolor=palette["text_muted"],
        padding=4,
    )

    # --- Checkbuttons ---------------------------------------------------#
    style.configure(
        "TCheckbutton",
        background=palette["panel"],
        foreground=palette["text"],
        focuscolor=palette["panel"],
        font=base_font,
    )
    style.map(
        "TCheckbutton",
        background=[("active", palette["panel"])],
        indicatorcolor=[("selected", palette["accent"]), ("!selected", palette["panel"])],
    )

    # --- Progressbar -----------------------------------------------------#
    style.configure(
        "TProgressbar",
        background=palette["accent"],
        troughcolor=palette["panel_alt"],
        bordercolor=palette["panel_alt"],
        lightcolor=palette["accent"],
        darkcolor=palette["accent"],
        thickness=10,
    )

    # --- Labelframe -------------------------------------------------- #
    style.configure(
        "TLabelframe",
        background=palette["panel"],
        bordercolor=palette["border"],
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label",
        background=palette["panel"],
        foreground=palette["text_muted"],
        font=(FONT_FAMILY, 9, "bold"),
    )

    # --- Scrollbar --------------------------------------------------- #
    style.configure(
        "Vertical.TScrollbar",
        background=palette["panel_alt"],
        troughcolor=palette["panel"],
        bordercolor=palette["panel"],
        arrowcolor=palette["text_muted"],
    )

    # --- Notebook (unused today, styled for future-proofing) -------- #
    style.configure("TNotebook", background=palette["bg"], bordercolor=palette["border"])
    style.configure(
        "TNotebook.Tab",
        background=palette["panel_alt"],
        foreground=palette["text_muted"],
        padding=(12, 6),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette["panel"])],
        foreground=[("selected", palette["text"])],
    )

    root.configure(background=palette["bg"])

    return palette


def status_color(palette: Dict[str, str], status: str) -> str:
    """Map a comparison status name to its accent color in the given palette."""
    return {
        "Referenced": palette["success"],
        "Unused": palette["danger"],
        "Protected": palette["info"],
        "Missing": palette["warning"],
    }.get(status, palette["text_muted"])
