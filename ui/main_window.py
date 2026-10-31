"""Tkinter front end. Analysis runs in a worker thread; the UI only polls a queue."""
from __future__ import annotations

import logging
import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from core.ai_assistant import architecture_context
from core.analysis_options import AnalysisOptions
from core.analysis_orchestrator import AnalysisCancelled, analyze_archive
from core.source_access import ZipSourceReader
from core.archive_manager import ArchiveError
from core.report_generator import write_reports
from core.settings import Settings, resolve_output_dir, to_limits
from database.db_manager import DbManager, HistoryError, file_sha256
from ui.findings_view import FindingsView
from ui.history_view import HistoryView
from ui.project_view import ProjectView
from ui.report_view import ReportView
from ui.settings_view import SettingsView
from core.patch_generators import can_suggest
from core.patch_manager import NoFixAvailable, PatchError, Workspace, propose, propose_from_finding
from services.environment_service import detect_isolation
from services.isolated_test_runner import IsolatedSandboxRunner
from ui.ai_panel import AIPanel
from ui.execution_dialog import ExecutionTab, request_execution_approval
from ui.live_run_view import LiveRunView
from ui.app_preview_view import AppPreviewView
from ui.screen_tools import SNAPSHOTS
from ui.patch_dialog import PatchDialog
from ui import theme
from ui.sidebar import FULL_W, RAIL_W, Sidebar
from ui.widgets import Collapsible, ScrollFrame, ensure_scrollbars, make_labelframes_collapsible, scrolled_text
from models.analysis_result import AnalysisResult

log = logging.getLogger("ase.ui")


