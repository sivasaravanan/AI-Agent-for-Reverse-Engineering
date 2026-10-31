"""Stage C: compliance tab - policy gate, licences, SBOM, SARIF, audit verification, portfolio scan, trends."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional


class CompliancePanel(ttk.Frame):
    def __init__(self, parent, out_dir, get_result: Callable, get_source: Callable, get_audit: Optional[Callable] = None,
                 get_db: Optional[Callable] = None, **kw):
        super().__init__(parent, **kw)
        self.out_dir = Path(out_dir)
        self.get_result, self.get_source, self.get_db = get_result, get_source, get_db or (lambda: None)
        self._audit = get_audit
        self.q: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.policy_var = tk.StringVar()
        top = ttk.LabelFrame(self, text="Policy")
        top.pack(fill="x", padx=3, pady=(3, 0))
        ttk.Label(top, text="Policy (JSON, optional)").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.policy_var, width=60).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_policy).grid(row=0, column=2)
        top.columnconfigure(1, weight=1)
        bar = ttk.Frame(self, padding=(3, 0))
        bar.pack(fill="x")
        self.btns = []
        for text, cmd in (("✔ Check policy + licences", self.check), ("⇩ SBOM", self.export_sbom), ("⇩ SARIF", self.export_sarif),
                          ("◔ Trend", self.show_trend), ("▦ Portfolio scan...", self.portfolio), ("⚖ Verify audit", self.verify_audit)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.btns.append(b)
        self.status_var = tk.StringVar(value="Run an analysis, then check it against a policy.")
        ttk.Label(self, textvariable=self.status_var, padding=3).pack(fill="x")
        self.text = tk.Text(self, height=18, wrap="word", state="disabled")
        self.text.pack(fill="both", expand=True)
        ttk.Label(self, text="Licence detection is a text heuristic, not legal advice. Policy results cover static findings only.", padding=3).pack(fill="x")

    def audit(self, action: str, subject: str = "", details=None) -> None:
        try:
            log = self._audit() if self._audit else None
            if log is None:
                from enterprise import AuditLog
                log = AuditLog(self.out_dir / "audit.jsonl")
            log.append(action, "gui", subject, details or {})
        except Exception:  # noqa: BLE001 - auditing must never break the UI
            pass

    def _show(self, text: str) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.config(state="disabled")

    def _pick_policy(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("Policy JSON", "*.json")])
        if p:
            self.policy_var.set(p)

    def _need_result(self):
        r = self.get_result()
        if r is None:
            messagebox.showinfo("Compliance", "Run an analysis first.")
        return r

    def _policy(self):
        from enterprise import default_policy, load_policy
        return load_policy(self.policy_var.get().strip()) if self.policy_var.get().strip() else default_policy()

    def _licenses(self):
        from enterprise import scan_licenses
        src = self.get_source()
        return scan_licenses(src) if src else None

    def check(self) -> None:
        r = self._need_result()
        if r is None:
            return
        try:
            from enterprise import evaluate_policy
            pol = self._policy()
            lic = self._licenses()
            pr = evaluate_policy(r, pol, lic)
        except Exception as exc:  # noqa: BLE001
            self._show(f"Could not evaluate: {exc}")
            self.status_var.set("Policy could not be evaluated.")
            return
        lines = [f"Policy '{pol.name}': {'PASS' if pr.passed else 'FAIL'}", f"Counts considered: {pr.summary}", ""]
        lines += [f"- {v['message']}" + (f"  [{v.get('file')}:{v.get('line')}]" if v.get("file") else "") for v in pr.violations] or ["No violations."]
        if pr.waived:
            lines += ["", f"{len(pr.waived)} finding(s) waived by policy (listed, not hidden)."]
        if lic:
            lines += ["", "Project licence(s): " + (", ".join(f"{p['license']} ({p['file']})" for p in lic.project) or "none found")] + [f"* {n}" for n in lic.notes]
        self._show("\n".join(lines))
        self.status_var.set("PASS" if pr.passed else f"FAIL: {len(pr.violations)} violation(s)")
        self.audit("policy.check", r.archive_name, {"passed": pr.passed, "policy": pol.name})

    def _export(self, name: str, build, label: str) -> None:
        r = self._need_result()
        if r is None:
            return
        try:
            data = build(r)
            dest = filedialog.asksaveasfilename(initialfile=name, defaultextension=".json")
            if not dest:
                return
            Path(dest).write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.status_var.set(f"{label} saved: {dest}")
            self.audit("export." + label.lower(), r.archive_name, {"file": Path(dest).name})
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Compliance", str(exc))

    def export_sbom(self) -> None:
        def build(r):
            from enterprise import build_sbom
            return build_sbom(r, self._licenses())
        self._export("sbom.cdx.json", build, "SBOM")

    def export_sarif(self) -> None:
        def build(r):
            from enterprise import to_sarif
            return to_sarif(r)
        self._export("results.sarif", build, "SARIF")

    def show_trend(self) -> None:
        r, db = self.get_result(), self.get_db()
        if r is None or db is None:
            messagebox.showinfo("◔ Trend", "Needs an analysis and the history database (enable history in Settings).")
            return
        from enterprise import project_trend
        t = project_trend(db, r.archive_name)
        self._show(f"{t['project']}: {t['runs']} stored run(s), trend: {t['direction']}\n\n" +
                   "\n".join(f"{p['at']}  total {p['total']}  high+ {p['high']}" for p in t["points"]) + f"\n\n{t['note']}")

    def verify_audit(self) -> None:
        from enterprise import AuditLog
        log = self._audit() if self._audit else None
        log = log or AuditLog(self.out_dir / "audit.jsonl")
        v = log.verify()
        self._show(f"{'OK' if v['ok'] else 'TAMPERED'}: {v['message']} ({v['entries']} entries)\n{log.path}")
        self.status_var.set("Audit chain intact." if v["ok"] else "AUDIT LOG FAILED VERIFICATION")

    def portfolio(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        d = filedialog.askdirectory(title="Folder with one project (zip/tar/folder) per entry")
        if not d:
            return
        try:
            pol = self._policy()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Policy", str(exc))
            return
        for b in self.btns:
            b.config(state="disabled")

        def work():
            try:
                from enterprise import AuditLog, scan_portfolio
                log = (self._audit() if self._audit else None) or AuditLog(self.out_dir / "audit.jsonl")
                self.q.put(("done", scan_portfolio(d, self.out_dir / "portfolio", pol, log, "gui", lambda f, m: self.q.put(("p", f, m)))))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("err", str(exc)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self.status_var.set(m[2])
                else:
                    for b in self.btns:
                        b.config(state="normal")
                    if m[0] == "err":
                        self.status_var.set(f"Failed: {m[1]}")
                        messagebox.showerror("Portfolio", m[1])
                    else:
                        res = m[1]
                        self._show("\n".join(f"{p['status'].upper():5} {p['project']}  high {p['high']}  {p['violations'] or p['error']}" for p in res.projects))
                        self.status_var.set(f"{len(res.projects)} project(s), {res.failed} not passing. Files in {Path(res.files['csv']).parent}")
                        webbrowser.open(Path(res.files["html"]).as_uri())
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)
