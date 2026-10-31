"""Lean copy controls for App Preview: what is copied into the disposable container is only what the program needs.

* **Include...**  - tick project files / folders to add to every run of this project (works for folders and ZIP archives alike).
* After a failed run, the program's own error text is read ("No such file or directory: 'settings.json'", "TemplateNotFound",
  "No module named 'pkg.sub'" ...). Files it asked for that exist in the project are offered for copying; one yes and it runs again.
Nothing is copied until the user says so; the project itself is only read."""
from __future__ import annotations

import tkinter as tk
from pathlib import PurePosixPath
from tkinter import messagebox, ttk
from typing import Dict, List

from app_preview.quick import needed_files
from plugins.build_execution import quickprep

MAX_LIST = 4000


class IncludeDialog(tk.Toplevel):
    """Multi-select list of the project's files and folders. OK adds them to the includes of this project."""

    def __init__(self, parent, names: List[str], current: List[str], on_ok):
        super().__init__(parent)
        self.title("Include files in the run")
        self.geometry("620x520")
        self.transient(parent.winfo_toplevel())
        self._on_ok = on_ok
        dirs = set()
        for n in names:
            p = PurePosixPath(n).parent
            while str(p) not in (".", ""):
                dirs.add(str(p) + "/")
                p = p.parent
        self.items = sorted(dirs) + sorted(names)
        ttk.Label(self, text="Only the script, its imports and the files its code names are copied into the container.\n"
                  "Tick here anything else the program needs (a folder includes everything inside it).", padding=8).pack(fill="x")
        self.filter = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.filter)
        e.pack(fill="x", padx=8)
        e.bind("<KeyRelease>", lambda _e: self._fill())
        fr = ttk.Frame(self)
        fr.pack(fill="both", expand=True, padx=8, pady=6)
        self.lb = tk.Listbox(fr, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(fr, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lb.pack(fill="both", expand=True)
        self.cur = ttk.Label(self, text="", style="Muted.TLabel", wraplength=580)
        self.cur.pack(fill="x", padx=8)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Include selected", command=self.ok).pack(side="left")
        ttk.Button(bar, text="Remove all includes", command=self.clear).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.current = list(current)
        self._shown: List[str] = []
        self._fill()

    def _fill(self) -> None:
        q = self.filter.get().strip().lower()
        self._shown = [i for i in self.items if not q or q in i.lower()][:MAX_LIST]
        self.lb.delete(0, "end")
        for i in self._shown:
            self.lb.insert("end", ("📁 " if i.endswith("/") else "") + i)
        self.cur.config(text=("Included now: " + ", ".join(self.current[:12]) + (" ..." if len(self.current) > 12 else "")) if self.current else "Nothing included by hand yet.")

    def ok(self) -> None:
        picked = [self._shown[i].rstrip("/") for i in self.lb.curselection()]
        if picked:
            self.current = self._on_ok(picked, False)
            self._fill()

    def clear(self) -> None:
        self.current = self._on_ok([], True)
        self._fill()


class LeanCopyMixin:
    _lean_tries = 0
    _lean_auto = False

    # ------------------------------------------------------------------ manual includes
    def _lean_source(self) -> str:
        return self.source or self.src_var.get().strip()

    def open_includes(self) -> None:
        src = self._lean_source()
        if not src:
            messagebox.showinfo("Include files", "Choose a folder, ZIP or file first.", parent=self)
            return
        if not getattr(self, "names", None):
            messagebox.showinfo("Include files", "The project list is still being read - try again in a moment.", parent=self)
            return
        IncludeDialog(self, list(self.names), quickprep.includes_for(src), self._lean_apply)

    def _lean_apply(self, rels: List[str], clear: bool) -> List[str]:
        src = self._lean_source()
        if clear:
            quickprep.clear_includes(src)
        else:
            quickprep.add_includes(src, rels)
        cur = quickprep.includes_for(src)
        self._lean_label()
        self.status_var.set(f"{len(cur)} item(s) included in the run." if cur else "No extra files included - only what the program needs is copied.")
        return cur

    def _lean_label(self) -> None:
        n = len(quickprep.includes_for(self._lean_source()))
        try:
            self.cpt.set_suffix(self.include_btn, f"({n})" if n else "")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ the program asked for a file
    def _lean_pre_start(self) -> None:
        """Called at the start of every start(): a start made by the user (not by the offer below) resets the retry counter."""
        if not self._lean_auto:
            self._lean_tries = 0
        self._lean_auto = False

    def _lean_after_result(self, res) -> bool:
        """After a failed run: offer to copy the files the program reported as missing. True when a re-run was started."""
        if getattr(res, "status", "") != "failed" or self._lean_tries >= 3 or not getattr(self, "names", None):
            return False
        text = (getattr(res, "stderr", "") or "") + "\n" + (getattr(res, "stdout", "") or "")
        entry_file = getattr(getattr(getattr(self.session, "plan", None), "entry", None), "file", "") or ""
        edir = str(PurePosixPath(entry_file).parent) if "/" in entry_file else ""
        found = needed_files(text, list(self.names), edir)
        want = found["files"] + found["dirs"]
        if found["absent"]:
            self._console("▸ Asked for but not inside the project: " + ", ".join(found["absent"]) + " (outside the folder, or created while running)\n", "phase")
        if not want:
            return False
        have = set(quickprep.includes_for(self._lean_source()))
        want = [w for w in want if w not in have]
        if not want:
            return False
        shown = "\n".join("  - " + w + ("/" if w in found["dirs"] else "") for w in want[:12])
        if not messagebox.askyesno("Copy the missing files?", "Only the files the program needs are copied (lean copy). It reported these as missing:\n\n"
                                   + shown + "\n\nCopy them into the container and run again?", parent=self):
            return False
        self._lean_tries += 1
        self._lean_apply(want, False)
        self._console("⚡ Copying " + ", ".join(want[:8]) + " and running again\n", "phase")
        self._lean_auto = True
        self.after(300, self.start)
        return True
