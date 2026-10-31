"""The machine window, VirtualBox style: menu bar (Machine / View / Input / Devices / Help), a slim toolbar, the screen of the
program (or its console), and a status bar with device icons that can be clicked.

It shows the machine the App Preview panel is running; when that machine is not running it shows its last picture ("Powered Off").
It only reads the panel's state and calls the panel's own methods, so a start still asks for approval of the exact plan.
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

from app_preview.vmstore import STATE_NAMES, human_size, machine_state, png_size
from plugins.build_execution import quickprep
from ui import theme
from ui.guest_input import GuestInput
from ui.screen_tools import SNAPSHOTS, VIEW_MODES, FrameView, view_label
from ui.vm_snapshots import _Modal, _style

PLACEHOLDER = {
    "Powered Off": "The machine is powered off.\n\nMachine > Start (or the ▶ button) runs it again.",
    "Aborted": "The last run ended with an error.\n\nThe Console page shows what it printed. Machine > Start runs it again.",
    "Starting": "Starting...\n\nThe approval window, the container and the first screen picture come first.",
    "Running": "Waiting for the first screen picture...\n\nA program that works before it opens its window shows nothing until it does.\nPrograms without a window show their output on the Console page.",
    "Paused": "Paused at a breakpoint.",
}
KEYS = (("Insert Ctrl-Alt-Del", "ctrl+alt+Delete"), ("Send Escape", "Escape"), ("Send Enter", "Return"), ("Send Tab", "Tab"),
        ("Send Alt+F4 (close the program's window)", "alt+F4"), ("Send Ctrl+C", "ctrl+c"), ("Send Ctrl+V", "ctrl+v"), ("Send F5", "F5"))


class MachineWindow(tk.Toplevel):
    def __init__(self, host, mid: str):
        super().__init__(host.winfo_toplevel())
        self.host, self.mid = host, mid
        self.store = host.vm_store
        self.page_var = tk.StringVar(value="auto")
        self.scale_var = tk.StringVar(value=view_label("fit"))                  # the machine screen always opens fitted to the window
        self.cap_var = tk.BooleanVar(value=True)                                  # mouse and keyboard go to the program (like a VirtualBox window with focus)
        self.hold_var = tk.BooleanVar(value=False)
        self.top_var = tk.BooleanVar(value=False)
        self.full_var = tk.BooleanVar(value=False)
        self.tool_var = tk.BooleanVar(value=True)
        self.bar_var = tk.BooleanVar(value=True)
        self._png = b""
        self._con_text = ""
        self._page = ""
        geo = str(host._prefs.get("vm_window_geometry", ""))
        self.geometry(geo if geo and geo.replace("x", "").replace("+", "").replace("-", "").isdigit() else "900x720")
        self.minsize(420, 320)
        self._build_menu()
        self._build_toolbar()
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.view = FrameView(self.body, mode=VIEW_MODES.get(self.scale_var.get(), "fit"), placeholder=PLACEHOLDER["Powered Off"])
        self.view.pack(fill="both", expand=True)
        self.console = tk.Text(self.body, wrap="none", state="disabled", font="TkFixedFont")
        self.csb = ttk.Scrollbar(self.body, command=self.console.yview)
        self.console.configure(yscrollcommand=self.csb.set)
        self._build_status()
        c = self.view.canvas
        self.guest = GuestInput(c, self.view.to_xy, lambda: self.host.session, self._capturing, host_combo=self._is_window_shortcut)
        c.bind("<Button-3>", self._context, add="+")
        c.bind("<Motion>", self._motion, add="+")
        c.bind("<Enter>", lambda _e: c.focus_set() if self.cap_var.get() else None)
        for seq, fn in (("<Control-t>", self.take_snapshot), ("<Control-E>", self.settings), ("<Control-e>", self.settings),
                        ("<Control-S>", self.snapshots), ("<F11>", self._toggle_full), ("<Escape>", self._leave_full)):
            self.bind(seq, lambda _e, f=fn: f())
        self.protocol("WM_DELETE_WINDOW", self.close_request)
        self.refresh()
        _style(self, host)
        self.after(250, self._tick)

    # ------------------------------------------------------------------ helpers
    @property
    def m(self):
        return self.store.machines.get(self.mid)

    def live(self) -> str:
        return self.host.vm_live_state(self.mid)

    def _display(self) -> bool:
        s = self.host.session
        return bool(self.live() and s is not None and s.plan.display)

    # ------------------------------------------------------------------ menus
    def _build_menu(self) -> None:
        mb = tk.Menu(self)
        self.config(menu=mb)
        h = self.host
        mm = tk.Menu(mb, tearoff=False, postcommand=self._post_machine)
        mm.add_command(label="Start", command=self.start)
        mm.add_command(label="Power Off", command=self.power_off)
        mm.add_command(label="Reset (restart)", command=self.restart)
        mm.add_separator()
        mm.add_command(label="Take Snapshot...        Ctrl+T", command=self.take_snapshot)
        mm.add_command(label="Snapshots...        Ctrl+Shift+S", command=self.snapshots)
        mm.add_separator()
        mm.add_command(label="Take Screenshot (quick save)", command=lambda: h.quick_snapshot())
        mm.add_command(label="Save Screenshot As...", command=h._save_frame)
        mm.add_separator()
        mm.add_command(label="Settings...        Ctrl+E", command=self.settings)
        mm.add_command(label="Session Information...", command=self.session_info)
        mm.add_separator()
        mm.add_command(label="Close...", command=self.close_request)
        self.mm = mm
        mb.add_cascade(label="Machine", menu=mm)
        mv = tk.Menu(mb, tearoff=False)
        sh = tk.Menu(mv, tearoff=False)
        for label, val in (("Automatic", "auto"), ("Screen", "screen"), ("Console", "console")):
            sh.add_radiobutton(label=label, value=val, variable=self.page_var, command=self.refresh)
        mv.add_cascade(label="Show", menu=sh)
        sc = tk.Menu(mv, tearoff=False)
        for label in VIEW_MODES:
            sc.add_radiobutton(label=label, value=label, variable=self.scale_var, command=self._scale)
        mv.add_cascade(label="Scale", menu=sc)
        mv.add_command(label="Resize Window to the Screen (100%)", command=self.fit_window)
        mv.add_checkbutton(label="Full-screen Mode        F11", variable=self.full_var, command=self._fullscreen)
        mv.add_checkbutton(label="Always on Top", variable=self.top_var, command=self._topmost)
        mv.add_checkbutton(label="Hold Screen (freeze the picture)", variable=self.hold_var)
        mv.add_separator()
        mv.add_checkbutton(label="Show Toolbar", variable=self.tool_var, command=self._layout)
        mv.add_checkbutton(label="Show Status Bar", variable=self.bar_var, command=self._layout)
        mb.add_cascade(label="View", menu=mv)
        mi = tk.Menu(mb, tearoff=False, postcommand=self._post_input)
        mi.add_checkbutton(label="Capture Mouse and Keyboard (send to the program)", variable=self.cap_var, command=self._capture_changed)
        mi.add_separator()
        kb = tk.Menu(mi, tearoff=False)
        for label, key in KEYS:
            kb.add_command(label=label, command=lambda k=key: self.send_key(k))
        kb.add_separator()
        kb.add_command(label="Type Text...", command=self.type_text)
        mi.add_cascade(label="Keyboard", menu=kb)
        self.kb = kb
        mb.add_cascade(label="Input", menu=mi)
        md = tk.Menu(mb, tearoff=False)
        md.add_command(label="Shared Folders...", command=lambda: h.open_vm_settings(self.mid, "Shared Folders"))
        md.add_command(label="Network...", command=lambda: h.open_vm_settings(self.mid, "Network"))
        md.add_command(label="Display...", command=lambda: h.open_vm_settings(self.mid, "Display"))
        md.add_separator()
        md.add_command(label="Storage & Clean-up...", command=h.open_storage)
        md.add_command(label="Open Snapshot Picture Folder", command=lambda: SNAPSHOTS.open_folder())
        md.add_command(label="Pop Out the Screen (own window)", command=h.pop_out)
        mb.add_cascade(label="Devices", menu=md)
        mh = tk.Menu(mb, tearoff=False)
        mh.add_command(label="About this window...", command=lambda: messagebox.showinfo(
            "Machine window", "This window shows the program that App Preview runs in a disposable container: its virtual screen (or its "
            "console), with VirtualBox-style menus.\n\nSnapshots keep the picture, console, settings and start command; the container "
            "itself is not saved. Starting always asks for approval of the exact plan.", parent=self))
        mb.add_cascade(label="Help", menu=mh)

    def _post_machine(self) -> None:
        live = bool(self.live())
        self.mm.entryconfig("Start", state="disabled" if live else "normal")
        self.mm.entryconfig("Power Off", state="normal" if live else "disabled")
        self.mm.entryconfig("Reset (restart)", state="normal" if live else "disabled")

    def _post_input(self) -> None:
        st = "normal" if self._display() else "disabled"
        for i in range(self.kb.index("end") + 1):
            if self.kb.type(i) == "command":
                self.kb.entryconfig(i, state=st)

    def _build_toolbar(self) -> None:
        self.tool = ttk.Frame(self, padding=(4, 2))
        self.tool.pack(fill="x")
        self.tb = {}
        for key, text, tip, cmd in (("start", "▶ Start", "Start the machine (asks for approval first)", self.start),
                                    ("stop", "■ Power off", "Stop the program and remove its container", self.power_off),
                                    ("reset", "⟳ Reset", "Stop and start again", self.restart),
                                    ("snap", "📷 Snapshot", "Take a snapshot (Ctrl+T)", self.take_snapshot),
                                    ("snaps", "⎇ Snapshots", "Open the snapshot tree", self.snapshots),
                                    ("set", "⚙ Settings", "Settings of this machine (Ctrl+E)", self.settings)):
            b = ttk.Button(self.tool, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=2)
            theme.Tooltip(b, tip)
            self.tb[key] = b
        self.chip = tk.Label(self.tool, text="Powered Off", padx=8, pady=2, font=("TkDefaultFont", 9, "bold"))
        self.chip.pack(side="right")

    def _build_status(self) -> None:
        self.bar = ttk.Frame(self, padding=(4, 2), relief="groove")
        self.bar.pack(fill="x", side="bottom")
        self.point = tk.StringVar(value="")
        ttk.Label(self.bar, textvariable=self.point, width=12, style="Muted.TLabel").pack(side="left")
        self.res = tk.StringVar(value="")
        ttk.Label(self.bar, textvariable=self.res, style="Muted.TLabel").pack(side="left", padx=8)
        self.icons = {}
        for key, tip, cmd in (("time", "Time the machine has been running", None), ("snaps", "Snapshots: click to open the tree", self.snapshots),
                              ("share", "Shared folders (files copied into every run): click to edit", lambda: self.host.open_vm_settings(self.mid, "Shared Folders")),
                              ("net", "Network adapter: click to open network settings", lambda: self.host.open_vm_settings(self.mid, "Network")),
                              ("disk", "Storage: click to open Storage & clean-up", self.host.open_storage),
                              ("desk", "Desktop inside the machine (window manager): title bars, move / resize / close windows", None),
                              ("mouse", "Capture mouse and keyboard: click to switch", self._toggle_capture)):
            lb = tk.Label(self.bar, text="", padx=4, cursor="hand2" if cmd else "")
            lb._keep_colors = True
            lb.pack(side="right")
            if cmd:
                lb.bind("<Button-1>", lambda _e, c=cmd: c())
            theme.Tooltip(lb, tip)
            self.icons[key] = lb

    def _layout(self) -> None:
        for w, var, kw in ((self.tool, self.tool_var, {"fill": "x", "before": self.body}), (self.bar, self.bar_var, {"fill": "x", "side": "bottom"})):
            w.pack_forget()
            if var.get():
                w.pack(**kw)

    # ------------------------------------------------------------------ commands
    def start(self) -> None:
        m, h = self.m, self.host
        if m is None:
            return
        if h.vm_live_state() != "" and not self.live():          # another machine holds the panel: offer to swap instead of a dead button
            other = h.vm_store.machines.get(h.vm_current_id)
            if not messagebox.askyesno("Start", f"'{other.name if other else 'Another machine'}' is running (App Preview runs one program at a time).\n\n"
                                       f"Power it off and start '{m.name}'?", parent=self):
                return
            h.vm_stop()
            self._start_when_off(0)
            return
        h.vm_start_machine(m)

    def _start_when_off(self, n: int) -> None:
        if not self.winfo_exists():
            return
        if self.host.vm_live_state() == "":
            if self.m is not None:
                self.host.vm_start_machine(self.m)
        elif n < 80:
            self.after(250, lambda: self._start_when_off(n + 1))

    def power_off(self) -> None:
        if self.live() and messagebox.askyesno("Power Off", "Stop the program and remove its container?", parent=self):
            self.host.vm_stop()

    def restart(self) -> None:
        if self.live():
            self.host.restart()

    def take_snapshot(self) -> None:
        self.host.vm_take_snapshot(self.mid)

    def snapshots(self) -> None:
        self.host.open_vm_snapshots(self.mid)

    def settings(self) -> None:
        self.host.open_vm_settings(self.mid)

    def send_key(self, key: str) -> None:
        s = self.host.session
        if self._display() and s is not None:
            s.input_event("key", key=key)

    def type_text(self) -> None:
        s = self.host.session
        if not self._display() or s is None:
            return
        t = simpledialog.askstring("Type Text", "Text to type into the program (up to 200 characters):", parent=self)
        if t:
            s.input_event("type", text=t)

    def session_info(self) -> None:
        m = self.m
        if m is None:
            return
        d = _Modal(self, f"Session Information - {m.name}", "520x420")
        t = tk.Text(d, wrap="word")
        lines = [f"Name: {m.name}", f"State: {machine_state(m, self.live())}", f"Project: {m.source}", f"Mode: {m.mode}", f"Start command: {m.command}",
                 f"Runs: {m.runs}", f"Last status: {m.last_status or '-'}", f"Snapshots: {len(self.store.snapshots(m.id))} ({human_size(self.store.disk_usage(m.id))} on disk)",
                 f"Shared files: {len(quickprep.includes_for(m.source)) if m.source else 0}", ""]
        if self.mid == self.host.vm_current_id:
            lines += [f"{k}: {v}" for k, v in self.host.vm_details().items()]
        t.insert("1.0", "\n".join(lines))
        t.config(state="disabled")
        t.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(d, text="Close", command=d.destroy).pack(pady=(0, 8))
        _style(d, self.host)
        d.run()

    # ------------------------------------------------------------------ screen interaction
    def _capturing(self) -> bool:
        return bool(self.cap_var.get()) and self._display()

    def _is_window_shortcut(self, e) -> bool:
        ctrl = bool(e.state & 0x4)
        return ctrl and str(e.keysym).lower() in ("t", "e", "s") and self.winfo_exists()

    def _context(self, e) -> None:
        """Right click: the program gets it while the machine is captured; otherwise the window's own menu."""
        if self._capturing():
            return
        cm = tk.Menu(self, tearoff=False)
        cm.add_command(label="Take Snapshot...", command=self.take_snapshot)
        cm.add_command(label="Save Screenshot As...", command=self.host._save_frame)
        cm.add_checkbutton(label="Capture Mouse and Keyboard", variable=self.cap_var, command=self._capture_changed)
        cm.add_checkbutton(label="Hold Screen", variable=self.hold_var)
        cm.add_command(label="Settings...", command=self.settings)
        try:
            cm.tk_popup(e.x_root, e.y_root)
        finally:
            cm.grab_release()

    def _motion(self, e) -> None:
        xy = self.view.to_xy(e)
        self.point.set(f"{xy[0]}, {xy[1]}" if xy else "")

    def _capture_changed(self) -> None:
        if not self.cap_var.get():
            self.guest.release_all()
        else:
            self.view.canvas.focus_set()
        self.refresh()

    def _toggle_capture(self) -> None:
        self.cap_var.set(not self.cap_var.get())
        self._capture_changed()

    # ------------------------------------------------------------------ view
    def _scale(self) -> None:
        self.view.set_mode(VIEW_MODES.get(self.scale_var.get(), "fit"))

    def fit_window(self) -> None:
        w, h = (int(x) for x in (png_size(self._png) or "0x0").split("x"))
        if not w:
            return
        self.scale_var.set("100%")
        self._scale()
        self.update_idletasks()
        cw, ch = self.view.canvas.winfo_width(), self.view.canvas.winfo_height()
        nw = min(self.winfo_screenwidth() - 40, self.winfo_width() - cw + w + 4)
        nh = min(self.winfo_screenheight() - 80, self.winfo_height() - ch + h + 4)
        self.geometry(f"{max(nw, 420)}x{max(nh, 320)}")

    def _topmost(self) -> None:
        try:
            self.attributes("-topmost", bool(self.top_var.get()))
        except tk.TclError:
            pass

    def _fullscreen(self) -> None:
        try:
            self.attributes("-fullscreen", bool(self.full_var.get()))
        except tk.TclError:
            pass

    def _toggle_full(self) -> None:
        self.full_var.set(not self.full_var.get())
        self._fullscreen()

    def _leave_full(self) -> None:
        if self.full_var.get():
            self.full_var.set(False)
            self._fullscreen()

    # ------------------------------------------------------------------ closing
    def close_request(self) -> None:
        if self.live():
            d = _Modal(self, "Close Virtual Machine", "460x210")
            ttk.Label(d, text=f"'{self.m.name if self.m else ''}' is {self.live()}.\nWhat should happen to it?", padding=12, justify="left").pack(anchor="w")
            ans = {"v": None}

            def pick(v):
                ans["v"] = v
                d.destroy()
            ttk.Button(d, text="Keep it running (close only this window)", command=lambda: pick("keep")).pack(fill="x", padx=12, pady=2)
            ttk.Button(d, text="Power off the machine", command=lambda: pick("off")).pack(fill="x", padx=12, pady=2)
            ttk.Button(d, text="Cancel", command=lambda: pick(None)).pack(fill="x", padx=12, pady=2)
            _style(d, self.host)
            d.run()
            if ans["v"] is None:
                return
            if ans["v"] == "off":
                self.host.vm_stop()
        self._remember()
        self.destroy()

    def _remember(self) -> None:
        if not self.full_var.get():
            try:
                self.host._save_pref("vm_window_geometry", self.geometry())
            except Exception:  # noqa: BLE001 - remembering the position is a convenience
                pass

    # ------------------------------------------------------------------ live update
    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            if self.m is None:
                self.destroy()
                return
            self.refresh()
        finally:
            if self.winfo_exists():
                self.after(250, self._tick)

    def _picture(self) -> bytes:
        h = self.host
        if self.live():
            return h._frame_png
        r = h.vm_restored
        if r and r[0] == self.mid and r[2]:
            return r[2]
        if self.mid == h.vm_current_id and h._frame_png:
            return h._frame_png
        return self.store.last_png(self.mid)

    def refresh(self) -> None:
        m = self.m
        if m is None:
            return
        h = self.host
        live = self.live()
        name = machine_state(m, live)
        self.title(f"{m.name} [{name}] - App Preview")
        c = getattr(h, "_c", theme.LIGHT)
        bg, fg = {"Running": (c["accent"], c["accent_fg"]), "Starting": (c["accent"], c["accent_fg"]), "Paused": (c["warn"], "#1b2333"),
                  "Aborted": (c["bad"], "#ffffff")}.get(name, (c["btn"], c["muted"]))
        self.chip.config(text=name, bg=bg, fg=fg)
        self.tb["start"].config(state="disabled" if live else "normal")
        self.tb["stop"].config(state="normal" if live else "disabled")
        self.tb["reset"].config(state="normal" if live else "disabled")
        png = self._picture()
        want = self.page_var.get()
        page = want if want in ("screen", "console") else ("screen" if (png or self._display()) else "console")
        self._show_page(page)
        if not self.hold_var.get():
            if png:
                self._png = png
                self.view.show(png)
            elif self._png:
                self._png = b""
                self.view.clear()
            self.view.placeholder = PLACEHOLDER.get(name, PLACEHOLDER["Powered Off"])
            if not png:
                self.view.redraw()
        if page == "console":
            self._fill_console(h.vm_console_text() if self.mid == h.vm_current_id else self.store.last_log(self.mid))
        self._status(m, live)

    def _show_page(self, page: str) -> None:
        if page == self._page:
            return
        self._page = page
        if page == "screen":
            self.console.pack_forget()
            self.csb.pack_forget()
            self.view.pack(fill="both", expand=True)
        else:
            self.view.pack_forget()
            self.csb.pack(side="right", fill="y")
            self.console.pack(fill="both", expand=True)
            self._con_text = ""

    def _fill_console(self, text: str) -> None:
        if text == self._con_text:
            return
        w = self.console
        w.config(state="normal")
        if text.startswith(self._con_text) and self._con_text:
            w.insert("end", text[len(self._con_text):])
        else:
            w.delete("1.0", "end")
            w.insert("1.0", text or "(no console output yet)")
        w.config(state="disabled")
        w.see("end")
        self._con_text = text

    def _status(self, m, live: str) -> None:
        h = self.host
        on = "#1f9d57"
        off = "#8a94ad"
        cur = self.mid == h.vm_current_id
        up = h.info["Uptime"].get() if live else "-"
        self.icons["time"].config(text=f"⏱ {up}", fg=on if live else off)
        n = len(self.store.snapshots(self.mid))
        self.icons["snaps"].config(text=f"📷 {n}", fg=on if n else off)
        shared = len(quickprep.includes_for(m.source)) if m.source else 0
        self.icons["share"].config(text=f"📁 {shared}", fg=on if shared else off)
        net = bool(m.settings.get("install"))
        self.icons["net"].config(text="🌐 install" if net else "🌐 off", fg="#d98a00" if net else off)
        self.icons["disk"].config(text="💾", fg=on if live else off)
        cap = bool(self.cap_var.get())
        self.icons["mouse"].config(text="🖱 captured" if cap else "🖱 free", fg=on if cap and self._display() else off)
        d = getattr(h.session, "desktop", "") if live and h.session is not None else ""
        self.icons["desk"].config(text="🪟 desktop" if d == "openbox" else ("🪟 none" if d == "none" else ""), fg=on if d == "openbox" else "#d98a00")
        self.res.set(h.info["Resources"].get() if live and cur else "")
