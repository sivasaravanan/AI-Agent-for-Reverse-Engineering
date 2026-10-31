"""App Preview panel: run, debug, trace or test the selected application in a container and watch it like a VM viewer.

Left   : project files + source code (current line, breakpoints, errors, execution heat map)
Right  : Screen (virtual display of GUI apps) | Web (live page of web apps) | Console | Trace | Debug | Insights
Bottom : the code block (function / class) that is executing or selected, and the output that block produced

The panel never starts anything by itself: Start shows the exact plan in the approval dialog first. It only listens to the
events of its own session (``plugins.build_execution.events.BUS`` and ``app_preview.events.PV``) through a queue.
"""
from __future__ import annotations

import base64
import html as _html
import queue
import re
import shlex
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from app_preview import (BACKENDS, build_gui_image, gui_image_ready, make_build_plan, Entry, OutputIndex, PreviewOptions, PreviewSession, approve, block_text, blocks_for, custom_entry,
                         discover_entries, innermost, make_plan)
from app_preview.events import PV
from app_preview.runtimes import image_present, status as runtime_status
from app_preview.tracemodel import TraceModel
from plugins.build_execution import cleanup
from plugins.build_execution.discovery import SourceView
from plugins.build_execution.run_targets import ProjectView
from plugins.build_execution.events import BUS
from ui import theme
from ui.compact import Compactor, FlowFrame
from ui.code_view import CodeView
from ui.preview_card import PreviewCard
from ui.lean_copy import LeanCopyMixin
from ui.quick_run import QuickRunMixin
from ui.vm_integration import VMManagerMixin
from ui.run_panels import ResourcePanel
from ui.screen_tools import (FPS_CHOICES, SIZE_PRESETS, SNAPSHOTS, VIEW_MODES, FrameView, ScreenWindow, SnapshotButton, ask_save_snapshot, fps_text,
                             parse_fps, parse_size, size_text, view_label)

CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".cs", ".go", ".c", ".cc", ".cpp", ".h", ".hpp", ".kt", ".php",
            ".rs", ".swift", ".html", ".css", ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".sh", ".bat",
            ".xml", ".csproj", ".gradle", ".env", ".rb"}
MAX_TREE_FILES = 6000
MAX_CONSOLE_LINES = 20000
KEYSYM = {"Return": "Return", "Tab": "Tab", "Escape": "Escape", "BackSpace": "BackSpace", "Delete": "Delete", "Up": "Up", "Down": "Down",
          "Left": "Left", "Right": "Right", "Home": "Home", "End": "End", "Prior": "Page_Up", "Next": "Page_Down"}


def page_text(body: str) -> str:
    """A readable text snapshot of an HTML page (this panel has no browser engine; use 'Open in browser' for the real page)."""
    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", "", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|section|article|header|footer)>", "\n", body)
    body = _html.unescape(re.sub(r"(?s)<[^>]+>", "", body))
    return re.sub(r"\n\s*\n+", "\n\n", body).strip()


