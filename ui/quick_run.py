"""'⚡ Run now' for the App Preview panel: one click, one consent, then the preview opens.

Steps (all analysis is read-only; every build/run is shown to the user first and bound to its exact plan hash):
  analyse -> one consolidated approval -> start the runtime if it is stopped -> build the GUI image / cached package image if
  missing -> run in a disposable container -> show the screen/web/console at once.
Automatic remedies afterwards: a missing Python package is installed into the cached image (one yes/no) and the run repeats;
an out-of-memory kill repeats once with more memory. Tk variables are read on the UI thread only; slow work runs on workers.
"""
from __future__ import annotations

import threading
from tkinter import messagebox
from typing import Dict

from app_preview import approve, build_gui_image, discover_entries
from app_preview.quick import QuickPrep, analyse, make_env_plan, missing_module, pip_for_module
from plugins.build_execution.autotune import retry_limits


class QuickRunMixin:
    _quick_busy = False
    _quick_ok = False
    _quick_tries = 0
    _quick_prep = None
    _image_override = ""

    # ------------------------------------------------------------ entry point
    def run_now(self, script: str = "") -> None:
        if self._quick_busy or (self.session and self.session.running):
            return
        src = self.src_var.get().strip()
        if not src:
            messagebox.showinfo("Run now", "Choose a ZIP archive, a folder or a file first.", parent=self)
            return
        script = script or self._focus_file                # a file picked with "File..." is what runs
        self._quick_busy, self._quick_tries = True, 0
        if src != self.source:
            self.set_source(src)                       # scan in the background; the analysis below does its own read
        opts = self._preview_options()
        known = list(self.entries) if src == self.source and self.entries else None
        self.status_var.set("⚡ Checking what this project needs...")

        def work() -> None:
            try:
                entries = known if known is not None else discover_entries(src)[0]
                prep = analyse(src, entries, opts, script)
                self.q.put({"type": "ui_quick_ready", "prep": prep, "src": src, "opts": opts})
            except Exception as exc:  # noqa: BLE001
                self.q.put({"type": "ui_quick_ready", "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
        threading.Thread(target=work, daemon=True).start()

    def _quick_ready(self, ev: Dict) -> None:
        if ev.get("err") or ev["prep"].problems:
            self._quick_busy = False
            msg = ev.get("err") or "\n".join(ev["prep"].problems)
            self.status_var.set("Run now: cannot continue.")
            messagebox.showerror("Run now", msg, parent=self)
            return
        prep: QuickPrep = ev["prep"]
        self._quick_prep = prep
        net = [p for p in (prep.gui_plan, prep.env_plan) if p and (prep.needs_gui_image if p is prep.gui_plan else prep.needs_env_image)]
        text = "Run now will:\n\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(prep.steps, 1))
        if net:
            text += "\n\nInternet is used ONLY for the one-time image step(s) above:\n" + "\n".join("  $ " + p.command_line for p in net)
        text += "\n\nYour project is never run on this computer. Approve all of this?"
        if not messagebox.askokcancel("Run now - approve", text, parent=self):
            self._quick_busy = False
            self.status_var.set("Run now was not approved. Nothing was started.")
            return
        self._console("⚡ Run now\n", "phase")
        self.right.select(2)
        opts = ev["opts"]

        def work() -> None:
            say = lambda t: self.q.put({"type": "ui_imgline", "text": t})                      # noqa: E731
            try:
                if prep.needs_runtime_start:
                    from plugins.build_execution.runtime_start import ensure_runtime
                    st = ensure_runtime(progress=say)
                    if not st["available"]:
                        raise RuntimeError(st.get("reason") or "The container runtime did not start.")
                for need, plan in ((prep.needs_gui_image, prep.gui_plan), (prep.needs_env_image, prep.env_plan)):
                    if need and plan:
                        res = build_gui_image(plan, approve(plan), progress=say)
                        if not res["ok"]:
                            raise RuntimeError("\n".join(res["notes"][:8]))
                self.q.put({"type": "ui_quick_go", "image": prep.image})
            except Exception as exc:  # noqa: BLE001
                self.q.put({"type": "ui_quick_fail", "msg": f"{type(exc).__name__}: {str(exc)[:600]}" if not isinstance(exc, RuntimeError) else str(exc)})
        threading.Thread(target=work, daemon=True).start()
        self.status_var.set("⚡ Preparing... (first run can take a few minutes; later runs are instant)")

    def _quick_go(self, ev: Dict, waited: int = 0) -> None:
        if not self.entries and waited < 30:           # the panel's own scan must finish first, or it would overwrite the choice
            self.after(100, lambda: self._quick_go(ev, waited + 1))
            return
        prep = self._quick_prep
        self._quick_busy = False
        e = prep.entry
        title = self._title(e)
        self.entry_by_title[title] = e
        self.mode_var.set("run")
        self._mode_changed()
        self.entry_box["values"] = list(self.entry_box["values"]) + ([title] if title not in self.entry_box["values"] else [])
        self.entry_var.set(title)                      # after _mode_changed, which may reset the box to its own first choice
        self._image_override = ev["image"]
        self._quick_ok = True
        self.start()

    def _quick_fail(self, ev: Dict) -> None:
        self._quick_busy = False
        self.status_var.set("Run now stopped.")
        self._console(ev["msg"] + "\n", "err")
        messagebox.showerror("Run now", ev["msg"][:900], parent=self)

    # ------------------------------------------------------------ automatic remedies
    def _quick_after_result(self, res) -> None:
        prep = self._quick_prep
        if prep is None or self._quick_tries >= 2 or getattr(res, "status", "") not in ("failed",):
            return
        text = (getattr(res, "stderr", "") or "") + "\n" + (getattr(res, "stdout", "") or "")
        mod = missing_module(text)
        pip = pip_for_module(mod) if mod else ""
        if pip and not any(pip.lower() == p.split("=")[0].split("<")[0].split(">")[0].split("~")[0].split("!")[0].lower() for p in prep.packages):
            self._quick_tries += 1
            if not messagebox.askyesno("Run now", f"The program needs the Python package '{pip}', which is not installed.\n\n"
                                       "Install it into the cached image (one-time, needs internet) and run again?", parent=self):
                return
            self._quick_busy = True
            packages = prep.packages + [pip]
            from app_preview.runtimes import find_runtime
            opts = self._preview_options()
            base = opts.gui_image if prep.display else opts.images.get("python") or "python:3.12-slim"
            name, exe = find_runtime(opts)
            try:
                prep.env_plan = make_env_plan(opts, base, packages, exe, name or "")
            except (ValueError, OSError) as exc:
                self._quick_busy = False
                self._console(f"Could not prepare the package image: {exc}\n", "err")
                return
            prep.packages, prep.image, prep.needs_env_image, prep.needs_gui_image = packages, prep.env_plan.tag, True, False
            plan = prep.env_plan
            self._console(f"⚡ Installing {pip} and running again\n", "phase")

            def work() -> None:
                say = lambda t: self.q.put({"type": "ui_imgline", "text": t})                  # noqa: E731
                r = build_gui_image(plan, approve(plan), progress=say)
                self.q.put({"type": "ui_quick_go", "image": plan.tag} if r["ok"] else {"type": "ui_quick_fail", "msg": "\n".join(r["notes"][:8])})
            threading.Thread(target=work, daemon=True).start()
            return
        if getattr(res, "exit_code", None) == 137:
            lim = retry_limits("failed", 137, self.options.limits)
            if lim is not None:
                self._quick_tries += 1
                self.options.limits = lim
                self._console(f"⚡ Out of memory - running again with {lim.memory_mb} MB\n", "phase")
                self._image_override, self._quick_ok = prep.image, True
                self.after(300, self.start)
