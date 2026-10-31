"""AI Assistant tab (Phase 7). AI is optional: with no provider configured every other feature still works.

Consent: when any provider in the route is external, the user sees EXACTLY what would be sent (redacted) and the
destination host(s) and must tick the consent box. Local (localhost) endpoints do not need consent.
All network work happens on a worker thread; results return through a queue.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from core.ai_assistant import NOTICE, TASKS, AIAssistant, AIResult, PreparedRequest, to_ai_finding
from core.ai_settings import load_settings, save_settings, build_router
from providers.discovery import (CLOUD_PROVIDERS, SPEC_BY_KEY, ModelCatalog, apply_cloud_choice, apply_local,
                                 detect_local)
from providers.model_router import RouterError
from security.consent_policy import ConsentGrant

TASK_LABELS = {"explain_finding": "Explain selected findings", "summarize_architecture": "Summarize architecture",
               "suggest_change": "Suggest code change", "generate_tests": "Generate tests",
               "draft_documentation": "Draft documentation"}


class ConsentDialog(tk.Toplevel):
    """Shows the exact outgoing text and hosts. ``self.grant`` is set only when the user consents."""

    def __init__(self, parent, req: PreparedRequest):
        super().__init__(parent)
        self.title("Send project content to an external AI provider?")
        self.transient(parent)
        self.grant: Optional[ConsentGrant] = None
        self.req = req
        p = req.preview
        head = (f"Destination(s): {', '.join(req.external_hosts)}\n"
                f"Items: {len(p.items)}   Bytes: {p.total_bytes:,}   Secrets redacted: {p.redaction_count}\n"
                + "\n".join(f"  - {i['kind']}: {i['path']} ({i['bytes']:,} bytes{', truncated' if i['truncated'] else ''})"
                            for i in p.items))
        ttk.Label(self, text=head, justify="left", padding=4).pack(fill="x")
        ttk.Label(self, text="Exact text that will be sent (after redaction):", padding=(4, 0)).pack(anchor="w")
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=4)
        txt = tk.Text(box, width=110, height=22, wrap="none")
        sb = ttk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("end", req.prompt)
        txt.config(state="disabled")
        self.var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, variable=self.var, text="I consent to send exactly this content to the host(s) above",
                        command=self._sync).pack(anchor="w", padx=4, pady=3)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=(0, 8))
        self.ok = ttk.Button(bar, text="Send", command=self._ok, state="disabled")
        self.ok.pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        self.grab_set()
        self.wait_window(self)

    def _sync(self):
        self.ok.config(state="normal" if self.var.get() else "disabled")

    def _ok(self):
        if self.var.get():
            self.grant = ConsentGrant.for_preview(self.req.preview, self.req.external_hosts)
        self.destroy()


class ModelCatalogDialog(tk.Toplevel):
    """Browse the live model catalogue of all cloud providers; filter by provider, Free / Paid and text; double-click to use."""

    def __init__(self, parent, catalog: ModelCatalog, on_pick: Callable[[str, str], None], key_for: Callable):
        super().__init__(parent)
        self.title("Cloud models (fetched live)")
        self.geometry("820x520")
        self.catalog, self.on_pick, self.key_for = catalog, on_pick, key_for
        self.q: "queue.Queue[dict]" = queue.Queue()
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self.prov = tk.StringVar(value="all")
        ttk.Label(top, text="Provider:").pack(side="left")
        ttk.Combobox(top, textvariable=self.prov, state="readonly", width=16,
                     values=["all"] + [c.key for c in CLOUD_PROVIDERS]).pack(side="left", padx=4)
        self.cost = tk.StringVar(value="all")
        for v, lab in (("all", "All"), ("free", "Free"), ("paid", "Paid")):
            ttk.Radiobutton(top, text=lab, value=v, variable=self.cost, command=self.fill).pack(side="left", padx=3)
        self.text = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.text, width=22)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda _e: self.fill())
        ttk.Button(top, text="Refresh from cloud", command=self.refresh).pack(side="right")
        self.prov.trace_add("write", lambda *_: self.fill())
        cols = ("provider", "model", "cost", "in", "out", "ctx")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (110, 330, 60, 90, 90, 80)):
            self.tree.heading(c, text={"in": "$/1M in", "out": "$/1M out", "ctx": "context"}.get(c, c))
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", lambda _e: self.use())
        self.msg = tk.StringVar()
        ttk.Label(self, textvariable=self.msg, wraplength=780, padding=6).pack(fill="x")
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="Use selected model", command=self.use).pack(side="left")
        ttk.Label(bar, text="Free/Paid is shown only where the provider publishes prices (OpenRouter); other providers list models "
                  "that need their own key, and their free plans have limits.", style="Muted.TLabel", wraplength=600).pack(side="left", padx=8)
        self.fill()
        if not self.catalog.models:
            self.refresh()
        self.after(150, self._poll)

    def fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        rows = self.catalog.search("" if self.prov.get() == "all" else self.prov.get(), self.cost.get(), self.text.get())
        for m in rows[:3000]:
            self.tree.insert("", "end", iid=f"{m.provider}|{m.id}", values=(
                m.provider, m.id, m.cost, "" if m.prompt_price is None else m.prompt_price,
                "" if m.completion_price is None else m.completion_price, m.context or ""))
        self.msg.set(f"{len(rows)} model(s) shown." if self.catalog.models else "No models yet - press 'Refresh from cloud'.")

    def refresh(self) -> None:
        self.msg.set("Fetching model lists from the cloud...")

        def work() -> None:
            self.q.put({"report": self.catalog.refresh(force=True)})
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            ev = self.q.get_nowait()
            self.fill()
            skipped = [m for m in ev["report"].values() if "model(s)" not in m]
            if skipped:
                self.msg.set(self.msg.get() + "  Not fetched: " + " | ".join(skipped[:4]))
        except queue.Empty:
            pass
        self.after(200, self._poll)

    def use(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        prov, _, mid = sel[0].partition("|")
        self.on_pick(prov, mid)
        self.destroy()


class AIPanel(ttk.Frame):
    """Callbacks: get_findings() -> list of findings, get_files() -> list of file names, get_architecture() -> dict|None,
    read_file(name) -> text|None, on_ai_finding(finding), on_propose_patch(file, new_text, finding_id, rationale),
    on_note(dict) records the result for reports."""

    def __init__(self, master, *, get_selected_findings: Callable[[], List[Any]], get_files: Callable[[], List[str]],
                 get_architecture: Callable[[], Optional[Dict[str, Any]]], read_file: Callable[[str], Optional[str]],
                 on_ai_finding: Optional[Callable] = None, on_propose_patch: Optional[Callable] = None,
                 on_note: Optional[Callable[[dict], None]] = None, settings_path=None, transport=None,
                 autodetect: Optional[bool] = None):
        super().__init__(master)
        self.g_find, self.g_files, self.g_arch = get_selected_findings, get_files, get_architecture
        self.read_file, self.on_ai_finding, self.on_propose_patch, self.on_note = read_file, on_ai_finding, on_propose_patch, on_note
        self.settings_path, self.transport = settings_path, transport
        self.autodetect = (transport is None and settings_path is None and "PYTEST_CURRENT_TEST" not in os.environ) if autodetect is None else autodetect
        self.catalog = ModelCatalog(transport=transport, key_for=self._key_for)
        self.settings = load_settings(settings_path)
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.last: Optional[AIResult] = None
        self.local_models: Dict[str, List[str]] = {}
        self.router = None
        self.assistant: Optional[AIAssistant] = None
        self._busy = False
        self._build()
        self._rebuild_router()
        self._after_id = self.after(150, self._poll)
        self.bind("<Destroy>", self._on_destroy)
        if self.autodetect:
            self.after(400, self.auto_setup)

    def _on_destroy(self, event) -> None:
        if event.widget is self and getattr(self, "_after_id", None):
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        ttk.Label(self, text=NOTICE, foreground="#b45309", wraplength=950, padding=3).pack(fill="x")
        cfg = ttk.LabelFrame(self, text="Provider (optional - the application works without AI)")
        cfg.pack(fill="x", padx=4, pady=4)
        ai = self.settings["ai"]
        self.enabled = tk.BooleanVar(value=ai["enabled"])
        ttk.Checkbutton(cfg, text="Enable AI", variable=self.enabled, command=self._rebuild_router).grid(row=0, column=0, sticky="w", padx=4)
        names = [p["name"] for p in ai["providers"]] or ["ollama"]
        self.prov = tk.StringVar(value=names[0])
        ttk.Label(cfg, text="Provider:").grid(row=0, column=1, sticky="e")
        cb = self.prov_box = ttk.Combobox(cfg, textvariable=self.prov, values=names, state="readonly", width=18)
        cb.grid(row=0, column=2, padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._load_provider_fields())
        self.url, self.model, self.fb = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.timeout, self.retries, self.maxtok = tk.IntVar(value=60), tk.IntVar(value=2), tk.IntVar(value=1024)
        self.key = tk.StringVar()
        self.penabled = tk.BooleanVar(value=True)
        rows = [("Base URL:", self.url, 46), ("Model:", self.model, 36), ("Fallback models (comma):", self.fb, 30)]
        for r, (lab, var, w) in enumerate(rows, start=1):
            ttk.Label(cfg, text=lab).grid(row=r, column=0, sticky="e")
            if var is self.model:
                self.model_box = ttk.Combobox(cfg, textvariable=var, width=w)
                self.model_box.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=1)
            else:
                ttk.Entry(cfg, textvariable=var, width=w).grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=1)
        ttk.Label(cfg, text="Timeout s / retries / max tokens:").grid(row=4, column=0, sticky="e")
        sp = ttk.Frame(cfg)
        sp.grid(row=4, column=1, columnspan=2, sticky="w", padx=4)
        for var, hi in ((self.timeout, 600), (self.retries, 5), (self.maxtok, 16000)):
            ttk.Spinbox(sp, from_=0 if var is self.retries else 1, to=hi, textvariable=var, width=7).pack(side="left", padx=2)
        ttk.Label(cfg, text="API key (this session only, never saved):").grid(row=5, column=0, sticky="e")
        ttk.Entry(cfg, textvariable=self.key, show="*", width=30).grid(row=5, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(cfg, text="Provider enabled", variable=self.penabled).grid(row=1, column=3, padx=4)
        ttk.Button(cfg, text="Check availability", command=self.check).grid(row=2, column=3, padx=4)
        ttk.Button(cfg, text="Save settings", command=self.save).grid(row=3, column=3, padx=4)
        ttk.Button(cfg, text="Detect local AI", command=lambda: self.auto_setup(True)).grid(row=4, column=3, padx=4)
        ttk.Button(cfg, text="Cloud models...", command=self.open_catalog).grid(row=5, column=3, padx=4)
        self.auto_start = tk.BooleanVar(value=self.settings["ai"].get("auto_start_local", True))
        ttk.Checkbutton(cfg, text="Start Ollama / LM Studio if installed but stopped", variable=self.auto_start).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=4)
        self.avail = tk.StringVar(value="Not checked.")
        ttk.Label(cfg, textvariable=self.avail, wraplength=900).grid(row=6, column=0, columnspan=4, sticky="w", padx=4)
        self._load_provider_fields()

        sel = ttk.LabelFrame(self, text="Files to include (only what you tick is sent)")
        sel.pack(fill="x", padx=4, pady=4)
        self.files = tk.Listbox(sel, selectmode="extended", height=5, exportselection=False)
        self.files.pack(side="left", fill="x", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(sel, command=self.files.yview)
        self.files.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")
        side = ttk.Frame(sel)
        side.pack(side="left", padx=3)
        ttk.Button(side, text="Refresh list", command=self.refresh_files).pack(fill="x")
        self.use_findings = tk.BooleanVar(value=True)
        ttk.Checkbutton(side, text="Include selected finding(s)", variable=self.use_findings).pack(anchor="w")
        self.use_arch = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text="Include architecture summary", variable=self.use_arch).pack(anchor="w")

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4)
        self.btns = {}
        for t in TASKS:
            b = ttk.Button(bar, text=TASK_LABELS[t], command=lambda t=t: self.start(t))
            b.pack(side="left", padx=2)
            self.btns[t] = b
        self.status = tk.StringVar(value="Idle.")
        ttk.Label(self, textvariable=self.status, padding=(8, 2)).pack(fill="x")
        self.out = tk.Text(self, wrap="word", height=16)
        self.out.pack(fill="both", expand=True, padx=4, pady=4)
        act = ttk.Frame(self)
        act.pack(fill="x", padx=4, pady=(0, 8))
        self.patch_btn = ttk.Button(act, text="Review as patch (Phase 8)...", command=self.to_patch, state="disabled")
        self.patch_btn.pack(side="left")
        self.save_btn = ttk.Button(act, text="Save output as...", command=self.save_output, state="disabled")
        self.save_btn.pack(side="left", padx=3)
        self.find_btn = ttk.Button(act, text="Add to Findings as AI hypothesis", command=self.to_findings, state="disabled")
        self.find_btn.pack(side="left")

    # ------------------------------------------------------------------ settings / router
    def _prov_dict(self) -> Dict[str, Any]:
        for p in self.settings["ai"]["providers"]:
            if p["name"] == self.prov.get():
                return p
        d = {"name": self.prov.get(), "kind": "ollama"}
        self.settings["ai"]["providers"].append(d)
        return d

    def _load_provider_fields(self) -> None:
        p = self._prov_dict()
        self.url.set(p.get("base_url", "")); self.model.set(p.get("model", ""))
        self.fb.set(", ".join(p.get("fallback_models", [])))
        self.timeout.set(p.get("timeout_seconds", 60)); self.retries.set(p.get("max_retries", 2))
        self.maxtok.set(p.get("max_tokens", 1024)); self.penabled.set(p.get("enabled", True))
        self._fill_model_choices()

    def _fill_model_choices(self) -> None:
        """Models offered for the selected provider: live local list / cloud catalogue (read from memory, no network)."""
        if not hasattr(self, "model_box"):
            return
        name = self.prov.get()
        vals = list(self.local_models.get(name, [])) or [m.id for m in self.catalog.models.get(name, [])]
        self.model_box["values"] = vals

    def _key_for(self, spec) -> str:
        """Key used ONLY to list that provider's models: its environment variable, else this session's typed key for it."""
        env = os.environ.get(spec.api_key_env, "").strip()
        if env:
            return env
        return self.key.get().strip() if hasattr(self, "key") and self.prov.get() == spec.key else ""

    def _store_provider_fields(self) -> None:
        p = self._prov_dict()
        p.update(base_url=self.url.get().strip(), model=self.model.get().strip(),
                 fallback_models=[m.strip() for m in self.fb.get().split(",") if m.strip()],
                 timeout_seconds=int(self.timeout.get()), max_retries=int(self.retries.get()),
                 max_tokens=int(self.maxtok.get()), enabled=bool(self.penabled.get()))
        self.settings["ai"]["enabled"] = bool(self.enabled.get())
        if hasattr(self, "auto_start"):
            self.settings["ai"]["auto_start_local"] = bool(self.auto_start.get())

    def _rebuild_router(self) -> None:
        self._store_provider_fields() if hasattr(self, "url") else None
        self.router = build_router(self.settings, transport=self.transport)
        lim = self.settings["ai"]["limits"]
        self.assistant = AIAssistant(self.router, self.read_file, lim["max_item_bytes"], lim["max_total_bytes"])
        self._apply_session_key()
        if hasattr(self, "status"):
            state = "AI enabled." if self.settings["ai"]["enabled"] else "AI is off (the rest of the application is unaffected)."
            self.status.set(state)

    def _apply_session_key(self) -> None:
        if self.router and hasattr(self, "key"):
            for p in self.router.providers:
                if p.name == self.prov.get():
                    p.set_session_key(self.key.get())

    def save(self) -> None:
        self._store_provider_fields()
        save_settings(self.settings, self.settings_path)
        self.status.set("Settings saved (API keys are never written to disk).")
        self._rebuild_router()

    def check(self) -> None:
        self._rebuild_router()
        prov = next((p for p in self.router.providers if p.name == self.prov.get()), None)
        if prov is None:
            self.avail.set("Provider is disabled or AI is off.")
            return
        self.avail.set("Checking...")
        threading.Thread(target=lambda: self.q.put(("avail", prov.check())), daemon=True).start()

    def refresh_files(self) -> None:
        self.files.delete(0, "end")
        for f in self.g_files():
            self.files.insert("end", f)

    # ------------------------------------------------------------------ run
    def request_explain(self, finding: Any) -> None:
        """Entry point for the Findings tab's 'Ask AI to explain' button."""
        self.start("explain_finding", findings_override=[finding])

    def start(self, task: str, findings_override: Optional[List[Any]] = None) -> None:
        if self._busy:
            return
        self._rebuild_router()
        if not self.settings["ai"]["enabled"] or not self.router.providers:
            messagebox.showinfo("AI", "AI is switched off or no provider is enabled. Enable it above; the rest of the "
                                      "application works without AI.", parent=self)
            return
        files = [self.files.get(i) for i in self.files.curselection()]
        findings = findings_override if findings_override is not None else (self.g_find() if self.use_findings.get() else [])
        arch = self.g_arch() if (self.use_arch.get() or task == "summarize_architecture") else None
        try:
            req = self.assistant.prepare(task, findings=findings, files=files, architecture=arch)
        except ValueError as exc:
            messagebox.showinfo("AI", str(exc), parent=self)
            return
        grant = None
        if req.needs_consent:
            dlg = ConsentDialog(self, req)
            grant = dlg.grant
            if grant is None:
                self.status.set("Not sent: no consent given.")
                return
        self._busy = True
        for b in self.btns.values():
            b.config(state="disabled")
        self.status.set(f"Asking {req.provider_name or 'provider'}... ({len(req.preview.items)} item(s), {req.preview.total_bytes:,} bytes)")

        def work():
            try:
                self.q.put(("done", self.assistant.run(req, grant)))
            except RouterError as exc:
                self.q.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("error", f"Unexpected error: {type(exc).__name__}"))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "local":
                    self._local_done(payload)
                elif kind == "catalog":
                    self._catalog_done(payload)
                elif kind == "avail":
                    ok, msg = payload
                    self.avail.set(("OK: " if ok else "Unavailable: ") + msg)
                elif kind in ("done", "error"):
                    self._busy = False
                    for b in self.btns.values():
                        b.config(state="normal")
                    if kind == "error":
                        self.status.set("Failed.")
                        self.out.delete("1.0", "end")
                        self.out.insert("end", payload)
                    else:
                        self._show(payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._after_id = self.after(150, self._poll)

    # ------------------------------------------------------------------ auto setup / catalogue
    def auto_setup(self, manual: bool = False) -> None:
        """Find Ollama / LM Studio (start them if installed and allowed), configure them, then refresh cloud model lists."""
        ai = self.settings["ai"]
        if not manual and not ai.get("auto_detect_local", True):
            return
        self.avail.set("Looking for local AI servers (Ollama, LM Studio)...")
        start = bool(self.auto_start.get())
        fetch_cloud = manual or ai.get("fetch_cloud_models", True)

        def work() -> None:
            servers = detect_local(self.transport, auto_start=start)
            self.q.put(("local", servers))
            if fetch_cloud:
                self.q.put(("catalog", self.catalog.refresh(force=manual)))
        threading.Thread(target=work, daemon=True).start()

    def _local_done(self, servers) -> None:
        self._store_provider_fields()
        log = apply_local(self.settings, servers)
        self.local_models = {("ollama" if s.key == "ollama" else "lmstudio"): s.models for s in servers if s.running}
        names = [p["name"] for p in self.settings["ai"]["providers"]]
        self.prov_box["values"] = names
        if self.prov.get() not in names:
            self.prov.set(names[0])
        notes = [f"{s.label}: {s.note}" for s in servers if s.note and not s.running] + [n for s in servers if s.running for n in [s.note] if n]
        ready = [s for s in servers if s.running and s.models]
        if ready and not self.settings["ai"]["enabled"]:
            notes.append("Local AI is ready - tick 'Enable AI' to use it (it stays on this computer).")
        self._load_provider_fields()
        self._rebuild_router()
        self.avail.set(" ".join(log + notes) or "No local AI server found.")

    def _catalog_done(self, report) -> None:
        self._fill_model_choices()
        n = sum(len(v) for v in self.catalog.models.values())
        bad = [msg for k, msg in report.items() if msg and "model(s)" not in msg and not msg.startswith("cached")]
        self.status.set(f"Cloud model catalogue: {n} model(s) from {len(self.catalog.models)} provider(s)."
                        + (f" {len(bad)} provider(s) need a key or were unreachable." if bad else ""))

    def open_catalog(self) -> None:
        ModelCatalogDialog(self, self.catalog, self._pick_cloud_model, self._key_for)

    def _pick_cloud_model(self, provider: str, model_id: str) -> None:
        spec = SPEC_BY_KEY[provider]
        self._store_provider_fields()
        apply_cloud_choice(self.settings, spec, model_id)
        names = [p["name"] for p in self.settings["ai"]["providers"]]
        self.prov_box["values"] = names
        self.prov.set(provider)
        self._load_provider_fields()
        self._rebuild_router()
        self.avail.set(f"{spec.label} / {model_id} selected. " + (f"Set the environment variable {spec.api_key_env} or type a session key, "
                                                                 "then press Save settings." if not self._key_for(spec) else "Press Save settings to keep it."))

    def _show(self, res: AIResult) -> None:
        self.last = res
        head = [f"=== {TASK_LABELS.get(res.task, res.task)} | {res.provider} / {res.model} ===", NOTICE]
        head += [f"WARNING: {w}" for w in res.warnings]
        if res.fallback_used:
            head.append("A fallback provider/model was used.")
        head.append(f"Sent: {', '.join(res.sent_items) or 'nothing from the project'}\n")
        self.out.delete("1.0", "end")
        self.out.insert("end", "\n".join(head) + "\n" + res.text)
        self.status.set("Done - unverified AI output.")
        self.patch_btn.config(state="normal" if res.proposed_file_text else "disabled")
        self.save_btn.config(state="normal" if res.task in ("generate_tests", "draft_documentation") else "disabled")
        self.find_btn.config(state="normal")
        if self.on_note:
            self.on_note({"task": res.task, "provider": res.provider, "model": res.model, "sent_items": res.sent_items,
                          "verified": False, "notice": NOTICE, "text": res.text[:8000], "warnings": res.warnings})

    # ------------------------------------------------------------------ outputs
    def to_patch(self) -> None:
        r = self.last
        if r and r.proposed_file_text and self.on_propose_patch:
            self.on_propose_patch(r.target_file, r.proposed_file_text, (r.finding_ids or [""])[0],
                                  f"AI suggestion from {r.provider}/{r.model} - unverified.")

    def save_output(self) -> None:
        r = self.last
        if not r:
            return
        blocks = r.code_blocks
        text = blocks[0]["code"] if (r.task == "generate_tests" and blocks) else r.text
        path = filedialog.asksaveasfilename(parent=self, title="Save AI output (never overwrites silently)",
                                            defaultextension=".py" if r.task == "generate_tests" else ".md")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.status.set(f"Saved to {path} (unverified).")

    def to_findings(self) -> None:
        if self.last and self.on_ai_finding:
            fnd = self.g_find()
            self.on_ai_finding(to_ai_finding(self.last, fnd[0] if fnd else None))
            self.status.set("Added to Findings as an AI hypothesis (weak, unverified).")
