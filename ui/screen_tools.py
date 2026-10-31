"""Screen tools shared by App Preview and Live Run: size presets, scaling, snapshot store, a frame viewer and an external (pop-out) window.

Everything here only draws PNG snapshots that the preview session already publishes; nothing starts or controls a container.
Plain Tk, no extra packages.

* ``parse_size`` / ``size_text``   - the "WIDTHxHEIGHT" text of the virtual screen (validated, so a typo never reaches a run)
* ``choose_scale``                 - Fit / 50% / 100% / 200% as exact Tk zoom/subsample ratios (pixel mapping for mouse input stays exact)
* ``SnapshotStore`` (``SNAPSHOTS``) - one place for "Save as...", "Quick save" (timestamped file in a folder) and "Open folder"
* ``FrameView``                    - a canvas that shows the latest frame (scrollbars when it is larger than the pane, centred when smaller)
* ``ScreenWindow``                 - the same picture in its own window: resizable, keep-on-top, full screen (second monitor), snapshots
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

SIZE_PRESETS = ("640x480", "800x600", "1024x768", "1280x720", "1366x768", "1600x900", "1920x1080")
MIN_SIZE, MAX_SIZE = (320, 240), (3840, 2160)
FPS_CHOICES = ("0.5 fps", "1 fps", "2 fps", "5 fps", "10 fps")
VIEW_MODES: Dict[str, str] = {"Fit": "fit", "50%": "50", "100%": "100", "200%": "200"}
MAX_IMAGE_SIDE = 4096                                   # a zoomed picture larger than this is not built (memory); 100% is used instead

_FIT_LADDER = ((2, 1), (1, 1), (3, 4), (2, 3), (1, 2), (2, 5), (1, 3), (1, 4), (1, 5), (1, 6), (1, 8), (1, 12), (1, 16))
_SIZE_RE = re.compile(r"^\s*(\d{3,4})\s*[xX×*]\s*(\d{3,4})\s*$")


# ---------------------------------------------------------------------------------------------------------------- size / rate
def parse_size(text: str) -> Optional[Tuple[int, int]]:
    """'1024x768' -> (1024, 768); None when it is not a size or is outside 320x240 .. 3840x2160."""
    m = _SIZE_RE.match(str(text or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if not (MIN_SIZE[0] <= w <= MAX_SIZE[0] and MIN_SIZE[1] <= h <= MAX_SIZE[1]):
        return None
    return w, h


def size_text(w: int, h: int) -> str:
    return f"{int(w)}x{int(h)}"


def parse_fps(text: str, default: float = 2.0) -> float:
    """'5 fps' / '5' -> 5.0, clamped to what the capture loop accepts (0.2 .. 10)."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)", str(text or ""))
    try:
        v = float(m.group(1)) if m else default
    except ValueError:
        v = default
    return max(0.2, min(10.0, v))


def fps_text(v: float) -> str:
    v = max(0.2, min(10.0, float(v)))
    return f"{v:g} fps"


def view_label(mode: str) -> str:
    return next((k for k, v in VIEW_MODES.items() if v == mode), "Fit")


# ---------------------------------------------------------------------------------------------------------------- scaling
def choose_scale(w: int, h: int, cw: int, ch: int, mode: str = "fit") -> Tuple[int, int]:
    """(zoom, subsample) for a w x h picture in a cw x ch pane. 'fit' = the largest exact ratio that still fits (the pane never
    scrolls); '50' / '100' / '200' are fixed (the pane then scrolls when the picture is larger)."""
    if mode == "100":
        return 1, 1
    if mode == "50":
        return 1, 2
    if mode == "200":
        return (2, 1) if max(w, h) * 2 <= MAX_IMAGE_SIDE else (1, 1)
    zoom, sub = 1, 1
    for z, sb in _FIT_LADDER:
        zoom, sub = z, sb
        if w * z // sb <= cw and h * z // sb <= ch:
            break
    return zoom, sub


