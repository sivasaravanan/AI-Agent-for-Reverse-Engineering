"""Live run preview, embedded in the application.

Shows what a sandboxed run (build, test, debug/trace command, patch verification) is doing while it runs: status, elapsed time,
the console stream as it arrives (stdout / stderr), the container's CPU and memory, its process list and a phase timeline.
Everything comes from ``plugins.build_execution.events.BUS``; nothing here starts or controls a process except the Stop button,
which only sets the run's cancel flag (the sandbox then kills the container).
"""
from __future__ import annotations

import html as _html
import queue
import re
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

from app_preview.events import PV                # preview-only events: the picture of a GUI app / the page of a web app
from plugins.build_execution.events import BUS
from ui import theme
from ui.run_panels import ResourcePanel
from ui.preview_card import PreviewCard          # VirtualBox-style monitor preview, shared with App Preview
from ui.screen_tools import SNAPSHOTS, ScreenWindow, ask_save_snapshot

MAX_RUNS = 20
MAX_LINES = 20000
SPARK_POINTS = 60


class _Run:
    def __init__(self, rid: str, title: str, meta: str, started: float):
        self.id, self.title, self.meta, self.started = rid, title, meta, started
        self.chunks: List[tuple] = []          # (tag, text) - capped
        self.size = 0
        self.phases: List[str] = []
        self.cpu: List[float] = []
        self.mem: List[float] = []
        self.status = "running"
        self.ended: Optional[float] = None
        self.exit_code = None
        self.top = None
        self.png = b""                         # last virtual-screen snapshot (programs with a window)
        self.web = None                        # (up, title, text, url) of a web app, from App Preview runs


