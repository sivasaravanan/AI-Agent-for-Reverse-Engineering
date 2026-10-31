"""Snapshots, VirtualBox style: a tree (each snapshot is the child of the one that was current when it was taken) ending in
"Current State", with Take / Restore / Delete / Properties and a details pane (picture, settings, console, description).

What a snapshot holds: the screen picture, the console text, the machine's settings and start command. The container itself is
disposable and is not saved; Restore puts the settings and the start command back and shows the recorded picture.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, Optional, Tuple

from app_preview.vmstore import Snapshot, human_size, when
from ui import theme
from ui.screen_tools import FrameView

CUR = "__current__"
EXPLAIN = ("A snapshot keeps the screen picture, the console text, the settings and the start command of the machine.\n"
           "The container's memory and disk are not saved (runs are disposable), so Restore sets the settings and start command back "
           "and shows the recorded picture; the program runs fresh at the next Start.")


def _style(win, host) -> None:
    try:
        theme.restyle_tk_widgets(win, getattr(host, "_c", theme.LIGHT))
    except tk.TclError:
        pass


class _Modal(tk.Toplevel):
    def __init__(self, parent, title: str, geometry: str):
        super().__init__(parent)
        self.title(title)
        self.geometry(geometry)
        self.result = None
        try:
            self.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        self.bind("<Escape>", lambda _e: self.destroy())

    def run(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)
        return self.result


def ask_take_snapshot(parent, default_name: str, png: bytes, machine_name: str = "") -> Optional[Tuple[str, str]]:
    dlg = _Modal(parent, f"Take Snapshot of Virtual Machine{' - ' + machine_name if machine_name else ''}", "520x430")
    body = ttk.Frame(dlg, padding=10)
    body.pack(fill="both", expand=True)
    view = FrameView(body, placeholder="No screen picture yet\n(the snapshot keeps the console and settings)", height=150)
    view.pack(fill="x")
    view.canvas.configure(height=150)
    if png:
        view.show(png)
    ttk.Label(body, text="Snapshot Name").pack(anchor="w", pady=(8, 0))
    name = tk.StringVar(value=default_name)
    e = ttk.Entry(body, textvariable=name)
    e.pack(fill="x")
    ttk.Label(body, text="Snapshot Description").pack(anchor="w", pady=(8, 0))
    desc = tk.Text(body, height=6, wrap="word")
    desc.pack(fill="both", expand=True)
    ttk.Label(body, text=EXPLAIN, style="Muted.TLabel", wraplength=480, justify="left").pack(fill="x", pady=(6, 0))
    bar = ttk.Frame(body)
    bar.pack(fill="x", pady=(8, 0))

    def ok(_e=None) -> None:
        dlg.result = (name.get().strip() or default_name, desc.get("1.0", "end-1c").strip())
        dlg.destroy()
    ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
    ttk.Button(bar, text="OK", command=ok).pack(side="right", padx=6)
    dlg.bind("<Return>", lambda e: ok() if e.widget is not desc else None)
    _style(dlg, parent)
    e.focus_set()
    e.select_range(0, "end")
    return dlg.run()


def ask_restore(parent, snap: Snapshot, offer_keep: bool) -> Optional[bool]:
    """None = cancelled, else the state of 'Create a snapshot of the current machine state first'."""
    dlg = _Modal(parent, "Restore Snapshot", "520x300")
    body = ttk.Frame(dlg, padding=12)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=f"Restore snapshot '{snap.name}'?", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
    ttk.Label(body, text="The start command and the settings of the machine go back to what they were then, and the recorded "
                         "picture is shown. Changes made since are lost unless you keep them in a snapshot.",
              wraplength=480, justify="left").pack(anchor="w", pady=6)
    keep = tk.BooleanVar(value=offer_keep)
    if offer_keep:
        ttk.Checkbutton(body, text="Create a snapshot of the current machine state first", variable=keep).pack(anchor="w")
    ttk.Label(body, text=EXPLAIN.split("\n", 1)[1], style="Muted.TLabel", wraplength=480, justify="left").pack(anchor="w", pady=8)
    bar = ttk.Frame(body)
    bar.pack(fill="x", side="bottom")

    def ok() -> None:
        dlg.result = bool(keep.get())
        dlg.destroy()
    ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
    ttk.Button(bar, text="Restore", command=ok).pack(side="right", padx=6)
    _style(dlg, parent)
    return dlg.run()


def snapshot_properties(parent, host, mid: str, sid: str, on_done: Optional[Callable[[], None]] = None) -> None:
    store = host.vm_store
    snap = store.get(mid, sid)
    if snap is None:
        return
    dlg = _Modal(parent, f"Snapshot Properties - {snap.name}", "540x520")
    body = ttk.Frame(dlg, padding=10)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Name").pack(anchor="w")
    name = tk.StringVar(value=snap.name)
    ttk.Entry(body, textvariable=name).pack(fill="x")
    ttk.Label(body, text="Description").pack(anchor="w", pady=(8, 0))
    desc = tk.Text(body, height=5, wrap="word")
    desc.insert("1.0", snap.description)
    desc.pack(fill="x")
    ttk.Label(body, text="Taken: " + when(snap.taken) + ("   (automatic)" if snap.auto else ""), style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
    view = FrameView(body, placeholder="This snapshot has no picture.")
    view.pack(fill="both", expand=True, pady=6)
    png = store.png(mid, sid)
    if png:
        view.show(png)
    bar = ttk.Frame(body)
    bar.pack(fill="x")

    def ok() -> None:
        store.edit(mid, sid, name.get(), desc.get("1.0", "end-1c").strip())
        dlg.destroy()
        if on_done:
            on_done()
    ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
    ttk.Button(bar, text="OK", command=ok).pack(side="right", padx=6)
    _style(dlg, host)
    dlg.run()


class SnapshotsPanel(ttk.Frame):
    def __init__(self, parent, host, mid: str = "", **kw):
        super().__init__(parent, **kw)
        self.host = host
        self.mid = mid
        bar = ttk.Frame(self, padding=(4, 3))
        bar.pack(fill="x")
        self.buttons: Dict[str, ttk.Button] = {}
        for key, text, tip, cmd in (("take", "📷 Take", "Take a snapshot of the machine now (Ctrl+T)", self.take),
                                    ("restore", "⟲ Restore", "Put the settings and start command of the selected snapshot back", self.restore),
                                    ("delete", "✖ Delete", "Delete the selected snapshot; its children move up one level (Delete)", self.delete),
                                    ("props", "✎ Properties", "Name, description and picture of the selected snapshot (F2)", self.properties),
                                    ("save", "💾 Save picture...", "Save the snapshot's picture as a PNG file", self.save_picture)):
            b = ttk.Button(bar, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=(0, 3))
            theme.Tooltip(b, tip)
            self.buttons[key] = b
        self.note = tk.StringVar()
        ttk.Label(bar, textvariable=self.note, style="Muted.TLabel").pack(side="right")
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        self.tree = ttk.Treeview(left, columns=("taken", "state"), selectmode="browse")
        self.tree.heading("#0", text="Snapshot")
        self.tree.heading("taken", text="Taken")
        self.tree.heading("state", text="State")
        self.tree.column("#0", width=190, minwidth=110)
        self.tree.column("taken", width=125, minwidth=80)
        self.tree.column("state", width=70, minwidth=45)
        self.tree.tag_configure("cur", font=("TkDefaultFont", 9, "bold"))
        self.tree.tag_configure("auto", foreground="#8a94ad")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        self.view = FrameView(right, placeholder="Select a snapshot to see its picture.", height=130)
        self.view.pack(fill="x", padx=2, pady=2)
        self.view.canvas.configure(height=130)
        self.head = tk.StringVar()
        ttk.Label(right, textvariable=self.head, font=("TkDefaultFont", 10, "bold"), wraplength=330).pack(anchor="w", padx=4)
        self.facts = tk.StringVar()
        ttk.Label(right, textvariable=self.facts, justify="left", wraplength=330, style="Muted.TLabel").pack(anchor="w", padx=4)
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, padx=2, pady=4)
        self.desc = self._text(nb, "Description")
        self.cons = self._text(nb, "Console")
        self.sett = self._text(nb, "Settings")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show())
        self.tree.bind("<Double-1>", lambda _e: self.properties())
        self.tree.bind("<F2>", lambda _e: self.properties())
        self.tree.bind("<Delete>", lambda _e: self.delete())
        self.tree.bind("<Button-3>", self._menu)
        self.bind_all("<Control-t>", self._hot_take, add="+")
        self.menu = tk.Menu(self, tearoff=False)
        for label, cmd in (("Take Snapshot...", self.take), ("Restore Snapshot...", self.restore), ("Delete Snapshot", self.delete),
                           ("Properties...", self.properties), ("Save Picture...", self.save_picture)):
            self.menu.add_command(label=label, command=cmd)
        self.refresh()
        _style(self, host)

    def _text(self, nb, title: str) -> tk.Text:
        f = ttk.Frame(nb)
        t = tk.Text(f, height=6, wrap="word", state="disabled")
        s = ttk.Scrollbar(f, command=t.yview)
        t.configure(yscrollcommand=s.set)
        s.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        nb.add(f, text=title)
        return t

    @staticmethod
    def _set(t: tk.Text, text: str) -> None:
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", text)
        t.config(state="disabled")

    def _hot_take(self, _e=None) -> None:
        if self.winfo_ismapped():
            self.take()

    # ------------------------------------------------------------------ data
    def set_machine(self, mid: str) -> None:
        self.mid = mid
        self.refresh()

    def refresh(self, select: str = "") -> None:
        keep = select or (self.tree.selection()[0] if self.tree.selection() else "")
        self.tree.delete(*self.tree.get_children())
        store = self.host.vm_store
        if not self.mid or self.mid not in store.machines:
            self.note.set("No machine selected.")
            self._show()
            return
        cur = store.current(self.mid)
        parents: Dict[str, str] = {}
        for depth, s in store.tree(self.mid):
            parent = s.parent if s.parent in parents else ""
            self.tree.insert(parent, "end", iid=s.id, text=s.name, open=True,
                             values=(when(s.taken), s.state or "-"), tags=("auto",) if s.auto else ())
            parents[s.id] = s.id
        live = self.host.vm_live_state(self.mid)
        self.tree.insert(cur if cur in parents else "", "end", iid=CUR, text="Current State" + (f" ({live})" if live else ""),
                         values=("now", live or "-"), tags=("cur",))
        n = len(parents)
        self.note.set(f"{n} snapshot(s), {human_size(store.disk_usage(self.mid))} on disk")
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
        else:
            self.tree.selection_set(CUR)
        self._show()

    def selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] != CUR else ""

    # ------------------------------------------------------------------ details
    def _show(self) -> None:
        sid = self.selected()
        store = self.host.vm_store
        has = bool(sid)
        for k in ("restore", "delete", "props", "save"):
            self.buttons[k].config(state="normal" if has else "disabled")
        self.buttons["take"].config(state="normal" if self.mid in store.machines else "disabled")
        if not has:
            m = store.machines.get(self.mid)
            self.view.clear()
            self.head.set("Current State" if m else "")
            self.facts.set((f"{m.name}\n{m.command}\nMode: {m.mode}" if m else "") + "\n\nTake a snapshot to be able to come back to this configuration.")
            for t in (self.desc, self.cons, self.sett):
                self._set(t, "")
            return
        s = store.get(self.mid, sid)
        if s is None:
            return
        png = store.png(self.mid, sid)
        if png:
            self.view.show(png)
        else:
            self.view.clear()
        self.head.set(s.name)
        self.facts.set(f"Taken {when(s.taken)}{'  (automatic)' if s.auto else ''}\nState: {s.state or '-'}   {s.status}\n"
                       f"Command: {' '.join(s.entry.get('argv') or []) or s.entry.get('title', '-')}   Mode: {s.mode}"
                       + (f"\nScreen picture: {s.screen}" if s.screen else ""))
        self._set(self.desc, s.description or "(no description)")
        self._set(self.cons, store.console(self.mid, sid) or "(no console text)")
        st = s.settings or {}
        lines = [f"{k}: {v}" for k, v in st.items()] + ([""] + [f"{k}: {v}" for k, v in s.info.items()] if s.info else [])
        self._set(self.sett, "\n".join(lines))

    def _menu(self, e) -> None:
        row = self.tree.identify_row(e.y)
        if row:
            self.tree.selection_set(row)
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------ commands
    def take(self) -> None:
        snap = self.host.vm_take_snapshot(self.mid)
        if snap is not None:
            self.refresh(select=snap.id)

    def restore(self) -> None:
        sid = self.selected()
        s = self.host.vm_store.get(self.mid, sid) if sid else None
        if s is None:
            return
        live = self.host.vm_live_state(self.mid) != ""
        keep = ask_restore(self, s, offer_keep=self.mid == self.host.vm_current_id)
        if keep is None:
            return
        if self.host.vm_restore(self.mid, sid, keep_current=keep):
            self.refresh(select=sid)
            if live:
                messagebox.showinfo("Restore Snapshot", "The machine is running, so the restored settings apply at its next start.", parent=self)

    def delete(self) -> None:
        sid = self.selected()
        s = self.host.vm_store.get(self.mid, sid) if sid else None
        if s is None:
            return
        if messagebox.askyesno("Delete Snapshot", f"Delete snapshot '{s.name}'?\nIts picture and console text are removed; snapshots below it move up one level.", parent=self):
            self.host.vm_store.delete(self.mid, sid)
            self.refresh()

    def properties(self) -> None:
        sid = self.selected()
        if sid:
            snapshot_properties(self, self.host, self.mid, sid, on_done=lambda: self.refresh(select=sid))

    def save_picture(self) -> None:
        sid = self.selected()
        s = self.host.vm_store.get(self.mid, sid) if sid else None
        if s is None:
            return
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".png", filetypes=[("PNG image", "*.png")], initialfile=f"{s.name}.png".replace(" ", "_"))
        if p:
            try:
                self.host.vm_store.export_png(self.mid, sid, Path(p))
            except OSError as exc:
                messagebox.showerror("Save picture", str(exc), parent=self)


class SnapshotsWindow(tk.Toplevel):
    def __init__(self, host, mid: str):
        super().__init__(host.winfo_toplevel())
        self.host, self.mid = host, mid
        m = host.vm_store.machines.get(mid)
        self.title(f"Snapshots - {m.name if m else ''}")
        self.geometry("860x560")
        self.minsize(560, 360)
        self.panel = SnapshotsPanel(self, host, mid)
        self.panel.pack(fill="both", expand=True)
        self.after(1000, self._tick)

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            if self.host.vm_live_state(self.mid):
                self.panel.refresh()
        finally:
            self.after(2000, self._tick)
