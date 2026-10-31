"""Phase 8 UI: review a proposed patch, approve it explicitly, apply, verify and optionally revert.

Nothing is applied until the user ticks the approval box and presses *Apply*. Work runs on a worker thread;
the dialog only touches Tk widgets from the UI thread (results travel through a queue).
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from core.patch_manager import (
    RollbackPolicy, Workspace, apply_and_verify, diff, revert,
)
from models.finding import Finding
from models.patch import ApplyOutcome, Patch, PatchError


class PatchDialog(tk.Toplevel):
    def __init__(self, parent, patch: Patch, finding: Optional[Finding], workspace: Workspace,
                 runner=None, on_close: Optional[Callable[[Optional[ApplyOutcome]], None]] = None):
        super().__init__(parent)
        self.title("Review proposed fix")
        self.geometry("900x650")
        self.patch, self.finding, self.workspace, self.runner = patch, finding, workspace, runner
        self.outcome: Optional[ApplyOutcome] = None
        self._on_close = on_close
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        p, f = self.patch, self.finding
        head = (f"{f.id}  [{f.severity.upper()}]  {f.title}\n" if f else "Manual patch\n") + \
               f"File: {p.file}" + (f":{f.line}" if f and f.line else "") + f"      Source: {p.source} (unverified proposal)"
        ttk.Label(self, text=head, padding=8, justify="left").pack(fill="x")
        notes = p.rationale + (f"\nCaution: {p.caution}" if p.caution else "")
        ttk.Label(self, text=notes, padding=(4, 0), wraplength=860, justify="left").pack(fill="x")
        ttk.Label(self, text="Proposed changes (diff):", padding=(8, 8, 8, 0)).pack(anchor="w")
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=4)
        self.diff_text = tk.Text(box, wrap="none", height=18, font=("Consolas", 10))
        sb = ttk.Scrollbar(box, command=self.diff_text.yview)
        self.diff_text.configure(yscrollcommand=sb.set)
        self.diff_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.diff_text.tag_config("add", foreground="#166534", background="#dcfce7")
        self.diff_text.tag_config("del", foreground="#991b1b", background="#fee2e2")
        self.diff_text.tag_config("hunk", foreground="#1d4ed8")
        for line in diff(p).splitlines(keepends=True):          # diff() records the hash apply() requires
            tag = "hunk" if line.startswith("@@") else "add" if line.startswith("+") and not line.startswith("+++") \
                else "del" if line.startswith("-") and not line.startswith("---") else ""
            self.diff_text.insert("end", line, tag)
        self.diff_text.config(state="disabled")
        self.approve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, variable=self.approve_var, command=self._sync_buttons,
                        text="I have reviewed this diff and approve applying it to the workspace copy "
                             "(the original ZIP is never modified).").pack(anchor="w", padx=4, pady=3)
        bar = ttk.Frame(self, padding=(4, 0))
        bar.pack(fill="x")
        self.apply_btn = ttk.Button(bar, text="Apply and verify", command=self.apply_clicked, state="disabled")
        self.apply_btn.pack(side="left")
        self.revert_btn = ttk.Button(bar, text="Revert", command=self.revert_clicked, state="disabled")
        self.revert_btn.pack(side="left", padx=3)
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        self.result = tk.Text(self, wrap="word", height=9, state="disabled")
        self.result.pack(fill="x", padx=4, pady=4)

    def _sync_buttons(self) -> None:
        ok = self.approve_var.get() and not self._busy and self.patch.status == "proposed"
        self.apply_btn.config(state="normal" if ok else "disabled")
        self.revert_btn.config(state="normal" if (self.outcome and self.outcome.backup and not self.outcome.reverted
                                                  and self.patch.status == "applied" and not self._busy) else "disabled")

    def _show(self, text: str) -> None:
        self.result.config(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.config(state="disabled")

    # ------------------------------------------------------------------ actions
    def apply_clicked(self) -> None:
        if not self.approve_var.get() or self._busy:
            return
        self._busy = True
        self._sync_buttons()
        self._show("Applying and verifying...")
        approved = self.approve_var.get() is True

        def work() -> None:
            try:
                self._q.put(("done", apply_and_verify(self.patch, approved, self.workspace, self.runner, RollbackPolicy())))
            except PatchError as exc:
                self._q.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("error", f"Unexpected error: {type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def revert_clicked(self) -> None:
        if not (self.outcome and self.outcome.backup):
            return
        try:
            revert(self.outcome.backup, patch=self.patch)
            self._show("Reverted: the original file content was restored from the backup.")
        except PatchError as exc:
            messagebox.showerror("Revert refused", str(exc), parent=self)
        self._sync_buttons()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self._busy = False
                if kind == "done":
                    self.outcome = payload
                    v = payload.verification
                    lines = [payload.message, "", v.claim, ""]
                    lines += [f"Static check: target finding resolved = {v.static.target_resolved}; "
                              f"{len(v.static.resolved)} resolved, {len(v.static.new)} new finding(s)."]
                    lines += [f"- {w}" for w in v.warnings + v.skipped_reasons]
                    if payload.backup:
                        lines += ["", f"Backup: {payload.backup.backup_path}"]
                    self._show("\n".join(lines))
                else:
                    self._show("Not applied: " + payload)
                    messagebox.showerror("Patch refused", payload, parent=self)
                self._sync_buttons()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def close(self) -> None:
        if self._on_close:
            self._on_close(self.outcome)
        self.destroy()
