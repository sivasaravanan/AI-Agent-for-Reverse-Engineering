"""Splits a container's stderr into plain text and structured preview events.

The in-container helper (``runtime/ase_tracer.py``) writes one JSON object per line, each line starting with the marker
``\\x1eASE\\x1e``. Everything else is ordinary program output. Chunks arrive at arbitrary byte boundaries, so the decoder
keeps the unfinished line between calls. Standard library only.
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

MARKER = "\x1eASE\x1e"
MAX_PENDING = 1_000_000


class ProtocolDecoder:
    """feed(text) -> (plain_text, [event dicts]). Plain text keeps its original order and line endings."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> Tuple[str, List[Dict]]:
        self._buf += text
        plain: List[str] = []
        events: List[Dict] = []
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
            self._line(line, plain, events)
        # An unfinished line is plain text until a marker character shows up: show it immediately (prompts, progress bars).
        if self._buf:
            i = self._buf.find("\x1e")
            if i < 0:
                plain.append(self._buf)
                self._buf = ""
            elif i > 0:
                plain.append(self._buf[:i])
                self._buf = self._buf[i:]
            if len(self._buf) > MAX_PENDING:
                plain.append(self._buf)
                self._buf = ""
        return "".join(plain), events

    def flush(self) -> Tuple[str, List[Dict]]:
        rest, self._buf = self._buf, ""
        plain: List[str] = []
        events: List[Dict] = []
        if rest:
            self._line(rest, plain, events)
        return "".join(plain), events

    @staticmethod
    def _line(line: str, plain: List[str], events: List[Dict]) -> None:
        i = line.find(MARKER)
        if i < 0:
            plain.append(line)
            return
        if i > 0:
            plain.append(line[:i])            # text written without a newline just before the marker
        body = line[i + len(MARKER):].strip()
        try:
            ev = json.loads(body)
        except ValueError:
            plain.append(line[i:])            # not valid protocol: keep it visible rather than dropping data
            return
        if isinstance(ev, dict):
            events.append(ev)


def encode(event: Dict) -> str:
    """The exact line the in-container helper writes (used by tests and by the host side for commands)."""
    return MARKER + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
