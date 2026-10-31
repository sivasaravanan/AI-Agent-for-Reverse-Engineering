"""Bridge between the setup dialog and the prerequisite manager (proxy / CA / offline folder / mirrors / IT request / diagnostics)."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional


def open_manager(parent, root_dir: Optional[Path] = None) -> tk.Toplevel:
    """Window with the full prerequisite manager (all tools, verified downloads, offline folder, hurdles, IT request, audit log)."""
    from prerequisites.ui import PrerequisitesTab
    win = tk.Toplevel(parent)
    win.title("Prerequisite manager - institution options")
    win.geometry("1180x560")
    tab = PrerequisitesTab(win, root_dir=root_dir)
    tab.pack(fill="both", expand=True)
    return win


def add_institution_button(dialog, bar) -> ttk.Button:
    btn = ttk.Button(bar, text="Institution options...", command=lambda: open_manager(dialog))
    btn.pack(side="left", padx=6)
    return btn
