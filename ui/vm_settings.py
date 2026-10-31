"""Settings window of a machine, VirtualBox style: categories on the left, one page on the right, OK / Cancel.

General | System | Display | Storage | Network | Shared Folders | User Interface

OK saves the machine (name, project, mode, start command and every setting). When the machine is the one loaded in the App Preview
panel and nothing is running, the panel takes the settings over at once; a running machine keeps them for its next start.
"""
from __future__ import annotations

import shlex
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, messagebox, ttk
from typing import Dict

from app_preview import BACKENDS, custom_entry
from app_preview.vmstore import KINDS, MODES, clean_settings
from plugins.build_execution import quickprep, storage
from ui import theme
from ui.screen_tools import FPS_CHOICES, SIZE_PRESETS, SNAPSHOTS, VIEW_MODES, fps_text, parse_size, view_label

PAGES = (("General", "⚙"), ("System", "▤"), ("Display", "▭"), ("Storage", "⛁"), ("Network", "⇄"), ("Shared Folders", "❐"), ("User Interface", "☰"))


class VMSettingsDialog(tk.Toplevel):
    def __init__(self, host, mid: str, page: str = "General"):
        super().__init__(host.winfo_toplevel())
        self.host, self.mid = host, mid
        self.m = host.vm_store.machines[mid]
        self.title(f"{self.m.name} - Settings")
        self.geometry("760x560")
        self.minsize(640, 460)
        s = self.m.settings
        self.v: Dict[str, tk.Variable] = {
            "name": tk.StringVar(value=self.m.name), "source": tk.StringVar(value=self.m.source), "mode": tk.StringVar(value=self.m.mode),
            "command": tk.StringVar(value=self.m.command), "kind": tk.StringVar(value=self.m.kind),
            "backend": tk.StringVar(value=s["backend"]), "custom_exe": tk.StringVar(value=s["custom_exe"]), "image": tk.StringVar(value=s["image"]),
            "timeout_s": tk.IntVar(value=s["timeout_s"]), "memory_mb": tk.IntVar(value=s["memory_mb"]), "cpus": tk.DoubleVar(value=s["cpus"]),
            "pids": tk.IntVar(value=s["pids"]), "install": tk.BooleanVar(value=s["install"]), "link_output": tk.BooleanVar(value=s["link_output"]),
            "stop_entry": tk.BooleanVar(value=s["stop_entry"]), "screen": tk.StringVar(value=s["screen"]), "fps": tk.StringVar(value=fps_text(s["fps"])),
            "view": tk.StringVar(value=view_label(s["view"])), "auto_snapshot_end": tk.BooleanVar(value=s["auto_snapshot_end"]),
            "max_workspace_mb": tk.IntVar(value=s["max_workspace_mb"]), "snap_dir": tk.StringVar(value=str(SNAPSHOTS.folder)),
        }
        self.frames: Dict[str, ttk.Frame] = {}
        top = ttk.Frame(self)
        top.pack(fill="both", expand=True)
        self.cats = tk.Listbox(top, width=18, exportselection=False, activestyle="none", font=("TkDefaultFont", 10))
        self.cats.pack(side="left", fill="y", padx=(6, 0), pady=6)
        for name, ic in PAGES:
            self.cats.insert("end", f" {ic}  {name}")
        self.area = ttk.Frame(top, padding=10)
        self.area.pack(side="left", fill="both", expand=True)
        for name, _ic in PAGES:
            f = ttk.Frame(self.area)
            getattr(self, "_page_" + name.lower().replace(" ", "_"))(f)
            self.frames[name] = f
        self.cats.bind("<<ListboxSelect>>", lambda _e: self._select())
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.msg = tk.StringVar()
        ttk.Label(bar, textvariable=self.msg, style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="OK", command=self.ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.show_page(page)
        try:
            theme.restyle_tk_widgets(self, getattr(host, "_c", theme.LIGHT))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ navigation
    def show_page(self, name: str) -> None:
        names = [p[0] for p in PAGES]
        name = name if name in names else "General"
        self.cats.selection_clear(0, "end")
        self.cats.selection_set(names.index(name))
        self._select()

    def _select(self) -> None:
        sel = self.cats.curselection()
        if not sel:
            return
        name = PAGES[sel[0]][0]
        for n, f in self.frames.items():
            f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)

    @staticmethod
    def _row(parent, r: int, label: str, widget_factory, hint: str = ""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        w = widget_factory(parent)
        w.grid(row=r, column=1, sticky="we", pady=3)
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel", wraplength=380, justify="left").grid(row=r + 1, column=1, sticky="w")
        parent.columnconfigure(1, weight=1)
        return w

    # ------------------------------------------------------------------ pages
    def _page_general(self, f) -> None:
        v = self.v
        self._row(f, 0, "Name:", lambda p: ttk.Entry(p, textvariable=v["name"]))
        self.desc = self._row(f, 1, "Description:", lambda p: tk.Text(p, height=3, wrap="word"))
        self.desc.insert("1.0", self.m.description)

        def project(p):
            r = ttk.Frame(p)
            ttk.Entry(r, textvariable=v["source"]).pack(side="left", fill="x", expand=True)
            ttk.Button(r, text="ZIP...", command=lambda: self._pick(False)).pack(side="left", padx=3)
            ttk.Button(r, text="Folder...", command=lambda: self._pick(True)).pack(side="left")
            return r
        self._row(f, 2, "Project:", project)

        def mode(p):
            r = ttk.Frame(p)
            for m in MODES:
                ttk.Radiobutton(r, text=m.title(), value=m, variable=v["mode"]).pack(side="left", padx=(0, 8))
            return r
        self._row(f, 3, "Start mode:", mode)
        self.cmd_box = self._row(f, 4, "Start command:", lambda p: ttk.Combobox(p, textvariable=v["command"]),
                                 "Runs inside the container only (never through a host shell). Pick one the project scan found, or type your own.")
        self._fill_discovered()
        self.cmd_box.bind("<<ComboboxSelected>>", lambda _e: self._discovered_picked())
        self._row(f, 6, "Kind:", lambda p: ttk.Combobox(p, textvariable=v["kind"], values=list(KINDS), state="readonly", width=10))
        self._row(f, 7, "Container runtime:", lambda p: ttk.Combobox(p, textvariable=v["backend"], values=list(BACKENDS), state="readonly", width=10))
        self._row(f, 8, "Custom runtime program:", lambda p: ttk.Entry(p, textvariable=v["custom_exe"]), "Only used when the runtime is 'custom' (nerdctl, finch, lima ...).")
        self._row(f, 10, "Image override:", lambda p: ttk.Entry(p, textvariable=v["image"]), "Empty = the image chosen for the project's language. Images are never pulled automatically.")

    def _fill_discovered(self) -> None:
        h = self.host
        self._found = {}
        if h.source and h.source == self.m.source:
            for e in h.entries:
                self._found[h._title(e)] = e
        self.cmd_box["values"] = [t for t in self._found] + ([self.v["command"].get()] if self.v["command"].get() and self.v["command"].get() not in self._found else [])

    def _discovered_picked(self) -> None:
        e = self._found.get(self.v["command"].get())
        if e is not None:
            self._picked_entry = e
            self.v["command"].set(" ".join(e.argv))
            self.v["kind"].set(e.kind)

    _picked_entry = None

    def _page_system(self, f) -> None:
        v = self.v
        self._row(f, 0, "Time limit (s):", lambda p: ttk.Spinbox(p, from_=10, to=86400, textvariable=v["timeout_s"], width=8), "The run is stopped when it takes longer.")
        self._row(f, 2, "Memory (MB):", lambda p: ttk.Spinbox(p, from_=128, to=65536, increment=128, textvariable=v["memory_mb"], width=8),
                  "Out-of-memory kills (exit 137) are repeated once with double memory by Run now.")
        self._row(f, 4, "Processors:", lambda p: ttk.Spinbox(p, from_=0.25, to=32, increment=0.25, textvariable=v["cpus"], width=8))
        self._row(f, 5, "Processes (max):", lambda p: ttk.Spinbox(p, from_=16, to=8192, increment=16, textvariable=v["pids"], width=8))
        ttk.Checkbutton(f, text="Install requirements / npm packages first (needs network)", variable=v["install"]).grid(row=6, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Link output to code (Python)", variable=v["link_output"]).grid(row=7, column=1, sticky="w")
        ttk.Checkbutton(f, text="Debug: stop at the first line", variable=v["stop_entry"]).grid(row=8, column=1, sticky="w")

    def _page_display(self, f) -> None:
        v = self.v
        self._row(f, 0, "Screen size:", lambda p: ttk.Combobox(p, textvariable=v["screen"], values=list(SIZE_PRESETS), width=12),
                  "WIDTHxHEIGHT from 320x240 to 3840x2160; used at the next start.")
        self._row(f, 2, "Snapshot rate:", lambda p: ttk.Combobox(p, textvariable=v["fps"], values=list(FPS_CHOICES), state="readonly", width=10))
        self._row(f, 3, "Scale:", lambda p: ttk.Combobox(p, textvariable=v["view"], values=list(VIEW_MODES), state="readonly", width=10))
        ttk.Checkbutton(f, text="Take a snapshot automatically when the run ends (the last 10 are kept)", variable=v["auto_snapshot_end"]).grid(row=4, column=1, sticky="w", pady=(10, 0))

    def _page_storage(self, f) -> None:
        cfg = storage.load()
        self._row(f, 0, "Largest project copied in (MB):", lambda p: ttk.Spinbox(p, from_=10, to=4096, textvariable=self.v["max_workspace_mb"], width=8))
        rows = (("Scratch folder:", cfg["scratch_dir"] or "(system temp folder)"), ("Delete the copy after a run:", "yes" if cfg["auto_clean"] else "no"),
                ("Lean copy:", "on - only what the script needs is copied" if cfg.get("lean_copy", True) else "off"))
        for i, (a, b) in enumerate(rows, start=2):
            ttk.Label(f, text=a).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=2)
            ttk.Label(f, text=b, style="Muted.TLabel").grid(row=i, column=1, sticky="w")
        ttk.Button(f, text="Storage & clean-up...", command=self.host.open_storage).grid(row=6, column=1, sticky="w", pady=10)

    def _page_network(self, f) -> None:
        ttk.Label(f, text="Network adapter", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Every run starts with the network disabled. It is switched on only for the dependency-install step "
                          "(and web apps are published on 127.0.0.1 only). The approval window shows the network step before anything runs.",
                  wraplength=480, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Checkbutton(f, text="Attach the network for the install step (needs network)", variable=self.v["install"]).grid(row=2, column=1, sticky="w")
        port = self.m.entry.get("port")
        ttk.Label(f, text=f"Web app container port: {port}  ->  published on 127.0.0.1 (free port chosen at start)" if port else "This start command publishes no port.",
                  style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=8)

    def _page_shared_folders(self, f) -> None:
        ttk.Label(f, text="Project files copied into every run (besides the script and what it imports):").pack(anchor="w")
        fr = ttk.Frame(f)
        fr.pack(fill="both", expand=True, pady=6)
        self.shared = tk.Listbox(fr, activestyle="none")
        sb = ttk.Scrollbar(fr, command=self.shared.yview)
        self.shared.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.shared.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add...", command=self._add_shared).pack(side="left")
        ttk.Button(bar, text="Remove all", command=self._clear_shared).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh", command=self._fill_shared).pack(side="left")
        self._fill_shared()

    def _fill_shared(self) -> None:
        self.shared.delete(0, "end")
        for r in (quickprep.includes_for(self.m.source) if self.m.source else []):
            self.shared.insert("end", r)
        if not self.shared.size():
            self.shared.insert("end", "(none - only what the program needs is copied)")

    def _add_shared(self) -> None:
        if self.host.source != self.m.source:
            messagebox.showinfo("Shared Folders", "The file list comes from the project loaded in App Preview. Load this machine first (Start it, or restore a snapshot), then add files.", parent=self)
            return
        self.host.open_includes()

    def _clear_shared(self) -> None:
        if self.m.source:
            quickprep.clear_includes(self.m.source)
        self._fill_shared()

    def _page_user_interface(self, f) -> None:
        h = self.host

        def folder(p):
            r = ttk.Frame(p)
            ttk.Entry(r, textvariable=self.v["snap_dir"]).pack(side="left", fill="x", expand=True)
            ttk.Button(r, text="Browse...", command=self._pick_snap_dir).pack(side="left", padx=3)
            return r
        self._row(f, 0, "Picture snapshot folder:", folder, "Where 'Quick save' and automatic PNG pictures go (this setting is shared by all machines).")
        for i, (text, var) in enumerate((("Save a PNG picture when a run ends", h.auto_snap_var), ("Clear leftover copies before / after a run", h.auto_clean_var),
                                         ("Start when a command is chosen", h.auto_go_var)), start=2):
            ttk.Checkbutton(f, text=text, variable=var).grid(row=i, column=1, sticky="w", pady=2)

    # ------------------------------------------------------------------ actions
    def _pick(self, folder: bool) -> None:
        p = filedialog.askdirectory(parent=self) if folder else filedialog.askopenfilename(parent=self, filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")])
        if p:
            self.v["source"].set(p)
            self._fill_discovered()

    def _pick_snap_dir(self) -> None:
        d = filedialog.askdirectory(parent=self)
        if d:
            self.v["snap_dir"].set(d)

    def ok(self) -> None:
        v = self.v
        sz = parse_size(v["screen"].get())
        if sz is None:
            self.msg.set("Screen size must look like 1024x768 (320x240 .. 3840x2160).")
            self.show_page("Display")
            return
        try:
            argv = shlex.split(v["command"].get().strip())
        except ValueError as exc:
            self.msg.set(f"Start command: {exc}")
            self.show_page("General")
            return
        try:
            raw = {k: v[k].get() for k in ("timeout_s", "memory_mb", "cpus", "pids", "max_workspace_mb")}
        except tk.TclError:
            self.msg.set("A number is not valid (System / Storage page).")
            self.show_page("System")
            return
        m = self.m
        m.name = v["name"].get().strip() or m.name
        m.description = self.desc.get("1.0", "end-1c").strip()
        m.source, m.mode = v["source"].get().strip(), v["mode"].get()
        if argv != m.argv:                                   # a changed command: rebuild the entry (a kept command keeps port, env ...)
            ent = self._picked_entry if self._picked_entry is not None and argv == list(self._picked_entry.argv) else None
            try:
                m.entry = asdict(ent) if ent is not None else (asdict(custom_entry(argv, kind=v["kind"].get())) if argv else {})
            except ValueError as exc:
                self.msg.set(str(exc))
                self.show_page("General")
                return
        elif m.entry:
            m.entry["kind"] = v["kind"].get()
        m.settings = clean_settings(dict(raw, screen=f"{sz[0]}x{sz[1]}", fps=v["fps"].get(), view=VIEW_MODES.get(v["view"].get(), "fit"),
                                         install=v["install"].get(), link_output=v["link_output"].get(), stop_entry=v["stop_entry"].get(),
                                         backend=v["backend"].get(), custom_exe=v["custom_exe"].get(), image=v["image"].get(),
                                         auto_snapshot_end=v["auto_snapshot_end"].get()))
        self.host.vm_store.update(m)
        d = v["snap_dir"].get().strip()
        if d and d != str(SNAPSHOTS.folder):
            SNAPSHOTS.set_custom(d)
            self.host._save_pref("snap_dir", d)
        h = self.host
        for key, var in (("auto_snap", h.auto_snap_var), ("auto_clean", h.auto_clean_var), ("auto_go", h.auto_go_var)):
            h._save_pref(key, bool(var.get()))
        if self.mid in (h.vm_current_id, h.vm_pending_id) and not h.vm_live_state():
            h.vm_load_machine(m)
        h.status_var.set(f"Settings of '{m.name}' saved" + (" - they apply at its next start." if h.vm_live_state(self.mid) else "."))
        self.destroy()
