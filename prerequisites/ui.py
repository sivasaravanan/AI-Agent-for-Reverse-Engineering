"""Tkinter front end: Prerequisites tab, approval dialog, progress window, settings dialog and the start-up prompt.
All long work runs on a worker thread; the UI polls a queue (no Tk calls off the main thread)."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

from prerequisites import (BY_ID, CATALOG, CONTAINER_RUNTIMES, Approval, Env, InstallPlan, PlanRefusal, build_it_request, build_plan,
                           container_ready, detect_all, diagnose, expand_dependencies, load_config, network, offline_manifest,
                           run_plan, save_user_config, start_runtime, tools_env)
from prerequisites import startup as st
from prerequisites.audit import AuditLog, audit_path
from prerequisites.config import DEFAULTS, user_config_path
from prerequisites.detect import INSTALLED, MISSING, READY, Status

STATE_TEXT = {READY: "Ready", INSTALLED: "Installed - not running", MISSING: "Missing", "unsupported": "n/a"}


def _modal(win: tk.Toplevel, parent, modal: bool) -> None:
    win.transient(parent.winfo_toplevel())
    if modal:
        win.grab_set()
        win.wait_window()


class ProgressWindow(tk.Toplevel):
    """Live log of an install with a Cancel button. ``post()`` is thread-safe."""

    def __init__(self, parent, title: str, cancel: threading.Event):
        super().__init__(parent)
        self.title(title)
        self.geometry("720x380")
        self._q: "queue.Queue[str]" = queue.Queue()
        self.cancel_ev = cancel
        self.text = tk.Text(self, wrap="word", height=18, state="disabled")
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.btn = ttk.Button(row, text="Cancel", command=self._cancel)
        self.btn.pack(side="right")
        self.status = tk.StringVar(value="Working...")
        ttk.Label(row, textvariable=self.status).pack(side="left")
        self.after(100, self._drain)

    def post(self, msg: str) -> None:
        self._q.put(msg)

    def post_finish(self, summary: str) -> None:
        """Thread-safe: switch the window to its 'done' state."""
        self._q.put(("__finish__", summary))     # type: ignore[arg-type]

    def _drain(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if isinstance(item, tuple):
                    self.finish(item[1])
                    continue
                self.text.configure(state="normal")
                self.text.insert("end", item + "\n")
                self.text.see("end")
                self.text.configure(state="disabled")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def _cancel(self) -> None:
        self.cancel_ev.set()
        self.status.set("Cancelling...")

    def finish(self, summary: str) -> None:
        self.status.set(summary)
        self.btn.configure(text="Close", command=self.destroy)


class ApprovalDialog(tk.Toplevel):
    """Shows every plan in full; the user must tick the licence box for licence-gated products. ``result`` = list of Approval or None."""

    def __init__(self, parent, plans: List[InstallPlan], refusals: List[PlanRefusal], modal: bool = True):
        super().__init__(parent)
        self.title("Review before installing")
        self.geometry("820x560")
        self.plans, self.result = plans, None
        txt = tk.Text(self, wrap="word", height=22)
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        body = "\n\n".join(p.describe() for p in plans)
        if refusals:
            body += "\n\nCannot be installed automatically:\n" + "\n".join(f"- {r.prereq_id}: {r.reason} {r.advice} {r.manual_url}" for r in refusals)
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        self.lic_vars: Dict[str, tk.BooleanVar] = {}
        for p in plans:
            if p.license_ack_required:
                v = tk.BooleanVar(value=False)
                self.lic_vars[p.prereq_id] = v
                ttk.Checkbutton(self, text=f"I accept the {p.name} licence terms (needed for the install)", variable=v).pack(anchor="w", padx=12)
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=8)
        self.ok_btn = ttk.Button(row, text="Install (approve these steps)", command=self._ok)
        self.ok_btn.pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        _modal(self, parent, modal)

    def _ok(self) -> None:
        for p in self.plans:
            if p.license_ack_required and not self.lic_vars[p.prereq_id].get():
                messagebox.showinfo("Licence", f"Please accept the {p.name} licence terms, or cancel.", parent=self)
                return
        self.result = [Approval.for_plan(p, license_accepted=p.license_ack_required) for p in self.plans]
        self.destroy()


class SettingsDialog(tk.Toplevel):
    FIELDS = (("mode", "When to offer installs", ("ask", "auto", "off")), ("prefer_runtime", "Preferred container runtime", ("auto", "podman", "docker")),
              ("startup_check", "Check at start-up", None), ("prefer_user_scope", "Prefer installs that need no administrator rights", None),
              ("proxy", "Proxy (http://host:port)", "str"), ("no_proxy", "No-proxy hosts (comma separated)", "str"),
              ("ca_bundle", "Institution root CA file (PEM)", "file"), ("offline_dir", "Offline installer folder", "dir"),
              ("mirror_base_url", "Internal installer mirror (https://...)", "str"), ("pip_index_url", "pip index URL", "str"),
              ("npm_registry", "npm registry URL", "str"), ("registry_mirror", "Container registry mirror prefix", "str"))

    def __init__(self, parent, cfg, user_path: Path, modal: bool = True):
        super().__init__(parent)
        self.title("Prerequisite settings")
        self.cfg, self.user_path = cfg, user_path
        self.vars: Dict[str, tk.Variable] = {}
        self.saved = False
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        for r, (key, label, kind) in enumerate(self.FIELDS):
            locked = key in cfg.locked
            ttk.Label(frm, text=label + ("  (set by institution policy)" if locked else "")).grid(row=r, column=0, sticky="w", pady=2)
            val = cfg.values[key]
            if isinstance(val, bool):
                var: tk.Variable = tk.BooleanVar(value=val)
                w = ttk.Checkbutton(frm, variable=var)
            elif isinstance(kind, tuple):
                var = tk.StringVar(value=val)
                w = ttk.Combobox(frm, textvariable=var, values=kind, state="readonly", width=12)
            else:
                var = tk.StringVar(value=val)
                w = ttk.Entry(frm, textvariable=var, width=52)
            w.grid(row=r, column=1, sticky="we", pady=2)
            if locked:
                w.state(["disabled"])
            if kind in ("file", "dir") and not locked:
                ttk.Button(frm, text="...", width=3, command=lambda v=var, k=kind: self._browse(v, k)).grid(row=r, column=2)
            self.vars[key] = var
        ttk.Label(frm, text="Credentials typed into a proxy URL are stored as plain text; prefer your system/environment proxy.",
                  foreground="#9a6700").grid(row=len(self.FIELDS), column=0, columnspan=3, sticky="w", pady=6)
        row = ttk.Frame(frm)
        row.grid(row=len(self.FIELDS) + 1, column=0, columnspan=3, sticky="e")
        ttk.Button(row, text="Save", command=self._save).pack(side="right")
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right", padx=6)
        _modal(self, parent, modal)

    def _browse(self, var: tk.Variable, kind: str) -> None:
        p = filedialog.askdirectory(parent=self) if kind == "dir" else filedialog.askopenfilename(parent=self)
        if p:
            var.set(p)

    def _save(self) -> None:
        warns = save_user_config({k: v.get() for k, v in self.vars.items()}, self.user_path, self.cfg)
        self.saved = True
        if warns:
            messagebox.showwarning("Settings", "\n".join(warns), parent=self)
        self.destroy()


class PrerequisitesTab(ttk.Frame):
    def __init__(self, parent, env: Optional[Env] = None, cfg=None, root_dir: Optional[Path] = None,
                 on_change: Optional[Callable[[], None]] = None, auto_refresh: bool = True):
        super().__init__(parent)
        self.env = env or Env.real(root=root_dir)
        self.cfg = cfg or load_config(user_config_path(self.env.root))
        self.on_change = on_change
        self.on_decision: Optional[Callable[[st.Decision], None]] = None
        self.statuses: List[Status] = []
        self._events: "queue.Queue[tuple]" = queue.Queue()
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        for label, cmd in (("Re-check", self.refresh_async), ("Install selected...", self.install_selected), ("Start runtime", self.start_selected),
                           ("Network test", self.network_test), ("IT request...", self.show_it_request), ("Offline list...", self.show_offline),
                           ("Hurdles...", self.show_hurdles), ("Pull sandbox images...", self.pull_images), ("Settings...", self.open_settings),
                           ("Audit log", self.show_audit)):
            ttk.Button(top, text=label, command=cmd).pack(side="left", padx=2)
        self.summary = tk.StringVar(value="Checking...")
        ttk.Label(self, textvariable=self.summary, padding=(8, 0), wraplength=1100).pack(fill="x")
        cols = ("Item", "Category", "State", "Version", "Needed for / detail")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended", height=14)
        for c, w in zip(cols, (210, 120, 150, 200, 520)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.after(150, self._poll)
        self._first_check_done = False
        if auto_refresh:
            self.refresh_async()
        else:                       # embedded in the main window: check when the tab is first shown (no work at start-up)
            self.summary.set("Select this tab to check what is installed.")
            self.bind("<Map>", self._first_show, add="+")

    def _first_show(self, _evt=None) -> None:
        if not self._first_check_done:
            self._first_check_done = True
            self.refresh_async()

    # ---- plumbing
    def _worker(self, fn: Callable[[], object], tag: str) -> None:
        def run() -> None:
            try:
                self._events.put((tag, fn(), None))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI, never crashes the app
                self._events.put((tag, None, exc))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                tag, value, err = self._events.get_nowait()
                if err is not None:
                    messagebox.showerror("Prerequisites", f"{type(err).__name__}: {err}", parent=self)
                elif tag == "statuses":
                    self._show(value)
                elif tag == "text":
                    self._text_window(*value)
                elif tag == "refresh":
                    self.refresh_async()
                elif tag == "decision" and self.on_decision:
                    self.on_decision(value)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def refresh_async(self) -> None:
        self._first_check_done = True
        self.summary.set("Checking installed tools...")
        self._worker(lambda: (tools_env.activate(self.env.root, self.env.platform), detect_all(self.env))[1], "statuses")

    def _show(self, statuses: List[Status]) -> None:
        self.statuses = statuses
        self.tree.delete(*self.tree.get_children())
        for s in statuses:
            p = BY_ID[s.id]
            self.tree.insert("", "end", iid=s.id, values=(s.name, p.category, STATE_TEXT.get(s.state, s.state), s.version, s.detail or p.why))
        ready, backend = container_ready(statuses)
        self.summary.set((f"Container runtime ready: {backend}. Builds and tests can run in isolation." if ready else
                          "No container runtime is ready - builds/tests are skipped (static analysis still works). Select Podman or Docker and choose "
                          "'Install selected...'. Nothing is installed without your approval."))
        if self.on_change:
            self.on_change()

    def selected_ids(self) -> List[str]:
        return list(self.tree.selection())

    # ---- actions
    def install_selected(self, ids: Optional[List[str]] = None, modal: bool = True) -> None:
        ids = ids or self.selected_ids()
        if not ids:
            messagebox.showinfo("Prerequisites", "Select one or more items first.", parent=self)
            return
        ready = {s.id for s in self.statuses if s.state == READY}
        missing = [i for i in ids if i not in ready]
        if not missing:
            messagebox.showinfo("Prerequisites", "The selected items are already installed.", parent=self)
            return
        built = [build_plan(i, self.env, self.cfg) for i in expand_dependencies(missing, self.env)]
        plans = [p for p in built if isinstance(p, InstallPlan)]
        refusals = [p for p in built if isinstance(p, PlanRefusal)]
        if not plans:
            self._text_window("Cannot install automatically", "\n".join(f"{r.prereq_id}: {r.reason}\n  {r.advice}\n  {r.manual_url}" for r in refusals) +
                              "\n\nUse 'IT request...' to get a text for your IT desk.")
            return
        dlg = ApprovalDialog(self, plans, refusals, modal=modal)
        if dlg.result:
            self.run_approved(plans, dlg.result)

    def run_approved(self, plans: List[InstallPlan], approvals: List[Approval]) -> None:
        cancel = threading.Event()
        win = ProgressWindow(self, "Installing prerequisites", cancel)
        audit = AuditLog()

        def work() -> None:
            summary: List[str] = []
            try:
                for plan, ap in zip(plans, approvals):
                    win.post(f"=== {plan.name} ===")
                    res = run_plan(plan, ap, self.env, self.cfg, progress=win.post, cancel=cancel, audit=audit)
                    summary.append(f"{plan.name}: {res.status}" + (f" - {res.detail}" if res.detail else ""))
                    win.post(summary[-1] + (f"\n   {res.advice}" if res.advice else ""))
                    if not res.ok:
                        break
            except Exception as exc:  # noqa: BLE001
                summary.append(f"Failed: {type(exc).__name__}: {exc}")
            win.post_finish("; ".join(summary))
            self._events.put(("refresh", None, None))
        threading.Thread(target=work, daemon=True).start()

    def start_selected(self) -> None:
        ids = [i for i in self.selected_ids() if i in CONTAINER_RUNTIMES] or [s.id for s in self.statuses if s.id in CONTAINER_RUNTIMES and s.state == INSTALLED]
        if not ids:
            messagebox.showinfo("Prerequisites", "Select an installed container runtime (Podman or Docker) to start.", parent=self)
            return
        win = ProgressWindow(self, "Starting runtime", threading.Event())

        def work() -> None:
            s = start_runtime(ids[0], self.env, progress=win.post)
            win.post(f"{s.name}: {STATE_TEXT.get(s.state, s.state)} {s.detail}")
            win.post_finish(f"{s.name}: {STATE_TEXT.get(s.state, s.state)}")
            self._events.put(("refresh", None, None))
        threading.Thread(target=work, daemon=True).start()

    def network_test(self) -> None:
        def work():
            rows = network.check_connectivity(self.cfg)
            return ("Network test (through the configured proxy / CA)", "\n".join(
                f"{r['host']:32} {'OK' if r['ok'] == 'yes' else 'FAILED'}  {r['detail']}\n    {r['advice']}" for r in rows))
        self._worker(work, "text")

    def show_it_request(self) -> None:
        ids = self.selected_ids() or [i for i in ("wsl2", "podman") if i in BY_ID and (i != "wsl2" or self.env.platform == "windows")]
        text = build_it_request(expand_dependencies(ids, self.env), self.env, self.cfg)
        self._text_window("IT request (copy and send)", text)

    def show_offline(self) -> None:
        import json
        ids = self.selected_ids() or ["podman", "docker"]
        self._text_window("Offline / air-gapped download list", json.dumps(offline_manifest(ids, self.env, self.cfg), indent=2))

    def show_hurdles(self) -> None:
        def work():
            dg = diagnose(self.env, self.cfg, connectivity=network.check_connectivity(self.cfg), statuses=self.statuses)
            body = "\n\n".join(f"[{h.severity.upper()}] {h.title}\n  {h.detail}\n  -> {h.fix}" for h in dg.hurdles) or "No hurdles detected."
            return ("Institutional hurdles", body)
        self._worker(work, "text")

    def pull_images(self) -> None:
        ok, backend = container_ready(self.statuses)
        if not ok:
            messagebox.showinfo("Prerequisites", "No container runtime is ready yet.", parent=self)
            return
        if not messagebox.askyesno("Pull sandbox images", "This downloads several GB of container images (python, node, .NET, Maven, Gradle) "
                                   "from the registry or your 'registry mirror'. Continue?", parent=self):
            return
        from prerequisites import pull_sandbox_images
        cancel = threading.Event()
        win = ProgressWindow(self, "Pulling sandbox images", cancel)

        def work() -> None:
            results = pull_sandbox_images(self.env, self.cfg, backend, progress=win.post, cancel=cancel)
            win.post_finish("; ".join(f"{r.prereq_id}: {r.status}" for r in results))
            self._events.put(("refresh", None, None))
        threading.Thread(target=work, daemon=True).start()

    def open_settings(self, modal: bool = True) -> None:
        dlg = SettingsDialog(self, self.cfg, user_config_path(self.env.root), modal=modal)
        if dlg.saved:
            self.cfg = load_config(user_config_path(self.env.root))

    def show_audit(self) -> None:
        recs = AuditLog().read(300)
        self._text_window(f"Audit log ({audit_path()})", "\n".join(f"{r.get('ts','')}  {r.get('event','')}  " + ", ".join(
            f"{k}={v}" for k, v in r.items() if k not in ('ts', 'event'))[:300] for r in recs) or "No actions recorded yet.")

    def _text_window(self, title: str, body: str) -> None:
        w = tk.Toplevel(self)
        w.title(title)
        w.geometry("820x520")
        t = tk.Text(w, wrap="word")
        t.pack(fill="both", expand=True, padx=8, pady=8)
        t.insert("1.0", body)
        row = ttk.Frame(w)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="Copy", command=lambda: (w.clipboard_clear(), w.clipboard_append(body))).pack(side="left")
        ttk.Button(row, text="Close", command=w.destroy).pack(side="right")


class StartupPrompt(tk.Toplevel):
    """Shown once at start when no container runtime is ready. Options: install (reviewed), start, IT request, remind later, never."""

    def __init__(self, parent, tab: PrerequisitesTab, decision: st.Decision, modal: bool = False):
        super().__init__(parent)
        self.tab, self.decision = tab, decision
        self.title("Set up isolated build/test execution?")
        self.geometry("640x400")
        installed = next((s for s in tab.statuses if s.id in CONTAINER_RUNTIMES and s.state == INSTALLED), None)
        msg = ("The analysis works without it, but builds and tests of uploaded projects only run inside a container (Podman or Docker). "
               "Nothing is installed without showing you the exact steps first.")
        if installed:
            msg = f"{installed.name} is installed but not running. " + msg
        ttk.Label(self, text=msg, wraplength=600, padding=10).pack(fill="x")
        self.choice = tk.StringVar(value=decision.recommended_runtime or "podman")
        for rid in CONTAINER_RUNTIMES:
            if rid in tab.cfg.blocked:
                continue
            p = BY_ID[rid]
            ttk.Radiobutton(self, text=f"{p.name} - {p.summary}" + ("  [licence required]" if p.license_ack else ""), variable=self.choice,
                            value=rid).pack(anchor="w", padx=16, pady=2)
        self.extras = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Also install the optional analysis tools Ruff and SQLFluff privately (no administrator rights)",
                        variable=self.extras).pack(anchor="w", padx=16, pady=8)
        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=10, side="bottom")
        ttk.Button(row, text="Review & install...", command=self._install).pack(side="left")
        if installed:
            ttk.Button(row, text="Start it now", command=self._start).pack(side="left", padx=6)
        ttk.Button(row, text="IT request...", command=lambda: (tab.show_it_request(), None)).pack(side="left", padx=6)
        ttk.Button(row, text="Don't ask again", command=self._never).pack(side="right")
        ttk.Button(row, text=f"Remind me in {tab.cfg.snooze_days} days", command=self._later).pack(side="right", padx=6)
        _modal(self, parent, modal)

    def _install(self) -> None:
        ids = [self.choice.get()] + (["ruff", "sqlfluff"] if self.extras.get() else [])
        self.destroy()
        self.tab.install_selected(ids)

    def _start(self) -> None:
        rid = self.choice.get()
        self.destroy()
        if self.tab.tree.exists(rid):
            self.tab.tree.selection_set([rid])
        self.tab.start_selected()

    def _later(self) -> None:
        st.snooze(self.tab.cfg.snooze_days, st.state_path(self.tab.env.root))
        self.destroy()

    def _never(self) -> None:
        st.dont_ask_again(st.state_path(self.tab.env.root))
        self.destroy()


def attach(root: tk.Tk, notebook: ttk.Notebook, root_dir: Optional[Path] = None, env: Optional[Env] = None, cfg=None,
           startup_delay_ms: int = 1500, skip_startup: bool = False) -> PrerequisitesTab:
    """Add the Prerequisites tab and schedule the (non-blocking) start-up check."""
    tab = PrerequisitesTab(notebook, env=env, cfg=cfg, root_dir=root_dir)
    notebook.add(tab, text="Prerequisites")

    def on_decision(d: st.Decision) -> None:
        if d.action == "prompt":
            StartupPrompt(root, tab, d)
        elif d.action == "auto":
            ids = expand_dependencies(d.offer, tab.env)
            plans = [p for p in (build_plan(i, tab.env, tab.cfg) for i in ids) if isinstance(p, InstallPlan)]
            if plans and not any(p.license_ack_required for p in plans):
                tab.run_approved(plans, [Approval.for_plan(p) for p in plans])

    tab.on_decision = on_decision

    def start_check() -> None:
        tab._worker(lambda: st.decide(tab.statuses or detect_all(tab.env), tab.cfg, st.load_state(st.state_path(tab.env.root)),
                                      env_skip=skip_startup), "decision")
    root.after(startup_delay_ms, start_check)
    return tab
