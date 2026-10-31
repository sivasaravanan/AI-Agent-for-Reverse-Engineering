"""Live run events: every sandbox run publishes what it is doing so a UI can show it as it happens.

Subscribers are called on the thread that emits (often a worker or pipe-reader thread), so they must be fast and thread-safe
(the Tk view just puts the event on a queue). A failing subscriber never affects a run.
Event types: runtime | start | phase | out | err | stats | top | end.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List


class RunBus:
    def __init__(self) -> None:
        self._subs: List[Callable[[Dict], None]] = []
        self._lock = threading.Lock()
        self._cancels: Dict[str, threading.Event] = {}

    def register_cancel(self, run: str, ev: threading.Event) -> None:
        with self._lock:
            self._cancels[run] = ev

    def unregister_cancel(self, run: str) -> None:
        with self._lock:
            self._cancels.pop(run, None)

    def active(self) -> List[str]:
        """Ids of the runs that are running right now (used so clean-up never deletes the copy of a live run)."""
        with self._lock:
            return list(self._cancels)

    def request_cancel(self, run: str) -> bool:
        """Ask a running run to stop (used by the live view's Stop button). Returns False if it is not running."""
        with self._lock:
            ev = self._cancels.get(run)
        if ev is None:
            return False
        ev.set()
        return True

    def subscribe(self, cb: Callable[[Dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subs:
                    self._subs.remove(cb)
        return unsubscribe

    def emit(self, type_: str, run: str = "", **data) -> None:
        ev = {"type": type_, "run": run, "t": time.time(), **data}
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 - a broken viewer must never break a run
                pass


BUS = RunBus()
