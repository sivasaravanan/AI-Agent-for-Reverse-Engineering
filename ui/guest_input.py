"""Mouse and keyboard of a virtual machine window: what you do on the picture happens in the program inside the container.

``GuestInput(canvas, to_xy, get_session, enabled)`` binds the canvas. It sends, as they happen: pointer moves (throttled), button press and
release (so dragging, selecting and moving windows work), the wheel, and key press / release with the key's own X name (so Shift, Ctrl,
Alt, arrows, F-keys and typed characters behave like on a real keyboard). Keys still held when the window loses focus are released.
Only the session's validated ``input_event`` is used; nothing is run on the host. Standard library only (Tk).
"""
from __future__ import annotations

import re
import tkinter as tk
from typing import Callable, Optional, Set

KEYNAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")
HOST_KEYS = {"F11"}                                    # kept by the window (full screen); everything else goes to the program
MOVE_EVERY_MS = 25


class GuestInput:
    def __init__(self, canvas: tk.Canvas, to_xy: Callable, get_session: Callable, enabled: Callable[[], bool], host_combo: Optional[Callable] = None):
        self.c, self.to_xy, self.get_session, self.enabled = canvas, to_xy, get_session, enabled
        self.host_combo = host_combo                    # (event) -> True when the key is a window shortcut that must not reach the program
        self.down: Set[str] = set()
        self._move_job = None
        self._pending = None
        self._buttons: Set[int] = set()
        for ev, fn in (("<ButtonPress-1>", lambda e: self.press(e, 1)), ("<ButtonPress-2>", lambda e: self.press(e, 2)),
                       ("<ButtonPress-3>", lambda e: self.press(e, 3)), ("<ButtonRelease-1>", lambda e: self.release(e, 1)),
                       ("<ButtonRelease-2>", lambda e: self.release(e, 2)), ("<ButtonRelease-3>", lambda e: self.release(e, 3)),
                       ("<Motion>", self.motion), ("<MouseWheel>", self.wheel), ("<Button-4>", lambda e: self.scroll(e, "up")),
                       ("<Button-5>", lambda e: self.scroll(e, "down")), ("<KeyPress>", self.key_down), ("<KeyRelease>", self.key_up),
                       ("<FocusOut>", lambda _e: self.release_all())):
            canvas.bind(ev, fn, add="+")

    # ------------------------------------------------------------------ helpers
    def _s(self):
        s = self.get_session()
        return s if (self.enabled() and s is not None) else None

    # ------------------------------------------------------------------ mouse
    def press(self, e, button: int) -> Optional[str]:
        self.c.focus_set()
        s = self._s()
        xy = self.to_xy(e)
        if s is None or xy is None:
            return None
        self._buttons.add(button)
        s.input_event("down", x=xy[0], y=xy[1], button=button)
        return "break"

    def release(self, e, button: int) -> Optional[str]:
        s = self._s()
        if s is None or button not in self._buttons:
            return None
        self._buttons.discard(button)
        xy = self.to_xy(e)
        if xy is not None:
            s.input_event("move", x=xy[0], y=xy[1])
        s.input_event("up", button=button)
        return "break"

    def motion(self, e) -> None:
        if self._s() is None:
            return
        xy = self.to_xy(e)
        if xy is None:
            return
        self._pending = xy
        if self._move_job is None:
            self._move_job = self.c.after(MOVE_EVERY_MS, self._flush_move)

    def _flush_move(self) -> None:
        self._move_job = None
        s, xy, self._pending = self._s(), self._pending, None
        if s is not None and xy is not None:
            s.input_event("move", x=xy[0], y=xy[1])

    def wheel(self, e) -> Optional[str]:
        s = self._s()
        if s is None or not getattr(e, "delta", 0):
            return None
        s.input_event("scroll", direction="up" if e.delta > 0 else "down", n=max(1, min(5, abs(int(e.delta)) // 120 or 1)))
        return "break"

    def scroll(self, e, direction: str) -> Optional[str]:
        s = self._s()
        if s is None:
            return None
        s.input_event("scroll", direction=direction, n=1)
        return "break"

    # ------------------------------------------------------------------ keyboard
    def key_down(self, e) -> Optional[str]:
        s = self._s()
        k = str(e.keysym)
        if s is None or k in HOST_KEYS or not KEYNAME.match(k) or (self.host_combo and self.host_combo(e)):
            return None
        self.down.add(k)
        s.input_event("keydown", key=k)
        return "break"

    def key_up(self, e) -> Optional[str]:
        s = self._s()
        k = str(e.keysym)
        if s is None or k not in self.down:
            return None
        self.down.discard(k)
        s.input_event("keyup", key=k)
        return "break"

    def release_all(self) -> None:
        s = self.get_session()
        keys, self.down = list(self.down), set()
        btns, self._buttons = list(self._buttons), set()
        if s is None:
            return
        for k in keys:
            s.input_event("keyup", key=k)
        for b in btns:
            s.input_event("up", button=b)