def scale_image(master, base: "tk.PhotoImage", zoom: int, sub: int) -> "tk.PhotoImage":
    """``base`` scaled by zoom/sub in ONE pass (no huge intermediate picture when both are > 1)."""
    if zoom == 1 and sub == 1:
        return base
    try:
        out = tk.PhotoImage(master=master)
        master.tk.call(str(out), "copy", str(base), "-zoom", zoom, zoom, "-subsample", sub, sub)
        return out
    except tk.TclError:                                  # very old Tk: two steps
        img = base
        if zoom > 1:
            img = img.zoom(zoom, zoom)
        if sub > 1:
            img = img.subsample(sub, sub)
        return img


# ---------------------------------------------------------------------------------------------------------------- snapshots
def default_snapshot_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "snapshots"


class SnapshotStore:
    """Where screen snapshots go. ``folder`` is the user's choice, else the default (the host sets it to <app>/data/snapshots)."""

    def __init__(self) -> None:
        self.default: Path = default_snapshot_dir()
        self.custom: str = ""

    def set_default(self, path) -> None:
        self.default = Path(path)

    def set_custom(self, path: str) -> None:
        self.custom = str(path or "")

    @property
    def folder(self) -> Path:
        return Path(self.custom) if self.custom else self.default

    @staticmethod
    def file_name(tag: str = "", when: Optional[float] = None) -> str:
        tag = re.sub(r"[^A-Za-z0-9_-]+", "", tag or "")[:16]
        return f"preview{'_' + tag if tag else ''}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(when or time.time()))}.png"

    def save(self, png: bytes, tag: str = "") -> Path:
        """Quick save: a timestamped PNG in the snapshot folder (created if needed; never overwrites). Raises OSError."""
        if not png:
            raise OSError("There is no screen snapshot yet.")
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / self.file_name(tag)
        n = 1
        while path.exists():
            path = self.folder / f"{path.stem.rsplit('_n', 1)[0]}_n{n}.png"
            n += 1
        path.write_bytes(png)
        return path

    def open_folder(self) -> bool:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(self.folder))                  # noqa: S606 - a folder of our own, chosen by the user
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.folder)])
            else:
                subprocess.Popen(["xdg-open", str(self.folder)])
            return True
        except (OSError, AttributeError):
            return False


SNAPSHOTS = SnapshotStore()


def ask_save_snapshot(parent, png: bytes, tag: str = "") -> Optional[Path]:
    """'Save as...': a file dialog that starts in the snapshot folder with a timestamped name. Returns the path or None."""
    if not png:
        messagebox.showinfo("Snapshot", "No screen snapshot yet.", parent=parent)
        return None
    try:
        SNAPSHOTS.folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    p = filedialog.asksaveasfilename(parent=parent, defaultextension=".png", filetypes=[("PNG image", "*.png")],
                                     initialdir=str(SNAPSHOTS.folder), initialfile=SNAPSHOTS.file_name(tag))
    if not p:
        return None
    try:
        Path(p).write_bytes(png)
    except OSError as exc:
        messagebox.showerror("Snapshot", str(exc), parent=parent)
        return None
    return Path(p)


class SnapshotButton(ttk.Menubutton):
    """'📷 Snapshot ▾': Save as... | Quick save | Open folder | Choose folder | (optional) auto-snapshot when the run ends."""

    def __init__(self, parent, on_save_as: Callable[[], None], on_quick: Callable[[], None], on_choose_folder: Optional[Callable[[], None]] = None,
                 auto_var: Optional[tk.BooleanVar] = None, **kw):
        super().__init__(parent, text="📷 Snapshot ▾", **kw)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Save as...", command=on_save_as)
        self.menu.add_command(label="Quick save (timestamped file in the snapshot folder)", command=on_quick)
        self.menu.add_separator()
        self.menu.add_command(label="Open snapshot folder", command=lambda: SNAPSHOTS.open_folder())
        if on_choose_folder is not None:
            self.menu.add_command(label="Choose snapshot folder...", command=on_choose_folder)
        if auto_var is not None:
            self.menu.add_separator()
            self.menu.add_checkbutton(label="Snapshot automatically when the run ends", variable=auto_var)
        self["menu"] = self.menu


