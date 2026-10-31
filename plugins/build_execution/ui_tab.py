"""Optional Tkinter tab: discover candidates, review the exact plan, approve, run in a disposable container.
Runs off the UI thread and talks to it only through a queue."""
from __future__ import annotations

import queue
import shlex
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .discovery import custom_candidate, discover_candidates
from .run_targets import discover_all          # build/test commands + root entry file / runnable files
from .autotune import apply_autotune, retry_limits
from .models import Approval, Limits, Options
from .sandbox import build_plan, container_command, isolation_status, run_plan


class ApprovalDialog(tk.Toplevel):
    """Modal. Shows everything that will happen. `self.approved` is True only if the user clicks Approve."""

    def __init__(self, parent, plan):
        super().__init__(parent)
        self.title("Approve execution")
        self.transient(parent)
        self.approved = False
        c, lim = plan.candidate, plan.limits
        txt = tk.Text(self, width=110, height=22, wrap="word")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        lines = [
            "Review before approving. Nothing has been executed yet.\n",
            f"Command (runs INSIDE the container):\n    {c.display}\n",
            f"Origin: {c.origin}   Source: {c.source_file or '-'}{':' + str(c.source_line) if c.source_line else ''}",
            f"Image: {plan.image}  (never pulled automatically)",
            f"Network: {'ENABLED - the project code can reach the network' if lim.network else 'disabled'}",
            f"Limits: {lim.cpus} CPU, {lim.memory_mb} MB RAM, {lim.pids} processes, {lim.timeout_s}s timeout",
            "Isolation: disposable copy of the project only; no host folders, no credentials, read-only root "
            "filesystem, all capabilities dropped, non-root user. Workspace and container are deleted afterwards.\n",
        ]
        if c.risks:
            lines.append("Risks:")
            lines += [f"  - {r}" for r in c.risks]
        if c.notes:
            lines.append("\nNotes:")
            lines += [f"  - {n}" for n in c.notes]
        lines.append("\nContainer command line:\n    " + " ".join(
            container_command("<runtime>", plan, "<scratch-copy>", "<name>")))
        txt.insert("end", "\n".join(lines))
        txt.config(state="disabled")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Approve and run", command=self._ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        self.approved = True
        self.destroy()


