"""Phase 9: execution approval dialog.

Fail-closed: when no isolated environment is available the dialog only explains that execution is
unavailable. The default button is Cancel and the command is shown verbatim (never executed here).
"""
from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Mapping, Optional, Sequence

from plugins.build_execution.run_targets import discover_all
from plugins.build_execution.ui_tab import BuildExecutionTab


class ExecutionApprovalDialog(tk.Toplevel):
    def __init__(self, parent, command: Sequence[str] | str, cwd: str, isolation, limits: Optional[Mapping] = None,
                 source: str = "project manifest", plan=None):
        super().__init__(parent)
        self.title("Approve execution?")
        self.resizable(False, False)
        self.approved = False
        self.isolation = isolation
        cmd = command if isinstance(command, str) else " ".join(command)
        ttk.Label(self, padding=10, justify="left", wraplength=560, text=(
            "The following command comes from untrusted project content and has NOT been run.\n"
            f"Discovered in: {source}")).pack(anchor="w")
        box = tk.Text(self, height=3, width=70, wrap="word", font=("Consolas", 10))
        box.insert("1.0", cmd)
        box.config(state="disabled")
        box.pack(padx=5)
        ttk.Label(self, padding=(10, 6, 10, 0), justify="left", text=f"Working folder: {cwd}").pack(anchor="w")
        lim = limits or {}
        ttk.Label(self, padding=(10, 0), justify="left", text=(
            "Limits: " + ", ".join(f"{k}={v}" for k, v in lim.items()) if lim else "Limits: defaults")).pack(anchor="w")
        avail = bool(getattr(isolation, "available", False))
        ttk.Label(self, padding=10, justify="left", wraplength=560, foreground="#166534" if avail else "#991b1b", text=(
            f"Isolation: {getattr(isolation, 'backend', '') or 'ready'}" if avail else
            "Execution is unavailable: " + str(getattr(isolation, "reason", "no isolated environment")) +
            "\nStatic analysis is still available; nothing will be run on this computer.")).pack(anchor="w")
        if plan is not None:            # Phase 4 Plan: show everything the approval covers (image, network, risks, notes)
            c, lim_ = plan.candidate, plan.limits
            lines = [f"Image: {plan.image} (never pulled automatically)",
                     "Network: " + ("ENABLED - the project code can reach the network" if lim_.network else "disabled"),
                     f"Limits: {lim_.cpus} CPU, {lim_.memory_mb} MB, {lim_.pids} processes, {lim_.timeout_s}s timeout"]
            lines += ["Risks: " + "; ".join(c.risks)] if c.risks else []
            lines += ["Notes: " + "; ".join(c.notes)] if c.notes else []
            ttk.Label(self, padding=(10, 4), justify="left", wraplength=560, text="\n".join(lines),
                      foreground="#991b1b" if lim_.network else "#1f2937").pack(anchor="w")
        self.check_var = tk.BooleanVar(value=False)
        self.check = ttk.Checkbutton(self, variable=self.check_var, command=self._sync,
                                     text="I have read this command and approve running it in the isolated environment.")
        self.check.pack(anchor="w", padx=5)
        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        self.run_btn = ttk.Button(bar, text="Run approved command", command=self._approve, state="disabled")
        self.run_btn.pack(side="right")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self._cancel, default="active")
        self.cancel_btn.pack(side="right", padx=3)
        if not avail:
            self.check.state(["disabled"])
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.cancel_btn.focus_set()

    def _sync(self) -> None:
        ok = self.check_var.get() and bool(getattr(self.isolation, "available", False))
        self.run_btn.config(state="normal" if ok else "disabled")

    def _approve(self) -> None:
        if self.check_var.get() and bool(getattr(self.isolation, "available", False)):
            self.approved = True
            self.destroy()

    def _cancel(self) -> None:
        self.approved = False
        self.destroy()


