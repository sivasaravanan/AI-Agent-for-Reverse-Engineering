"""Storage & clean-up window (Build & Test and App Preview): where the disposable project copy lives, how big things may be,
what this application left behind (scratch folders, containers, the GUI image) and what the container runtime stores.

Everything slow (``docker ps``, ``system df``, deleting folders) runs on a worker thread; results return through a queue.
Clean-up only removes objects this application created; pruning dangling images / the build cache is global to the container
runtime and is a separate, confirmed option. Docker's own storage location is shown but never changed from here.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, Optional

from app_preview.models import PreviewOptions
from app_preview.runtimes import find_runtime
from plugins.build_execution import cleanup, storage

MOVE_HINT = ("To move Docker's own storage: Docker Desktop > Settings > Resources > Advanced > 'Disk image location' (Windows/macOS), or "
             "'data-root' in daemon.json (Linux); Podman: the machine/graphroot setting. This application only controls where its "
             "disposable project copy is stored and how big things may be.")


class StorageDialog(tk.Toplevel):
    def __init__(self, parent, options: Optional[PreviewOptions] = None, runner: Optional[Callable] = None, on_saved: Optional[Callable] = None):
        super().__init__(parent)
        self.title("Storage & clean-up")
        self.geometry("900x780")
        self.options = options or PreviewOptions()
        self.on_saved = on_saved
        self._run = runner or storage._run
        self.q: "queue.Queue" = queue.Queue()
        self.exe: Optional[str] = None
        self._inv: Dict = {}
        self._build()
        self.bind("<Destroy>", lambda e: None, add="+")
        self.after(80, self._poll)
        self.refresh()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        cur = storage.load()
        box = ttk.LabelFrame(self, text="Storage settings")
        box.pack(fill="x", padx=8, pady=6)
        self.scratch = tk.StringVar(value=cur["scratch_dir"])
        self.auto = tk.BooleanVar(value=cur["auto_clean"])
        self.keep = tk.BooleanVar(value=cur["keep_workspace"])
        self.size = tk.IntVar(value=cur["max_workspace_mb"])
        self.tmp = tk.IntVar(value=cur["tmp_mb"])
        ttk.Label(box, text="Scratch folder (disposable project copy):").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(box, textvariable=self.scratch, width=58).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(box, text="Browse...", command=self._browse).grid(row=0, column=2, padx=4)
        ttk.Label(box, text="(empty = system temp folder; pick a drive with enough space)", style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="Delete the project copy after every run (recommended)", variable=self.auto).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="Keep the project copy after a run (for inspection; remove it below)", variable=self.keep).grid(row=3, column=1, sticky="w", padx=4)
        sp = ttk.Frame(box)
        sp.grid(row=4, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(sp, text="Largest project copied in (MB):").pack(side="left")
        ttk.Spinbox(sp, from_=10, to=4096, textvariable=self.size, width=7).pack(side="left", padx=4)
        ttk.Label(sp, text="Container /tmp size (MB):").pack(side="left", padx=(12, 0))
        ttk.Spinbox(sp, from_=64, to=8192, textvariable=self.tmp, width=7).pack(side="left", padx=4)
        ttk.Button(box, text="Save settings", command=self.save).grid(row=4, column=2, padx=4)
        # added: Quick run / trimmed copies / automatic clear-up
        self.file_mb = tk.IntVar(value=cur["max_file_mb"])
        self.narrow_mb = tk.IntVar(value=cur["quick_narrow_mb"])
        self.trim = tk.BooleanVar(value=cur["trim_large"])
        self.clear_run = tk.BooleanVar(value=cur["clear_after_run"])
        self.forget_run = tk.BooleanVar(value=cur["forget_after_run"])
        self.pull = tk.BooleanVar(value=cur["auto_pull"])
        self.lean = tk.BooleanVar(value=cur["lean_copy"])
        sp2 = ttk.Frame(box)
        sp2.grid(row=5, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(sp2, text="Largest data file copied in (MB):").pack(side="left")
        ttk.Spinbox(sp2, from_=1, to=2048, textvariable=self.file_mb, width=7).pack(side="left", padx=4)
        ttk.Label(sp2, text="Copy only the script + its imports above (MB):").pack(side="left", padx=(12, 0))
        ttk.Spinbox(sp2, from_=10, to=4096, textvariable=self.narrow_mb, width=7).pack(side="left", padx=4)
        ttk.Checkbutton(box, text="Trim oversized projects instead of refusing (big files / folders are left out and listed)",
                        variable=self.trim).grid(row=6, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="After every run also clear every leftover copy and stopped container this tool made (recommended)",
                        variable=self.clear_run).grid(row=7, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="After every run forget the selected project (only the selection; your files are not touched)",
                        variable=self.forget_run).grid(row=8, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="Quick run downloads a missing container image without asking",
                        variable=self.pull).grid(row=9, column=1, sticky="w", padx=4)
        ttk.Checkbutton(box, text="Lean copy (recommended): a script run copies ONLY the files it needs - never the whole folder / archive",
                        variable=self.lean).grid(row=10, column=1, sticky="w", padx=4)
        box.columnconfigure(1, weight=1)

        inv = ttk.LabelFrame(self, text="What is stored now")
        inv.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(inv, columns=("what", "size", "info"), show="tree headings", height=9)
        for c, w, t in (("#0", 130, "Kind"), ("what", 380, "Item"), ("size", 90, "Size"), ("info", 220, "State")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(inv, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.runtime_var = tk.StringVar(value="Looking at the container runtime...")
        ttk.Label(self, textvariable=self.runtime_var, wraplength=820).pack(fill="x", padx=10)
        self.df = tk.Text(self, height=5, wrap="none", state="disabled")
        self.df.pack(fill="x", padx=8, pady=2)

        act = ttk.LabelFrame(self, text="Clean up")
        act.pack(fill="x", padx=8, pady=6)
        self.c_scratch = tk.BooleanVar(value=True)
        self.c_cont = tk.BooleanVar(value=True)
        self.c_run = tk.BooleanVar(value=False)
        self.c_gui = tk.BooleanVar(value=False)
        self.c_dang = tk.BooleanVar(value=False)
        self.c_cache = tk.BooleanVar(value=False)
        items = [("Leftover project copies (be_ws_* / be_pv_* folders)", self.c_scratch),
                 ("Stopped containers made by this application (be-*)", self.c_cont),
                 ("... also containers that are still running (stops them)", self.c_run),
                 ("GUI preview image (ase-preview-gui; rebuilt on demand)", self.c_gui),
                 ("Dangling images - affects ALL projects in the container runtime", self.c_dang),
                 ("Build cache - affects ALL projects in the container runtime", self.c_cache)]
        for i, (t, v) in enumerate(items):
            ttk.Checkbutton(act, text=t, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=6, pady=1)
        bar = ttk.Frame(act)
        bar.grid(row=3, column=0, columnspan=2, sticky="we", pady=4)
        ttk.Button(bar, text="Clean selected", command=self.clean).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Clear everything now", command=self.clear_all).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right", padx=6)
        self.msg = tk.StringVar()
        ttk.Label(self, textvariable=self.msg, wraplength=820, foreground="#1d6b3a").pack(fill="x", padx=10)
        ttk.Label(self, text=MOVE_HINT, style="Muted.TLabel", wraplength=820).pack(fill="x", padx=10, pady=(0, 8))

    # ------------------------------------------------------------------ actions
    def _browse(self) -> None:
        p = filedialog.askdirectory(title="Scratch folder", parent=self)
        if p:
            self.scratch.set(p)

    def save(self) -> None:
        try:
            storage.save(scratch_dir=self.scratch.get().strip(), auto_clean=bool(self.auto.get()), keep_workspace=bool(self.keep.get()),
                         max_workspace_mb=int(self.size.get()), tmp_mb=int(self.tmp.get()),
                         max_file_mb=int(self.file_mb.get()), quick_narrow_mb=int(self.narrow_mb.get()), trim_large=bool(self.trim.get()),
                         clear_after_run=bool(self.clear_run.get()), forget_after_run=bool(self.forget_run.get()),
                         auto_pull=bool(self.pull.get()), lean_copy=bool(self.lean.get()))
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Storage", str(exc) or "Enter whole numbers for the sizes.", parent=self)
            return
        if self.on_saved:
            self.on_saved(storage.load())
        self.refresh()
        self.msg.set("Saved. New runs use these settings (sizes are part of the plan you approve).")

    def refresh(self, keep_msg: bool = False) -> None:
        if not keep_msg:
            self.msg.set("")
        name, exe = find_runtime(self.options)
        self.exe = exe

        def work() -> None:
            scratch = cleanup.inventory()                       # container-run copies AND analysis copies
            inv = storage.runtime_inventory(exe, self._run) if exe else None
            self.q.put(("inv", scratch, inv, name))
        threading.Thread(target=work, daemon=True).start()

    def clean(self) -> None:
        glob_ops = bool(self.c_dang.get() or self.c_cache.get())
        if glob_ops and not messagebox.askyesno(
                "Storage", "Dangling images / build cache are shared by ALL projects in your container runtime (not only this "
                "application). They are re-created when needed, but the next build may be slower.\n\nContinue?", parent=self):
            return
        if self.c_run.get() and not messagebox.askyesno("Storage", "This stops containers that are still running. Continue?", parent=self):
            return
        opts = dict(scratch=bool(self.c_scratch.get()), containers=bool(self.c_cont.get()), running=bool(self.c_run.get()),
                    gui=bool(self.c_gui.get()), dang=bool(self.c_dang.get()), cache=bool(self.c_cache.get()))
        exe = self.exe
        self.msg.set("Cleaning...")

        def work() -> None:
            report = []
            if opts["scratch"]:
                gone = cleanup.remove()
                report.append(f"removed {len(gone)} leftover project cop{'y' if len(gone) == 1 else 'ies'}")
            if exe and any((opts["containers"], opts["gui"], opts["dang"], opts["cache"])):
                report += storage.clean_runtime(exe, containers=opts["containers"], include_running=opts["running"], gui_image=opts["gui"],
                                                dangling_images=opts["dang"], build_cache=opts["cache"], run=self._run)
            elif not exe and any((opts["containers"], opts["gui"], opts["dang"], opts["cache"])):
                report.append("no container runtime found, only local folders were cleaned")
            self.q.put(("cleaned", report))
        threading.Thread(target=work, daemon=True).start()

    def clear_all(self) -> None:
        """Every copy and container of ours at once, any age (running containers only when that box is ticked)."""
        if not messagebox.askyesno("Storage", "Remove every leftover project copy and stopped container made by this tool?\n\nYour own "
                                   "folders, archives and files are not touched.", parent=self):
            return
        exe, inc = self.exe, bool(self.c_run.get())
        self.msg.set("Clearing...")

        def work() -> None:
            self.q.put(("cleaned", cleanup.clear_everything(exe, include_running=inc, run=self._run)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ events
    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "inv":
                    self._show(ev[1], ev[2], ev[3])
                elif ev[0] == "cleaned":
                    self.msg.set("Done: " + "; ".join(ev[1]) + ".")
                    self.refresh(keep_msg=True)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _show(self, scratch, inv, backend) -> None:
        self.tree.delete(*self.tree.get_children())
        total = 0
        for s in scratch:
            total += s["bytes"]
            self.tree.insert("", "end", text="Project copy", values=(s["path"], storage.human(s["bytes"]),
                                                                       f"{s.get('kind', '')}, {s['age_s'] / 60:.0f} min old"))
        if not scratch:
            self.tree.insert("", "end", text="Project copy", values=("none left behind", "", ""))
        if inv:
            for c in inv["containers"]:
                self.tree.insert("", "end", text="Container", values=(c["name"], "", f"{c['state']} - {c['status']}"))
            if inv["gui_image"]:
                self.tree.insert("", "end", text="Image", values=(inv["gui_image"]["tag"], inv["gui_image"]["size"], "GUI preview image"))
            self.tree.insert("", "end", text="Images", values=(f"{inv['dangling']} dangling image(s)", "", "can be pruned (global)"))
            self.runtime_var.set(f"Container runtime: {backend}" + (f"   storage folder: {inv['root']}" if inv["root"] else "")
                                 + f"   |   leftover project copies: {storage.human(total)}")
            self.df.config(state="normal")
            self.df.delete("1.0", "end")
            self.df.insert("1.0", inv["df"] or "(the runtime did not report its storage use)")
            self.df.config(state="disabled")
        else:
            self.runtime_var.set("No container runtime found (docker / podman / nerdctl / finch): only local folders are shown."
                                 f"   |   leftover project copies: {storage.human(total)}")


def open_storage_dialog(parent, options: Optional[PreviewOptions] = None, **kw) -> StorageDialog:
    dlg = StorageDialog(parent, options, **kw)
    try:
        dlg.transient(parent.winfo_toplevel())
    except tk.TclError:
        pass
    return dlg