class BuildExecutionTab(ttk.Frame):
    def __init__(self, master, get_source=None):
        super().__init__(master)
        self._get_source = get_source
        self._q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._cands: list = []
        self._build()
        self.after(150, self._poll)
        self._check_isolation()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.src = tk.StringVar(value=(self._get_source() or "") if self._get_source else "")
        self._discovered_for = ""
        if self._get_source is not None:
            # Hosted in the main window: the project chosen there is used automatically (no second picker).
            ttk.Label(top, text="Project:").pack(side="left")
            ttk.Label(top, textvariable=self.src, style="Muted.TLabel" if False else "TLabel").pack(side="left", fill="x", expand=True, padx=6)
            self.bind("<Map>", lambda _e: self.refresh_source(), add="+")
        else:
            ttk.Label(top, text="Source (ZIP or folder):").pack(side="left")
            ttk.Entry(top, textvariable=self.src).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(top, text="ZIP...", command=lambda: self._pick(False)).pack(side="left")
            ttk.Button(top, text="Folder...", command=lambda: self._pick(True)).pack(side="left", padx=4)
        ttk.Button(top, text="Discover commands", command=self._discover).pack(side="left")
        self.auto_start = tk.BooleanVar(value=True)

        self.iso = tk.StringVar(value="Checking container runtime...")
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8)
        ttk.Label(row, textvariable=self.iso, wraplength=900).pack(side="left")
        ttk.Button(row, text="Re-check", command=self._check_isolation).pack(side="right")
        self.start_btn = ttk.Button(row, text="Start Docker/Podman", command=self._start_runtime)
        self.start_btn.pack(side="right", padx=4)
        ttk.Checkbutton(row, text="Start automatically when needed", variable=self.auto_start).pack(side="right", padx=6)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        self.run_btn = ttk.Button(bar, text="Review and run...", command=self._run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel run", command=self._cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.status = tk.StringVar(value="Idle.")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=10)
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=120)
        self.prog.pack(side="right")

        box = ttk.LabelFrame(self, text="Run options")          # click the title to fold / unfold
        box.pack(fill="x", padx=8, pady=(0, 4))
        opt = ttk.Frame(box)
        opt.pack(fill="x")
        self.timeout = tk.IntVar(value=300)
        self.mem = tk.IntVar(value=1024)
        ttk.Label(opt, text="Timeout (s):").pack(side="left")
        ttk.Spinbox(opt, from_=10, to=3600, textvariable=self.timeout, width=6).pack(side="left", padx=4)
        ttk.Label(opt, text="Memory (MB):").pack(side="left")
        ttk.Spinbox(opt, from_=128, to=16384, textvariable=self.mem, width=6).pack(side="left", padx=4)
        ttk.Label(opt, text="Image override:").pack(side="left", padx=(10, 2))
        self.image = tk.StringVar()
        ttk.Entry(opt, textvariable=self.image, width=34).pack(side="left")
        self.autotune = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Auto-tune limits for this project", variable=self.autotune).pack(side="left", padx=10)
        cust = ttk.Frame(box)
        cust.pack(fill="x", pady=(4, 0))
        ttk.Label(cust, text="Custom command:").pack(side="left")
        self.custom = tk.StringVar()
        ttk.Entry(cust, textvariable=self.custom).pack(side="left", fill="x", expand=True, padx=6)
        self.eco = tk.StringVar(value="python")
        ttk.Combobox(cust, textvariable=self.eco, width=8, values=("python", "node", "dotnet", "maven", "gradle")
                     ).pack(side="left")

        # commands table above, output below: drag the splitter between them
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tbox = ttk.Frame(pw)
        cols = ("id", "network", "command", "risks")
        self.tree = ttk.Treeview(tbox, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (170, 70, 420, 300)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        tsb = ttk.Scrollbar(tbox, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        pw.add(tbox, weight=1)
        obox = ttk.Frame(pw)
        self.out = tk.Text(obox, height=10, wrap="none")
        osy = ttk.Scrollbar(obox, orient="vertical", command=self.out.yview)
        osx = ttk.Scrollbar(obox, orient="horizontal", command=self.out.xview)
        self.out.configure(yscrollcommand=osy.set, xscrollcommand=osx.set)
        osy.pack(side="right", fill="y")
        osx.pack(side="bottom", fill="x")
        self.out.pack(side="left", fill="both", expand=True)
        pw.add(obox, weight=2)

    def _source(self) -> str:
        if self._get_source is not None:
            return (self._get_source() or "").strip()
        return self.src.get().strip()

    def refresh_source(self):
        """Follow the host's current project; re-discover (read-only, nothing is run) when it changed."""
        if self._get_source is None:
            return
        cur = (self._get_source() or "").strip()
        self.src.set(cur or "No project selected - choose one in the toolbar above.")
        if cur and cur != self._discovered_for:
            self._discovered_for = cur
            self._discover()

    def _pick(self, folder):
        p = filedialog.askdirectory() if folder else filedialog.askopenfilename(
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")])
        if p:
            self.src.set(p)

    def _check_isolation(self):
        def work():
            self._q.put(("iso", isolation_status()))
        threading.Thread(target=work, daemon=True).start()

    def _start_runtime(self):
        """Explicit start (also what a run does automatically when the box is ticked). Installed runtimes only; nothing is installed."""
        self.start_btn.config(state="disabled")
        self._start_cancel = threading.Event()

        def work():
            from .runtime_start import ensure_runtime
            st = ensure_runtime(Options(), progress=lambda m: self._q.put(("progress", m)), cancel=self._start_cancel)
            self._q.put(("iso", st))
            self._q.put(("startdone", None))
        threading.Thread(target=work, daemon=True).start()

    def _discover(self):
        if not self._source():
            messagebox.showinfo("Discover", "Choose a project (Archive or Folder) in the toolbar first.", parent=self)
            return
        try:
            self._cands, notes = discover_all(self._source())
        except Exception as exc:
            messagebox.showerror("Discover", f"Could not read the source: {exc}", parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        for c in self._cands:
            self.tree.insert("", "end", values=(c.id, "YES" if c.needs_network else "no", c.display,
                                                "; ".join(c.risks)[:200]))
        self.out.delete("1.0", "end")
        self.out.insert("end", "\n".join(notes) or "Select a command and click 'Review and run...'.")

    def _run(self, retry_limits_override=None, _skip_gui_check=False):
        if retry_limits_override is None:
            self._retry_in_progress = False
        src = self._source()
        try:
            if self.custom.get().strip():
                cand = custom_candidate(shlex.split(self.custom.get()), self.eco.get())
            else:
                sel = self.tree.selection()
                if not sel:
                    messagebox.showinfo("Run", "Discover and select a command, or type a custom one.", parent=self)
                    return
                cand = self._cands[self.tree.index(sel[0])]
            if retry_limits_override is None and not _skip_gui_check and self._offer_window_program(cand, src):
                return
            lim = retry_limits_override or Limits(timeout_s=int(self.timeout.get()), memory_mb=int(self.mem.get()))
            if self.autotune.get():
                cand, lim, tuned = apply_autotune(cand, lim, src)
                if retry_limits_override is not None:       # keep the larger retry limits, never fall back below them
                    lim = Limits(**{**vars(lim), "timeout_s": max(lim.timeout_s, retry_limits_override.timeout_s),
                                    "memory_mb": max(lim.memory_mb, retry_limits_override.memory_mb)})
                if tuned:
                    self.status.set("Auto-tuned: " + "; ".join(tuned)[:160])
            self._last_run = (cand, lim, bool(self.autotune.get()))
            plan = build_plan(src, cand, Options(), image=self.image.get().strip(), limits=lim)
        except Exception as exc:
            messagebox.showerror("Run", str(exc), parent=self)
            return
        dlg = ApprovalDialog(self, plan)
        if not dlg.approved:
            self.status.set("Not approved - nothing was executed.")
            return
        approval = Approval.for_plan(plan)
        self._cancel.clear()
        self.out.delete("1.0", "end")
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog.start(12)

        auto = self.auto_start.get()

        def work():
            res = run_plan(plan, approval, Options(auto_start=bool(auto)), progress=lambda m: self._q.put(("progress", m)),
                           cancel=self._cancel)
            self._q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()

    on_gui_run = None            # hook(source, script): hosts that have App Preview set it; window programs are handed over

    def _offer_window_program(self, cand, src) -> bool:
        """A Tk/Qt/pygame script cannot show its window in this plain sandbox (no display, no Tk libraries): offer App Preview.
        Reading the project to find out can take long on a big folder, so it runs on a worker thread; this returns True when the
        hand-over has been started (the normal run then waits for the answer)."""
        hook = self.on_gui_run
        if hook is None:
            return False
        try:
            from plugins.build_execution.sandbox import entry_script
            script = entry_script(cand.argv)
        except Exception:  # noqa: BLE001
            return False
        if not script:
            return False
        argv = list(cand.argv)

        def check() -> None:
            try:
                from app_preview.discovery import custom_entry, entry_uses_gui
                uses = bool(entry_uses_gui(src, custom_entry(argv)))
            except Exception:  # noqa: BLE001 - detection only; the normal run path stays available
                uses = False
            self._q.put(("gui_check", (uses, script, src)))
        self.status.set("Checking whether the program opens a window...")
        self.run_btn.config(state="disabled")
        threading.Thread(target=check, daemon=True).start()
        return True

    def _gui_checked(self, payload) -> None:
        uses, script, src = payload
        self.run_btn.config(state="normal")
        hook = self.on_gui_run
        if uses and hook is not None and messagebox.askyesno(
                "Run", f"{script} opens a window (Tk / Qt / pygame).\n\nBuild & Test runs in a plain container with no "
                "screen, so it would fail or wait until the timeout.\n\nRun it in App Preview instead? That uses a "
                "container with a virtual screen and shows the window live. You approve the exact plan there.", parent=self):
            self.status.set("Handed over to App Preview.")
            hook(src, script)
            return
        self.status.set("")
        self._run(retry_limits_override=None, _skip_gui_check=True)

    def _cancel_run(self):
        self._cancel.set()
        self.status.set("Cancelling...")

    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _handle(self, kind, payload):
        if kind == "gui_check":
            return self._gui_checked(payload)
        if kind == "iso":
            self.iso.set((f"Container runtime: {payload['backend']} {payload['version']} - available"
                          + ("  (started automatically)" if payload.get("started") else "")) if payload["available"]
                         else "Execution UNAVAILABLE: " + payload["reason"])
        elif kind == "startdone":
            self.start_btn.config(state="normal")
        elif kind == "progress":
            self.status.set(payload)
        elif kind == "done":
            self.prog.stop()
            self.run_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            r = payload
            self.status.set(f"{r.status}  exit={r.exit_code}  {r.duration_s}s")
            self.out.insert("end", "\n".join("NOTE: " + n for n in r.notes) + "\n\n--- stdout ---\n"
                            + r.stdout + "\n--- stderr ---\n" + r.stderr)
            self._check_isolation()                     # the runtime may have been started by this run
            self._on_done(r)
            self._offer_retry(r)

    def _offer_retry(self, r):
        """After a timeout or an out-of-memory kill, offer ONE retry with larger limits (still shown for approval)."""
        last = getattr(self, "_last_run", None)
        if not last or not last[2] or getattr(self, "_retry_in_progress", False):
            self._retry_in_progress = False
            return
        bigger = retry_limits(r.status, r.exit_code, last[1])
        if bigger is None:
            return
        what = (f"timeout {last[1].timeout_s}s -> {bigger.timeout_s}s" if r.status == "timeout"
                else f"memory {last[1].memory_mb} MB -> {bigger.memory_mb} MB")
        if messagebox.askyesno("Retry with larger limits", f"The run ended with '{r.status}' (exit {r.exit_code}).\n\n"
                               f"Retry once with {what}? You will review and approve the plan again.", parent=self):
            self._retry_in_progress = True
            self._run(retry_limits_override=bigger)

    def _on_done(self, result):
        """Hook for hosts (see ui.execution_dialog.ExecutionTab)."""
