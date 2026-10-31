"""Glue between App Preview and the VirtualBox-style windows (Manager, Machine window, Snapshots, Settings).

``VMManagerMixin`` is mixed into ``AppPreviewView``. It adds methods only; nothing existing is replaced. It watches the panel
(by polling its own attributes, so it takes no events away from the panel), keeps a *machine* for every program that is run, and
offers the operations the windows need: capture / apply settings, take / restore snapshots, start / stop a machine.

Every start still goes through the panel's ordinary Start, so the approval of the exact plan is asked as always.
"""
from __future__ import annotations

import time
import tkinter as tk
from dataclasses import asdict, fields
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, Optional

from app_preview import BACKENDS, Entry, custom_entry
from app_preview.vmstore import Machine, VMStore, clean_settings
from ui.screen_tools import SNAPSHOTS, VIEW_MODES, fps_text, view_label


class VMManagerMixin:
    _vm_store_obj: Optional[VMStore] = None
    vm_current_id: str = ""                    # the machine whose run the panel shows (running or last)
    vm_pending_id: str = ""                    # machine chosen in the Manager, claimed by the next run
    vm_restored = None                         # (machine id, snapshot id, png) shown by the Machine window after a restore
    _vm_seen_run = ""
    _vm_ended_run = ""
    _vm_windows: Dict = {}

    # ================================================================== set-up
    def _vm_init(self) -> None:
        self._vm_windows = {}
        try:
            self.snap_btn.menu.add_separator()
            self.snap_btn.menu.add_command(label="Take machine snapshot (picture + console + settings)...", command=lambda: self.vm_take_snapshot())
            self.snap_btn.menu.add_command(label="Snapshots of this machine...", command=lambda: self.open_vm_snapshots())
        except (AttributeError, tk.TclError):
            pass
        self.after(400, self._vm_tick)

    @property
    def vm_store(self) -> VMStore:
        if self._vm_store_obj is None:
            self._vm_store_obj = VMStore(SNAPSHOTS.default.parent / "vm")
        return self._vm_store_obj

    def vm_build_menubutton(self, parent) -> ttk.Menubutton:
        b = ttk.Menubutton(parent, text="🖥 Virtual machines ▾", style="Tool.TButton")
        m = tk.Menu(b, tearoff=False)
        m.add_command(label="Manager...", command=self.open_vm_manager)
        m.add_command(label="Machine window (screen, input, devices)", command=lambda: self.open_vm_window())
        m.add_separator()
        m.add_command(label="Take snapshot...", command=lambda: self.vm_take_snapshot())
        m.add_command(label="Snapshots...", command=lambda: self.open_vm_snapshots())
        m.add_command(label="Settings...", command=lambda: self.open_vm_settings())
        b["menu"] = m
        return b

    # ================================================================== watching the panel
    def _vm_tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self._vm_watch()
        except Exception as exc:  # noqa: BLE001 - the windows are a convenience; they must never disturb a run
            self._vm_last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.after(250, self._vm_tick)

    def _vm_watch(self) -> None:
        if self.session is not None and self.run_id and self.run_id != self._vm_seen_run:
            self._vm_seen_run = self.run_id
            self._vm_ended_run = ""
            m = self._vm_machine_for_run()
            self.vm_current_id, self.vm_pending_id, self.vm_restored = m.id, "", None
            self.vm_store.record_start(m)
        if self.state == "ended" and self.run_id and self._vm_ended_run != self.run_id and self.vm_current_id:
            self._vm_ended_run = self.run_id
            m = self.vm_store.machines.get(self.vm_current_id)
            if m is not None:
                self.vm_store.record_end(m, self._last_status, self._frame_png, self.vm_console_text())
                if m.settings.get("auto_snapshot_end"):
                    self.vm_take_snapshot(m.id, ask=False, auto=True)

    def _vm_machine_for_run(self) -> Machine:
        plan = self.session.plan
        store = self.vm_store
        ent = asdict(plan.entry)
        m = store.machines.get(self.vm_pending_id) if self.vm_pending_id else None
        if m is None:
            m = store.find_machine(self.source, list(plan.entry.argv), plan.mode)
        if m is None:
            project = Path(self.source).stem or Path(self.source).name or "project"
            title = plan.entry.title
            name = title if project.lower() in title.lower() else f"{title} [{project}]"
            m = store.new_machine(name, self.source, plan.mode, ent, self.vm_capture_settings())
        else:
            keep = bool(m.settings.get("auto_snapshot_end"))
            m.source, m.mode, m.entry = self.source or m.source, plan.mode, ent
            m.settings = dict(self.vm_capture_settings(), auto_snapshot_end=keep)
            store.update(m)
        return m

    # ================================================================== settings <-> panel
    def vm_capture_settings(self, base: Optional[Dict] = None) -> Dict:
        o = self._preview_options()
        lim = o.limits
        s = dict(base or {})
        s.update({"screen": o.screen, "fps": o.fps, "view": VIEW_MODES.get(self.view_var.get(), "fit"),
                  "timeout_s": lim.timeout_s, "max_workspace_mb": lim.max_workspace_mb, "memory_mb": lim.memory_mb,
                  "cpus": lim.cpus, "pids": lim.pids, "install": bool(self.install_var.get()), "link_output": bool(self.link_var.get()),
                  "stop_entry": bool(self.stop_entry_var.get()), "backend": o.backend, "custom_exe": o.custom_exe,
                  "image": self.image_var.get().strip()})
        return clean_settings(s)

    def vm_apply_settings(self, settings: Dict) -> None:
        s = clean_settings(settings)
        self.res_var.set(s["screen"])
        self._size_changed()
        self.rate_var.set(fps_text(s["fps"]))
        self._rate_changed()
        self.view_var.set(view_label(s["view"]))
        self._view_changed()
        self.timeout_var.set(s["timeout_s"])
        self.size_var.set(s["max_workspace_mb"])
        lim = self.options.limits
        lim.memory_mb, lim.cpus, lim.pids = s["memory_mb"], s["cpus"], s["pids"]
        self.install_var.set(s["install"])
        self.link_var.set(s["link_output"])
        self.stop_entry_var.set(s["stop_entry"])
        self.backend_var.set(s["backend"] if s["backend"] in BACKENDS else "auto")
        self.custom_var.set(s["custom_exe"])
        self.image_var.set(s["image"])

    # ================================================================== machine <-> panel
    def vm_live_state(self, mid: str = "") -> str:
        """'starting' | 'running' | 'paused' for the machine the panel is running now, else ''."""
        if mid and mid != self.vm_current_id:
            return ""
        if self.session is not None and self.session.running:
            return self.state if self.state in ("starting", "running", "paused") else "running"
        return ""

    def vm_console_text(self) -> str:
        try:
            return self.console.get("1.0", "end-1c")[-64 * 1024:]
        except tk.TclError:
            return ""

    def vm_details(self) -> Dict[str, str]:
        return {k: v.get() for k, v in self.info.items()}

    def _vm_entry(self, m: Machine) -> Optional[Entry]:
        d = m.entry or {}
        if not d.get("argv"):
            return None
        names = {f.name for f in fields(Entry)}
        try:
            return Entry(**{k: v for k, v in d.items() if k in names})
        except TypeError:
            try:
                return custom_entry([str(a) for a in d["argv"]], kind=m.kind, title=str(d.get("title") or ""))
            except ValueError:
                return None

    def vm_load_machine(self, m: Machine) -> bool:
        """Put a machine's project, mode, start command and settings into the panel (nothing is started)."""
        if self.session is not None and self.session.running:
            return False
        self.vm_apply_settings(m.settings)
        if m.source and m.source != self.source:
            self.set_source(m.source)
        self.mode_var.set(m.mode)
        self._mode_changed()
        ent = self._vm_entry(m)
        if ent is not None:
            title = self._title(ent)
            self.entry_by_title[title] = ent
            vals = list(self.entry_box["values"])
            self.entry_box["values"] = vals + ([title] if title not in vals else [])
            self.entry_var.set(title)
            self._keep_entry = title
        self.vm_pending_id = m.id
        return True

    def vm_start_machine(self, m: Machine) -> bool:
        if self.session is not None and self.session.running:
            messagebox.showinfo("Virtual machines", "A machine is running already. Stop it first: App Preview runs one program at a time.", parent=self)
            return False
        if not m.source or not Path(m.source).exists():
            messagebox.showerror("Virtual machines", f"The project of '{m.name}' was not found:\n{m.source or '(none)'}\n\nChoose it again in Settings > General.", parent=self)
            return False
        if not m.entry.get("argv"):
            messagebox.showinfo("Virtual machines", f"'{m.name}' has no start command yet. Open Settings > General and type one, or run the program once from App Preview.", parent=self)
            return False
        if not self.vm_load_machine(m):
            return False
        self.vm_restored = None
        self.start()                                     # the usual approval of the exact plan is asked here
        return True

    def vm_stop(self) -> None:
        self.stop()

    # ================================================================== snapshots
    def vm_take_snapshot(self, mid: str = "", ask: bool = True, auto: bool = False, name: str = "", description: str = ""):
        store = self.vm_store
        mid = mid or self.vm_current_id
        if not mid or mid not in store.machines:
            messagebox.showinfo("Snapshot", "There is no machine yet. Start a program (▶ Start or ⚡ Run now) or create a machine in the Manager first.", parent=self)
            return None
        live = mid == self.vm_current_id
        png = self._frame_png if live else store.last_png(mid)
        console = self.vm_console_text() if live else store.last_log(mid)
        if ask and not auto:
            from ui.vm_snapshots import ask_take_snapshot
            got = ask_take_snapshot(self, store.next_name(mid), png, store.machines[mid].name)
            if got is None:
                return None
            name, description = got
        state = self.state if live else "idle"
        status = self._last_status if live else store.machines[mid].last_status
        try:
            snap = store.take(mid, name or ("Auto - " + time.strftime("%H:%M:%S") if auto else ""), description, png, console, state,
                              status, self.vm_details() if live else {}, auto=auto)
        except (ValueError, OSError) as exc:
            if not auto:
                messagebox.showerror("Snapshot", str(exc), parent=self)
            return None
        self.status_var.set(f"Snapshot '{snap.name}' taken.")
        return snap

    def vm_restore(self, mid: str, sid: str, keep_current: bool = False) -> bool:
        store = self.vm_store
        snap, m = store.get(mid, sid), store.machines.get(mid)
        if snap is None or m is None:
            return False
        if keep_current and mid == self.vm_current_id:
            self.vm_take_snapshot(mid, ask=False, auto=False, name="Before restoring " + snap.name[:40])
        m.source, m.mode = snap.source or m.source, snap.mode if snap.mode else m.mode
        m.entry = dict(snap.entry) or m.entry
        m.settings = clean_settings(snap.settings or m.settings)
        store.update(m)
        store.set_current(mid, sid)
        self.vm_restored = (mid, sid, store.png(mid, sid))
        running = self.vm_live_state(mid) != ""
        if not running:
            self.vm_load_machine(m)
        self.status_var.set(f"Restored '{snap.name}': settings and start command are back" + ("; they apply at the next start." if running else "."))
        return True

    # ================================================================== windows
    def _vm_open(self, key: str, factory):
        w = self._vm_windows.get(key)
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                w.focus_force()
                return w
        except tk.TclError:
            pass
        w = factory()
        self._vm_windows[key] = w
        return w

    def _vm_target(self, mid: str = "") -> str:
        mid = mid or self.vm_current_id
        if mid and mid in self.vm_store.machines:
            return mid
        ms = self.vm_store.sorted_machines()
        return ms[0].id if ms else ""

    def open_vm_manager(self):
        from ui.vm_manager import VMManagerWindow
        return self._vm_open("manager", lambda: VMManagerWindow(self))

    def open_vm_window(self, mid: str = ""):
        from ui.vm_window import MachineWindow
        mid = self._vm_target(mid)
        if not mid:
            messagebox.showinfo("Machine window", "There is no machine yet. Start a program or create one in the Manager.", parent=self)
            return None
        return self._vm_open("window:" + mid, lambda: MachineWindow(self, mid))

    def open_vm_snapshots(self, mid: str = ""):
        from ui.vm_snapshots import SnapshotsWindow
        mid = self._vm_target(mid)
        if not mid:
            messagebox.showinfo("Snapshots", "There is no machine yet. Start a program or create one in the Manager.", parent=self)
            return None
        return self._vm_open("snaps:" + mid, lambda: SnapshotsWindow(self, mid))

    def open_vm_settings(self, mid: str = "", page: str = "General"):
        from ui.vm_settings import VMSettingsDialog
        mid = self._vm_target(mid)
        if not mid:
            messagebox.showinfo("Settings", "There is no machine yet. Start a program or create one in the Manager.", parent=self)
            return None
        return self._vm_open("settings:" + mid, lambda: VMSettingsDialog(self, mid, page))