def request_execution_approval(parent, command, cwd, isolation, limits=None, source="project manifest", plan=None) -> bool:
    """Show the dialog modally; True only if the user explicitly approved and isolation exists."""
    dlg = ExecutionApprovalDialog(parent, command, cwd, isolation, limits, source, plan)
    dlg.transient(parent)
    dlg.grab_set()
    dlg.wait_window()
    return dlg.approved


class ExecutionTab(BuildExecutionTab):
    """Plugin tab + a hook so finished runs flow into the report and the Findings log analysis."""

    def __init__(self, master, get_source=None, on_result: Optional[Callable] = None, on_gui_run: Optional[Callable] = None,
                 on_forget: Optional[Callable] = None):
        self.on_result = on_result
        self.on_gui_run = on_gui_run
        self.on_forget = on_forget          # host hook: forget the selected project (Clear button / "forget after run")
        self._auto_job = None
        self._last_discovered = None
        super().__init__(master, get_source=get_source)
        self._build_command_picker()
        self.src.trace_add("write", self._src_changed)
        self.eco.trace_add("write", self._eco_changed)

    # ---------------------------------------------------------------- command picker (additive)
    def _build_command_picker(self) -> None:
        """Combobox listing every command found in the project (build/test commands and run targets), the file each came from,
        and the 'Set up Docker...' entry to the prerequisite helpers. Placed above the commands table."""
        pw = next((w for w in self.pack_slaves() if isinstance(w, ttk.PanedWindow)), None)
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=(0, 4), **({"before": pw} if pw is not None else {}))
        ttk.Label(row, text="Project command:").pack(side="left")
        self.cmd_pick = tk.StringVar()
        self.cmd_box = ttk.Combobox(row, textvariable=self.cmd_pick, state="readonly", values=())
        self.cmd_box.pack(side="left", fill="x", expand=True, padx=6)
        self.cmd_box.bind("<<ComboboxSelected>>", self._on_pick)
        ttk.Button(row, text="Set up Docker...", command=self._open_setup).pack(side="left")
        ttk.Button(row, text="Storage & clean-up...", command=self._open_storage).pack(side="left", padx=(4, 0))
        self.quick_btn = ttk.Button(row, text="\u26a1 Quick run", command=self._quick_run)      # one click: run + live preview, hiccups handled
        self.quick_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Clear project & copies", command=self._clear_now).pack(side="left", padx=(4, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select, add="+")
        self.cmd_info = tk.StringVar(value="Choose a ZIP or folder: the commands found in it are listed here automatically.")
        ttk.Label(self, textvariable=self.cmd_info, wraplength=900).pack(fill="x", padx=8, **({"before": pw} if pw is not None else {}))
    # ---------------------------------------------------------------- Quick run and Clear (additive)
    def _ask_ui(self, fn: Callable, timeout_s: float = 600.0):
        """Run ``fn`` on the UI thread (dialogs) from a worker thread and return its result."""
        import threading
        box: dict = {"ev": threading.Event(), "val": None}
        self._q.put(("ask", (fn, box)))
        box["ev"].wait(timeout_s)
        return box["val"]
    def _quick_run(self) -> None:
        """\u26a1 Quick run: the chosen command (or the best one found) runs at once with live output; the tool handles the usual hiccups
        (typo in a script name, big project, stopped Docker, missing image, timeout, missing package). Only a plan that needs the
        NETWORK asks for approval."""
        import shlex
        import threading
        from plugins.build_execution import quickrun
        from plugins.build_execution.discovery import custom_candidate
        src = self._source()
        if not src:
            messagebox.showinfo("Quick run", "Choose a project (folder, archive or file) in the toolbar first.", parent=self)
            return
        cand = None
        try:
            if self.custom.get().strip():
                cand = custom_candidate(shlex.split(self.custom.get()), self.eco.get())
            else:
                sel = self.tree.selection()
                if sel and self.tree.index(sel[0]) < len(self._cands):
                    cand = self._cands[self.tree.index(sel[0])]
        except ValueError as exc:
            messagebox.showerror("Quick run", str(exc), parent=self)
            return
        self._cancel.clear()
        self._last_run = None
        self.out.delete("1.0", "end")
        self.run_btn.config(state="disabled")
        self.quick_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog.start(12)
        self.status.set("Quick run...")
        image = self.image.get().strip()
        gui_hook = self.on_gui_run

        def confirm(title: str, text: str) -> bool:
            return bool(self._ask_ui(lambda: messagebox.askyesno(title, text, parent=self)))

        def approve(plan) -> bool:
            return bool(self._ask_ui(lambda: ApprovalDialog(self, plan).approved))

        def gui(source: str, script: str) -> bool:
            return bool(self._ask_ui(lambda: (gui_hook(source, script), True)[1])) if gui_hook is not None else False

        hooks = quickrun.Hooks(progress=lambda m: self._q.put(("progress", m)), confirm=confirm, approve=approve,
                               gui=gui if gui_hook is not None else None)

        def work() -> None:
            self._q.put(("quickdone", quickrun.quick_run(src, cand, hooks, self._cancel, image=image)))
        threading.Thread(target=work, daemon=True).start()

    def _clear_now(self) -> None:
        """Remove every copy and container this tool made and forget the selected project. Your own files are never touched."""
        import threading
        from plugins.build_execution import cleanup
        from plugins.build_execution.models import Options
        from plugins.build_execution.sandbox import find_runtime
        if not messagebox.askyesno("Clear", "Remove every leftover copy of a project and every stopped container made by this tool, and forget "
                                   "the selected project?\n\nYour own folder, archive or file is not touched.", parent=self):
            return
        self.status.set("Clearing...")

        def work() -> None:
            _name, exe = find_runtime(Options())
            self._q.put(("cleared", cleanup.clear_everything(exe)))
        threading.Thread(target=work, daemon=True).start()
    def forget_display(self) -> None:
        """Empty the discovered commands (the host forgot the project)."""
        self._cands = []
        self._discovered_for = ""
        self.tree.delete(*self.tree.get_children())
        self.cmd_box.configure(values=[])
        self.cmd_pick.set("")
        self.cmd_info.set("Choose a ZIP, folder or file: the commands found in it are listed here automatically.")

    def _handle(self, kind, payload):
        if kind == "ask":                                       # a worker thread needs a dialog
            fn, box = payload
            try:
                box["val"] = fn()
            except Exception:  # noqa: BLE001
                box["val"] = None
            box["ev"].set()
            return
        if kind == "quickdone":
            from plugins.build_execution import quickrun
            self.quick_btn.config(state="normal")
            self.run_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            self.prog.stop()
            self.out.insert("end", quickrun.summary(payload) + "\n\n")
            if payload.final is not None:
                return super()._handle("done", payload.final)
            self.status.set("Handed to App Preview." if payload.handed_over else (payload.message.splitlines() or ["Nothing was run."])[0][:160])
            return None
        if kind == "cleared":
            self.status.set("Cleared: " + "; ".join(payload) + ".")
            self.out.insert("end", "Cleared: " + "; ".join(payload) + "\n")
            self.forget_display()
            if self.on_forget is not None:
                self.on_forget()
            return None
        return super()._handle(kind, payload)

    def _open_storage(self) -> None:
        from ui.storage_dialog import open_storage_dialog
        open_storage_dialog(self)

    def _open_setup(self) -> None:
        from ui.setup_dialog import open_setup
        open_setup(self.winfo_toplevel(), on_done=self._check_isolation)

    def _discover(self):                      # same table as the plugin tab (now including run targets), plus the picker
        self._do_discover(interactive=True)

    def _do_discover(self, interactive: bool) -> None:
        src = self._source()
        if not src:
            if interactive:
                messagebox.showinfo("Discover", "Choose a project (Archive or Folder) in the toolbar first.", parent=self)
            return
        self._last_discovered = src           # also on failure: no retry loop for a source that cannot be read
        try:
            self._cands, notes = discover_all(src)
        except Exception as exc:  # noqa: BLE001 - shown, never raised into the UI loop
            self._cands = []
            self.cmd_box.configure(values=[])
            self.cmd_info.set(f"Could not read the source: {type(exc).__name__}: {exc}")
            if interactive:
                messagebox.showerror("Discover", f"Could not read the source: {exc}", parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        for c in self._cands:
            self.tree.insert("", "end", values=(c.id, "YES" if c.needs_network else "no", c.display, "; ".join(c.risks)[:200]))
        self.out.delete("1.0", "end")
        self.out.insert("end", "\n".join(notes) or "Select a command and click 'Review and run...'.")
        self._refresh_picker()

    @staticmethod
    def _label(c) -> str:
        return f"{c.id}  |  {c.display}" + (f"  |  {c.source_file}" if c.source_file else "")

    def _ecosystems(self) -> list:
        seen: list = []
        for c in self._cands:
            if c.ecosystem not in seen:
                seen.append(c.ecosystem)
        return seen

    def _summary(self) -> str:
        if not self._cands:
            return "No build/test/run commands were found in this project. You can type a custom command below."
        return (f"{len(self._cands)} command(s) found ({', '.join(self._ecosystems())}). Pick one here or in the table, "
                "then click 'Review and run...'.")

    def _refresh_picker(self) -> None:
        self.cmd_box.configure(values=[self._label(c) for c in self._cands])
        self.cmd_pick.set("")
        self.cmd_info.set(self._summary())
        if self._cands:
            self.eco.set(self._cands[0].ecosystem)        # keep the custom-command ecosystem consistent with the project
        if len(self._cands) == 1:
            self.cmd_box.current(0)
            self._on_pick()

    def _on_pick(self, _evt=None) -> None:
        i = self.cmd_box.current()
        kids = self.tree.get_children()
        if 0 <= i < len(self._cands) and i < len(kids):
            self.tree.selection_set(kids[i])
            self.tree.focus(kids[i])
            self.tree.see(kids[i])
            self.eco.set(self._cands[i].ecosystem)
            self.status.set(f"Selected {self._cands[i].id}. Click 'Review and run...' to see exactly what would run.")

    def _on_tree_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        if sel:
            i = self.tree.index(sel[0])
            if 0 <= i < len(self._cands) and self.cmd_box.current() != i:
                self.cmd_box.current(i)

    def _src_changed(self, *_a) -> None:
        """Discover automatically (debounced) when the source path is typed, pasted or chosen."""
        try:
            if self._auto_job is not None:
                self.after_cancel(self._auto_job)
            self._auto_job = self.after(700, self._auto_discover)
        except tk.TclError:
            pass

    def _auto_discover(self) -> None:
        self._auto_job = None
        try:
            src = self._source()
            if src and src != self._last_discovered and Path(src).exists():
                self._do_discover(interactive=False)
        except (tk.TclError, OSError):
            pass

    def _eco_changed(self, *_a) -> None:
        if self._cands and self.eco.get() not in self._ecosystems():
            self.cmd_info.set(f"No {self.eco.get()} commands were found in this project (found: {', '.join(self._ecosystems())}). "
                              "This box only sets the ecosystem of a custom command; choose a discovered command from the list instead.")
        elif self._cands:
            self.cmd_info.set(self._summary())

    def _on_done(self, r):                # finished runs flow into the report and the Findings log analysis
        try:                              # added: setting "forget the selected project after every run" (nothing on disk is touched)
            from plugins.build_execution import storage as _st
            if self.on_forget is not None and _st.load().get("forget_after_run"):
                self.after(400, lambda: (self.forget_display(), self.on_forget()))
        except Exception:  # noqa: BLE001
            pass
        if self.on_result:
            try:
                self.on_result(r)
            except Exception as exc:  # noqa: BLE001 - a hook failure must not break the tab
                self.status.set(f"Result hook failed: {type(exc).__name__}")
