"""Stage D: team tab - shared workspace login, users, approvals, signing. Role checks live in collab/, not here."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional


class TeamPanel(ttk.Frame):
    def __init__(self, parent, on_change: Optional[Callable] = None, **kw):
        super().__init__(parent, **kw)
        self.ws = None
        self.session = None
        self.on_change = on_change or (lambda: None)
        self.folder_var, self.user_var, self.pw_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.status_var = tk.StringVar(value="Not signed in. Single-user mode is unchanged.")
        top = ttk.LabelFrame(self, text="Workspace and sign-in")
        top.pack(fill="x", padx=3, pady=(3, 0))
        ttk.Label(top, text="Shared folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.folder_var, width=50).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick).grid(row=0, column=2)
        ttk.Label(top, text="User").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.user_var, width=20).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Password").grid(row=2, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.pw_var, show="*", width=20).grid(row=2, column=1, sticky="w", padx=4)
        self.login_btn = ttk.Button(top, text="Sign in", command=self.login)
        self.login_btn.grid(row=2, column=2)
        ttk.Button(top, text="Create first admin", command=self.bootstrap).grid(row=1, column=2)
        top.columnconfigure(1, weight=1)
        ttk.Label(self, textvariable=self.status_var, padding=3).pack(fill="x")
        bar = ttk.Frame(self, padding=(3, 0))
        bar.pack(fill="x")
        self.action_btns = []
        for text, cmd in (("＋ Add user...", self.add_user), ("✉ Request approval...", self.request), ("✔ Approve", lambda: self.decide(True)),
                          ("✖ Reject", lambda: self.decide(False)), ("✎ Sign file...", self.sign), ("⌕ Verify file...", self.verify),
                          ("⎋ Sign out", self.logout)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.action_btns.append(b)
        self.tree = ttk.Treeview(self, columns=("id", "status", "kind", "subject", "by", "decided"), show="headings", height=10)
        for c, w in (("id", 40), ("status", 80), ("kind", 70), ("subject", 260), ("by", 90), ("decided", 110)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        ttk.Label(self, text="Shared-folder model: no server, no SSO. Anyone with file access to the folder can bypass it. See Known issues.",
                  padding=3).pack(fill="x")

    def _pick(self) -> None:
        d = filedialog.askdirectory(title="Shared team folder")
        if d:
            self.folder_var.set(d)

    def _open(self):
        from collab import TeamWorkspace
        f = self.folder_var.get().strip()
        if not f:
            raise ValueError("Choose the shared folder first.")
        self.ws = TeamWorkspace(f)
        return self.ws

    def bootstrap(self) -> None:
        try:
            ws = self._open()
            ws.add_user(None, self.user_var.get().strip(), self.pw_var.get(), "admin")
            self.status_var.set("Admin created. Now sign in.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Team", str(exc))

    def login(self) -> None:
        try:
            ws = self._open()
            self.session = ws.login(self.user_var.get().strip(), self.pw_var.get())
            self.pw_var.set("")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(str(exc))
            return
        self.status_var.set(f"Signed in as {self.session.username} ({self.session.role}). Analyses are saved to the shared history.")
        self.refresh()
        self.on_change()

    def logout(self) -> None:
        self.session = None
        self.status_var.set("Signed out.")
        self.tree.delete(*self.tree.get_children())
        self.on_change()

    def _guard(self) -> bool:
        if not self.session or not self.ws:
            messagebox.showinfo("Team", "Sign in first.")
            return False
        live = self.ws.users.session_for(self.session.username)       # role changes / disabling apply immediately
        if live is None:
            self.logout()
            messagebox.showwarning("Team", "Your account is no longer active.")
            return False
        self.session = live
        return True

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.ws:
            for r in self.ws.approvals.list():
                self.tree.insert("", "end", values=(r["id"], r["status"], r["kind"], r["subject"], r["requested_by"], r.get("decided_by") or ""))

    def _run(self, fn) -> None:
        if not self._guard():
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - AuthError/ApprovalError etc. are user-facing messages
            messagebox.showerror("Team", str(exc))
        self.refresh()

    def add_user(self) -> None:
        def go():
            n = simpledialog.askstring("Add user", "Username:", parent=self)
            if not n:
                return
            pw = simpledialog.askstring("Add user", "Initial password (min 10 chars):", show="*", parent=self)
            role = simpledialog.askstring("Add user", "Role (admin, reviewer, analyst, viewer):", parent=self)
            if pw and role:
                self.ws.add_user(self.session, n, pw, role.strip().lower())
        self._run(go)

    def request(self) -> None:
        def go():
            subject = simpledialog.askstring("Request approval", "What needs approval (subject)?", parent=self)
            sha = simpledialog.askstring("Request approval", "SHA-256 of the item (a patch's diff hash is shown in the fix dialog):", parent=self)
            if subject and sha:
                self.ws.request_approval(self.session, "patch", subject, sha.strip())
        self._run(go)

    def decide(self, approve: bool) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Team", "Select a request first.")
            return
        rid = int(self.tree.item(sel[0], "values")[0])

        def go():
            c = simpledialog.askstring("Decision", "Comment" + ("" if approve else " (required)") + ":", parent=self) or ""
            self.ws.decide(self.session, rid, approve, c)
        self._run(go)

    def sign(self) -> None:
        p = filedialog.askopenfilename(title="File to sign (e.g. an HTML/JSON report)")
        if p:
            self._run(lambda: messagebox.showinfo("Signed", f"Signature written: {self.ws.sign(self.session, p)}"))

    def verify(self) -> None:
        p = filedialog.askopenfilename(title="File to verify")
        if not p:
            return
        try:
            ws = self.ws or self._open()
            v = ws.verify(p)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Team", str(exc))
            return
        messagebox.showinfo("Verify", f"VALID - signed by {v['signer']} ({v['role']}) at {v['signed_at']}" if v["valid"] else f"INVALID: {v['reason']}")
