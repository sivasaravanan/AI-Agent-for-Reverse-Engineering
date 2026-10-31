"""Phase 9: settings editor (validated; saved to the per-user settings file)."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable, Optional

from core.settings import LIMIT_BOUNDS, Settings, save_settings, validate
from services.environment_service import detect_isolation

LABELS = {
    "max_files": "Max entries in a ZIP", "max_total_uncompressed_mb": "Max total uncompressed size (MB)",
    "max_member_mb": "Max size of one file (MB)", "max_text_file_kb": "Max text file scanned (KB)",
    "max_compression_ratio": "Max compression ratio", "max_scanned_files": "Max text files scanned",
}


class SettingsView(ttk.Frame):
    def __init__(self, parent, settings: Settings, user_path: Optional[Path] = None,
                 on_saved: Optional[Callable[[Settings], None]] = None, **kwargs):
        super().__init__(parent, **kwargs)
        self.settings, self.user_path, self.on_saved = settings, user_path, on_saved
        body = ttk.Frame(self, padding=12)
        body.pack(anchor="nw")
        self.out_var = tk.StringVar()
        self.hist_var = tk.BooleanVar()
        self.max_hist_var = tk.StringVar()
        self.dash_var = tk.BooleanVar()
        self.limit_vars = {k: tk.StringVar() for k in LIMIT_BOUNDS}
        r = 0
        ttk.Label(body, text="Report output folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.out_var, width=48).grid(row=r, column=1, sticky="w")
        ttk.Button(body, text="Browse...", command=self._browse).grid(row=r, column=2, padx=4)
        r += 1
        ttk.Checkbutton(body, text="Keep local analysis history", variable=self.hist_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        ttk.Label(body, text="Max history entries").grid(row=r, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.max_hist_var, width=10).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(body, text="Show the dashboard after each analysis", variable=self.dash_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        box = ttk.LabelFrame(body, text="Archive safety limits (a ZIP over a limit is rejected or its entry skipped)")
        box.grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        for i, key in enumerate(LIMIT_BOUNDS):
            ttk.Label(box, text=LABELS[key]).grid(row=i, column=0, sticky="w", padx=3, pady=1)
            ttk.Entry(box, textvariable=self.limit_vars[key], width=12).grid(row=i, column=1, padx=3)
        r += 1
        iso = detect_isolation()
        ttk.Label(body, justify="left", wraplength=620, text=(
            "Execution of project code: " + ("available" if iso.available else "UNAVAILABLE") +
            f" - {iso.reason}\nExecution always needs your explicit approval and an isolated environment; it cannot be "
            "enabled from here. AI providers are off by default and never receive source code without your consent."
        )).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1
        bar = ttk.Frame(body)
        bar.grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Button(bar, text="Save", command=self.save).pack(side="left")
        ttk.Button(bar, text="Reset to defaults", command=self.reset).pack(side="left", padx=3)
        self.msg_var = tk.StringVar()
        ttk.Label(body, textvariable=self.msg_var, wraplength=620, justify="left").grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=3)
        self.load(settings)

    def load(self, s: Settings) -> None:
        self.out_var.set(s.output_dir)
        self.hist_var.set(s.history_enabled)
        self.max_hist_var.set(str(s.max_history))
        self.dash_var.set(s.show_dashboard_after_analysis)
        for k, v in self.limit_vars.items():
            v.set(str(s.limits[k]))

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Report output folder")
        if d:
            self.out_var.set(d)

    def _collect(self) -> dict:
        def num(v: tk.StringVar):
            try:
                return int(v.get().strip())
            except ValueError:
                return v.get()          # validate() reports it
        return {"output_dir": self.out_var.get(), "history_enabled": self.hist_var.get(),
                "max_history": num(self.max_hist_var), "show_dashboard_after_analysis": self.dash_var.get(),
                "limits": {k: num(v) for k, v in self.limit_vars.items()}}

    def save(self) -> None:
        new, warnings = validate(self._collect(), self.settings)
        self.settings = new
        self.load(new)                                  # show the values actually accepted
        try:
            if self.user_path:
                save_settings(new, self.user_path)
        except OSError as exc:
            self.msg_var.set(f"Settings could not be saved: {exc}")
            return
        self.msg_var.set("Saved." + ("  Adjusted: " + " ".join(warnings) if warnings else ""))
        if self.on_saved:
            self.on_saved(new)

    def reset(self) -> None:
        self.load(Settings())
        self.msg_var.set("Defaults loaded - press Save to keep them.")
