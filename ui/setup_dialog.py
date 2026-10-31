"""Setup dialog: shows which prerequisites exist, and installs the missing ones only after the user confirms."""
from __future__ import annotations

import json
import os
import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Callable, Dict, List, Optional

from setup_doctor import apply_known_paths, run_actions, run_checks, summarize
from setup_doctor.catalog import Action, load_image_action
from setup_doctor.checks import find_runtime_exe, start_action, SystemEnv
from setup_doctor.recovery import diagnose

STATUS_TEXT = {"ok": "OK", "missing": "MISSING", "optional": "optional", "info": "info"}
STATUS_COLOR = {"ok": "#15803d", "missing": "#b91c1c", "optional": "#b45309", "info": "#6b7280"}


def state_path() -> Path:
    """Per-user file (not the install folder, which may be read-only)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "AISoftwareEngineeringAgent" / "setup_doctor.json"


def load_state(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict, path: Optional[Path] = None) -> None:
    p = path or state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _check_worker(env, q) -> None:
    """Thread body. Deliberately holds no Tk object: a worker that outlives the window must not free Tk objects itself."""
    try:
        q.put(("items", run_checks(env)))
    except Exception as exc:  # noqa: BLE001
        q.put(("log", f"Check failed: {type(exc).__name__}: {exc}"))
        q.put(("idle", None))


def _install_worker(groups, env, q, cancel) -> None:
    try:
        failed: List[str] = []
        for chain in groups:
            if cancel.is_set():
                break
            results = run_actions(chain, env, progress=lambda m: q.put(("log", m)), cancel=cancel)
            bad = [r for r in results if not r.ok]
            if bad:
                failed.append(f"{bad[0].id}: {bad[0].message}")
                if bad[0].id.startswith("install:docker") and not cancel.is_set():          # say WHY, and offer the way forward
                    try:
                        q.put(("recovery", diagnose(env, bad[0].returncode, bad[0].tail)))
                    except Exception as exc:  # noqa: BLE001 - a failed diagnosis must not hide the original failure
                        q.put(("log", f"(could not analyse the failure: {type(exc).__name__}: {exc})"))
        q.put(("log", "\nAll selected steps finished." if not failed else
               "\nFinished with problems:\n  " + "\n  ".join(failed)))
        q.put(("items", run_checks(env)))
    except Exception as exc:  # noqa: BLE001
        q.put(("log", f"Unexpected error: {type(exc).__name__}: {exc}"))
        q.put(("idle", None))


class SetupDialog(tk.Toplevel):
    def __init__(self, parent, env=None, on_done: Optional[Callable[[], None]] = None, state_file: Optional[Path] = None):
        super().__init__(parent)
        self.title("Set up prerequisites")
        self.geometry("860x640")
        self.env = env or SystemEnv()
        self.on_done = on_done
        self.state_file = state_file
        self.items: List = []
        self._vars: Dict[str, tk.BooleanVar] = {}
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self._recovery: List = []
        ttk.Label(self, padding=8, wraplength=820, justify="left", text=(
            "These tools are optional helpers. Analysis of ZIP projects works without them; Docker or Podman is only needed to "
            "run builds and tests in isolation. Nothing is downloaded or installed until you tick items and click "
            "'Install selected'. Windows shows its own permission prompt for installers.")).pack(fill="x")
        self.list_frame = ttk.Frame(self, padding=(8, 0))
        self.list_frame.pack(fill="x")
        ttk.Label(self, text="Commands that will run:", padding=(8, 6, 8, 0)).pack(anchor="w")
        self.plan = tk.Text(self, height=6, wrap="word", font=("Consolas", 9), state="disabled")
        self.plan.pack(fill="x", padx=8)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.install_btn = ttk.Button(bar, text="Install selected", command=self._install)
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Stop", command=self._cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.start_btn = ttk.Button(bar, text="▶ Start Docker", command=self._start_runtime, state="disabled")
        self.start_btn.pack(side="left", padx=(6, 0))
        self._tip(self.start_btn, "Start the installed container runtime (Docker Desktop / Podman machine) and wait until it answers. "
                                  "Enabled when a runtime is installed but not running.")
        ttk.Button(bar, text="Re-check", command=self.refresh).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Load images from file...", command=self._load_images).pack(side="left", padx=6)
        try:
            from ui.institution_menu import add_institution_button
            add_institution_button(self, bar)
        except Exception:  # noqa: BLE001 - optional extra
            pass
        ttk.Button(bar, text="Close", command=self._close).pack(side="right")
        self.dismiss_var = tk.BooleanVar(value=bool(load_state(state_file).get("dismissed")))
        ttk.Checkbutton(bar, text="Don't ask when the app starts", variable=self.dismiss_var).pack(side="right", padx=10)
        self.log = tk.Text(self, height=12, wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(150, self._poll)
        self.refresh()

    # ------------------------------------------------------------------ checks
    def refresh(self) -> None:
        if self._busy:
            return
        apply_known_paths(self.env)
        self._set_busy(True, "Checking...")
        threading.Thread(target=_check_worker, args=(self.env, self._q), daemon=True).start()

    def _tip(self, w, text: str) -> None:
        try:
            from ui import theme
            theme.Tooltip(w, text)
        except Exception:  # noqa: BLE001 - a tooltip is a convenience
            pass

    def _show_items(self, items: List) -> None:
        if any(i.id == "runtime" and i.status == "ok" for i in items):
            self._recovery = []                                       # a runtime is installed now: the repair rows are done (Start covers the rest)
        have = {i.id for i in items}
        items = [r for r in self._recovery if r.id not in have] + list(items)
        first_fix = next((r.id for r in items if r.id.startswith("fix:") and r.fixable), "")
        self.items = items
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._vars.clear()
        for r, it in enumerate(items):
            lbl = ttk.Label(self.list_frame, text=STATUS_TEXT.get(it.status, it.status), width=9,
                            foreground=STATUS_COLOR.get(it.status, "#000"))
            lbl.grid(row=r, column=0, sticky="w")
            var = tk.BooleanVar(value=(it.status == "missing" and it.required) or it.id == first_fix)
            self._vars[it.id] = var
            if it.fixable:
                ttk.Checkbutton(self.list_frame, variable=var, command=self._update_plan).grid(row=r, column=1)
            ttk.Label(self.list_frame, text=it.title, width=44).grid(row=r, column=2, sticky="w")
            ttk.Label(self.list_frame, text=it.detail, wraplength=470, foreground="#374151").grid(row=r, column=3, sticky="w")
            if not it.fixable and it.url and it.status in ("missing", "optional"):
                ttk.Button(self.list_frame, text="Open download page", command=lambda u=it.url: webbrowser.open(u)
                           ).grid(row=r, column=4, padx=4)
        self._update_plan()
        self._update_start()
        s = summarize(items)
        self._say("Everything required is ready." if s["ok"] else
                  "Missing: " + ", ".join(s["missing_required"]) + ". Tick what you want and click 'Install selected'.")

    def _selected_groups(self) -> List[List[Action]]:
        """One action chain per ticked item. A chain stops at its first failure; other items still run."""
        groups: List[List[Action]] = []
        seen = set()
        for it in self.items:
            if it.fixable and self._vars.get(it.id) is not None and self._vars[it.id].get():
                chain = [a for a in it.actions if a.id not in seen]
                seen.update(a.id for a in chain)
                if chain:
                    groups.append(chain)
        return groups

    def _selected_actions(self) -> List[Action]:
        out: List[Action] = []
        seen = set()
        for it in self.items:
            if it.fixable and self._vars.get(it.id) is not None and self._vars[it.id].get():
                for a in it.actions:
                    if a.id not in seen:
                        seen.add(a.id)
                        out.append(a)
        return out

    def _update_plan(self) -> None:
        acts = self._selected_actions()
        mb = sum(a.approx_mb for a in acts)
        text = "\n".join(f"{a.display}" + (f"    # {a.note}" if a.note else "") for a in acts) or "(nothing selected)"
        if mb:
            text += f"\n\nApproximate download: {mb} MB"
        self.plan.config(state="normal")
        self.plan.delete("1.0", "end")
        self.plan.insert("end", text)
        self.plan.config(state="disabled")

    # ------------------------------------------------------------------ actions
    def _install(self) -> None:
        acts = self._selected_actions()
        if not acts or self._busy:
            return
        mb = sum(a.approx_mb for a in acts)
        if not messagebox.askyesno("Confirm", f"Run {len(acts)} command(s) shown above"
                                   + (f" (about {mb} MB to download)" if mb else "") + "?", parent=self):
            return
        self._run(self._selected_groups())

    def _start_action(self):
        down = [i for i in self.items if i.id == "daemon" and i.status != "ok"]
        if not down:
            return None
        return start_action(self.env)

    def _update_start(self) -> None:
        act = self._start_action()
        be = find_runtime_exe(self.env)[0] or "Docker"
        self.start_btn.config(text=f"▶ Start {be.title()}", state="normal" if act is not None and not self._busy else "disabled")

    def _start_runtime(self) -> None:
        act = self._start_action()
        if act is None or self._busy:
            return
        self._say(f"Starting the container runtime ({act.display})...")
        self._run([[act]])

    def _load_images(self) -> None:
        _b, exe, _p = find_runtime_exe(self.env)
        if not exe:
            messagebox.showinfo("Load images", "Install Docker or Podman first.", parent=self)
            return
        p = filedialog.askopenfilename(parent=self, title="Image archive made with 'docker save'",
                                       filetypes=[("Image archives", "*.tar *.tar.gz *.tgz")])
        if p:
            self._run([[load_image_action(exe, p)]])

    def _run(self, acts: List[List[Action]]) -> None:
        self._cancel.clear()
        self._set_busy(True, "Working...")
        threading.Thread(target=_install_worker, args=(acts, self.env, self._q, self._cancel), daemon=True).start()

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self._busy = busy
        self.install_btn.config(state="disabled" if busy else "normal")
        if busy:
            self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal" if busy and msg == "Working..." else "disabled")
        if msg:
            self._say(msg)

    def _say(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._say(payload)
                elif kind == "recovery":
                    self._say("\nWhy it failed and what to do:\n  " + payload.message.replace("\n", "\n  "))
                    self._recovery = list(payload.items)
                elif kind == "items":
                    self._set_busy(False)
                    self._show_items(payload)
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        try:
            self.after(150, self._poll)
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._busy and not messagebox.askyesno("Setup", "A step is still running. Stop it and close?", parent=self):
            return
        self._cancel.set()
        st = load_state(self.state_file)
        st["dismissed"] = bool(self.dismiss_var.get())
        save_state(st, self.state_file)
        self.destroy()
        if self.on_done:
            try:
                self.on_done()
            except Exception:  # noqa: BLE001
                pass


def open_setup(parent, on_done=None, env=None) -> SetupDialog:
    dlg = SetupDialog(parent, env=env, on_done=on_done)
    dlg.transient(parent)
    return dlg


def schedule_startup_check(root, delay_ms: int = 1500, env=None, state_file: Optional[Path] = None) -> None:
    """After the window is up: check in the background; open the dialog only if something REQUIRED is missing."""
    env = env or SystemEnv()
    try:                                  # institution policy / environment can switch the start-up prompt off
        from prerequisites import load_config
        _pol = load_config()
        if _pol.mode == "off" or not _pol.startup_check or os.environ.get("AISEA_NO_PREREQ_CHECK") == "1":
            apply_known_paths(env)
            return
    except Exception:  # noqa: BLE001 - policy problems never block the application
        pass
    if load_state(state_file).get("dismissed"):
        apply_known_paths(env)
        return
    result: "queue.Queue[bool]" = queue.Queue()

    def work() -> None:
        try:
            apply_known_paths(env)
            result.put(not summarize(run_checks(env, include_optional=False))["ok"])
        except Exception:  # noqa: BLE001 - a failed check must never disturb the application
            result.put(False)

    def poll() -> None:
        try:
            need = result.get_nowait()
        except queue.Empty:
            root.after(300, poll)
            return
        if need:
            open_setup(root, env=env)

    def start() -> None:
        threading.Thread(target=work, daemon=True).start()
        root.after(300, poll)

    root.after(delay_ms, start)
