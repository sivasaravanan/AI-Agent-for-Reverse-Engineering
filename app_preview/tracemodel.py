"""Aggregates the trace events of one run: which lines ran how often, per-function call counts, a bounded timeline and the
position that is executing right now. Pure data, no Tk."""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

MAX_FILES = 2000
MAX_FUNCS = 20000


class TraceModel:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.hits: Dict[str, Dict[int, int]] = {}
        self.funcs: Dict[Tuple[str, str], List[int]] = {}          # (file, function) -> [calls, lines executed]
        self.timeline: Deque[Dict] = deque(maxlen=4000)
        self.cur: Optional[Tuple[str, int, str]] = None
        self.depth = 0
        self.events = 0
        self.errors: List[Dict] = []
        self.dirty = False

    def add_batch(self, items: List[Dict]) -> None:
        for it in items:
            self.events += 1
            k, f, l, fn = it.get("k"), it.get("f") or "", int(it.get("l") or 0), it.get("fn") or ""
            if not f:
                continue
            if k == "line":
                h = self.hits.get(f)
                if h is None:
                    if len(self.hits) >= MAX_FILES:
                        continue
                    h = self.hits[f] = {}
                h[l] = h.get(l, 0) + 1
                row = self.funcs.get((f, fn))
                if row is not None:
                    row[1] += 1
                self.cur = (f, l, fn)
            elif k == "call":
                row = self.funcs.get((f, fn))
                if row is None and len(self.funcs) < MAX_FUNCS:
                    row = self.funcs[(f, fn)] = [0, 0]
                if row is not None:
                    row[0] += 1
                self.depth = int(it.get("d") or self.depth + 1)
                self.timeline.append({"k": "call", "f": f, "l": l, "fn": fn, "d": self.depth, "args": it.get("args") or {}})
            elif k == "ret":
                self.timeline.append({"k": "ret", "f": f, "l": l, "fn": fn, "d": int(it.get("d") or self.depth), "v": it.get("v", "")})
                self.depth = max(0, self.depth - 1)
            elif k == "exc":
                e = {"k": "exc", "f": f, "l": l, "fn": fn, "e": it.get("e", ""), "et": it.get("et", "")}
                self.timeline.append(e)
                if len(self.errors) < 200:
                    self.errors.append(e)
        self.dirty = True

    def hot_lines(self, file: str) -> Dict[int, int]:
        return self.hits.get(file, {})

    def top_functions(self, n: int = 200) -> List[Tuple[str, str, int, int]]:
        rows = [(f, fn, c, ln) for (f, fn), (c, ln) in self.funcs.items()]
        rows.sort(key=lambda r: (-r[3], -r[2]))
        return rows[:n]