class MainWindow:
    def __init__(self, root: tk.Tk, out_dir: str | Path = "reports", settings: Settings | None = None,
                 db: DbManager | None = None, base_dir: str | Path | None = None):
        self.root = root
        try:                                    # housekeeping: project copies left behind by a crashed run (older than an hour)
            import threading as _th
            from plugins.build_execution import storage as _storage
            _th.Thread(target=_storage.sweep_old_scratch, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        self.settings = settings            # Phase 9 (optional): None keeps the previous behavior exactly
        self.base_dir = Path(base_dir) if base_dir else None
        self.db = db
        if self.db is None and settings is not None and settings.history_enabled and self.base_dir is not None:
            try:
                self.db = DbManager(self.base_dir / "data" / "history.db", settings.max_history)
            except HistoryError as exc:
                log.warning("History disabled: %s", exc)
        self._selected_finding = None
        self.out_dir = Path(out_dir)
        self.zip_path: str = ""
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.report_paths: dict = {}
        self.workspace = None
        self.workspace_zip = ""
        self._last_result = None
        self._pending_finding = None
        root.title("AI Software Engineering Agent")
        self.reader = None
        self._pending_ai_patch = None
        self.findings_plugin = None
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1360, sw - 40)}x{min(780, sh - 80)}+10+10")
        root.minsize(min(900, sw - 40), min(520, sh - 80))
        self._maximize()
        self.dark = theme.load_pref_dark(self.base_dir)
        self.colors = theme.apply_theme(root, self.dark)
        try:
            self._app_icon = theme.make_app_icon()
            root.iconphoto(True, self._app_icon)
        except tk.TclError:
            pass
        self._build()
        self.root.after(100, self._poll)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        c = self.colors
        # ---- toolbar (icon buttons, one slim row)
        top = ttk.Frame(self.root, padding=(6, 4))
        top.pack(fill="x")
        self.path_var = tk.StringVar(value="No source selected")
        self._tip(ttk.Button(top, text=theme.icon("archive", "Archive"), style="Tool.TButton", command=self.select_zip), "Select a ZIP / tar / tar.gz archive").pack(side="left")
        self._tip(ttk.Button(top, text=theme.icon("folder", "Folder"), style="Tool.TButton", command=self.select_folder), "Select a project folder (read-only)").pack(side="left", padx=(3, 0))
        self.analyze_btn = ttk.Button(top, text=theme.icon("analyze", "Analyze"), style="Accent.TButton", command=self.start, state="disabled")
        self.analyze_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(top, text=theme.icon("cancel", "Cancel"), style="Tool.TButton", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Label(top, textvariable=self.path_var, style="Muted.TLabel").pack(side="left", padx=10)
        self.theme_btn = self._tip(ttk.Button(top, text=theme.icon("theme"), width=3, style="Tool.TButton", command=self.toggle_theme), "Switch light / dark theme")
        self.theme_btn.pack(side="right")
        self.html_btn = ttk.Button(top, text=theme.icon("html", "HTML"), style="Tool.TButton", command=lambda: self.open_report("html"), state="disabled")
        self.html_btn.pack(side="right", padx=3)
        self.json_btn = ttk.Button(top, text=theme.icon("json", "JSON"), style="Tool.TButton", command=lambda: self.open_report("json"), state="disabled")
        self.json_btn.pack(side="right")
        self.fix_btn = ttk.Button(top, text=theme.icon("fix", "Fix"), style="Tool.TButton", command=self.propose_fix, state="disabled")
        self.fix_btn.pack(side="right", padx=3)
        self._tip(self.html_btn, "Open the HTML report"); self._tip(self.json_btn, "Open the JSON report"); self._tip(self.fix_btn, "Propose a human-approved fix for the selected finding")
        # ---- options strip (collapsible)
        self.prefs = theme.load_prefs(self.base_dir)
        self.opts_section = Collapsible(self.root, "Options", expanded=not self.prefs.get("opts_collapsed", False),
                                        on_toggle=lambda ex: theme.save_prefs(self.base_dir, opts_collapsed=not ex))
        self.opts_section.pack(fill="x", padx=6)
        opts = ttk.Frame(self.opts_section.body, padding=(2, 0, 2, 3))
        opts.pack(fill="x")
        self.opt_static = tk.BooleanVar(value=True)
        self.opt_db = tk.BooleanVar(value=True)
        self.opt_exec = tk.BooleanVar(value=True)
        self._tip(ttk.Checkbutton(opts, text="Languages", variable=self.opt_static), "Language-specific static analyzers (Phase 3)").pack(side="left")
        self._tip(ttk.Checkbutton(opts, text="Databases", variable=self.opt_db), "Database analysis (Phase 5)").pack(side="left", padx=8)
        self._tip(ttk.Checkbutton(opts, text="Discover build/test", variable=self.opt_exec), "Discover build/test commands (Phase 4). Nothing is run.").pack(side="left")
        for key, label, cmd, tip in (("delivery", "Delivery", self.build_delivery, "Build the secret-scanned delivery package"),
                                     ("docs", "Docs", self.generate_docs, "Generate technical documentation (Markdown)"),
                                     ("update", "Update", self.update_reports, "Rewrite reports including AI notes, runs and patches so far")):
            self._tip(ttk.Button(opts, text=theme.icon(key, label), style="Tool.TButton", command=cmd), tip).pack(side="right", padx=(3, 0))
        # ---- status bar (bottom): message + thin progress
        bar = ttk.Frame(self.root, style="Panel.TFrame")
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Select a ZIP archive or folder to begin.")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, maximum=100, length=160)
        self.progress.pack(side="right", padx=6)
        # ---- body: sidebar | pages (drag the splitter to resize; « collapses the sidebar to an icon rail)
        self.paned = ttk.PanedWindow(self.root, orient="horizontal")
        self.paned.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(self.paned)
        rail = bool(self.prefs.get("side_rail", False))
        self.sidebar = Sidebar(self.paned, self.nb, theme.SIDEBAR_GROUPS, c, collapsed=tuple(self.prefs.get("side_collapsed", ["Project"])),
                               on_width=self._side_width, rail=rail)
        self.paned.add(self.sidebar, weight=0)
        self.paned.add(self.nb, weight=1)
        self._place_sash(RAIL_W if rail else int(self.prefs.get("side_w", FULL_W)))
        _f, self.overview = scrolled_text(self.nb, wrap="word", state="disabled")
        self.nb.add(_f, text="Overview")
        self.langs = self._tree(("Language", "Files", "Size (bytes)", "% bytes", "Confidence"), "Languages")
        self.fw = self._tree(("Name", "Kind", "Confidence", "Evidence"), "Frameworks")
        self.dbs = self._tree(("Database", "Confidence", "Evidence", "Note"), "Databases")
        self.mf = self._tree(("File", "Ecosystem", "Kind", "Dependencies"), "Manifests")
        self.fnd = self._tree(("ID", "Severity", "Category", "Title", "Location", "Confidence"), "Findings")
        self.fnd.bind("<<TreeviewSelect>>", self._show_finding)
        _f, self.detail = scrolled_text(self.nb, wrap="word", state="disabled")
        self.nb.add(_f, text="Finding detail")
        self._findings: dict = {}
        # Phase 2 tabs (additive)
        self.struct = self._tree(("Name", "Files", "Size (bytes)", "Note"), "Structure", show="tree headings")
        self.entry = self._tree(("File", "Line", "Kind", "Language", "Confidence", "Evidence"), "Entry points")
        self.imp = self._tree(("Package", "Language", "Imports", "Declared", "First seen"), "Imports")
        self.cfg = self._tree(("Item", "Type", "Status / form", "Location"), "Config & Env")
        self.conf = self._tree(("Subject", "Kind", "Evidence class", "Reasons"), "Confidence")
        self.swarn = self._tree(("Severity", "Code", "Message", "Location"), "Config warnings")
        # Phase 3 / 5 / 6 / 4 / 7 tabs
        self.tools = self._tree(("Analyzer", "Status", "Version", "Exit", "Command", "Note"), "Static tools")
        self.cmap = self._tree(("Kind", "Name", "Location", "Language", "Method", "Params", "Complexity"), "Code map")
        self._build_db_tab()
        self._build_findings_explorer()
        _pg = ScrollFrame(self.nb, min_width=760)
        self.exec_tab = ExecutionTab(_pg.inner, get_source=lambda: self.zip_path, on_result=self._exec_result,
                                     on_gui_run=self._gui_to_preview,
                                     on_forget=self._forget_source)
        self.exec_tab.pack(fill="both", expand=True)
        self.nb.add(_pg, text="Build & Test")
        self._bt_page = _pg
        self.exec_tab.auto_start.set(bool(self.prefs.get("auto_start_runtime", True)))
        self.exec_tab.auto_start.trace_add("write", lambda *_: theme.save_prefs(self.base_dir, auto_start_runtime=bool(self.exec_tab.auto_start.get())))
        self.live_view = LiveRunView(self.nb, on_run_start=self._on_run_started)
        self.live_view._dark = self.dark
        self.live_view.recolor(self.colors)
        self.nb.add(self.live_view, text="Live Run")
        if self.base_dir:
            SNAPSHOTS.set_default(self.base_dir / "data" / "snapshots")                # screen snapshots (Save / Quick save) go here by default
        self.preview_view = AppPreviewView(self.nb, get_source=lambda: self.zip_path, on_result=self._exec_result,
                                           on_source_changed=self._set_source, on_forget=self._forget_source,
                                           prefs={k[8:]: v for k, v in self.prefs.items() if k.startswith("preview_")},          # screen size / rate / view
                                           on_pref=lambda k, v: theme.save_prefs(self.base_dir, **{"preview_" + k: v}))
        self.preview_view.recolor(self.colors)
        self.nb.add(self.preview_view, text="App Preview")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.prefs.get("show_legacy_tabs", False):              # one run view: App Preview does everything; these two return only when needed
            for page in (self._bt_page, self.live_view):
                self.nb.hide(page)
        _pg = ScrollFrame(self.nb, min_width=760)
        self.ai_panel = AIPanel(
            _pg.inner, get_selected_findings=self._selected_findings, get_files=self._ai_files,
            get_architecture=lambda: architecture_context(self._last_result) if self._last_result else None,
            read_file=self._ai_read, on_ai_finding=self._ai_finding, on_propose_patch=self._ai_patch,
            on_note=self._ai_note, settings_path=getattr(self, "settings_path", None))
        self.ai_panel.pack(fill="both", expand=True)
        self.nb.add(_pg, text="AI Assistant")
        self._build_phase9()

    def _build_phase9(self) -> None:
        # Phase 9 views (appended; existing tabs are unchanged)
        self.dashboard = ProjectView(self.nb)
        self.nb.add(self.dashboard, text="Dashboard")
        self.findings_view = FindingsView(self.nb, on_select=self._on_view_selection)
        self.nb.add(self.findings_view, text="Findings (filter)")
        self.report_view = ReportView(self.nb, open_report=self.open_report)
        self.nb.add(self.report_view, text="Report preview")
        self.history_view = HistoryView(self.nb, db=self.db, on_open=self._open_history)
        self.nb.add(self.history_view, text="History")
        from ui.grading_panel import GradingPanel
        _pg = ScrollFrame(self.nb, min_width=760)
        self.grading_panel = GradingPanel(_pg.inner, self.out_dir)
        self.grading_panel.pack(fill="both", expand=True)
        self.nb.add(_pg, text="Batch grading")
        from ui.compliance_panel import CompliancePanel
        from ui.team_panel import TeamPanel
        _pg = ScrollFrame(self.nb, min_width=760)
        self.compliance_panel = CompliancePanel(
            _pg.inner, self.out_dir, get_result=lambda: self._last_result, get_source=lambda: self.zip_path or None,
            get_audit=lambda: self.team_panel.ws.audit if getattr(self, "team_panel", None) and self.team_panel.ws else None,
            get_db=lambda: self.db)
        self.compliance_panel.pack(fill="both", expand=True)
        self.nb.add(_pg, text="Compliance")
        _pg = ScrollFrame(self.nb, min_width=760)
        self.team_panel = TeamPanel(_pg.inner)
        self.team_panel.pack(fill="both", expand=True)
        self.nb.add(_pg, text="Team")
        self._settings_page = ScrollFrame(self.nb)
        self.settings_view = SettingsView(
            self._settings_page.inner, self.settings or Settings(),
            user_path=(self.base_dir / "data" / "user_settings.json") if self.base_dir else None,
            on_saved=self._settings_saved)
        self.settings_view.pack(fill="both", expand=True)
        self.nb.add(self._settings_page, text="Settings")
        self._build_portfolio_tab()
        self._build_insights_tab()
        self._build_prerequisites_tab()
        self.sidebar.build()
        ensure_scrollbars(self.root)
        make_labelframes_collapsible(self.root)
        theme.restyle_tk_widgets(self.root, self.colors)
        self._select_tab("Overview")

    def _build_insights_tab(self) -> None:
        """Run Insights tab (restored): receives every finished sandbox run in addition to the existing handler."""
        try:
            from ui.run_insights_view import RunInsightsView
            _pg = ScrollFrame(self.nb, min_width=760)
            self.insights_view = RunInsightsView(_pg.inner, get_source=lambda: self.exec_tab._source(), out_dir=self.out_dir)
            self.insights_view.pack(fill="both", expand=True)
            self.nb.add(_pg, text="Run Insights")
            previous = self.exec_tab.on_result

            def chained(res, _prev=previous):
                if _prev:
                    _prev(res)
                self.insights_view.show_result(res)
            self.exec_tab.on_result = chained
        except Exception:  # noqa: BLE001 - a failure here never prevents the rest of the window from working
            log.exception("Run Insights tab could not be created")

    def _build_portfolio_tab(self) -> None:
        """Institution portfolio tab (restored)."""
        try:
            from ui.portfolio_view import PortfolioView
            from core.settings import to_limits
            limits = to_limits(self.settings) if self.settings is not None else None
            profiles_path = (self.base_dir / "config" / "institution_profiles.json") if self.base_dir else None
            _pg = ScrollFrame(self.nb, min_width=760)
            self.portfolio_view = PortfolioView(_pg.inner, self.out_dir, limits=limits, profiles_path=profiles_path)
            self.portfolio_view.pack(fill="both", expand=True)
            self.nb.add(_pg, text="Portfolio")
        except Exception:  # noqa: BLE001
            log.exception("Portfolio tab could not be created")

    def _build_prerequisites_tab(self) -> None:
        """Prerequisite manager (Docker/Podman/analysis tools; proxy, offline folder, IT request). Checks only when first shown."""
        try:
            from prerequisites.ui import PrerequisitesTab
            _pg = ScrollFrame(self.nb, min_width=760)
            self.prereq_tab = PrerequisitesTab(_pg.inner, root_dir=self.base_dir, auto_refresh=False)
            self.prereq_tab.pack(fill="both", expand=True)
            self.nb.add(_pg, text="Prerequisites")
        except Exception:  # noqa: BLE001
            log.exception("Prerequisites tab could not be created")

    def _maximize(self) -> None:
        """Start maximized (Windows/macOS: zoomed state; X11: -zoomed). Harmless where the window manager ignores it."""
        for how in (lambda: self.root.state("zoomed"), lambda: self.root.attributes("-zoomed", True)):
            try:
                how()
                return
            except tk.TclError:
                continue

    def _place_sash(self, w: int, tries: int = 40) -> None:
        """The splitter only exists once the window is mapped; retry until it can be positioned."""
        try:
            self.paned.sashpos(0, int(w))
            if self.paned.sashpos(0) >= min(int(w), 40):
                return
        except tk.TclError:
            pass
        if tries > 0:
            self.root.after(60, lambda: self._place_sash(w, tries - 1))

    def _side_width(self, w: int, save: bool = True) -> None:
        try:
            self.paned.sashpos(0, int(w))
        except tk.TclError:
            return
        if save:
            theme.save_prefs(self.base_dir, side_rail=self.sidebar.rail, side_w=int(w) if not self.sidebar.rail else self.prefs.get("side_w", FULL_W),
                             side_collapsed=sorted(self.sidebar.collapsed))

    def _tip(self, widget, text: str):
        theme.Tooltip(widget, text)
        return widget

    def _show_page(self, widget) -> None:
        """Select the notebook page that contains ``widget`` (pages may be wrapped in a scroll frame)."""
        w = widget
        while w is not None and w is not self.root:
            try:
                self.nb.select(w)
                return
            except tk.TclError:
                w = w.master
        
    def _on_run_started(self, rid: str) -> None:
        """A run started: runs begun in App Preview stay there (it shows everything); others (Build & Test) switch to Live Run."""
        pv = getattr(self, "preview_view", None)
        if pv is not None and pv.run_id == rid:
            self._show_page(pv)
        else:
            self._select_tab("Live Run")                               # shows the hidden page again for runs started elsewhere

    def _on_close(self) -> None:
        """Closing: stop a running preview, then delete every leftover copy / stopped container (your own files stay)."""
        try:
            pv = self.preview_view
            if pv.session and pv.session.running:
                pv.session.stop()
                pv.session.wait(15)
            if pv.auto_clean_var.get():
                from plugins.build_execution import cleanup
                from plugins.build_execution.models import Options
                from plugins.build_execution.sandbox import find_runtime
                self.root.title("Cleaning up...")
                self.root.update_idletasks()
                cleanup.fresh_start(find_runtime(Options())[1])
        except Exception:  # noqa: BLE001 - closing must always work
            log.exception("clean-up at close failed")
        self.root.destroy()

    def _gui_to_preview(self, source: str, script: str) -> None:
        """Build & Test found a window program: run it in App Preview (virtual screen) instead; the approval dialog is shown there."""
        self._select_tab("App Preview")
        self.preview_view.run_script(source, script)

    def _select_tab(self, title: str) -> None:
        self.sidebar.select(title)

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.colors = theme.apply_theme(self.root, self.dark)
        self.sidebar.recolor(self.colors)
        self.live_view._dark = self.dark
        self.live_view.recolor(self.colors)
        self.preview_view.recolor(self.colors)
        theme.save_pref_dark(self.base_dir, self.dark)

    def _build_db_tab(self) -> None:
        frame = ttk.Frame(self.nb)
        cols = ("Database", "Status", "Rationale")
        self.dbt = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for c in cols:
            self.dbt.heading(c, text=c)
            self.dbt.column(c, width=260 if c == "Rationale" else 140, anchor="w")
        self.dbt.pack(fill="x", padx=4, pady=4)
        cols2 = ("Database", "Level", "Kind", "Location", "Scope", "Redacted value")
        self.dbc = ttk.Treeview(frame, columns=cols2, show="headings", height=7)
        for c in cols2:
            self.dbc.heading(c, text=c)
            self.dbc.column(c, width=130, anchor="w")
        self.dbc.pack(fill="x", padx=4, pady=4)
        _f, self.dbx = scrolled_text(frame, wrap="word", height=10, state="disabled")
        _f.pack(fill="both", expand=True, padx=4, pady=4)
        self.nb.add(frame, text="Databases")

    def _build_findings_explorer(self) -> None:
        from findings_plugin import register
        self.findings_plugin = register(self.nb, tab_text="Findings Explorer", on_request_ai=self._ask_ai_about)

    def _ask_ai_about(self, finding) -> None:
        self._show_page(self.ai_panel)
        self.ai_panel.request_explain(finding)

    def _selected_findings(self):
        return [self._findings[i] for i in self.fnd.selection() if i in self._findings]

    def _ai_files(self):
        if not self.zip_path:
            return []
        self.reader = ZipSourceReader(self.zip_path)
        try:
            return self.reader.list_text_files()
        except Exception:  # noqa: BLE001
            return []

    def _ai_read(self, name):
        if self.reader is None and self.zip_path:
            self.reader = ZipSourceReader(self.zip_path)
        return self.reader.read(name) if self.reader else None

    def _ai_finding(self, finding) -> None:
        if self.findings_plugin is not None:
            self.findings_plugin.store.add(finding)
            self._show_page(self.findings_plugin.view)

    def _ai_note(self, note: dict) -> None:
        if self._last_result is not None:
            self._last_result.ai_notes.append(note)

    def _exec_result(self, res) -> None:
        d = res.to_dict()
        d["stdout"], d["stderr"] = d.get("stdout", "")[-20000:], d.get("stderr", "")[-20000:]
        if self._last_result is not None:
            self._last_result.execution_results.append(d)
        if self.findings_plugin is not None and res.status in ("completed", "failed", "timeout"):
            self.findings_plugin.analyze_log_text((res.stdout or "") + "\n" + (res.stderr or ""), f"run:{res.candidate_id}")

    def build_delivery(self) -> None:
        """Phase 10: secret-scanned, checksummed ZIP of THIS application (never the analysed project)."""
        if not self.base_dir:
            messagebox.showinfo("Delivery package", "The application folder is unknown; start with `python app.py`.")
            return
        dest = filedialog.askdirectory(title="Folder for the delivery package")
        if not dest:
            return
        run_tests = messagebox.askyesno("Delivery package", "Also run this application's own test suite and store the real "
                                        "result as build/test evidence? (can take a few minutes)")
        self.status_var.set("Building delivery package...")
        base = self.base_dir

        def work() -> None:
            try:
                from core.delivery_packager import build_package
                self.q.put(("delivery", str(build_package(base, dest, run_tests=run_tests))))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("delivery_error", str(exc)[:300]))
        threading.Thread(target=work, daemon=True).start()

    def update_reports(self) -> None:
        if self._last_result is None:
            return
        self.report_paths = write_reports(self._last_result, self.out_dir)
        self.status_var.set(f"Reports updated in {self.out_dir} (includes AI notes, runs and patches so far).")

    def _tree(self, cols, title, show="headings") -> ttk.Treeview:
        frame = ttk.Frame(self.nb)
        tree = ttk.Treeview(frame, columns=cols, show=show)
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=140, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.nb.add(frame, text=title)
        return tree

    # ---------------------------------------------------------------- actions
    def select_zip(self) -> None:
        path = filedialog.askopenfilename(title="Select a project archive",
                                          filetypes=[("Project archives", "*.zip *.tar *.tar.gz *.tgz *.tar.bz2 *.tar.xz"),
                                                     ("All files", "*.*")])
        if path:
            self._set_source(path)

    def select_folder(self) -> None:
        path = filedialog.askdirectory(title="Select a project folder (read-only; nothing is executed)")
        if path:
            self._set_source(path)

    def generate_docs(self) -> None:
        if self._last_result is None:
            messagebox.showinfo("Documentation", "Run an analysis first.")
            return
        from core.doc_generator import write_documentation
        p = write_documentation(self._last_result, self.out_dir)
        self.status_var.set(f"Documentation written: {p}")

    def _set_source(self, path: str) -> None:
        if True:
            self.zip_path = path
            self.path_var.set(path)
            self.analyze_btn.config(state="normal")
            self.status_var.set("Ready to analyze.")
            pv = getattr(self, "preview_view", None)
            if pv is not None:
                pv.src_var.set(path)                       # App Preview follows the chosen project (scans when shown)
                if pv.winfo_ismapped():
                    pv.set_source(path)
            tab = getattr(self, "exec_tab", None)
            if tab is not None:
                tab.src.set(path)                          # Build & Test follows the chosen project automatically
                tab._discovered_for = ""
                if tab.winfo_ismapped():
                    tab.refresh_source()

    def _forget_source(self) -> None:
        """Forget the selected project / archive / file (Build & Test 'Clear project & copies', or the 'forget after run' setting).
        Only the selection is reset; nothing on disk is touched."""
        self.zip_path = ""
        self.path_var.set("")
        self.analyze_btn.config(state="disabled")
        self.status_var.set("Project cleared.")
        pv = getattr(self, "preview_view", None)
        if pv is not None:
            pv.src_var.set("")
        tab = getattr(self, "exec_tab", None)
        if tab is not None:
            tab.src.set("")
            tab.forget_display()
    def start(self) -> None:
        if not self.zip_path or (self.worker and self.worker.is_alive()):
            return
        self.cancel_event.clear()
        self.analyze_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.html_btn.config(state="disabled")
        self.json_btn.config(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("Starting analysis...")
        # Tk variables are read HERE (UI thread); the worker thread must never touch Tk.
        opts = AnalysisOptions(static_plugins=self.opt_static.get(), database_analysis=self.opt_db.get(),
                               discover_execution=self.opt_exec.get())
        self.worker = threading.Thread(target=self._work, args=(self.zip_path, opts), daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling...")

    def open_report(self, kind: str) -> None:
        p = self.report_paths.get(kind)
        if p:
            webbrowser.open(Path(p).resolve().as_uri())

    # ---------------------------------------------------------------- worker thread (no Tk calls here)
    def _work(self, path: str, opts: AnalysisOptions) -> None:
        try:
            limits = to_limits(self.settings) if self.settings else None
            db = self.db
            result = analyze_archive(path, limits=limits, progress=lambda f, m: self.q.put(("progress", f, m)),
                                     cancel=self.cancel_event, options=opts)
            paths = write_reports(result, self.out_dir)
            hid, note = None, ""
            if db is not None:
                try:
                    hid = db.save_result(result, paths, file_sha256(path))
                except (HistoryError, OSError) as exc:
                    log.warning("History not saved: %s", exc)
                    note = " (not saved to history)"
            self.q.put(("done", result, paths, hid, note))
        except AnalysisCancelled:
            self.q.put(("cancelled",))
        except ArchiveError as exc:
            self.q.put(("error", str(exc)))
        except Exception:  # noqa: BLE001 - last-resort guard so the UI never crashes
            log.exception("Unexpected analysis failure")
            self.q.put(("error", "An unexpected error occurred. Details were written to logs/app.log."))

    # ---------------------------------------------------------------- UI thread
    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.progress["value"] = msg[1] * 100
                    self.status_var.set(msg[2])
                elif kind == "done":
                    self._finish(msg[1], msg[2], msg[3] if len(msg) > 3 else None, msg[4] if len(msg) > 4 else "")
                elif kind == "delivery":
                    self.status_var.set(f"Delivery package written: {msg[1]}")
                elif kind == "delivery_error":
                    self.status_var.set("Delivery package failed.")
                    messagebox.showerror("Delivery package", msg[1])
                elif kind == "approve":
                    plan, evt, holder = msg[1], msg[2], msg[3]
                    holder["ok"] = self._ask_plan_approval(plan)
                    evt.set()
                elif kind == "workspace":
                    self.workspace, self.workspace_zip = msg[1], self.zip_path
                    self.status_var.set(f"Workspace ready: {msg[1].root}")
                    self._open_patch_dialog()
                elif kind == "workspace_error":
                    self.status_var.set("Workspace creation failed.")
                    messagebox.showerror("Workspace", msg[1])
                elif kind == "cancelled":
                    self._idle("Analysis cancelled.")
                elif kind == "error":
                    self._idle("Analysis failed.")
                    messagebox.showerror("Analysis failed", msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _idle(self, status: str) -> None:
        self.analyze_btn.config(state="normal" if self.zip_path else "disabled")
        self.cancel_btn.config(state="disabled")
        self.status_var.set(status)

    def _finish(self, result: AnalysisResult, paths: dict, history_id=None, note: str = "") -> None:
        self.report_paths = paths
        self.progress["value"] = 100
        self._idle(f"Done: {len(result.findings)} finding(s). Reports saved to {self.out_dir}")
        self.html_btn.config(state="normal")
        self.json_btn.config(state="normal")
        self._last_result = result
        self._save_to_team(result, paths)
        self._fill(result)
        self._show_phase9(result, paths)
        if note:
            self.status_var.set(self.status_var.get() + note)
        if history_id is not None:
            self.history_view.refresh()

    def _save_to_team(self, result, paths) -> None:
        """Team mode (Stage D): when signed in, also save the run to the shared history. Never breaks single-user use."""
        tp = getattr(self, "team_panel", None)
        if tp is None or tp.session is None or tp.ws is None:
            return
        try:
            tp.ws.save_analysis(tp.session, result, paths)
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Shared history not updated: {exc}")

    def _fill(self, r: AnalysisResult) -> None:
        for t in (self.langs, self.fw, self.dbs, self.mf, self.fnd):
            t.delete(*t.get_children())
        for l in r.languages:
            self.langs.insert("", "end", values=(l.language, l.files, l.bytes, l.percent_of_bytes, l.confidence))
        for d in r.frameworks:
            self.fw.insert("", "end", values=(d.name, d.kind, d.confidence,
                                              "; ".join(f"{e.file}:{e.line}" for e in d.evidence[:3])))
        for d in r.databases:
            self.dbs.insert("", "end", values=(d.name, d.confidence,
                                               "; ".join(f"{e.file}:{e.line}" for e in d.evidence[:3]), d.note))
        for m in r.manifests:
            self.mf.insert("", "end", values=(m.path, m.ecosystem, m.kind, len(m.dependencies)))
        self._findings = {}
        for f in r.findings:
            iid = self.fnd.insert("", "end", values=(f.id, f.severity, f.category, f.title,
                                                     f"{f.file or ''}:{f.line or ''}", f.confidence))
            self._findings[iid] = f
        a = r.archive
        lines = [f"Archive: {r.archive_name}", f"Files: {a.file_count:,}   Folders: {a.dir_count:,}",
                 f"Uncompressed: {a.total_uncompressed:,} bytes   Compressed: {a.total_compressed:,} bytes",
                 f"Text files scanned: {a.scanned_text_files:,}   Vendored files ignored: {a.vendored_file_count:,}",
                 f"Skipped unsafe/unreadable entries: {a.skipped_total:,}", "",
                 "Languages: " + (", ".join(f"{l.language} ({l.confidence})" for l in r.languages) or "none"),
                 "Frameworks: " + (", ".join(f"{d.name} ({d.confidence})" for d in r.frameworks) or "none"),
                 "Databases: " + (", ".join(f"{d.name} ({d.confidence})" for d in r.databases) or "none"),
                 "", f"Findings: {len(r.findings)}", ""]
        if r.warnings:
            lines += ["Warnings:"] + [f"  - {w}" for w in r.warnings] + [""]
        lines += ["Limitations:"] + [f"  - {x}" for x in r.limitations]
        self._set_text(self.overview, "\n".join(lines))
        if r.structure is not None:
            self._fill_structure(r)
        self._fill_phase3_5_6(r)

    def _fill_phase3_5_6(self, r: AnalysisResult) -> None:
        self.reader = ZipSourceReader(self.zip_path) if self.zip_path else None
        for t in (self.tools, self.dbt, self.dbc, self.cmap):
            t.delete(*t.get_children())
        for s in (r.code_map or {}).get("symbols", [])[:5000]:
            self.cmap.insert("", "end", values=(s["kind"], s["qualname"], f"{s['file']}:{s['line']}", s["language"], s["method"],
                                                ", ".join(s["params"])[:60], s["complexity"] if s["complexity"] is not None else ""))
        sa = r.static_analysis or {}
        for run in sa.get("tool_runs", []):
            self.tools.insert("", "end", values=(run.get("analyzer"), run.get("status"), run.get("version"), run.get("exit_code", ""),
                                                 " ".join(map(str, run.get("command", [])))[:160], run.get("note", "")))
        db = r.database_analysis or {}
        for d in db.get("databases", []):
            self.dbt.insert("", "end", values=(d.get("display"), d.get("status"), d.get("rationale")))
        for c in db.get("connections", []):
            self.dbc.insert("", "end", values=(c.get("display"), c.get("level"), c.get("kind"),
                                               f"{c.get('file')}:{c.get('line')}", c.get("scope"), c.get("redacted")))
        lines = [f"ORM entities: {len(db.get('orm_entities', []))}   Migrations: {len(db.get('migrations', []))}   "
                 f"Schema objects: {len(db.get('schema_objects', []))}   SQL scripts: {len(db.get('sql_scripts', []))}", ""]
        lines += [f"  entity {e['orm']}: {e['name']} ({e['file']}:{e['line']})" for e in db.get("orm_entities", [])[:40]]
        lines += [f"  migration {m['tool']}: {m['file']}" for m in db.get("migrations", [])[:40]]
        lines += [f"  {s['kind']} {s['name']} ({s['file']}:{s['line']})" for s in db.get("schema_objects", [])[:60]]
        lines += ["", "Limitations:"] + [f"  - {x}" for x in db.get("limitations", [])]
        ex = r.execution or {}
        if ex:
            iso = ex.get("isolation", {})
            lines += ["", "Execution (Phase 4): " + ("container runtime available" if iso.get("available") else
                                                       "UNAVAILABLE - " + str(iso.get("reason", ""))[:200]),
                      f"  {len(ex.get('candidates', []))} candidate command(s) discovered; nothing was run."]
        self._set_text(self.dbx, "\n".join(lines))
        if self.findings_plugin is not None:
            self.findings_plugin.store.clear()
            self.findings_plugin.add(r.findings, source="analysis")

    def _fill_structure(self, r: AnalysisResult) -> None:
        st = r.structure
        for t in (self.struct, self.entry, self.imp, self.cfg, self.conf, self.swarn):
            t.delete(*t.get_children())
        stack = [("", st.tree)]
        while stack:                       # iterative: no recursion limits
            parent, node = stack.pop()
            note = "vendored - not expanded" if node.vendored else ("truncated" if node.truncated else "")
            iid = self.struct.insert(parent, "end", text=node.name, open=(parent == ""),
                                     values=(node.file_count if node.is_dir else "", node.size, note))
            stack.extend((iid, c) for c in reversed(node.children))
        for e in st.entry_points:
            self.entry.insert("", "end", values=(e.file, e.line or "", e.kind, e.language, e.confidence, e.evidence))
        for p in st.import_summary.external_packages:
            decl = {True: "yes", False: "NO", None: "unknown"}[p["declared"]]
            self.imp.insert("", "end", values=(p["name"], p["language"], p["count"], decl,
                                               f"{p['first_file']}:{p['first_line']}"))
        for c in st.config_files:
            self.cfg.insert("", "end", values=(c.path, "config file", c.kind, c.path))
        for v in st.env_vars:
            ref = v.references[0] if v.references else None
            self.cfg.insert("", "end", values=(v.name, "env var", v.status, f"{ref.file}:{ref.line}" if ref else ""))
        for d in st.db_connections:
            self.cfg.insert("", "end", values=(f"{d.database} [{d.key}]", "db connection", f"{d.form} / {d.confidence}",
                                               f"{d.file}:{d.line}"))
        for c in st.confidence:
            self.conf.insert("", "end", values=(c.subject, c.kind, c.evidence_class, "; ".join(c.reasons)))
        for w in st.warnings:
            self.swarn.insert("", "end", values=(w.severity, w.code, w.message, f"{w.file}:{w.line or ''}" if w.file else ""))

    def _show_finding(self, _evt=None) -> None:
        sel = self.fnd.selection()
        f = self._findings.get(sel[0]) if sel else None
        if not f:
            return
        text = (f"{f.id}  [{f.severity.upper()}]  {f.title}\n\nFile: {f.file}:{f.line or '-'}\n"
                f"Rule: {f.rule_id}   Source: {f.source}\nConfidence: {f.confidence}   Verification: {f.verification}\n\n"
                f"Evidence:\n  {f.evidence}\n\nExplanation:\n  {f.explanation}\n\nSuggested remediation:\n  {f.remediation}\n")
        self._set_text(self.detail, text)
        self._show_page(self.detail)
        self._selected_finding = f
        self.fix_btn.config(state="normal" if can_suggest(f) else "disabled")

    # ---------------------------------------------------------------- Phase 8: propose a fix (human approved)
    def propose_fix(self) -> None:
        finding = self._selected_finding
        if finding is None or not can_suggest(finding):
            return
        self._pending_finding = finding
        self._pending_ai_patch = None
        if self.workspace is not None and self.workspace_zip == self.zip_path:
            self._open_patch_dialog()
            return
        self._start_workspace_creation()

    def _start_workspace_creation(self) -> None:
        dest = filedialog.askdirectory(title="Choose an EMPTY folder for the editable workspace copy")
        if not dest:
            return
        if not messagebox.askyesno("Create workspace copy",
                                   "Safe files from the ZIP will be copied into:\n" + dest +
                                   "\n\nThe original ZIP is never modified. Continue?"):
            return
        self.status_var.set("Creating workspace copy...")
        zip_path = self.zip_path

        def work() -> None:
            try:
                ws, _info = Workspace.create_from_zip(zip_path, dest)
                self.q.put(("workspace", ws))
            except PatchError as exc:
                self.q.put(("workspace_error", str(exc)))
            except Exception:  # noqa: BLE001
                log.exception("Workspace creation failed")
                self.q.put(("workspace_error", "Unexpected error; see logs/app.log."))

        threading.Thread(target=work, daemon=True).start()

    def _approve_from_worker(self, plan) -> bool:
        """Called on a worker thread: ask the UI thread to show the approval dialog and wait for the answer."""
        evt, holder = threading.Event(), {"ok": False}
        self.q.put(("approve", plan, evt, holder))
        evt.wait(timeout=900)
        return bool(holder["ok"])

    def _ask_plan_approval(self, plan) -> bool:
        """UI thread: show the fail-closed approval dialog for a Phase 4 plan."""
        lim = {"wall_seconds": plan.limits.timeout_s, "memory_mb": plan.limits.memory_mb,
               "processes": plan.limits.pids, "network": plan.limits.network}
        return request_execution_approval(self.root, plan.candidate.display, plan.source, detect_isolation(), lim,
                                          plan.candidate.source_file or plan.candidate.origin, plan=plan)

    def _make_runner(self):
        """Phase 4 sandbox runner for patch verification, or None when no container runtime exists."""
        from plugins.build_execution import Options
        opts = Options(auto_start=bool(self.exec_tab.auto_start.get()))
        info = detect_isolation(opts)
        if not info.available and not (opts.auto_start and info.executable):
            return None                    # not installed (or auto-start is off): verification is skipped, never run on the host
        return IsolatedSandboxRunner(approver=self._approve_from_worker, options=opts)

    def _patch_closed(self, outcome) -> None:
        if outcome is not None and self._last_result is not None:
            d = outcome.to_dict()
            d["patch"].pop("original_text", None)
            d["patch"].pop("new_text", None)
            self._last_result.patches.append(d)

    def _ai_patch(self, file: str, new_text: str, finding_id: str, rationale: str) -> None:
        """Phase 7 -> Phase 8: an AI suggestion goes through the same diff/approve/backup/verify workflow."""
        finding = next((f for f in self._findings.values() if f.id == finding_id), None)
        self._pending_ai_patch = (file, new_text, finding, rationale)
        self._pending_finding = finding
        if self.workspace is not None and self.workspace_zip == self.zip_path:
            self._open_patch_dialog()
            return
        self._start_workspace_creation()

    def _open_patch_dialog(self) -> None:
        if self.workspace is None:
            return
        finding = self._pending_finding
        try:
            if self._pending_ai_patch is not None:
                file, new_text, finding, rationale = self._pending_ai_patch
                self._pending_ai_patch = None
                patch = propose(file, new_text, self.workspace, finding, rationale, "ai-unverified",
                                "AI-generated and unverified: read the whole diff before approving.")
            elif finding is not None:
                patch = propose_from_finding(finding, self.workspace)
            else:
                return
        except (NoFixAvailable, PatchError) as exc:
            messagebox.showinfo("No fix to review", str(exc))
            return
        PatchDialog(self.root, patch, finding, self.workspace, runner=self._make_runner(), on_close=self._patch_closed)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    # ---------------------------------------------------------------- Phase 9 helpers
    def _show_phase9(self, result: AnalysisResult, paths: dict) -> None:
        self.dashboard.show(result)
        self.findings_view.show(result.findings)
        self.report_view.show(result, has_html=bool(paths.get("html")), has_json=bool(paths.get("json")))
        if self.settings is not None and self.settings.show_dashboard_after_analysis:
            self._show_page(self.dashboard)

    def _on_view_selection(self, f) -> None:
        self._selected_finding = f
        self.fix_btn.config(state="normal" if (f is not None and can_suggest(f)) else "disabled")

    def _open_history(self, analysis_id: int) -> None:
        if self.db is None:
            return
        try:
            result = self.db.load_result(analysis_id)
            row = self.db.get_row(analysis_id)
        except HistoryError as exc:
            messagebox.showinfo("History", str(exc))
            return
        self.report_paths = {}
        if row:
            for kind, p in (("html", row.html_path), ("json", row.json_path)):
                if p and Path(p).is_file():
                    self.report_paths[kind] = Path(p)
        self._last_result = result
        self.html_btn.config(state="normal" if "html" in self.report_paths else "disabled")
        self.json_btn.config(state="normal" if "json" in self.report_paths else "disabled")
        self._fill(result)
        self._show_phase9(result, self.report_paths)
        self.report_view.show(result, "html" in self.report_paths, "json" in self.report_paths)
        self.status_var.set(f"Loaded saved analysis #{analysis_id} ({result.archive_name}). The ZIP itself is not loaded; "
                            "select it again to propose fixes.")

    def _settings_saved(self, new: Settings) -> None:
        self.settings = new
        if self.base_dir is not None:
            self.out_dir = resolve_output_dir(new, self.base_dir)
        if new.history_enabled:
            if self.db is None and self.base_dir is not None:
                try:
                    self.db = DbManager(self.base_dir / "data" / "history.db", new.max_history)
                except HistoryError as exc:
                    messagebox.showerror("History", str(exc))
            elif self.db is not None:
                self.db.max_history = new.max_history
        else:
            self.db = None
        self.history_view.set_db(self.db)
