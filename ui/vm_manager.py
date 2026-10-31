"""Manager window, VirtualBox style: a list of machines on the left, a toolbar (New, Settings, Remove, Start, Stop, Show, Snapshots)
and, for the selected machine, three tabs - Details (click a heading to open that Settings page), Snapshots, Log - beside a live Preview.

A machine is created by running a program in App Preview, or here with New. App Preview runs one program at a time, so one machine
can be running; Start asks for approval of the exact plan as always.
"""
from __future__ import annotations

import shlex
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Dict, List

from app_preview import custom_entry
from app_preview.vmstore import DEFAULT_SETTINGS, KINDS, MODES, human_size, machine_state, when
from plugins.build_execution import quickprep
from ui import theme
from ui.screen_tools import SNAPSHOTS, FrameView, fps_text, view_label
from ui.vm_snapshots import SnapshotsPanel, _Modal, _style

SECTION_PAGE = {"General": "General", "System": "System", "Display": "Display", "Storage": "Storage", "Network": "Network",
                "Shared Folders": "Shared Folders", "Description": "General"}
STATE_ICON = {"Running": "▶", "Starting": "…", "Paused": "⏸", "Aborted": "✖", "Powered Off": "⏻"}


def details_sections(m, snaps: int, snap_bytes: int) -> List:
    s = m.settings
    shared = quickprep.includes_for(m.source) if m.source else []
    return [
        ("General", [("Name", m.name), ("Project", m.source or "(none)"), ("Mode", m.mode.title()), ("Start command", m.command or "(not set yet)"),
                     ("Kind", m.kind), ("Runs", str(m.runs)), ("Last started", when(m.last_started)), ("Last result", m.last_status or "-")]),
        ("System", [("Time limit", f"{s['timeout_s']} s"), ("Memory", f"{s['memory_mb']} MB"), ("Processors", f"{s['cpus']:g}"),
                    ("Processes", str(s["pids"])), ("Install step", "yes (needs network)" if s["install"] else "no")]),
        ("Display", [("Screen", s["screen"]), ("Snapshot rate", fps_text(s["fps"])), ("Scale", view_label(s["view"])),
                     ("Auto snapshot at end", "on" if s["auto_snapshot_end"] else "off")]),
        ("Storage", [("Largest project copy", f"{s['max_workspace_mb']} MB"), ("Snapshots", f"{snaps} ({human_size(snap_bytes)} on disk)"),
                     ("Runtime", s["backend"] + (f" ({s['custom_exe']})" if s["custom_exe"] else "")), ("Image", s["image"] or "(automatic)")]),
        ("Network", [("Adapter", "attached for the install step only" if s["install"] else "disabled")]),
        ("Shared Folders", [("Included", f"{len(shared)} item(s)")] + [("", r) for r in shared[:6]]),
        ("Description", [("", m.description or "(none)")]),
    ]