class AppPreviewView(LeanCopyMixin, QuickRunMixin, VMManagerMixin, ttk.Frame):
    def __init__(self, parent, get_source: Optional[Callable[[], str]] = None, on_result: Optional[Callable] = None,
                 approver: Optional[Callable] = None, options: Optional[PreviewOptions] = None,
                 on_source_changed: Optional[Callable[[str], None]] = None, prefs: Optional[Dict] = None,
                 on_pref: Optional[Callable[[str, object], None]] = None, on_forget: Optional[Callable[[], None]] = None, **kw):
        super().__init__(parent, **kw)
        self.get_source = get_source or (lambda: "")
        self.on_source_changed = on_source_changed      # host callback: a project picked here becomes the project everywhere
        self.on_result = on_result
        self.approver = approver or self._ask_approval
        self.options = options or PreviewOptions()
        self._prefs = dict(prefs or {})                 # remembered screen settings (host supplies them; optional)
        self.on_pref = on_pref
        self.on_forget = on_forget                      # host hook: forget the selected project (Clear project & copies)
        self._history: List[Dict] = []                  # console text of earlier runs (Live Run's run picker, now here)
        self._last_status = ""
        self._viewing = False
        self._live_text = ""
        _sz = parse_size(str(self._prefs.get("screen", "")))
        if _sz:
            self.options.screen = size_text(*_sz)
        if "fps" in self._prefs:
            self.options.fps = parse_fps(str(self._prefs["fps"]), self.options.fps)
        if self._prefs.get("snap_dir"):
            SNAPSHOTS.set_custom(str(self._prefs["snap_dir"]))
        self.q: "queue.Queue" = queue.Queue()
        self.session: Optional[PreviewSession] = None
        self.run_id = ""
        self.source = ""
        self.entries: List[Entry] = []
        self.entry_by_title: Dict[str, Entry] = {}
        self._sv: Optional[SourceView] = None
        self._texts: Dict[str, Optional[str]] = {}
        self.trace = TraceModel()
        self.outputs = OutputIndex()
        self.breaks: Dict[str, set] = {}
        self.errors: Dict[str, set] = {}
        self.stack: List[Dict] = []
        self.sel: tuple = ("", 0)                      # (file, line) chosen for the block panes
        self.state = "idle"
        self.mode = "run"
        self._frame_img = None
        self._frame_png = b""
        self._auto_continue = False
        self._t_start = 0.0
        self._last_follow = 0.0
        self._block_dirty = False
        self._vars_frame = 0
        self._c: Dict[str, str] = dict(theme.LIGHT)
        self.src_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="run")
        self.entry_var = tk.StringVar()
        self.backend_var = tk.StringVar(value=self.options.backend)
        self.custom_var = tk.StringVar(value=self.options.custom_exe)
        self.image_var = tk.StringVar()
        self.install_var = tk.BooleanVar(value=False)
        self.link_var = tk.BooleanVar(value=True)
        self.stop_entry_var = tk.BooleanVar(value=True)
        self.follow_var = tk.BooleanVar(value=True)
        self.send_input_var = tk.BooleanVar(value=False)
        self.timeout_var = tk.IntVar(value=self.options.limits.timeout_s)
        self.size_var = tk.IntVar(value=self.options.limits.max_workspace_mb)
        self.res_var = tk.StringVar(value=self.options.screen)                 # virtual screen size (applies at the next start)
        self.rate_var = tk.StringVar(value=fps_text(self.options.fps))         # snapshot rate (applies at once)
        self.view_var = tk.StringVar(value=view_label(str(self._prefs.get("view", "fit"))))
        self.auto_snap_var = tk.BooleanVar(value=bool(self._prefs.get("auto_snap", False)))
        self.auto_clean_var = tk.BooleanVar(value=bool(self._prefs.get("auto_clean", True)))   # delete leftover copies before a run and at close
        self.auto_go_var = tk.BooleanVar(value=bool(self._prefs.get("auto_go", True)))         # choosing a command starts it (approval still asked)
        self._cpu: List[float] = []
        self._mem: List[float] = []
        self._popout: Optional[ScreenWindow] = None
        self._frame_job = None
        self._run_title = ""
        self.runtime_var = tk.StringVar(value="Container runtime: not checked")
        self.status_var = tk.StringVar(value="Choose a ZIP or folder, pick what to start and press Start.")
        self.stats_var = tk.StringVar()
        self.web_var = tk.StringVar(value="No web app running.")
        self.block_var = tk.StringVar(value="Click a line in the code to see its code block and the output it produced.")
        self._build()
        self._unsub = (BUS.subscribe(self.q.put), PV.subscribe(self.q.put))
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Map>", lambda _e: self._on_map(), add="+")
        self.after(60, self._poll)
        self.after(300, self._tick)
        self._vm_init()

    # ================================================================== layout
    def _build(self) -> None:
        self._prefs.setdefault("compact", True)
        self.cpt = Compactor(self, compact=bool(self._prefs.get("compact", True)))
        R = self.cpt.reg
        # ---- row 1: project
        top = FlowFrame(self, padding=(3, 1))
        top.pack(fill="x")

        def lbl(parent, icon, label, tip):
            w = ttk.Label(parent, text=label)
            R(w, icon, label, tip)
            return w

        def tb(parent, icon, label, tip, cmd, **kw):
            b = ttk.Button(parent, text=label, style="Tool.TButton", command=cmd, **kw)
            R(b, icon, label, tip)
            return b
        top.add(lbl(top, "📂", "Project:", "The ZIP archive, folder or single file to run. Your own files are never changed."), padx=(0, 2))
        top.add(ttk.Entry(top, textvariable=self.src_var), stretch=True, padx=(0, 3))
        top.add(tb(top, "📦", "ZIP...", "Choose a ZIP archive as the project (it is read in place, never unpacked as a whole).", lambda: self._pick(False)))
        top.add(tb(top, "📁", "Folder...", "Choose a project folder.", lambda: self._pick(True)))
        top.add(tb(top, "📄", "File...", "Pick ONE program file: only it and the files it imports are listed, copied and run.", self.pick_file))
        self.whole_btn = tb(top, "🗂", "Whole folder", "List every file of the folder / archive again (a run still copies only what the program needs).", self.show_whole_folder)
        top.add(self.whole_btn)
        self.include_btn = tb(top, "➕", "Include...", "Only what the program needs is copied into the container. Add other project files or folders here.", self.open_includes)
        top.add(self.include_btn)
        top.add(tb(top, "🔗", "Use analysed project", "Use the project that is open in the analysis view.", self.use_main_source))
        top.add(tb(top, "🔄", "Rescan", "Read the project again.", self.rescan))
        # ---- row 2: what to run, which runtime
        row = FlowFrame(self, padding=(3, 0))
        row.pack(fill="x")
        for m, label in (("run", "Run"), ("debug", "Debug"), ("trace", "Trace"), ("test", "Test")):
            row.add(ttk.Radiobutton(row, text=label, value=m, variable=self.mode_var, command=self._mode_changed), padx=(0, 4))
        row.add(lbl(row, "▶", "Start:", "What to start: a program found in the project, or any command you type (it runs inside the container)."), padx=(4, 2))
        self.entry_box = ttk.Combobox(row, textvariable=self.entry_var, width=30)
        row.add(self.entry_box, stretch=True, padx=(0, 4))
        self.entry_box.bind("<<ComboboxSelected>>", lambda _e: self._entry_picked())
        row.add(lbl(row, "🐳", "Runtime:", "Container runtime to use (auto picks Docker or Podman)."), padx=(2, 2))
        self.backend_box = ttk.Combobox(row, textvariable=self.backend_var, values=list(BACKENDS), state="readonly", width=7)
        row.add(self.backend_box, padx=(0, 2))
        self.backend_box.bind("<<ComboboxSelected>>", lambda _e: self.check_runtime())
        ce = ttk.Entry(row, textvariable=self.custom_var, width=12)
        row.add(ce, padx=(0, 2))
        theme.Tooltip(ce, "Runtime = custom: path of any docker-compatible program (nerdctl, finch, lima ...)")
        row.add(tb(row, "🔍", "Re-check", "Check the container runtime again.", self.check_runtime))
        # ---- row 3: run controls, limits, tools, options (wraps onto more rows; nothing is cut off)
        opt = FlowFrame(self, padding=(3, 0))
        opt.pack(fill="x")
        self.chip = tk.Label(opt, text="● Idle", padx=6, pady=1, font=("TkDefaultFont", 9, "bold"))
        opt.add(self.chip, padx=(0, 3))
        self.quick_btn = tb(opt, "⚡", "Run now", "One click: pick the program, prepare what it needs, run it and show it here.", self.run_now)
        opt.add(self.quick_btn)
        self.start_btn = tb(opt, "▶", "Start", "Start the chosen command (you approve the exact plan first).", self.start)
        opt.add(self.start_btn)
        self.stop_btn = tb(opt, "■", "Stop", "Stop the run: the container is killed.", self.stop, state="disabled")
        opt.add(self.stop_btn)
        self.restart_btn = tb(opt, "⟳", "Restart", "Stop and start again.", self.restart, state="disabled")
        opt.add(self.restart_btn, padx=(0, 6))
        self.dbg = {}
        for key, icon, label, tip in (("continue", "⏵", "Continue", "Continue to the next breakpoint (F5)"),
                                      ("next", "⤼", "Over", "Run the current line, stepping over calls (F10)"),
                                      ("step", "⤵", "Into", "Step into the next call (F11)"),
                                      ("return", "⤴", "Out", "Run until the current function returns (Shift+F11)")):
            b = tb(opt, icon, label, tip, lambda c=key: self.debug_cmd(c), state="disabled")
            opt.add(b, padx=(0, 1))
            self.dbg[key] = b
        opt.add(lbl(opt, "🖼", "Image:", "Container image to run in (empty = chosen automatically)."), padx=(6, 2))
        opt.add(ttk.Entry(opt, textvariable=self.image_var, width=16))
        opt.add(lbl(opt, "⏱", "Timeout s:", "Time limit of a run in seconds."), padx=(6, 2))
        opt.add(ttk.Spinbox(opt, from_=10, to=86400, textvariable=self.timeout_var, width=6))
        opt.add(lbl(opt, "📏", "Max project MB:", "Largest project that is copied whole. A bigger one is narrowed to what the program needs."), padx=(6, 2))
        opt.add(ttk.Spinbox(opt, from_=10, to=4096, textvariable=self.size_var, width=5), padx=(0, 6))
        opt.add(tb(opt, "🗄", "Storage & clean-up...", "Where copies live, how big they may get, and clean-up.", self.open_storage))
        opt.add(tb(opt, "🛠", "Set up Docker...", "Check / install the container runtime (Docker, Podman ...). Nothing is installed without asking.", self.open_setup))
        opt.add(tb(opt, "🧹", "Clear project & copies", "Delete every leftover copy and stopped container made by this tool and forget the selected project. Your own files are never touched.", self.clear_project))
        vb = self.vm_build_menubutton(opt)                      # VirtualBox-style windows: Manager, machine window, snapshots, settings
        R(vb, "🖥", "Virtual machines ▾", "Manager, machine window with menus, snapshot tree, settings.")
        opt.add(vb)
        self.compact_btn = ttk.Button(opt, text="Aa", style="Tool.TButton", command=self._toggle_compact)
        theme.Tooltip(self.compact_btn, "Toolbar labels: icons only (compact) or icons with text. Nothing is hidden in either mode.")
        opt.add(self.compact_btn, padx=(0, 6))

        def cbtn(icon, label, tip, var, cmd=None):
            c = ttk.Checkbutton(opt, text=label, variable=var, **({"command": cmd} if cmd else {}))
            R(c, icon, label, tip)
            opt.add(c, padx=(0, 4))
            return c
        cbtn("📥", "Install requirements / npm packages first (needs network)", "Install requirements / npm packages before the run (needs network).", self.install_var)
        cbtn("🔗", "Link output to code (Python)", "Show which code line produced which output (Python).", self.link_var)
        cbtn("⏸", "Debug: stop at first line", "Debug mode stops at the first line.", self.stop_entry_var)
        cbtn("👁", "Follow execution", "Scroll the code along with the running line.", self.follow_var)
        cbtn("🧹", "Clear leftovers before / after", "Before a run, and when the application closes, every leftover copy of a project and every stopped container made by this tool is deleted. Your own files are never touched.",
             self.auto_clean_var, lambda: self._save_pref("auto_clean", bool(self.auto_clean_var.get())))
        cbtn("⚡", "Start when a command is chosen", "Picking a command in the Start box starts it (you still approve the exact plan first). Uses requirements.txt when the project has one.",
             self.auto_go_var, lambda: self._save_pref("auto_go", bool(self.auto_go_var.get())))
        opt.add(ttk.Label(opt, textvariable=self.stats_var, style="Muted.TLabel"))
        info_row = ttk.Frame(self, padding=(5, 0))
        info_row.pack(fill="x")
        rt = ttk.Label(info_row, textvariable=self.runtime_var, style="Muted.TLabel", justify="left")
        rt.pack(side="left", anchor="nw")
        st = ttk.Label(info_row, textvariable=self.status_var, justify="left")
        st.pack(side="left", padx=(10, 0), anchor="nw")

        def _wrap(_e=None) -> None:                           # long messages wrap instead of being cut off at the pane edge
            try:
                w = max(200, info_row.winfo_width() - 14)
                rt.configure(wraplength=w // 2 - 6)
                st.configure(wraplength=w // 2 - 6)
            except tk.TclError:
                pass
        info_row.bind("<Configure>", _wrap, add="+")

        outer = ttk.PanedWindow(self, orient="vertical")
        outer.pack(fill="both", expand=True, padx=3, pady=(1, 3))
        upper = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(upper, weight=4)
        # ---- left: files + code
        left = ttk.Frame(upper)
        upper.add(left, weight=3)
        lp = ttk.PanedWindow(left, orient="horizontal")
        lp.pack(fill="both", expand=True)
        ff = ttk.Frame(lp)
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(ff, textvariable=self.filter_var)
        fe.pack(fill="x", padx=2, pady=2)
        fe.bind("<KeyRelease>", lambda _e: self._fill_tree())
        self.tree = ttk.Treeview(ff, show="tree", selectmode="browse")
        ts = ttk.Scrollbar(ff, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        ts.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree)
        lp.add(ff, weight=1)
        cf = ttk.Frame(lp)
        self.file_var = tk.StringVar(value="No file")
        ttk.Label(cf, textvariable=self.file_var, style="Muted.TLabel").pack(fill="x")
        self.code = CodeView(cf, on_toggle_break=self._toggle_break, on_line_click=self._on_line_click)
        self.code.pack(fill="both", expand=True)
        lp.add(cf, weight=4)
        # ---- right: viewer tabs
        self.right = ttk.Notebook(upper)
        upper.add(self.right, weight=3)
        side = ttk.Frame(upper)
        upper.add(side, weight=1)
        self.card = PreviewCard(side, on_click=self._card_clicked)
        self.card.pack(fill="x", padx=2, pady=2)
        info = ttk.LabelFrame(side, text="Details")
        info.pack(fill="x", padx=2, pady=2)
        self.info = {k: tk.StringVar(value="-") for k in ("State", "Mode", "Start", "Image", "Network", "Ports", "Screen", "Uptime", "Resources")}
        for i, (k, v) in enumerate(self.info.items()):
            ttk.Label(info, text=k + ":", style="Muted.TLabel").grid(row=i, column=0, sticky="nw", padx=(4, 3), pady=0)
            ttk.Label(info, textvariable=v, wraplength=170, justify="left").grid(row=i, column=1, sticky="nw", pady=0)
        info.columnconfigure(1, weight=1)
        self._build_screen()
        self._build_web()
        self._build_console()
        self._build_trace()
        self._build_debug()
        self._build_insights()
        self._build_resources()
        # ---- bottom: code block + its output
        low = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(low, weight=2)
        bl = ttk.LabelFrame(low, text="Code block")
        ttk.Label(bl, textvariable=self.block_var, style="Muted.TLabel").pack(fill="x", padx=4)
        self.block_text = self._text(bl)
        low.add(bl, weight=1)
        br = ttk.LabelFrame(low, text="Output of this block")
        self.block_out = self._text(br)
        low.add(br, weight=1)
        for w in (self.block_text, self.block_out):
            w.tag_configure("ln", foreground="#8a94ad")
            w.tag_configure("err", foreground="#d64545")
            w.tag_configure("cur", background="#fff3b0", foreground="#1b2333")
        for key, name in (("<F5>", "continue"), ("<F10>", "next"), ("<F11>", "step"), ("<Shift-F11>", "return")):
            self.bind_all(key, lambda e, n=name: self._hotkey(n), add="+")
        self.recolor(self._c)
        self._set_buttons()

    def _toggle_compact(self) -> None:
        on = not self.cpt.compact
        self.cpt.set_compact(on)
        self._save_pref("compact", on)
        self.status_var.set("Toolbar: icons only (hover for the name)." if on else "Toolbar: icons with text.")

    def _text(self, parent, height: int = 6) -> tk.Text:
        f = ttk.Frame(parent)
        f.pack(fill="both", expand=True, padx=2, pady=2)
        t = tk.Text(f, wrap="none", state="disabled", font="TkFixedFont", height=height)
        sy = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        sx = ttk.Scrollbar(f, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        t.pack(side="left", fill="both", expand=True)
        return t

    def _build_screen(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Screen")
        bar = FlowFrame(f, padding=(3, 1))
        bar.pack(fill="x")
        R = self.cpt.reg

        def lb(icon, label, tip):
            w = ttk.Label(bar, text=label)
            R(w, icon, label, tip)
            bar.add(w, padx=(0, 1))
        lb("↔", "Size:", "Virtual screen size.")
        self.res_box = ttk.Combobox(bar, textvariable=self.res_var, values=list(SIZE_PRESETS), width=10)
        bar.add(self.res_box, padx=(0, 6))
        theme.Tooltip(self.res_box, "Virtual screen size (WIDTHxHEIGHT, 320x240 up to 3840x2160). Type your own or pick one. "
                                    "Takes effect the next time the app starts (use Restart).")
        for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.res_box.bind(ev, lambda _e: self._size_changed())
        lb("🎞", "Rate:", "Snapshots of the screen per second.")
        self.rate_box = ttk.Combobox(bar, textvariable=self.rate_var, values=list(FPS_CHOICES), state="readonly", width=7)
        bar.add(self.rate_box, padx=(0, 6))
        theme.Tooltip(self.rate_box, "How often a snapshot of the virtual screen is taken. Lower = lighter on the container, higher = smoother. Applies at once.")
        self.rate_box.bind("<<ComboboxSelected>>", lambda _e: self._rate_changed())
        lb("🔍", "View:", "How the picture is scaled in this pane.")
        self.view_box = ttk.Combobox(bar, textvariable=self.view_var, values=list(VIEW_MODES), state="readonly", width=6)
        bar.add(self.view_box, padx=(0, 6))
        theme.Tooltip(self.view_box, "Fit = the largest exact size that fits this pane. 50% / 100% / 200% keep the app's pixels (the pane scrolls when it is larger).")
        self.view_box.bind("<<ComboboxSelected>>", lambda _e: self._view_changed())
        sc = ttk.Checkbutton(bar, text="Send mouse and keyboard to the app", variable=self.send_input_var)
        R(sc, "🖱", "Send mouse and keyboard to the app", "Clicks and keys on the screen below are sent to the app inside the container.")
        bar.add(sc, padx=(0, 6))
        self.pop_btn = ttk.Button(bar, text="⧉ Pop out", style="Tool.TButton", command=self.pop_out)
        R(self.pop_btn, "⧉", "Pop out", "Show the screen in its own window (drag it to another monitor, F11 = full screen). A web app opens in your browser.")
        bar.add(self.pop_btn)
        self.snap_btn = SnapshotButton(bar, self._save_frame, self.quick_snapshot, self.choose_snapshot_folder, self.auto_snap_var)
        R(self.snap_btn, "📷", "Snapshot ▾", "Save the current screen picture as a PNG: choose a file, or one click into the snapshot folder.")
        bar.add(self.snap_btn)
        self.screen_note = tk.StringVar(value="GUI apps are drawn on a virtual screen inside the container and shown here.")
        ttk.Label(f, textvariable=self.screen_note, style="Muted.TLabel", padding=(4, 0)).pack(fill="x")
        self.img_bar = ttk.Frame(f, padding=(6, 2))                  # shown only while the GUI image is missing
        self.img_msg = tk.StringVar()
        ttk.Label(self.img_bar, textvariable=self.img_msg, wraplength=520, foreground="#b45309").pack(side="left", fill="x", expand=True)
        self.img_btn = ttk.Button(self.img_bar, text="Prepare GUI preview image...", command=self.prepare_gui_image)
        self.img_btn.pack(side="right", padx=4)
        self.frame_view = FrameView(f, mode=VIEW_MODES.get(self.view_var.get(), "fit"))
        self.frame_view.pack(fill="both", expand=True, padx=2, pady=2)
        self.screen = self.frame_view.canvas                         # dark "monitor" backdrop in both themes
        self.screen.bind("<Button-1>", self._screen_click)
        self.screen.bind("<Button-3>", lambda e: self._screen_click(e, 3))
        self.screen.bind("<Key>", self._screen_key)

    def _build_web(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Web")
        bar = ttk.Frame(f, padding=(4, 2))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.web_var).pack(side="left", fill="x", expand=True)
        self.open_btn = ttk.Button(bar, text="Open in browser", style="Tool.TButton", command=self._open_web, state="disabled")
        self.open_btn.pack(side="right")
        self.web_text = self._text(f)
        self.web_text.tag_configure("h", font=("TkDefaultFont", 10, "bold"))
        self.web_text.tag_configure("muted", foreground="#8a94ad")
        self._web_url = ""

    def _build_console(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Console")
        bar = ttk.Frame(f, padding=(4, 2))
        bar.pack(fill="x")
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Autoscroll", variable=self.autoscroll_var).pack(side="left")
        ttk.Button(bar, text="Save...", style="Tool.TButton", command=self._save_console).pack(side="right")
        ttk.Button(bar, text="Copy", style="Tool.TButton", command=self._copy_console).pack(side="right", padx=3)
        ttk.Button(bar, text="Clear", style="Tool.TButton", command=lambda: self._set_text(self.console, "")).pack(side="right")
        self.wrap_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Wrap", variable=self.wrap_var, command=lambda: self.console.configure(wrap="word" if self.wrap_var.get() else "none")).pack(side="left", padx=6)
        self.find_var = tk.StringVar()
        ttk.Label(bar, text="Find:").pack(side="left")
        fe = ttk.Entry(bar, textvariable=self.find_var, width=16)
        fe.pack(side="left", padx=(2, 6))
        fe.bind("<KeyRelease>", lambda _e: self._highlight_console())
        self.hist_var = tk.StringVar(value="Current run")
        self.hist_box = ttk.Combobox(bar, textvariable=self.hist_var, state="readonly", width=30, values=["Current run"])
        self.hist_box.pack(side="left")
        theme.Tooltip(self.hist_box, "Output of earlier runs (shown while no run is in progress).")
        self.hist_box.bind("<<ComboboxSelected>>", lambda _e: self._history_picked())
        self.console = self._text(f)
        self.console.tag_configure("hit", background="#ffe58a", foreground="#1b2333")
        self.console.tag_configure("err", foreground="#d64545")
        self.console.tag_configure("phase", foreground="#8a94ad", font=("TkDefaultFont", 9, "italic"))

    def _build_trace(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Trace")
        self.trace_var = tk.StringVar(value="Trace mode records which functions and lines ran (Python).")
        ttk.Label(f, textvariable=self.trace_var, style="Muted.TLabel", padding=(6, 2)).pack(fill="x")
        pan = ttk.PanedWindow(f, orient="vertical")
        pan.pack(fill="both", expand=True)
        cols = ("file", "fn", "calls", "lines")
        self.funcs = ttk.Treeview(pan, columns=cols, show="headings", selectmode="browse", height=6)
        for c, t, w in (("file", "File", 190), ("fn", "Function", 150), ("calls", "Calls", 60), ("lines", "Lines run", 70)):
            self.funcs.heading(c, text=t)
            self.funcs.column(c, width=w, anchor="w" if c in ("file", "fn") else "e")
        self.funcs.bind("<<TreeviewSelect>>", self._on_func)
        pan.add(self.funcs, weight=2)
        self.timeline = tk.Listbox(pan, activestyle="none", font="TkFixedFont", height=6)
        pan.add(self.timeline, weight=2)
        self.timeline.bind("<<ListboxSelect>>", self._on_timeline)
        self._timeline_rows: List[Dict] = []
        self.trace_tab = f

    def _build_debug(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Debug")
        self.debug_var = tk.StringVar(value="Start in Debug mode; the program stops at breakpoints (click a line number to set one).")
        ttk.Label(f, textvariable=self.debug_var, style="Muted.TLabel", padding=(6, 2), wraplength=420).pack(fill="x")
        pan = ttk.PanedWindow(f, orient="vertical")
        pan.pack(fill="both", expand=True)
        self.stack_list = tk.Listbox(pan, activestyle="none", height=5)
        self.stack_list.bind("<<ListboxSelect>>", self._on_stack)
        pan.add(self.stack_list, weight=1)
        self.vars = ttk.Treeview(pan, columns=("val",), show="tree headings", height=8)
        self.vars.heading("#0", text="Name")
        self.vars.heading("val", text="Value")
        self.vars.column("#0", width=150)
        self.vars.column("val", width=300)
        pan.add(self.vars, weight=3)
        ev = ttk.Frame(f)
        ev.pack(fill="x", padx=4, pady=3)
        ttk.Label(ev, text="Evaluate:").pack(side="left")
        self.eval_var = tk.StringVar()
        e = ttk.Entry(ev, textvariable=self.eval_var)
        e.pack(side="left", fill="x", expand=True, padx=4)
        e.bind("<Return>", lambda _e: self._eval())
        ttk.Button(ev, text="Run", style="Tool.TButton", command=self._eval).pack(side="left")
        self.eval_out = self._text(f, height=4)

    def _build_insights(self) -> None:
        f = ttk.Frame(self.right)
        self.right.add(f, text="Insights")
        self.insight_var = tk.StringVar(value="Located errors and hints appear here when a run ends.")
        ttk.Label(f, textvariable=self.insight_var, padding=(6, 2), wraplength=460).pack(fill="x")
        self.diag = ttk.Treeview(f, columns=("sev", "loc", "msg"), show="headings", selectmode="browse", height=8)
        for c, t, w in (("sev", "Severity", 70), ("loc", "Location", 170), ("msg", "Message", 300)):
            self.diag.heading(c, text=t)
            self.diag.column(c, width=w, anchor="w")
        self.diag.pack(fill="both", expand=True, padx=4)
        self.diag.bind("<<TreeviewSelect>>", self._on_diag)
        self.hint_text = self._text(f, height=5)
        self._diags: List[Dict] = []

    def _build_resources(self) -> None:
        self.res_panel = ResourcePanel(self.right, self._c)            # same panel as Live Run: CPU / memory, processes, timeline
        self.right.add(self.res_panel, text="Resources")

    # ================================================================== colours
    def recolor(self, colors: Optional[Dict[str, str]] = None) -> None:
        c = colors or self._c
        self._c = c
        self._chips = {"idle": (c["btn"], c["muted"]), "running": (c["accent"], c["accent_fg"]), "ok": (c["good"], "#ffffff"),
                       "bad": (c["bad"], "#ffffff"), "warn": (c["warn"], "#1b2333"), "paused": (c["warn"], "#1b2333")}
        self.code.recolor(c)
        self.card.recolor(c)
        if getattr(self, "res_panel", None) is not None:
            self.res_panel.recolor(c)
        for t in (self.console,):
            t.tag_configure("err", foreground=c["bad"])
            t.tag_configure("hit", background=c["sel"], foreground=c["sel_fg"])
        for t in (self.block_text, self.block_out):
            t.tag_configure("err", foreground=c["bad"])
            t.tag_configure("ln", foreground=c["muted"])
            t.tag_configure("cur", background=c["sel"], foreground=c["sel_fg"])
        self.screen.configure(bg="#101418")                     # fixed dark monitor backdrop in both themes
        self._chip_set(self._chip_state)

    _chip_state = "idle"

    def _chip_set(self, state: str, label: str = "") -> None:
        self._chip_state = state
        bg, fg = self._chips.get(state, self._chips["idle"])
        names = {"idle": "● Idle", "running": "● Running", "ok": "✔ Completed", "bad": "✖ Failed", "warn": "■ Stopped", "paused": "⏸ Paused"}
        self.chip.configure(text=label or names[state], bg=bg, fg=fg)

    # ================================================================== source / discovery
    def _on_map(self) -> None:
        cur = (self.get_source() or "").strip()
        if cur and cur != getattr(self, "_main_seen", None):       # the project was (re)selected elsewhere: follow it
            self._main_seen = cur
            if cur != self.source:
                self.set_source(cur)
        if not self.source and self.get_source():
            self.use_main_source()
        self.check_runtime()

    def use_main_source(self) -> None:
        p = self.get_source()
        if p:
            self.set_source(p)

    def set_source(self, path: str) -> None:
        if not path or path == self.source:
            return
        self.source = path
        self.src_var.set(path)
        self._lean_label()
        self._texts.clear()
        if self._sv:
            self._sv.close()
            self._sv = None
        self.entries, self.entry_by_title = [], {}
        self.names = []
        self.entry_box["values"] = []
        self.status_var.set("Scanning the project...")
        self._scanning = True
        self._fill_tree()
        threading.Thread(target=self._scan, args=(path,), daemon=True).start()   # listing a big folder can take a long time: worker thread

    def rescan(self) -> None:
        p, self.source = self.src_var.get().strip(), ""
        if p:
            self.set_source(p)

    def _scan(self, path: str) -> None:
        sv, names, err, focus = None, [], "", None
        try:
            sv = ProjectView(path)                                  # never walks into virtual environments (tens of thousands of files)
            names = sorted(sv.names)
            focus = None
            if self._focus_file and self._focus_file in names:
                focus = {self._focus_file}
                if self._focus_file.endswith(".py"):
                    from plugins.build_execution.sandbox import python_closure
                    focus = python_closure(self._focus_file, set(names), sv.read) | {self._focus_file}
        except Exception as exc:  # noqa: BLE001 - bad archive / unreadable folder
            sv, err = None, f"Could not open the project: {type(exc).__name__}: {str(exc)[:120]}"
        try:
            entries, notes = discover_entries(path)
            self.q.put({"type": "ui_entries", "path": path, "entries": entries, "notes": notes, "sv": sv, "names": names, "err": err, "focus": focus})
        except Exception as exc:  # noqa: BLE001
            self.q.put({"type": "ui_entries", "path": path, "entries": [], "sv": sv, "names": names, "err": err,
                        "notes": [f"Scan failed: {type(exc).__name__}: {str(exc)[:120]}"]})

    def run_script(self, source: str, script: str, mode: str = "run") -> None:
        """Start one script of ``source`` from another view (Build & Test hands window programs over here).
        It starts at once: the project listing is read in the background and is never waited for. Asks for approval as usual."""
        if source and source != self.source:
            self.set_source(source)                      # listing/scan in the background (tree, code view); not needed to start
        elif not self.entries and not self.names:
            self.rescan()
        self._start_script(script, mode)

    def _start_script(self, script: str, mode: str = "run") -> None:
        script = script[2:] if script.startswith("./") else script
        ent = next((e for e in self.entries if e.file == script and e.kind == "gui"), None) or \
            next((e for e in self.entries if e.file == script and (e.traceable or e.kind == "console")), None)
        if ent is None or ent.kind == "console":        # no 'main' guard, or only found as a console entry: it is a window program here
            ent = custom_entry(["python", script], kind="gui", title=f"{script} (window program)")
        self.mode_var.set(mode)
        self._mode_changed()
        title = self._title(ent)
        self.entry_by_title[title] = ent
        self.entry_box["values"] = list(self.entry_box["values"]) + ([title] if title not in self.entry_box["values"] else [])
        self.entry_var.set(title)
        self._keep_entry = title                         # the background scan finishing must not replace this choice
        self.status_var.set(f"Starting {script} ...")
        self.start()

    def _start_pending(self) -> None:                    # kept for callers of the older flow
        pend = getattr(self, "_pending_run", None)
        self._pending_run = None
        if pend:
            self._start_script(*pend)

    def _entries_ready(self, ev: Dict) -> None:
        if ev["path"] != self.source:
            if ev.get("sv"):
                ev["sv"].close()                           # an older scan finished after the project was changed
            return
        if self._sv:
            self._sv.close()
        self._sv, self.names = ev.get("sv"), ev.get("names") or []
        self._focus = ev.get("focus")
        self._fill_tree()
        if ev.get("err"):
            self.status_var.set(ev["err"])
        self._scanning = False
        keep = getattr(self, "_keep_entry", "")
        kept = self.entry_by_title.get(keep) if keep and self.entry_var.get() == keep else None     # still what the user/hand-over chose
        self.entries = ev["entries"]
        self.entry_by_title = {self._title(e): e for e in self.entries}
        self.entry_box["values"] = list(self.entry_by_title)
        self._mode_changed(select=True)
        if kept is not None:                              # a run was started from a hand-over: keep what it runs
            self.entry_by_title[keep] = kept
            self.entry_box["values"] = list(self.entry_box["values"]) + [keep]
            self.entry_var.set(keep)
        elif self._focus_file:
            self._select_focus_entry()
        note = (" " + ev["notes"][0]) if ev["notes"] and not self.entries else ""
        self.status_var.set(f"{len(self.entries)} start option(s) found.{note}")
        if getattr(self, "_pending_run", None):
            self.after(80, self._start_pending)
        if self._start_when_listed:
            self._start_when_listed = False
            self.after(80, self.start)
        self._sync_install()
        if any(e.kind == "gui" for e in self.entries):
            self.check_gui_image()

    def _sync_install(self) -> None:
        """requirements.txt found for the chosen command: install it in the container (the approval shows the network step)."""
        ent = self.entry_by_title.get(self.entry_var.get().strip())
        if ent is not None:
            self.install_var.set(bool(ent.install))

    def _entry_picked(self) -> None:
        self._sync_install()
        if self.auto_go_var.get() and not (self.session and self.session.running) and not self._scanning:
            self.after(60, self.start)

    @staticmethod
    def _title(e: Entry) -> str:
        return f"[{e.kind}] {e.title}"

    def _mode_changed(self, select: bool = False) -> None:
        """Offer the entries that fit the mode (Test -> test commands; Debug/Trace -> Python scripts)."""
        mode = self.mode_var.get()
        fit = [e for e in self.entries if (e.kind == "test") == (mode == "test") and (mode not in ("debug", "trace") or e.traceable)]
        self.entry_box["values"] = [self._title(e) for e in fit]
        if select or self.entry_var.get() not in self.entry_box["values"]:
            self.entry_var.set(self._title(fit[0]) if fit else "")
        self._set_buttons()

    # ================================================================== files / code
    def _read(self, path: str) -> Optional[str]:
        if path not in self._texts:
            self._texts[path] = self._sv.read(path, 1_000_000) if self._sv else None
        return self._texts[path]

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        q = self.filter_var.get().strip().lower()
        nodes: Dict[str, str] = {}
        shown = 0
        for n in getattr(self, "names", []):
            if Path(n).suffix.lower() not in CODE_EXT or (q and q not in n.lower()):
                continue
            if self._focus is not None and n not in self._focus:
                continue
            parts = n.split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                key = "/".join(parts[:i + 1])
                if key not in nodes:
                    nodes[key] = self.tree.insert(parent, "end", iid="d:" + key, text=part, open=bool(q))
                parent = nodes[key]
            self.tree.insert(parent, "end", iid="f:" + n, text=parts[-1])
            shown += 1
            if shown >= MAX_TREE_FILES:
                break

    def _on_tree(self, _e=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0].startswith("f:"):
            self.show_file(sel[0][2:])

    def show_file(self, path: str, line: int = 0) -> None:
        text = self._read(path)
        if text is None:
            self.file_var.set(f"{path} (not readable)")
            return
        if self.code.path != path:
            self.code.set_breaks(self.breaks.get(path, ()))
            self.code.show(path, text, line)
            self.file_var.set(path)
            self.code.set_errors(self.errors.get(path, ()))
            self.code.set_heat(self.trace.hot_lines(path))
            if self.tree.exists("f:" + path):
                self.tree.selection_set("f:" + path)
                self.tree.see("f:" + path)
        elif line:
            self.code.goto(line)

    def _toggle_break(self, line: int) -> None:
        f = self.code.path
        if not f:
            return
        s = self.breaks.setdefault(f, set())
        add = line not in s
        (s.add if add else s.discard)(line)
        self.code.set_breaks(s)
        if self.session and self.session.running and self.mode == "debug":
            self.session.send({"cmd": "set_break" if add else "clear_break", "file": f, "line": line})

    def _on_line_click(self, line: int) -> None:
        self.sel = (self.code.path, line)
        self._update_block(self.code.path, line)

    # ================================================================== code block + output
    def _update_block(self, path: str, line: int) -> None:
        text = self._read(path) if path else None
        self._set_text(self.block_text, "")
        self._set_text(self.block_out, "")
        if not text:
            return
        blocks = blocks_for(path, text)
        b = innermost(blocks, line)
        if b is None:
            lo, hi = max(1, line - 12), min(text.count("\n") + 1, line + 12)
            lines = text.splitlines()[lo - 1:hi]
            first, label = lo, f"{path} - module level, lines {lo}-{hi}"
        else:
            body, first = block_text(text, b)
            lines, label = body.splitlines(), f"{path} - {b.label}, lines {b.start}-{b.end}"
        self.block_var.set(label)
        w = self.block_text
        w.config(state="normal")
        for i, ln in enumerate(lines):
            n = first + i
            w.insert("end", f"{n:>5}  ", "ln")
            w.insert("end", ln + "\n", "cur" if n == line else ())
        w.config(state="disabled")
        recs = [(l, s, t) for f, l, _fn, s, t, _ts in self.outputs.records
                if f == path and ((b is None and innermost(blocks, l) is None) or (b is not None and b.start <= l <= b.end))]
        o = self.block_out
        o.config(state="normal")
        if not recs:
            o.insert("end", "No output was recorded from this block." if self.outputs.records or self.state != "idle" else
                     "Run a Python program in Run (with 'Link output to code'), Trace or Debug mode to see which code printed what.", "ln")
        for l, s, t in recs[-400:]:
            o.insert("end", f"[line {l}] ", "ln")
            o.insert("end", t if t.endswith("\n") else t + "\n", "err" if s == "stderr" else ())
        o.config(state="disabled")
        o.see("end")

    # ================================================================== start / stop
    def check_runtime(self) -> None:
        opts = self._preview_options()
        threading.Thread(target=lambda: self.q.put({"type": "ui_runtime", "st": runtime_status(opts)}), daemon=True).start()

    def open_storage(self) -> None:
        from ui.storage_dialog import open_storage_dialog
        open_storage_dialog(self, self._preview_options(), on_saved=self._storage_saved)

    def _storage_saved(self, cfg: Dict) -> None:
        self.size_var.set(cfg["max_workspace_mb"])                 # the panel's own box follows the saved setting
        self.options.limits.max_workspace_mb = cfg["max_workspace_mb"]
        self.options.limits.tmp_mb = cfg["tmp_mb"]

    def check_gui_image(self) -> None:
        opts = self._preview_options()
        image = self.image_var.get().strip()                      # Tk variables are read on the UI thread only
        threading.Thread(target=lambda: self.q.put({"type": "ui_image", "st": gui_image_ready(opts, image)}), daemon=True).start()

    def _show_image_bar(self, msg: str) -> None:
        self.img_msg.set(msg)
        self.img_btn.config(state="normal")
        if not self.img_bar.winfo_ismapped():
            self.img_bar.pack(fill="x", before=self.frame_view)
        self.right.select(0)

    def prepare_gui_image(self, then_start: bool = False) -> None:
        """Build the GUI preview image after the user approves the exact build command."""
        opts = self._preview_options()
        try:
            plan = make_build_plan(opts, tag=self.image_var.get().strip())
        except RuntimeError as exc:
            messagebox.showerror("App Preview", str(exc), parent=self)
            return
        text = ("Build the GUI preview image?\n\nCommand:\n  " + plan.command_line + f"\n\nDockerfile SHA-256: {plan.dockerfile_sha256[:24]}...\n\n"
                + "\n".join("- " + r for r in plan.risks))
        if not messagebox.askokcancel("App Preview - approve image build", text, parent=self):
            self.status_var.set("Image build not approved. Nothing was built.")
            return
        approval = approve(plan)
        self.img_btn.config(state="disabled")
        self.status_var.set("Building the GUI preview image (this can take a few minutes)...")
        self._console("▸ Building GUI preview image\n", "phase")
        self.right.select(2)

        def work() -> None:
            res = build_gui_image(plan, approval, progress=lambda t: self.q.put({"type": "ui_imgline", "text": t}))
            self.q.put({"type": "ui_imgdone", "res": res, "then": then_start})
        threading.Thread(target=work, daemon=True).start()

    def _preview_options(self) -> PreviewOptions:
        o = self.options
        o.backend = self.backend_var.get() if self.backend_var.get() in BACKENDS else "auto"
        o.custom_exe = self.custom_var.get().strip()
        sz = parse_size(self.res_var.get())                    # a size typed but not confirmed yet still counts
        if sz:
            o.screen = size_text(*sz)
        o.fps = parse_fps(self.rate_var.get(), o.fps)
        try:
            lim = o.limits
            lim.timeout_s = max(10, int(self.timeout_var.get()))
            lim.max_workspace_mb = max(10, min(4096, int(self.size_var.get())))
        except (tk.TclError, ValueError):
            pass
        return o

    def _current_entry(self) -> Optional[Entry]:
        text = self.entry_var.get().strip()
        if text in self.entry_by_title:
            return self.entry_by_title[text]
        if not text:
            return None
        try:                                           # not a discovered title: a command typed by the user
            return custom_entry(shlex.split(text), kind="test" if self.mode_var.get() == "test" else "console")
        except ValueError as exc:
            messagebox.showerror("App Preview", str(exc), parent=self)
            return None

    def start(self) -> None:
        if self.session and self.session.running:
            return
        self._lean_pre_start()
        src = self.src_var.get().strip()
        if not src:
            messagebox.showinfo("App Preview", "Choose a ZIP archive or a folder first.", parent=self)
            return
        if src != self.source:
            self.set_source(src)
        entry = self._current_entry()
        if entry is None and not self.entry_var.get().strip():
            if self._scanning:                           # the list is still being read: start by itself the moment it is ready
                self._start_when_listed = True
                self.status_var.set("Still reading the project - it will start as soon as the list is ready.")
                return
            fit = [e for e in self.entries if (e.kind == "test") == (self.mode_var.get() == "test")
                   and (self.mode_var.get() not in ("debug", "trace") or e.traceable)]
            from app_preview.quick import pick_entry
            entry = pick_entry(fit, self._focus_file)    # nothing chosen: the most useful one (window > web > console)
            if entry is not None:
                title = self._title(entry)
                self.entry_by_title[title] = entry
                self.entry_var.set(title)
        if entry is None:
            messagebox.showinfo("App Preview", "Nothing to start. This project has no runnable file; use File... or type a command (it runs inside the container).", parent=self)
            return
        mode = self.mode_var.get()
        bps = [f"{f}:{n}" for f, s in self.breaks.items() for n in sorted(s)]
        opts = self._preview_options()
        if getattr(self, "_starting", False):
            return
        self._starting = True
        image, install, link = (self._image_override or self.image_var.get()).strip(), self.install_var.get(), self.link_var.get()   # Tk variables: UI thread only
        self._image_override = ""
        self.status_var.set("Checking the container runtime...")

        def work() -> None:                              # hashing a big ZIP, `docker info` and `image inspect` can take seconds: not on the UI thread
            try:
                plan = make_plan(src, entry, mode, opts, image=image, install=install, breakpoints=bps, link_output=link)
            except (ValueError, OSError) as exc:
                self.q.put({"type": "ui_start", "err": str(exc)})
                return
            try:
                st = runtime_status(opts)
                have_image = image_present(st["executable"], plan.image) if plan.display and st["available"] else True
            except Exception as exc:  # noqa: BLE001
                self.q.put({"type": "ui_start", "err": f"Could not check the container runtime: {type(exc).__name__}: {str(exc)[:160]}"})
                return
            self.q.put({"type": "ui_start", "plan": plan, "st": st, "have_image": have_image, "opts": opts})
        threading.Thread(target=work, daemon=True).start()

    def _start_ready(self, ev: Dict) -> None:
        self._starting = False
        if ev.get("err"):
            self.status_var.set("Could not start.")
            messagebox.showerror("App Preview", ev["err"], parent=self)
            return
        plan, st, opts = ev["plan"], ev["st"], ev["opts"]
        if plan.display and st["available"] and not ev["have_image"]:
            self._show_image_bar(f"The virtual-screen image '{plan.image}' is not on this computer yet, so GUI apps cannot be shown.")
            self.status_var.set("The GUI preview image is missing.")
            if messagebox.askyesno("App Preview", f"GUI apps need the preview image '{plan.image}' (virtual screen + Tk).\n\n"
                                   "It is built once with your container runtime and needs the internet.\n\nPrepare it now?", parent=self):
                self.prepare_gui_image(then_start=True)
            return
        iso = SimpleNamespace(available=st["available"], backend=st["backend"] or "", reason=st["reason"])
        pre_ok, self._quick_ok = self._quick_ok, False       # "Run now" already got one approval covering this run
        if not (pre_ok or self.approver(plan, iso)):
            self.status_var.set("Not approved. Nothing was run.")
            return
        self._fresh_then(plan, opts, st)

    def _fresh_then(self, plan, opts, st: Dict) -> None:
        """Approved: first delete every leftover copy / stopped container of earlier runs (off the UI thread), then run."""
        if not self.auto_clean_var.get() or not cleanup.has_leftovers():
            self._begin(plan, opts)
            return
        self.status_var.set("Clearing leftover copies of earlier runs...")
        exe = st.get("executable") if st.get("available") else None

        def work() -> None:
            self.q.put({"type": "ui_fresh", "plan": plan, "opts": opts, "report": cleanup.fresh_start(exe)})
        threading.Thread(target=work, daemon=True).start()

    def _begin(self, plan, opts) -> None:
        self._archive_run()
        self.mode = plan.mode
        self.trace.reset()
        self.outputs.clear()
        self.errors.clear()
        self.stack = []
        self._diags = []
        self.diag.delete(*self.diag.get_children())
        for t in (self.console, self.web_text, self.eval_out, self.hint_text):
            self._set_text(t, "")
        self.vars.delete(*self.vars.get_children())
        self.stack_list.delete(0, "end")
        self.timeline.delete(0, "end")
        self.funcs.delete(*self.funcs.get_children())
        self.code.set_current(0)
        self.code.set_errors(())
        self.code.set_heat({})
        self._frame_png = b""
        self._cpu, self._mem = [], []
        self.res_panel.reset()
        self.frame_view.clear()
        if self._popout is not None and self._popout.winfo_exists():
            self._popout.clear("Waiting for the first screen snapshot..." if plan.display else "This run has no virtual screen.")
        self._run_title = plan.entry.title
        self.stats_var.set("")
        self.web_var.set("No web app running." if not plan.ports else f"Waiting for http://127.0.0.1:{plan.ports[0][0]}/ ...")
        self.open_btn.config(state="disabled")
        self._auto_continue = plan.mode == "debug" and not self.stop_entry_var.get()
        if plan.entry.file and getattr(self, "names", None):
            name = self._match_name(plan.entry.file)
            if name:
                self.show_file(name, plan.entry.line or 0)
        self.session = PreviewSession(plan, approve(plan), opts, on_result=lambda r: self.q.put({"type": "ui_result", "res": r}))
        self.run_id = self.session.run_id
        self._t_start = time.time()
        self.state = "starting"
        self.card.reset()
        self.info["Mode"].set(plan.mode.title())
        self.info["Start"].set(plan.entry.title)
        self.info["Image"].set(plan.image)
        self.info["Network"].set("ENABLED" if plan.limits.network else "disabled")
        self.info["Ports"].set(", ".join(f"127.0.0.1:{h} -> {c}" for h, c in plan.ports) or "none")
        self.info["Screen"].set(f"{self.session.screen_size[0]}x{self.session.screen_size[1]} @ {self.options.fps:g} fps" if plan.display else "none")
        self.info["State"].set("Starting")
        self.info["Resources"].set("-")
        self._chip_set("running")
        self.status_var.set(f"Starting {plan.mode}: {plan.entry.title}")
        self.right.select({"gui": 0, "web": 1}.get(plan.entry.kind, 2) if plan.mode in ("run", "test") or plan.entry.kind != "console" else 2)
        if plan.mode == "trace":
            self.right.select(self.trace_tab)
        self._set_buttons()
        self.session.start()

    def stop(self) -> None:
        if self.session and self.session.running:
            if self.mode == "debug":
                self.session.send({"cmd": "quit"})
            self.session.stop()
            self.status_var.set("Stopping - the container is being killed...")

    def restart(self) -> None:
        if self.session and self.session.running:
            self.session.stop()
            self.session.wait(20)
        self.start()

    def debug_cmd(self, cmd: str) -> None:
        if self.session and self.state == "paused" and self.session.send({"cmd": cmd}):
            self.state = "running"
            self._set_buttons()

    def _card_clicked(self, mode: str) -> None:
        """Clicking the thumbnail opens the full-size view of what it shows."""
        self.right.select({"gui": 0, "web": 1}.get(mode, 2))

    def _hotkey(self, name: str) -> None:
        if self.winfo_ismapped() and self.dbg[name]["state"] != "disabled":
            self.debug_cmd(name)

    def _set_buttons(self) -> None:
        running = bool(self.session and self.session.running)
        paused = running and self.state == "paused"
        self.start_btn.config(state="disabled" if running else "normal")
        self.quick_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.restart_btn.config(state="normal" if self.session else "disabled")
        for b in self.dbg.values():
            b.config(state="normal" if paused else "disabled")

    def _ask_approval(self, plan, iso) -> bool:
        from ui.execution_dialog import request_execution_approval
        return request_execution_approval(self.winfo_toplevel(), " ".join(plan.argv), "a disposable copy of the project",
                                          iso, limits=dict({"mode": plan.mode, "entry": plan.entry.title,
                                                            "ports": ", ".join(f"127.0.0.1:{h}->{c}" for h, c in plan.ports) or "none"},
                                                           **({"screen": str(plan.env.get("ASE_SCREEN", "")).rsplit("x", 1)[0]} if plan.display else {})),
                                          source=f"App Preview ({plan.mode})", plan=plan)

    _focus_file = ""
    _focus = None
    _keep_entry = ""
    _scanning = False
    _start_when_listed = False

    def pick_file(self) -> None:
        """One program file: its folder becomes the source, but only the file and what it imports are listed, copied and run."""
        p = filedialog.askopenfilename(title="Select the program file to run")
        if p:
            self.open_file(p)

    def open_file(self, path: str) -> None:
        f = Path(path)
        folder = str(f.parent)
        self._focus_file = f.name
        self.source = ""                                   # force a rescan even if the folder is already selected
        self.set_source(folder)
        self._announce_source(folder)

    def show_whole_folder(self) -> None:
        self._focus_file, self._focus = "", None
        self._fill_tree()
        self.status_var.set("Showing the whole folder.")

    def _select_focus_entry(self) -> None:
        name = self._focus_file
        ent = next((e for e in self.entries if e.file == name), None)
        if ent is None and name.endswith(".py"):
            ent = custom_entry(["python", name], kind="console", title=f"python {name}")
        if ent is None:
            return
        title = self._title(ent)
        self.entry_by_title[title] = ent
        self.entry_box["values"] = list(self.entry_box["values"]) + ([title] if title not in self.entry_box["values"] else [])
        self.entry_var.set(title)
        if self._focus:
            self.status_var.set(f"{name} and {max(0, len(self._focus) - 1)} file(s) it imports.")

    def _pick(self, folder: bool) -> None:
        p = filedialog.askdirectory(title="Select a project folder") if folder else filedialog.askopenfilename(
            title="Select a project ZIP", filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")])
        if p:
            self._focus_file, self._focus = "", None
            self.source = "" if self.source == p else self.source
            self.set_source(p)
            self._announce_source(p)

    def _announce_source(self, path: str) -> None:
        """One selection for every execution place: tell the host, which updates Build & Test, analysis and the rest."""
        self._main_seen = path
        if self.on_source_changed is not None:
            try:
                self.on_source_changed(path)
            except Exception:  # noqa: BLE001 - never let the host break the picker
                pass

    # ================================================================== events
    def _poll(self) -> None:
        n = 0
        try:
            while n < 800:
                self._handle(self.q.get_nowait())
                n += 1
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(50 if n else 100, self._poll)

    def _handle(self, ev: Dict) -> None:
        t = ev.get("type", "")
        if t == "ui_entries":
            return self._entries_ready(ev)
        if t == "ui_start":
            return self._start_ready(ev)
        if t == "ui_runtime":
            st = ev["st"]
            self.runtime_var.set(f"Container runtime: {st['backend']} {st['version']} - ready" if st["available"] else
                                 f"Container runtime: {st['reason'][:220]}")
            return
        if t == "ui_image":
            st = ev["st"]
            if st["ready"]:
                self.img_bar.pack_forget()
                self.screen_note.set(f"GUI apps are drawn on a virtual screen inside the container ({st['image']} ready).")
            elif st.get("backend") is not None and "does not exist" in st["reason"]:
                self._show_image_bar(st["reason"] + " GUI apps need it to be shown here.")
            return
        if t == "ui_cleared":
            self.status_var.set("Cleared: " + "; ".join(ev["report"]) + ".")
            self._console("Cleared: " + "; ".join(ev["report"]) + "\n", "phase")
            self.forget_project()
            if self.on_forget is not None:
                self.on_forget()
            return
        if t == "ui_fresh":
            self._begin(ev["plan"], ev["opts"])
            self._console("▸ Before the run: " + "; ".join(ev["report"]) + "\n", "phase")
            return
        if t == "ui_quick_ready":
            return self._quick_ready(ev)
        if t == "ui_quick_go":
            return self._quick_go(ev)
        if t == "ui_quick_fail":
            return self._quick_fail(ev)
        if t == "ui_imgline":
            self._console(ev["text"] + "\n")
            return
        if t == "ui_imgdone":
            res = ev["res"]
            self._console("\n".join(res["notes"]) + "\n", "phase" if res["ok"] else "err")
            self.status_var.set(res["notes"][0])
            self.img_btn.config(state="normal")
            if res["ok"]:
                self.img_bar.pack_forget()
                if ev["then"]:
                    self.after(100, self.start)
            else:
                messagebox.showerror("App Preview", "\n".join(res["notes"][:8]), parent=self)
            return
        if t == "ui_result":
            try:
                if not self._lean_after_result(ev["res"]):       # a file the program asked for is offered first; a missing package after that
                    self._quick_after_result(ev["res"])
            except Exception as exc:  # noqa: BLE001
                self.status_var.set(f"Run-now follow-up failed: {type(exc).__name__}")
            if self.on_result:
                try:
                    self.on_result(ev["res"])
                except Exception as exc:  # noqa: BLE001 - a hook failure must not break the panel
                    self.status_var.set(f"Result hook failed: {type(exc).__name__}")
            return
        if ev.get("run") != self.run_id or not self.run_id:
            return
        if t in ("out", "err"):
            self._console(ev["text"], "err" if t == "err" else "")
        elif t == "phase":
            self._console(f"▸ {ev['text']}\n", "phase")
            self.status_var.set(ev["text"])
            self.res_panel.add_phase(f"{time.strftime('%H:%M:%S', time.localtime(ev['t']))}  {ev['text']}")
        elif t == "top":
            self.res_panel.set_top((ev["header"], ev["rows"]))
        elif t == "stats":
            self._cpu = (self._cpu + [float(ev.get("cpu") or 0)])[-60:]
            self._mem = (self._mem + [float(ev.get("mem_mib") or 0)])[-60:]
            self.res_panel.draw(self._cpu, self._mem)
            self.stats_var.set(f"CPU {ev.get('cpu', 0):.1f}%   Memory {ev.get('mem_text', '')}   Processes {ev.get('pids', '')}")
            self.info["Resources"].set(f"CPU {ev.get('cpu', 0):.1f}%, {ev.get('mem_text', '')}")
        elif t == "end":
            self._ended(ev)
        elif t == "pv_state":
            self._pv_state(ev)
        elif t == "pv_trace":
            self.trace.add_batch(ev["items"])
        elif t == "pv_out":
            self.outputs.add(ev["f"], ev["l"], ev["fn"], ev["s"], ev["t"], ev.get("ts") or 0.0)
            self._block_dirty = True
        elif t == "pv_stop":
            self._stopped(ev)
        elif t == "pv_vars":
            self._fill_vars(ev.get("vars") or {})
        elif t == "pv_eval":
            self._append(self.eval_out, f"{ev.get('expr')}  ->  {ev.get('v')}\n", () if ev.get("ok") else "err")
        elif t == "pv_break":
            if not ev.get("ok"):
                self.status_var.set(f"Breakpoint {ev.get('f')}:{ev.get('l')} not set: {ev.get('err')}")
        elif t == "pv_exc":
            self._exception(ev)
        elif t == "pv_diag":
            self._diagnostics(ev)
        elif t == "pv_frame":
            self._frame(ev)
        elif t == "pv_web":
            self._web(ev)
        elif t == "pv_note":
            info = ev.get("info") or {}
            if info.get("k") == "hello":
                self.trace_var.set(f"Python {info.get('py')} - {info.get('mode')} of {info.get('script')}")
            elif info.get("text"):
                self._console(f"▸ {info['text']}\n", "phase")

    def _pv_state(self, ev: Dict) -> None:
        self.state = ev.get("state", self.state)
        if self.state in ("running", "paused"):
            self.card.set_state(self.state)
        self.info["State"].set(self.state.title())
        if self.state == "paused":
            self._chip_set("paused")
        elif self.state == "running":
            self._chip_set("running")
            if self._run_title and self.session and self.session.running:      # not "Starting run in container ..." for the whole run
                self.status_var.set(f"Running: {self._run_title}")
        self._set_buttons()

    def _ended(self, ev: Dict) -> None:
        st = ev.get("status", "")
        self._last_status = st
        self.state = "ended"
        self._chip_set("ok" if st == "completed" else ("warn" if st in ("cancelled", "timeout", "skipped", "refused") else "bad"),
                       {"completed": "✔ Completed", "failed": "✖ Failed", "cancelled": "■ Stopped", "timeout": "⏱ Timed out",
                        "skipped": "■ Skipped", "refused": "■ Refused"}.get(st, "✖ " + st.title()))
        self.card.set_state("ended", {"completed": "Finished", "failed": "Failed", "cancelled": "Stopped", "timeout": "Timed out",
                                      "skipped": "Not started", "refused": "Refused"}.get(st, st.title()))
        self.info["State"].set(st.title())
        code = ev.get("exit_code")
        self.status_var.set(f"{st}" + (f", exit {code}" if code is not None else "") + f", {ev.get('duration_s', 0):.1f}s  " + " ".join(ev.get("notes") or [])[:240])
        self._set_buttons()
        self._refresh_trace(force=True)
        if self.sel[0]:
            self._update_block(*self.sel)
        if self.auto_snap_var.get() and self._frame_png:                  # the last picture of the run, kept for the record
            self.quick_snapshot(quiet_if_none=True)

    def _stopped(self, ev: Dict) -> None:
        self.state = "paused"
        f, l = ev.get("f") or "", int(ev.get("l") or 0)
        self.stack = ev.get("stack") or []
        self.stack_list.delete(0, "end")
        for fr in self.stack:
            self.stack_list.insert("end", f"{fr.get('fn')}   {fr.get('f')}:{fr.get('l')}")
        self.debug_var.set(f"Paused ({ev.get('why')}) at {f}:{l} in {ev.get('fn')}" + (f"  -> {ev['value']}" if ev.get("value") else ""))
        self._fill_vars(ev.get("vars") or {})
        if self._auto_continue and ev.get("why") == "step":
            self._auto_continue = False
            if self.session:
                self.session.send({"cmd": "continue"})
            return
        self._auto_continue = False
        self._chip_set("paused")
        self._set_buttons()
        self.show_file(f, l)
        self.code.set_current(l)
        self.sel = (f, l)
        self._update_block(f, l)
        self.right.select(self.right.index(self.right.tabs()[4]))

    def _fill_vars(self, v: Dict) -> None:
        self.vars.delete(*self.vars.get_children())
        for title, key in (("Locals", "locals"), ("Globals", "globals")):
            items = v.get(key) or {}
            node = self.vars.insert("", "end", text=f"{title} ({len(items)})", open=key == "locals")
            for k, val in items.items():
                self.vars.insert(node, "end", text=k, values=(val,))

    def _on_stack(self, _e=None) -> None:
        sel = self.stack_list.curselection()
        if sel and sel[0] < len(self.stack):
            fr = self.stack[sel[0]]
            self.show_file(fr.get("f") or "", int(fr.get("l") or 0))
            self.code.set_current(int(fr.get("l") or 0))
            if self.session and self.state == "paused":
                self.session.send({"cmd": "vars", "frame": sel[0]})

    def _eval(self) -> None:
        e = self.eval_var.get().strip()
        if e and self.session and self.state == "paused":
            self.session.send({"cmd": "eval", "expr": e})

    def _exception(self, ev: Dict) -> None:
        frames = ev.get("frames") or []
        for fr in frames:
            if fr.get("f"):
                self.errors.setdefault(fr["f"], set()).add(int(fr.get("l") or 0))
        if frames:
            last = frames[-1]
            self.show_file(last["f"], int(last.get("l") or 0))
            self.code.set_errors(self.errors.get(last["f"], ()))
            self.code.set_current(int(last.get("l") or 0))
            self.sel = (last["f"], int(last.get("l") or 0))
            self._update_block(*self.sel)
        self.status_var.set(f"{ev.get('et')}: {ev.get('e')}"[:300])

    def _diagnostics(self, ev: Dict) -> None:
        self._diags = [d for d in ev.get("diagnostics") or []]
        self.diag.delete(*self.diag.get_children())
        for i, d in enumerate(self._diags):
            loc = f"{d.get('file')}:{d.get('line')}" if d.get("file") else ""
            self.diag.insert("", "end", iid=str(i), values=(d.get("severity"), loc, (d.get("message") or "")[:200]))
            if d.get("file") and d.get("line") and d.get("severity") == "error":
                name = self._match_name(d["file"])
                if name:
                    self.errors.setdefault(name, set()).add(int(d["line"]))
        self.insight_var.set(ev.get("summary") or "")
        self._set_text(self.hint_text, "\n".join(f"- {h.get('title', '')}: {h.get('text', '')}" for h in ev.get("hints") or []) or "No hints.")
        self.code.set_errors(self.errors.get(self.code.path, ()))
        if self._diags and not self.code.path:
            self._on_diag(first=True)

    def _match_name(self, rel: str) -> Optional[str]:
        names = getattr(self, "names", [])
        rel = rel.strip("/")
        if rel in names:
            return rel
        hit = [n for n in names if n.endswith("/" + rel) or rel.endswith("/" + n)]
        return min(hit, key=len) if hit else None

    def _on_diag(self, _e=None, first: bool = False) -> None:
        sel = self.diag.selection() if not first else ("0",)
        if sel and int(sel[0]) < len(self._diags):
            d = self._diags[int(sel[0])]
            name = self._match_name(d.get("file") or "")
            if name:
                self.show_file(name, int(d.get("line") or 0))
                self.code.set_current(int(d.get("line") or 0))
                self.code.set_errors(self.errors.get(name, ()))
                self.sel = (name, int(d.get("line") or 0))
                self._update_block(*self.sel)

    # ---- screen
    def _frame(self, ev: Dict) -> None:
        self._frame_png = ev["png"]
        self.card.set_frame(ev["png"])
        if self._frame_job is None:                           # frames that arrive together are drawn once, after the queue is drained
            self._frame_job = self.after_idle(self._flush_frame)
        self.screen_note.set(f"Virtual screen {ev.get('w')}x{ev.get('h')} (snapshots, about {self.options.fps:g} per second)")
        if self.info["Screen"].get() in ("-", "none") or "@" in self.info["Screen"].get():
            self.info["Screen"].set(f"{ev.get('w')}x{ev.get('h')} @ {self.options.fps:g} fps")

    def _flush_frame(self) -> None:
        self._frame_job = None
        if not self.winfo_exists():
            return
        png = self._frame_png
        if not png:
            return
        self.frame_view.show(png)                              # draws only while the Screen tab is visible
        if self._popout is not None and self._popout.winfo_exists():
            self._popout.show(png, f"Last snapshot {time.strftime('%H:%M:%S')}  -  {self.screen_note.get()}")

    def _draw_frame(self) -> None:
        self.frame_view.redraw()

    @property
    def _frame_meta(self):                                     # (width, height, subsample, zoom) of what is drawn
        return self.frame_view.meta

    def _screen_xy(self, e):
        return self.frame_view.to_xy(e)

    def _send_click(self, xy, button: int = 1) -> None:
        if xy is not None and self.send_input_var.get() and self.session and self.session.running:
            self.session.input_event("click", x=xy[0], y=xy[1], button=button)

    def _screen_click(self, e, button: int = 1) -> None:
        self.screen.focus_set()
        self._send_click(self._screen_xy(e), button)

    def _screen_key(self, e) -> None:
        if not (self.send_input_var.get() and self.session and self.session.running):
            return
        if e.keysym in KEYSYM:
            self.session.input_event("key", key=KEYSYM[e.keysym])
        elif e.char and e.char.isprintable():
            self.session.input_event("type", text=e.char)

    # ---- screen options (size, rate, view)
    def _save_pref(self, key: str, value) -> None:
        self._prefs[key] = value
        if self.on_pref is not None:
            try:
                self.on_pref(key, value)
            except Exception:  # noqa: BLE001 - a preference that cannot be saved is not an error
                pass

    def _size_changed(self) -> None:
        sz = parse_size(self.res_var.get())
        if sz is None:
            self.res_var.set(self.options.screen)
            self.screen_note.set("The size must look like 1024x768 (from 320x240 up to 3840x2160).")
            return
        new = size_text(*sz)
        self.res_var.set(new)
        if new == self.options.screen:
            return
        self.options.screen = new
        self._save_pref("screen", new)
        live = bool(self.session and self.session.running and self.session.plan.display)
        self.screen_note.set(f"Screen size {new} is used the next time the app starts" + (" - press ⟳ Restart to apply it now." if live else "."))

    def _rate_changed(self) -> None:
        self.options.fps = parse_fps(self.rate_var.get(), self.options.fps)
        self._save_pref("fps", self.options.fps)
        if self.session and self.session.running and self.session.plan.display:
            self.info["Screen"].set(f"{self.session.screen_size[0]}x{self.session.screen_size[1]} @ {self.options.fps:g} fps")

    def _view_changed(self) -> None:
        mode = VIEW_MODES.get(self.view_var.get(), "fit")
        self.frame_view.set_mode(mode)
        self._save_pref("view", mode)

    # ---- external view
    def pop_out(self) -> None:
        """GUI app: the screen in a window of its own. Web app: the real page in the browser. Otherwise says what is missing."""
        if self._popout is not None and self._popout.winfo_exists():
            self._popout.deiconify()
            self._popout.lift()
            self._popout.focus_force()
            return
        gui = bool(self._frame_png or (self.session and self.session.running and self.session.plan.display))
        if not gui:
            if self._web_url and self.open_btn["state"] == "normal":
                self._open_web()
                return
            self.status_var.set("Nothing to show outside yet: start a GUI app (its screen opens in a window) or a web app (it opens in the browser).")
            return
        self._popout = ScreenWindow(self.winfo_toplevel(), f"App Preview - screen ({self._run_title or 'no run'})", on_close=self._popout_closed,
                                    on_click=lambda x, y, b: self._send_click((x, y), b), on_key=self._screen_key, input_var=self.send_input_var,
                                    on_save_as=self._save_frame, on_quick=self.quick_snapshot,
                                    on_geometry=lambda g: self._save_pref("popout_geometry", g), geometry=str(self._prefs.get("popout_geometry", "")))
        if self._frame_png:
            self._popout.show(self._frame_png, self.screen_note.get())

    def _popout_closed(self) -> None:
        self._popout = None

    # ---- snapshots
    def _snapshot_tag(self) -> str:
        return self.run_id or ""

    def _save_frame(self) -> None:
        """Save as...: choose the file (starts in the snapshot folder with a timestamped name)."""
        p = ask_save_snapshot(self, self._frame_png, self._snapshot_tag())
        if p:
            self.status_var.set(f"Snapshot saved: {p}")

    def quick_snapshot(self, quiet_if_none: bool = False):
        """One click: a timestamped PNG in the snapshot folder. Returns the path (None when there is no picture or it cannot be written)."""
        if not self._frame_png:
            if not quiet_if_none:
                messagebox.showinfo("Snapshot", "No screen snapshot yet.", parent=self)
            return None
        try:
            p = SNAPSHOTS.save(self._frame_png, self._snapshot_tag())
        except OSError as exc:
            self.status_var.set(f"Snapshot not saved: {exc}")
            return None
        self.status_var.set(f"Snapshot saved: {p}")
        self._console(f"▸ Snapshot saved: {p}\n", "phase")
        return p

    def choose_snapshot_folder(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Folder for screen snapshots", initialdir=str(SNAPSHOTS.folder) if SNAPSHOTS.folder.exists() else None)
        if d:
            SNAPSHOTS.set_custom(d)
            self._save_pref("snap_dir", d)
            self.status_var.set(f"Snapshots now go to {d}")

    # ---- web
    def _web(self, ev: Dict) -> None:
        self._web_url = ev.get("url", "")
        w = self.web_text
        w.config(state="normal")
        w.delete("1.0", "end")
        if not ev.get("up"):
            self.web_var.set(f"{self._web_url} is not answering.")
            w.insert("end", "The web app is not answering (yet).\n", "muted")
            self.card.set_web(False, "", "", self._web_url)
        else:
            body = ev.get("body") or ""
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
            self.web_var.set(f"{self._web_url}  -  HTTP {ev.get('status')}" + (f"  -  {_html.unescape(title.group(1).strip())[:80]}" if title else ""))
            self.open_btn.config(state="normal")
            ct = str((ev.get("headers") or {}).get("Content-Type", "")).lower()
            self.card.set_web(True, _html.unescape(title.group(1).strip()) if title else "", page_text(body) if "html" in ct or "<html" in body[:500].lower() else body[:600], self._web_url)
            w.insert("end", "Response headers\n", "h")
            for k, v in list((ev.get("headers") or {}).items())[:20]:
                w.insert("end", f"{k}: {v}\n", "muted")
            w.insert("end", "\nPage text (snapshot; use 'Open in browser' for the real page)\n", "h")
            ctype = str((ev.get("headers") or {}).get("Content-Type", "")).lower()
            w.insert("end", (page_text(body) if "html" in ctype or "<html" in body[:500].lower() else body)[:60000] or "(empty body)")
        w.config(state="disabled")

    def _open_web(self) -> None:
        if self._web_url.startswith("http://127.0.0.1:"):
            webbrowser.open(self._web_url)

    # ---- console
    def _console(self, text: str, tag: str = "") -> None:
        if self._viewing:
            self._restore_live()
        if tag != "phase":
            self.card.add_text(text)
        self._append(self.console, text, tag or (), scroll=self.autoscroll_var.get())
        n = int(self.console.index("end-1c").split(".")[0])
        if n > MAX_CONSOLE_LINES:
            self.console.config(state="normal")
            self.console.delete("1.0", f"{n - MAX_CONSOLE_LINES}.0")
            self.console.config(state="disabled")

    @staticmethod
    def _append(w: tk.Text, text: str, tag="", scroll: bool = True) -> None:
        w.config(state="normal")
        w.insert("end", text, tag)
        w.config(state="disabled")
        if scroll:
            w.see("end")

    @staticmethod
    def _set_text(w: tk.Text, text: str) -> None:
        w.config(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text)
        w.config(state="disabled")

    def _copy_console(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.console.get("1.0", "end-1c"))

    def _save_console(self) -> None:
        p = filedialog.asksaveasfilename(defaultextension=".log", initialfile="preview_output.log")
        if p:
            Path(p).write_text(self.console.get("1.0", "end-1c"), encoding="utf-8")

    # ---- run history, find, project clean-up, Docker set-up (what Live Run / Build & Test offered, in one place)
    def _archive_run(self) -> None:
        if self._viewing:
            self._restore_live()
        text = self.console.get("1.0", "end-1c")
        if self._run_title and text.strip():
            self._history.append({"label": f"{time.strftime('%H:%M:%S')}  {self._run_title[:34]}  [{self._last_status or 'ended'}]", "text": text})
            self._history = self._history[-20:]
            self.hist_box["values"] = ["Current run"] + [h["label"] for h in reversed(self._history)]
            self.hist_var.set("Current run")

    def _history_picked(self) -> None:
        i = self.hist_box.current()
        if i <= 0:
            self._restore_live()
            return
        if self.session and self.session.running:
            self.status_var.set("A run is in progress: earlier output can be viewed after it ends.")
            self.hist_var.set("Current run")
            return
        if not self._viewing:
            self._live_text = self.console.get("1.0", "end-1c")
        self._viewing = True
        self._set_text(self.console, list(reversed(self._history))[i - 1]["text"])
        self._highlight_console()

    def _restore_live(self) -> None:
        if self._viewing:
            self._viewing = False
            self._set_text(self.console, self._live_text)
        self.hist_var.set("Current run")

    def _highlight_console(self) -> None:
        w = self.console
        w.tag_remove("hit", "1.0", "end")
        q = self.find_var.get()
        if not q:
            return
        i, first = "1.0", None
        while True:
            i = w.search(q, i, stopindex="end", nocase=True)
            if not i:
                break
            j = f"{i}+{len(q)}c"
            w.tag_add("hit", i, j)
            first = first or i
            i = j
        if first:
            w.see(first)

    def open_setup(self) -> None:
        from ui.setup_dialog import open_setup
        open_setup(self.winfo_toplevel(), on_done=self.check_runtime)

    def clear_project(self) -> None:
        if self.session and self.session.running:
            messagebox.showinfo("Clear", "Stop the running preview first.", parent=self)
            return
        if not messagebox.askyesno("Clear", "Remove every leftover copy of a project and every stopped container made by this tool, and forget "
                                   "the selected project?\n\nYour own folder, archive or file is not touched.", parent=self):
            return
        self.status_var.set("Clearing...")

        def work() -> None:
            from plugins.build_execution.models import Options
            from plugins.build_execution.sandbox import find_runtime
            self.q.put({"type": "ui_cleared", "report": cleanup.clear_everything(find_runtime(Options())[1])})
        threading.Thread(target=work, daemon=True).start()

    def forget_project(self) -> None:
        self.source = ""
        self.src_var.set("")
        self.entries, self.entry_by_title, self.names = [], {}, []
        self.entry_box["values"] = []
        self.entry_var.set("")
        self._focus_file, self._focus = "", None
        self._fill_tree()

    # ---- trace tab
    def _on_func(self, _e=None) -> None:
        sel = self.funcs.selection()
        if sel:
            f, fn = self.funcs.item(sel[0], "values")[:2]
            text = self._read(f)
            if text:
                b = next((b for b in blocks_for(f, text) if b.name == fn or b.qualname == fn), None)
                self.show_file(f, b.start if b else 0)
                self.sel = (f, b.start if b else 1)
                self._update_block(*self.sel)

    def _on_timeline(self, _e=None) -> None:
        sel = self.timeline.curselection()
        if sel and sel[0] < len(self._timeline_rows):
            r = self._timeline_rows[sel[0]]
            self.show_file(r["f"], r["l"])
            self.code.set_current(r["l"])

    def _refresh_trace(self, force: bool = False) -> None:
        tm = self.trace
        if not (tm.dirty or force):
            return
        tm.dirty = False
        self.trace_var.set(f"{tm.events:,} trace events, {len(tm.funcs)} functions, {sum(len(v) for v in tm.hits.values())} lines executed")
        if self.right.select() == str(self.trace_tab) or force:
            self.funcs.delete(*self.funcs.get_children())
            for i, (f, fn, c, ln) in enumerate(tm.top_functions(200)):
                self.funcs.insert("", "end", iid=str(i), values=(f, fn, c, ln))
            rows = list(tm.timeline)[-300:]
            self._timeline_rows = rows
            self.timeline.delete(0, "end")
            for r in rows:
                mark = {"call": "→", "ret": "←", "exc": "✖"}.get(r["k"], "·")
                extra = (", ".join(f"{k}={v}" for k, v in list((r.get("args") or {}).items())[:3]) if r["k"] == "call"
                         else (f" = {r.get('v')}" if r["k"] == "ret" else f"  {r.get('et')}: {r.get('e')}"))
                self.timeline.insert("end", "  " * min(int(r.get("d") or 0), 12) + f"{mark} {r['fn']}({extra if r['k'] == 'call' else ''}){'' if r['k'] == 'call' else extra}   {r['f']}:{r['l']}")
            self.timeline.see("end")
        if self.code.path:
            self.code.set_heat(tm.hot_lines(self.code.path))

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.card.tick()
            if self.session and self.session.running:
                self.info["Uptime"].set(f"{int(time.time() - self._t_start)} s")
            self._refresh_trace()
            if self.follow_var.get() and self.session and self.session.running and self.state == "running":
                cur = self.trace.cur
                if cur and time.time() - self._last_follow > 0.25:
                    self._last_follow = time.time()
                    f, l, _fn = cur
                    self.show_file(f)
                    self.code.set_current(l)
                    self.sel = (f, l)
                    self._update_block(f, l)
                    self._block_dirty = False
            if self._block_dirty and self.sel[0]:
                self._block_dirty = False
                self._update_block(*self.sel)
            if self.session and self.session.running and self.state in ("running", "paused", "starting"):
                self.status_var.set(self.status_var.get()) if self.state == "paused" else None
        finally:
            self.after(250, self._tick)

    def _on_destroy(self, e) -> None:
        if e.widget is self:
            for u in self._unsub:
                u()
            if self._popout is not None:
                try:
                    self._popout.destroy()
                except tk.TclError:
                    pass
                self._popout = None
            if self.session and self.session.running:
                self.session.stop()
            if self._sv:
                self._sv.close()