# ---------------------------------------------------------------------------------------------------------------- frame viewer
class FrameView(ttk.Frame):
    """Draws one PNG snapshot at the chosen view mode. Keeps the decoded picture so a resize or a mode change never decodes again,
    skips drawing while hidden, and maps mouse positions back to the app's own screen pixels."""
    BG = "#101418"                                       # dark monitor backdrop in both themes

    def __init__(self, parent, mode: str = "fit", placeholder: str = "", **kw):
        super().__init__(parent, **kw)
        self.mode = mode if mode in VIEW_MODES.values() else "fit"
        self.placeholder = placeholder
        self.png = b""
        self.meta: Tuple[int, int, int, int] = (0, 0, 1, 1)      # picture width, height, subsample, zoom
        self.offset: Tuple[int, int] = (0, 0)                    # where the picture starts inside the pane (it is centred)
        self._png_id = 0
        self._base = None                                        # (png_id, PhotoImage) decoded once per frame
        self._img = None
        self._key = None
        self.canvas = tk.Canvas(self, highlightthickness=0, takefocus=1, bg=self.BG)
        self.canvas._keep_colors = True
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(-1 if e.delta > 0 else 1, e.state & 1))
        self.canvas.bind("<Button-4>", lambda e: self._wheel(-1, e.state & 1))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(1, e.state & 1))
        self.bind("<Map>", lambda _e: self.redraw(), add="+")
        self._draw_placeholder()

    # ------------------------------------------------------------------ feed
    def set_mode(self, mode: str) -> None:
        if mode in VIEW_MODES.values() and mode != self.mode:
            self.mode = mode
            self.redraw()

    def show(self, png: bytes) -> None:
        if png and png != self.png:
            self.png, self._png_id = png, self._png_id + 1
            self._base = None
            if self.winfo_ismapped():
                self.redraw()

    def clear(self) -> None:
        self.png, self._base, self._img, self._key = b"", None, None, None
        self.meta, self.offset = (0, 0, 1, 1), (0, 0)
        self.canvas.delete("all")
        self._bars(False, False)
        self._draw_placeholder()

    def _draw_placeholder(self) -> None:
        if self.placeholder and not self.png:
            cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
            self.canvas.create_text(cw // 2, ch // 2, text=self.placeholder, fill="#8a94ad", justify="center", width=max(cw - 40, 80))

    # ------------------------------------------------------------------ drawing
    def redraw(self) -> None:
        if not self.png:
            if self.placeholder:
                self.canvas.delete("all")
                self._draw_placeholder()
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        key = (self._png_id, self.mode, cw, ch)
        if key == self._key:
            return
        if self._base is None or self._base[0] != self._png_id:
            try:
                self._base = (self._png_id, tk.PhotoImage(master=self.canvas, data=base64.b64encode(self.png).decode("ascii")))
            except tk.TclError:
                return
        base = self._base[1]
        w, h = base.width(), base.height()
        zoom, sub = choose_scale(w, h, cw, ch, self.mode)
        img = scale_image(self.canvas, base, zoom, sub)
        iw, ih = img.width(), img.height()
        ox, oy = max((cw - iw) // 2, 0), max((ch - ih) // 2, 0)
        self._img, self._key, self.meta, self.offset = img, key, (w, h, sub, zoom), (ox, oy)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=img)
        self.canvas.configure(scrollregion=(0, 0, max(iw, cw), max(ih, ch)))
        self._bars(iw > cw, ih > ch)

    def _bars(self, horizontal: bool, vertical: bool) -> None:
        if vertical:
            self.vbar.grid(row=0, column=1, sticky="ns")
        else:
            self.vbar.grid_remove()
        if horizontal:
            self.hbar.grid(row=1, column=0, sticky="ew")
        else:
            self.hbar.grid_remove()

    def _wheel(self, step: int, horizontal) -> None:
        if horizontal:
            self.canvas.xview_scroll(step * 3, "units")
        else:
            self.canvas.yview_scroll(step * 3, "units")

    # ------------------------------------------------------------------ mouse -> the app's own pixels
    def to_xy(self, event) -> Optional[Tuple[int, int]]:
        w, h, sub, zoom = self.meta
        if not w:
            return None
        ix, iy = self.canvas.canvasx(event.x) - self.offset[0], self.canvas.canvasy(event.y) - self.offset[1]
        if ix < 0 or iy < 0:
            return None
        x, y = int(ix * sub / zoom), int(iy * sub / zoom)
        return (x, y) if x < w and y < h else None


# ---------------------------------------------------------------------------------------------------------------- external window
class ScreenWindow(tk.Toplevel):
    """The preview screen in a window of its own. Drag it to another monitor, F11 = full screen, Esc leaves full screen.
    ``on_click(x, y, button)`` / ``on_key(event)`` are given only when the owner may send input (App Preview, with its checkbox)."""

    def __init__(self, parent, title: str, on_close: Optional[Callable[[], None]] = None, on_click: Optional[Callable[[int, int, int], None]] = None,
                 on_key: Optional[Callable] = None, input_var: Optional[tk.BooleanVar] = None, on_save_as: Optional[Callable[[], None]] = None,
                 on_quick: Optional[Callable[[], None]] = None, on_geometry: Optional[Callable[[str], None]] = None, geometry: str = "",
                 placeholder: str = "Waiting for the first screen snapshot...", mode: str = "fit"):
        super().__init__(parent)
        self.title(title)
        self._on_close, self._on_geometry = on_close, on_geometry
        self._on_click, self._on_key = on_click, on_key
        self.geometry(geometry if re.match(r"^\d+x\d+([+-]\d+){0,2}$", geometry or "") else "900x700")
        self.minsize(320, 240)
        bar = ttk.Frame(self, padding=(4, 3))
        bar.pack(fill="x")
        if on_save_as and on_quick:                        # packed first: it keeps its room when the window is narrow
            SnapshotButton(bar, on_save_as, on_quick).pack(side="right")
        ttk.Label(bar, text="View:").pack(side="left")
        self.view_var = tk.StringVar(value=view_label(mode))
        vb = ttk.Combobox(bar, textvariable=self.view_var, values=list(VIEW_MODES), state="readonly", width=6)
        vb.pack(side="left", padx=(2, 8))
        vb.bind("<<ComboboxSelected>>", lambda _e: self.view.set_mode(VIEW_MODES.get(self.view_var.get(), "fit")))
        if input_var is not None and on_click is not None:
            ttk.Checkbutton(bar, text="Send input", variable=input_var).pack(side="left", padx=(0, 8))
        self.top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="On top", variable=self.top_var, command=self._topmost).pack(side="left")
        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Full screen (F11)", variable=self.full_var, command=self._fullscreen).pack(side="left", padx=8)
        self.note = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.note, padding=(6, 0)).pack(fill="x")
        self.view = FrameView(self, mode=mode, placeholder=placeholder)
        self.view.pack(fill="both", expand=True, padx=4, pady=4)
        c = self.view.canvas
        if on_click is not None:
            c.bind("<Button-1>", lambda e: self._click(e, 1))
            c.bind("<Button-3>", lambda e: self._click(e, 3))
        if on_key is not None:
            c.bind("<Key>", on_key)
        self.bind("<F11>", lambda _e: self._toggle_full())
        self.bind("<Escape>", lambda _e: self._leave_full())
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ feed
    def show(self, png: bytes, note: str = "") -> None:
        self.view.show(png)
        if note:
            self.note.set(note)

    def clear(self, placeholder: str = "") -> None:
        if placeholder:
            self.view.placeholder = placeholder
        self.view.clear()
        self.note.set("")

    # ------------------------------------------------------------------ window behaviour
    def _click(self, e, button: int) -> None:
        self.view.canvas.focus_set()
        xy = self.view.to_xy(e)
        if xy is not None and self._on_click is not None:
            self._on_click(xy[0], xy[1], button)

    def _topmost(self) -> None:
        try:
            self.attributes("-topmost", bool(self.top_var.get()))
        except tk.TclError:
            pass

    def _fullscreen(self) -> None:
        try:
            self.attributes("-fullscreen", bool(self.full_var.get()))
        except tk.TclError:
            pass

    def _toggle_full(self) -> None:
        self.full_var.set(not self.full_var.get())
        self._fullscreen()

    def _leave_full(self) -> None:
        if self.full_var.get():
            self.full_var.set(False)
            self._fullscreen()

    def close(self) -> None:
        if self._on_geometry is not None and not self.full_var.get():
            try:
                self._on_geometry(self.geometry())
            except Exception:  # noqa: BLE001 - remembering the position is a convenience
                pass
        cb, self._on_close = self._on_close, None
        try:
            self.destroy()
        finally:
            if cb:
                cb()