class NewMachineDialog(_Modal):
    def __init__(self, host):
        super().__init__(host, "New Machine", "560x330")
        self.host = host
        v = self.v = {"name": tk.StringVar(), "source": tk.StringVar(value=host.source or ""), "mode": tk.StringVar(value="run"),
                      "command": tk.StringVar(), "kind": tk.StringVar(value="console")}
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Name:").grid(row=0, column=0, sticky="e", pady=3)
        ttk.Entry(f, textvariable=v["name"]).grid(row=0, column=1, sticky="we", pady=3)
        ttk.Label(f, text="Project:").grid(row=1, column=0, sticky="e", pady=3)
        r = ttk.Frame(f)
        r.grid(row=1, column=1, sticky="we")
        ttk.Entry(r, textvariable=v["source"]).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="ZIP...", command=lambda: self._pick(False)).pack(side="left", padx=3)
        ttk.Button(r, text="Folder...", command=lambda: self._pick(True)).pack(side="left")
        ttk.Label(f, text="Mode:").grid(row=2, column=0, sticky="e", pady=3)
        r = ttk.Frame(f)
        r.grid(row=2, column=1, sticky="w")
        for m in MODES:
            ttk.Radiobutton(r, text=m.title(), value=m, variable=v["mode"]).pack(side="left", padx=(0, 8))
        ttk.Label(f, text="Start command:").grid(row=3, column=0, sticky="e", pady=3)
        self.found: Dict = {}
        if host.source:
            self.found = {host._title(e): e for e in host.entries}
        self.cmd = ttk.Combobox(f, textvariable=v["command"], values=list(self.found))
        self.cmd.grid(row=3, column=1, sticky="we", pady=3)
        self.cmd.bind("<<ComboboxSelected>>", self._found)
        ttk.Label(f, text="e.g.  python main.py   (runs inside the container only)", style="Muted.TLabel").grid(row=4, column=1, sticky="w")
        ttk.Label(f, text="Kind:").grid(row=5, column=0, sticky="e", pady=3)
        ttk.Combobox(f, textvariable=v["kind"], values=list(KINDS), state="readonly", width=10).grid(row=5, column=1, sticky="w")
        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#d64545").grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        bar = ttk.Frame(f)
        bar.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Create", command=self.ok).pack(side="right", padx=6)
        self._entry = None
        _style(self, host)

    def _pick(self, folder: bool) -> None:
        p = filedialog.askdirectory(parent=self) if folder else filedialog.askopenfilename(parent=self, filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")])
        if p:
            self.v["source"].set(p)
            if not self.v["name"].get():
                self.v["name"].set(Path(p).stem or Path(p).name)

    def _found(self, _e=None) -> None:
        e = self.found.get(self.v["command"].get())
        if e is not None:
            self._entry = e
            self.v["command"].set(" ".join(e.argv))
            self.v["kind"].set(e.kind)
            if not self.v["name"].get():
                self.v["name"].set(e.title[:60])

    def ok(self) -> None:
        v = self.v
        try:
            argv = shlex.split(v["command"].get().strip())
        except ValueError as exc:
            self.msg.set(str(exc))
            return
        if not v["source"].get().strip() or not Path(v["source"].get().strip()).exists():
            self.msg.set("Choose an existing project folder or ZIP.")
            return
        if not argv:
            self.msg.set("Type a start command or pick one from the list.")
            return
        try:
            ent = self._entry if self._entry is not None and list(self._entry.argv) == argv else custom_entry(argv, kind=v["kind"].get())
        except ValueError as exc:
            self.msg.set(str(exc))
            return
        self.result = (v["name"].get().strip() or ent.title, v["source"].get().strip(), v["mode"].get(), asdict(ent))
        self.destroy()


class VMManagerWindow(tk.Toplevel):
    def __init__(self, host):
        super().__init__(host.winfo_toplevel())
        self.host = host
        self.store = host.vm_store
        self.sel = host.vm_current_id if host.vm_current_id in self.store.machines else ""
        self._sig = None
        self._list_sig = None
        self.title("App Preview - Virtual Machine Manager")
        geo = str(host._prefs.get("vm_manager_geometry", ""))
        self.geometry(geo if geo and geo.replace("x", "").replace("+", "").replace("-", "").isdigit() else "1040x640")
        self.minsize(760, 440)
        self._menu()
        self._toolbar()
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        self.filter = tk.StringVar()
        fe = ttk.Entry(left, textvariable=self.filter)
        fe.pack(fill="x", padx=2, pady=2)
        fe.bind("<KeyRelease>", lambda _e: self.fill(force=True))
        self.tree = ttk.Treeview(left, columns=("state",), selectmode="browse", height=12)
        self.tree.heading("#0", text="Machines  (type to filter)")
        self.tree.heading("state", text="State")
        self.tree.column("#0", width=230)
        self.tree.column("state", width=100)
        self.tree.tag_configure("run", foreground="#1f9d57", font=("TkDefaultFont", 9, "bold"))
        self.tree.tag_configure("bad", foreground="#d64545")
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", lambda _e: self.start_or_show())
        self.tree.bind("<Return>", lambda _e: self.start_or_show())
        self.tree.bind("<Delete>", lambda _e: self.remove())
        self.tree.bind("<F2>", lambda _e: self.rename())
        self.tree.bind("<Button-3>", self._context)
        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        self.head = tk.StringVar()
        ttk.Label(right, textvariable=self.head, font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=6, pady=(2, 0))
        self.sub = tk.StringVar()
        ttk.Label(right, textvariable=self.sub, style="Muted.TLabel").pack(anchor="w", padx=6)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True, pady=4)
        det = ttk.Frame(self.nb)
        self.nb.add(det, text="Details")
        top = ttk.Frame(det)
        top.pack(fill="both", expand=True)
        prev = ttk.LabelFrame(top, text="Preview")
        prev.pack(side="right", fill="y", padx=4, pady=2)
        self.preview = FrameView(prev, placeholder="Powered Off", width=300, height=190)
        self.preview.pack(padx=4, pady=4)
        self.preview.canvas.configure(width=300, height=190)
        self.preview.canvas.bind("<Button-1>", lambda _e: self.show_window())
        self.text = tk.Text(top, wrap="word", state="disabled", cursor="arrow", font="TkDefaultFont")
        ts = ttk.Scrollbar(top, command=self.text.yview)
        self.text.configure(yscrollcommand=ts.set)
        ts.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.text.tag_configure("h", font=("TkDefaultFont", 10, "bold"), foreground="#2f6fed", spacing1=8)
        self.text.tag_configure("k", foreground="#6b7691")
        self.snaps = SnapshotsPanel(self.nb, host, self.sel)
        self.nb.add(self.snaps, text="Snapshots")
        lg = ttk.Frame(self.nb)
        self.nb.add(lg, text="Log")
        self.log = tk.Text(lg, wrap="none", state="disabled", font="TkFixedFont")
        ls = ttk.Scrollbar(lg, command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.snaps.refresh() if self.nb.index("current") == 1 else None)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", padding=(8, 2), relief="groove").pack(fill="x", side="bottom")
        self.bind("<Control-n>", lambda _e: self.new())
        self.bind("<Control-s>", lambda _e: self.settings())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(80, lambda: pane.sashpos(0, 340) if self.winfo_exists() else None)
        self.fill(force=True)
        _style(self, host)
        self.after(500, self._tick)

    # ------------------------------------------------------------------ chrome
    def _menu(self) -> None:
        mb = tk.Menu(self)
        self.config(menu=mb)
        f = tk.Menu(mb, tearoff=False)
        f.add_command(label="New Machine...        Ctrl+N", command=self.new)
        f.add_command(label="Add the Program Selected in App Preview", command=self.add_current)
        f.add_separator()
        f.add_command(label="Open Snapshot Picture Folder", command=lambda: SNAPSHOTS.open_folder())
        f.add_command(label="Open Machines Folder", command=self.open_folder)
        f.add_separator()
        f.add_command(label="Close", command=self.close)
        mb.add_cascade(label="File", menu=f)
        m = tk.Menu(mb, tearoff=False)
        for label, cmd in (("Settings...        Ctrl+S", self.settings), ("Clone...", self.clone), ("Rename...        F2", self.rename), ("Remove...        Del", self.remove),
                           (None, None), ("Start", self.start), ("Power Off", self.stop), ("Show Machine Window", self.show_window),
                           (None, None), ("Take Snapshot...", self.take_snapshot), ("Snapshots", self.show_snapshots)):
            if label is None:
                m.add_separator()
            else:
                m.add_command(label=label, command=cmd)
        mb.add_cascade(label="Machine", menu=m)
        h = tk.Menu(mb, tearoff=False)
        h.add_command(label="About", command=lambda: messagebox.showinfo(
            "Virtual Machine Manager", "Machines are saved ways of running a program in App Preview (project, mode, start command, settings).\n"
            "Runs happen in disposable containers; a snapshot keeps the picture, console, settings and start command.\n"
            "Start asks for approval of the exact plan, as always.", parent=self))
        mb.add_cascade(label="Help", menu=h)

    def _toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self.tb: Dict[str, ttk.Button] = {}
        for key, text, tip, cmd in (("new", "✚ New", "Create a machine (Ctrl+N)", self.new), ("set", "⚙ Settings", "Settings of the selected machine (Ctrl+S)", self.settings),
                                    ("rm", "✖ Remove", "Remove the machine and its snapshots", self.remove), (None, "", "", None),
                                    ("start", "▶ Start", "Start the machine (approval of the exact plan is asked)", self.start_or_show),
                                    ("stop", "■ Stop", "Stop the running machine", self.stop), ("show", "▣ Show", "Open the machine window", self.show_window),
                                    (None, "", "", None), ("snap", "📷 Snapshot", "Take a snapshot of the selected machine", self.take_snapshot),
                                    ("snaps", "⎇ Snapshots", "Show the snapshot tree", self.show_snapshots)):
            if key is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
                continue
            b = ttk.Button(bar, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=2)
            theme.Tooltip(b, tip)
            self.tb[key] = b

    # ------------------------------------------------------------------ list
    def _live(self, mid: str) -> str:
        return self.host.vm_live_state(mid)

    def _state(self, m) -> str:
        return machine_state(m, self._live(m.id))

    def fill(self, force: bool = False) -> None:
        q = self.filter.get().strip().lower()
        ms = [m for m in self.store.sorted_machines() if not q or q in m.name.lower() or q in m.source.lower() or q in m.command.lower()]
        sig = tuple((m.id, m.name) for m in ms)
        if force or sig != self._list_sig:
            self._list_sig = sig
            self.tree.delete(*self.tree.get_children())
            for m in ms:
                self.tree.insert("", "end", iid=m.id, text=f"🖥 {m.name}", values=("",))
            if self.sel in {m.id for m in ms}:
                self.tree.selection_set(self.sel)
            elif ms:
                self.sel = ms[0].id
                self.tree.selection_set(self.sel)
            else:
                self.sel = ""
        for m in ms:
            st = self._state(m)
            tag = ("run",) if st in ("Running", "Starting", "Paused") else (("bad",) if st == "Aborted" else ())
            self.tree.item(m.id, values=(f"{STATE_ICON.get(st, '')} {st}",), tags=tag)
        self.status.set(f"{len(self.store.machines)} machine(s)   |   data: {self.store.base}")

    def _selected(self, _e=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0] != self.sel:
            self.sel = sel[0]
            self._sig = None
            self.snaps.set_machine(self.sel)
        self.update_view()

    @property
    def m(self):
        return self.store.machines.get(self.sel)

    def _context(self, e) -> None:
        row = self.tree.identify_row(e.y)
        if row:
            self.tree.selection_set(row)
            self._selected()
        cm = tk.Menu(self, tearoff=False)
        for label, cmd in (("Start", self.start), ("Power Off", self.stop), ("Show Machine Window", self.show_window), (None, None), ("Settings...", self.settings),
                           ("Snapshots", self.show_snapshots), ("Take Snapshot...", self.take_snapshot), (None, None), ("Clone...", self.clone),
                           ("Rename...", self.rename), ("Remove...", self.remove)):
            cm.add_separator() if label is None else cm.add_command(label=label, command=cmd)
        try:
            cm.tk_popup(e.x_root, e.y_root)
        finally:
            cm.grab_release()

    # ------------------------------------------------------------------ details
    def update_view(self) -> None:
        m = self.m
        live = self._live(self.sel) if m else ""
        for k in ("set", "rm", "start", "stop", "show", "snap", "snaps"):
            self.tb[k].config(state="normal" if m else "disabled")
        if m is None:
            self.head.set("No machines yet")
            self.sub.set("Run a program in App Preview, or press New.")
            self._text(self.text, "")
            self.preview.clear()
            return
        st = machine_state(m, live)
        busy = self.host.vm_live_state() != ""
        self.tb["start"].config(state="normal" if not (live or busy) else "disabled")
        self.tb["stop"].config(state="normal" if live else "disabled")
        self.tb["rm"].config(state="disabled" if live else "normal")
        self.head.set(m.name)
        self.sub.set(f"{st}   |   {m.command or 'no start command'}")
        png = self.host._frame_png if live else self.store.last_png(m.id)
        if png:
            self.preview.show(png)
        else:
            self.preview.clear()
            self.preview.placeholder = st
            self.preview.redraw()
        snaps = self.store.snapshots(m.id)
        sig = (m.id, st, repr(m.settings), m.name, m.description, m.source, m.command, m.runs, m.last_status, len(snaps), len(quickprep.includes_for(m.source) if m.source else []))
        if sig != self._sig:
            self._sig = sig
            self._write_details(m, len(snaps))
        text = self.host.vm_console_text() if m.id == self.host.vm_current_id and (live or self.host.state == "ended") else self.store.last_log(m.id)
        if getattr(self, "_log_text", None) != text:
            self._log_text = text
            self._text(self.log, text or "(no console text stored for this machine yet)", end=True)

    @staticmethod
    def _text(w: tk.Text, text: str, end: bool = False) -> None:
        w.config(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text)
        w.config(state="disabled")
        if end:
            w.see("end")

    def _write_details(self, m, n_snaps: int) -> None:
        w = self.text
        w.config(state="normal")
        w.delete("1.0", "end")
        for tag in [t for t in w.tag_names() if t.startswith("sec:")]:
            w.tag_delete(tag)
        for name, rows in details_sections(m, n_snaps, self.store.disk_usage(m.id)):
            tag = "sec:" + name
            w.insert("end", f"{name}\n", ("h", tag))
            w.tag_bind(tag, "<Button-1>", lambda _e, p=SECTION_PAGE[name]: self.settings(p))
            w.tag_bind(tag, "<Enter>", lambda _e: w.config(cursor="hand2"))
            w.tag_bind(tag, "<Leave>", lambda _e: w.config(cursor="arrow"))
            for k, v in rows:
                if k:
                    w.insert("end", f"   {k}:  ", "k")
                else:
                    w.insert("end", "   ")
                w.insert("end", f"{v}\n")
        w.config(state="disabled")

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.fill()
            self.update_view()
            if self.snaps.mid != self.sel:
                self.snaps.set_machine(self.sel)
        finally:
            if self.winfo_exists():
                self.after(500, self._tick)

    # ------------------------------------------------------------------ commands
    def new(self) -> None:
        got = NewMachineDialog(self.host).run()
        if got:
            name, src, mode, ent = got
            m = self.store.new_machine(name, src, mode, ent, DEFAULT_SETTINGS)
            self.sel = m.id
            self.filter.set("")
            self.fill(force=True)

    def add_current(self) -> None:
        h = self.host
        ent = h._current_entry()
        if not h.source or ent is None:
            messagebox.showinfo("Add machine", "Pick a project and a start command in App Preview first.", parent=self)
            return
        m = self.store.new_machine(ent.title[:60], h.source, h.mode_var.get(), asdict(ent), h.vm_capture_settings())
        self.sel = m.id
        self.fill(force=True)

    def settings(self, page: str = "General") -> None:
        if self.m:
            self.host.open_vm_settings(self.sel, page if isinstance(page, str) else "General")

    def clone(self) -> None:
        m = self.m
        if m:
            name = simpledialog.askstring("Clone Machine", "Name of the new machine:", initialvalue=m.name + " Clone", parent=self)
            if name:
                self.sel = self.store.clone(m.id, name).id
                self.fill(force=True)

    def rename(self) -> None:
        m = self.m
        if m:
            name = simpledialog.askstring("Rename Machine", "New name:", initialvalue=m.name, parent=self)
            if name and name.strip():
                m.name = name.strip()
                self.store.update(m)
                self.fill(force=True)

    def remove(self) -> None:
        m = self.m
        if m is None:
            return
        if self._live(m.id):
            messagebox.showinfo("Remove", "Stop the machine first.", parent=self)
            return
        n = len(self.store.snapshots(m.id))
        if messagebox.askyesno("Remove Machine", f"Remove '{m.name}'?\n\nIts {n} snapshot(s) are deleted too. The project files are never touched.", parent=self):
            if m.id == self.host.vm_current_id:
                self.host.vm_current_id = ""
            self.store.remove(m.id, delete_files=True)
            self.sel = ""
            self.fill(force=True)

    def start(self) -> None:
        if self.m and self.host.vm_start_machine(self.m):
            self.host.open_vm_window(self.sel)

    def start_or_show(self) -> None:
        if self.m is None:
            return
        self.show_window() if self._live(self.sel) else self.start()

    def stop(self) -> None:
        if self.m and self._live(self.sel) and messagebox.askyesno("Power Off", f"Stop '{self.m.name}'?", parent=self):
            self.host.vm_stop()

    def show_window(self) -> None:
        if self.m:
            self.host.open_vm_window(self.sel)

    def take_snapshot(self) -> None:
        if self.m:
            snap = self.host.vm_take_snapshot(self.sel)
            if snap is not None:
                self.snaps.refresh(select=snap.id)

    def show_snapshots(self) -> None:
        if self.m:
            self.nb.select(1)
            self.snaps.set_machine(self.sel)

    def open_folder(self) -> None:
        import os
        import subprocess
        import sys
        try:
            self.store.base.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(self.store.base))             # noqa: S606 - our own data folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.store.base)])
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.host._save_pref("vm_manager_geometry", self.geometry())
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