class LiveRunView(ttk.Frame):
    def __init__(self, parent, on_run_start: Optional[Callable[[str], None]] = None, **kw):
        super().__init__(parent, **kw)
        self.on_run_start = on_run_start
        self.q: "queue.Queue" = queue.Queue()
        self.runs: Dict[str, _Run] = {}
        self.order: List[str] = []
        self.current: Optional[str] = None      # run shown
        self.latest: Optional[str] = None       # most recent run
        self.follow_var = tk.BooleanVar(value=True)
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.wrap_var = tk.BooleanVar(value=False)
        self.find_var = tk.StringVar()
        self.pick_var = tk.StringVar()
        self.status_var = tk.StringVar(value="No run yet. Start a build, test or command from Build & Test.")
        self.meta_var = tk.StringVar()
        self.stats_var = tk.StringVar()
        self._popout: Optional[ScreenWindow] = None
        self._build()
        self._unsub = (BUS.subscribe(self.q.put),
                       PV.subscribe(lambda ev: self.q.put(ev) if ev.get("type") in ("pv_frame", "pv_web") else None))   # only what the thumbnail needs
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.after(80, self._poll)
        self.after(500, self._tick)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        head = ttk.Frame(self, padding=(4, 3))
        head.pack(fill="x")
        self.chip = tk.Label(head, text="● Idle", padx=8, pady=2, font=("TkDefaultFont", 9, "bold"))
        self.chip.pack(side="left")
        ttk.Label(head, textvariable=self.status_var, style="Title.TLabel").pack(side="left", padx=8)
        self.stop_btn = ttk.Button(head, text="■ Stop", style="Tool.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.pick = ttk.Combobox(head, textvariable=self.pick_var, state="readonly", width=44)
        self.pick.pack(side="right", padx=6)
        self.pick.bind("<<ComboboxSelected>>", self._on_pick)
        ttk.Label(self, textvariable=self.meta_var, style="Muted.TLabel", padding=(6, 0)).pack(fill="x")
        ttk.Label(self, textvariable=self.stats_var, padding=(6, 0)).pack(fill="x")
        bar = ttk.Frame(self, padding=(4, 3))
        bar.pack(fill="x")
        ttk.Checkbutton(bar, text="Switch here when a run starts", variable=self.follow_var).pack(side="left")
        ttk.Checkbutton(bar, text="Autoscroll", variable=self.autoscroll_var).pack(side="left", padx=8)
        ttk.Checkbutton(bar, text="Wrap", variable=self.wrap_var, command=self._wrap).pack(side="left")
        ttk.Button(bar, text="Save...", style="Tool.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Copy", style="Tool.TButton", command=self.copy).pack(side="right", padx=3)
        ttk.Button(bar, text="Clear", style="Tool.TButton", command=self.clear).pack(side="right")
        e = ttk.Entry(bar, textvariable=self.find_var, width=22)
        e.pack(side="right", padx=6)
        ttk.Label(bar, text="Find:").pack(side="right")
        e.bind("<KeyRelease>", lambda _e: self._highlight())
        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        left = ttk.Frame(pw)
        self.text = tk.Text(left, wrap="none", state="disabled", font="TkFixedFont", undo=False)
        sy = ttk.Scrollbar(left, orient="vertical", command=self.text.yview)
        sx = ttk.Scrollbar(left, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)
        pw.add(left, weight=4)
        side = ttk.Frame(pw)
        pw.add(side, weight=1)
        self.card = PreviewCard(side)                      # the container's virtual screen / terminal, like the VM Preview card
        self.card.pack(fill="x", padx=2, pady=(0, 3))
        self.card.on_click = self._card_click
        theme.Tooltip(self.card.canvas, "Click: open the screen in its own window (a web app opens in the browser).")
        self.res = ResourcePanel(side)                      # CPU / memory, processes, timeline: shared with App Preview
        self.res.pack(fill="both", expand=True)
        self.cpu_canvas, self.mem_canvas, self.procs, self.timeline = self.res.cpu_canvas, self.res.mem_canvas, self.res.procs, self.res.timeline
        self.recolor()

    def recolor(self, colors: Optional[Dict[str, str]] = None) -> None:
        from ui import theme
        c = colors or (theme.DARK if getattr(self, "_dark", False) else theme.LIGHT)
        try:
            st = ttk.Style(self)
            bg = st.lookup("TFrame", "background") or c["bg"]
        except tk.TclError:
            bg = c["bg"]
        self._c = c
        self._chip_colors = {"idle": (c["btn"], c["muted"]), "running": (c["accent"], c["accent_fg"]), "ok": (c["good"], "#ffffff"),
                             "bad": (c["bad"], "#ffffff"), "warn": (c["warn"], "#1b2333")}
        self.text.tag_configure("err", foreground=c["bad"])
        self.text.tag_configure("phase", foreground=c["muted"], font=("TkDefaultFont", 9, "italic"))
        self.text.tag_configure("hit", background=c["sel"], foreground=c["sel_fg"])
        self.res.recolor(c)
        if getattr(self, "card", None) is not None:
            self.card.recolor(c)
        self._chip(self._chip_state)
        self._spark()

    _chip_state = "idle"

    def _chip(self, state: str, label: str = "") -> None:
        self._chip_state = state
        bg, fg = self._chip_colors.get(state, self._chip_colors["idle"])
        names = {"idle": "● Idle", "running": "● Running", "ok": "✔ Completed", "bad": "✖ Failed", "warn": "■ Stopped"}
        self.chip.configure(text=label or names[state], bg=bg, fg=fg)

    # ------------------------------------------------------------------ events
    def _poll(self) -> None:
        n = 0
        self.card.tick()
        try:
            while n < 500:
                self._handle(self.q.get_nowait())
                n += 1
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(60 if n else 120, self._poll)

    def _handle(self, ev: dict) -> None:
        t, rid = ev["type"], ev.get("run", "")
        if t == "runtime":
            return
        if t == "start":
            meta = f"image {ev.get('image')}  |  network {'ENABLED' if ev.get('network') else 'off'}  |  timeout {ev.get('timeout_s')}s"
            run = _Run(rid, ev.get("title") or "run", meta, ev["t"])
            self.runs[rid] = run
            self.order.append(rid)
            while len(self.order) > MAX_RUNS:
                self.runs.pop(self.order.pop(0), None)
            self.latest = rid
            self._refresh_pick()
            self._show(rid)
            if self.follow_var.get() and self.on_run_start:
                self.on_run_start(rid)
            return
        run = self.runs.get(rid)
        if run is None:
            return
        if t in ("screen", "pv_frame"):             # screen = Build & Test GUI run; pv_frame = App Preview run (same run id)
            run.png = ev.get("png") or b""
            if rid == self.current and run.png:
                self.card.set_frame(run.png)
                if run.ended is None:
                    self.card.set_state("running")
                self._popout_show(run)
            return
        if t == "pv_web":
            run.web = self._web_card(ev)
            if rid == self.current:
                self.card.set_web(*run.web)
            return
        if t in ("out", "err"):
            if run.size < 3_000_000:
                run.chunks.append((t, ev["text"]))
                run.size += len(ev["text"])
            if rid == self.current:
                self._append(ev["text"], "err" if t == "err" else "")
                self.card.add_text(ev["text"])
        elif t == "phase":
            run.phases.append(f"{time.strftime('%H:%M:%S', time.localtime(ev['t']))}  {ev['text']}")
            run.chunks.append(("phase", f"▸ {ev['text']}\n"))
            if rid == self.current:
                self._append(f"▸ {ev['text']}\n", "phase")
                self.timeline.insert("end", run.phases[-1])
                self.timeline.see("end")
        elif t == "stats":
            run.cpu.append(float(ev.get("cpu") or 0))
            run.mem.append(float(ev.get("mem_mib") or 0))
            run.cpu, run.mem = run.cpu[-SPARK_POINTS:], run.mem[-SPARK_POINTS:]
            if rid == self.current:
                self.stats_var.set(f"CPU {ev.get('cpu', 0):.1f}%   Memory {ev.get('mem_text', '')}   Processes {ev.get('pids', '')}")
                self._spark()
        elif t == "top":
            run.top = (ev["header"], ev["rows"])
            if rid == self.current:
                self._fill_top(run.top)
        elif t == "end":
            run.ended = ev["t"]
            run.exit_code = ev.get("exit_code")
            st = ev.get("status", "")
            run.status = st
            run.chunks.append(("phase", f"▸ finished: {st}" + (f" (exit {run.exit_code})" if run.exit_code is not None else "") + "\n"))
            if rid == self.current:
                self._append(run.chunks[-1][1], "phase")
                self._finish_ui(run)
                self._card_end(run)
            self._refresh_pick()

    def _card_end(self, run: _Run) -> None:
        self.card.set_state("ended", {"completed": "Finished", "failed": "Failed", "cancelled": "Stopped", "timeout": "Timed out",
                                      "skipped": "Not started", "refused": "Refused"}.get(run.status, run.status.title()))

    def _finish_ui(self, run: _Run) -> None:
        ok = run.status == "completed"
        state = "ok" if ok else ("warn" if run.status in ("cancelled", "timeout", "skipped", "refused") else "bad")
        self._chip(state, {"completed": "✔ Completed", "failed": "✖ Failed", "cancelled": "■ Cancelled", "timeout": "⏱ Timed out",
                           "skipped": "■ Skipped", "refused": "■ Refused"}.get(run.status, "✖ " + run.status.title()))
        self.stop_btn.config(state="disabled")
        dur = (run.ended or time.time()) - run.started
        self.status_var.set(f"{run.title}   -   {run.status}" + (f", exit {run.exit_code}" if run.exit_code is not None else "") + f", {dur:.1f}s")

    def _tick(self) -> None:                      # elapsed timer while running
        run = self.runs.get(self.current) if self.current else None
        if run is not None and run.ended is None:
            self.status_var.set(f"{run.title}   -   {time.time() - run.started:.0f}s")
        if self.winfo_exists():
            self.after(500, self._tick)

    # ------------------------------------------------------------------ rendering
    def _show(self, rid: str) -> None:
        self.current = rid
        run = self.runs[rid]
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        for tag, txt in run.chunks:
            self._append(txt, {"out": "", "err": "err", "phase": "phase"}.get(tag, ""), scroll=False)
        self._scroll()
        self.timeline.delete(0, "end")
        for p in run.phases:
            self.timeline.insert("end", p)
        self.card.reset()
        for tag, txt in run.chunks:
            if tag in ("out", "err"):
                self.card.add_text(txt)
        if run.png:
            self.card.set_frame(run.png)
        if run.web:
            self.card.set_web(*run.web)
        self._popout_show(run)
        if run.ended is not None:
            self._card_end(run)
        else:
            self.card.set_state("running")
        self.meta_var.set(run.meta)
        self.stats_var.set("")
        self._fill_top(run.top)
        self._spark()
        if run.ended is None:
            self._chip("running")
            self.stop_btn.config(state="normal")
        else:
            self._finish_ui(run)
        self.pick_var.set(self._label(run))

    def _append(self, text: str, tag: str = "", scroll: bool = True) -> None:
        self.text.config(state="normal")
        self.text.insert("end", text, tag or ())
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            self.text.delete("1.0", f"{lines - MAX_LINES}.0")
        self.text.config(state="disabled")
        if scroll:
            self._scroll()

    def _scroll(self) -> None:
        if self.autoscroll_var.get():
            self.text.see("end")

    def _wrap(self) -> None:
        self.text.configure(wrap="word" if self.wrap_var.get() else "none")

    def _fill_top(self, top) -> None:
        self.res.set_top(top)

    def _spark(self) -> None:
        run = self.runs.get(self.current) if self.current else None
        self.res.draw(run.cpu if run else [], run.mem if run else [])

    def _highlight(self) -> None:
        self.text.tag_remove("hit", "1.0", "end")
        q = self.find_var.get()
        if not q:
            return
        i, first = "1.0", None
        while True:
            i = self.text.search(q, i, stopindex="end", nocase=True)
            if not i:
                break
            j = f"{i}+{len(q)}c"
            self.text.tag_add("hit", i, j)
            first = first or i
            i = j
        if first:
            self.text.see(first)

    # ------------------------------------------------------------------ thumbnail: external view, snapshots
    @staticmethod
    def _web_card(ev: dict) -> tuple:
        url = ev.get("url", "")
        if not ev.get("up"):
            return (False, "", "", url)
        body = ev.get("body") or ""
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
        ct = str((ev.get("headers") or {}).get("Content-Type", "")).lower()
        if "html" in ct or "<html" in body[:500].lower():
            from ui.app_preview_view import page_text           # the same readable-text snapshot App Preview uses
            text = page_text(body)
        else:
            text = body[:600]
        return (True, _html.unescape(m.group(1).strip()) if m else "", text, url)

    def _card_click(self, mode: str) -> None:
        run = self.runs.get(self.current) if self.current else None
        if run is None:
            return
        if mode == "gui" and run.png:
            self._open_popout(run)
        elif mode == "web" and run.web and run.web[0] and str(run.web[3]).startswith("http://127.0.0.1:"):
            webbrowser.open(run.web[3])

    def _open_popout(self, run: _Run) -> None:
        if self._popout is not None and self._popout.winfo_exists():
            self._popout.deiconify()
            self._popout.lift()
        else:
            self._popout = ScreenWindow(self.winfo_toplevel(), "Live Run - screen", on_close=self._popout_closed,
                                        on_save_as=self._snap_save_as, on_quick=self._snap_quick,
                                        placeholder="This run has no screen picture.")
        self._popout_show(run)

    def _popout_closed(self) -> None:
        self._popout = None

    def _popout_show(self, run: _Run) -> None:
        if self._popout is None or not self._popout.winfo_exists():
            return
        self._popout.title(f"Live Run - screen ({run.title[:50]})")
        if run.png:
            self._popout.show(run.png, f"{run.title[:60]}  -  {run.status}")
        else:
            self._popout.clear("This run has no screen picture.")

    def _snap_png(self) -> bytes:
        run = self.runs.get(self.current) if self.current else None
        return run.png if run else b""

    def _snap_save_as(self) -> None:
        p = ask_save_snapshot(self, self._snap_png(), self.current or "")
        if p:
            self.status_var.set(f"Snapshot saved: {p}")

    def _snap_quick(self) -> None:
        try:
            self.status_var.set(f"Snapshot saved: {SNAPSHOTS.save(self._snap_png(), self.current or '')}")
        except OSError as exc:
            self.status_var.set(f"Snapshot not saved: {exc}")

    # ------------------------------------------------------------------ actions
    def _label(self, run: _Run) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(run.started))}  {run.title[:40]}  [{run.status}]"

    def _refresh_pick(self) -> None:
        self.pick["values"] = [self._label(self.runs[r]) for r in self.order if r in self.runs]

    def _on_pick(self, _e=None) -> None:
        i = self.pick.current()
        if 0 <= i < len(self.order):
            self._show(self.order[i])

    def stop(self) -> None:
        if self.current and not BUS.request_cancel(self.current):
            self.stop_btn.config(state="disabled")
        else:
            self.status_var.set("Stopping - the container is being killed...")

    def clear(self) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        run = self.runs.get(self.current)
        if run:
            run.chunks, run.size = [], 0

    def copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end-1c"))

    def save(self) -> None:
        p = filedialog.asksaveasfilename(defaultextension=".log", initialfile="run_output.log")
        if p:
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(self.text.get("1.0", "end-1c"))
            except OSError as exc:
                messagebox.showerror("Save", str(exc), parent=self)

    def _on_destroy(self, e) -> None:
        if e.widget is self:
            for u in self._unsub:
                u()
            if self._popout is not None:
                try:
                    self._popout.destroy()
                except tk.TclError:
                    pass
                self._popout = None
