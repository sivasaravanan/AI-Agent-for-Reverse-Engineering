"""Thread-safe in-memory finding store with grouping, filtering, search and relations."""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .models import Finding, Severity, Verification

log = logging.getLogger("findings_plugin.store")
SCHEMA = "findings/1"


class FindingStore:
    def __init__(self) -> None:
        self._items: "OrderedDict[str, Finding]" = OrderedDict()
        self._lock = threading.RLock()
        self._subs: list[Callable[[], None]] = []

    # ---- mutation -------------------------------------------------------
    def add(self, finding: Finding) -> bool:
        """Add a finding. Returns False if an identical one (same id) already exists."""
        with self._lock:
            if finding.id in self._items:
                return False
            self._items[finding.id] = finding
        self._notify()
        return True

    def add_many(self, findings: Iterable[Finding]) -> int:
        added = 0
        with self._lock:
            for f in findings:
                if f.id not in self._items:
                    self._items[f.id] = f
                    added += 1
        if added:
            self._notify()
        return added

    def remove(self, fid: str) -> bool:
        with self._lock:
            ok = self._items.pop(fid, None) is not None
        if ok:
            self._notify()
        return ok

    def clear(self, source: Optional[str] = None) -> None:
        with self._lock:
            if source is None:
                self._items.clear()
            else:
                for k in [k for k, f in self._items.items() if f.source == source]:
                    del self._items[k]
        self._notify()

    def set_verification(self, fid: str, status: Verification, evidence: str) -> None:
        """Record a verification result. Evidence (e.g. test command + outcome) is mandatory."""
        status = Verification(status)
        if status is not Verification.NOT_VERIFIED and not (evidence or "").strip():
            raise ValueError("verification evidence is required (e.g. test command and result)")
        with self._lock:
            f = self._items[fid]
            f.verification = status
            f.extra["verification_evidence"] = evidence
        self._notify()

    # ---- access ---------------------------------------------------------
    def get(self, fid: str) -> Optional[Finding]:
        with self._lock:
            return self._items.get(fid)

    def all(self) -> list[Finding]:
        with self._lock:
            return list(self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.all())

    # ---- subscriptions ----------------------------------------------------
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a change callback; returns an unsubscribe function.
        Callbacks may fire from any thread - GUI code must marshal to its own thread."""
        with self._lock:
            self._subs.append(callback)

        def _unsub() -> None:
            with self._lock:
                if callback in self._subs:
                    self._subs.remove(callback)
        return _unsub

    def _notify(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb()
            except Exception:  # a broken listener must not break the store
                log.exception("findings store listener failed")

    # ---- query ------------------------------------------------------------
    def query(self, *, min_severity: Optional[Severity] = None, severities: Optional[Iterable[Severity]] = None,
              categories: Optional[Iterable[str]] = None, languages: Optional[Iterable[str]] = None,
              files: Optional[Iterable[str]] = None, kinds: Optional[Iterable] = None,
              text: Optional[str] = None, only_detected: bool = False) -> list[Finding]:
        """Filter findings. `files` matches case-insensitive substrings of the path;
        `text` searches title, evidence, explanation, remediation, path, rule and category."""
        sevs = {Severity.parse(s) for s in severities} if severities else None
        cats = {c.lower() for c in categories} if categories else None
        langs = {l.lower() for l in languages} if languages else None
        kset = {getattr(k, "value", str(k)) for k in kinds} if kinds else None
        fparts = [p.lower().replace("\\", "/") for p in files] if files else None
        needle = text.strip().lower() if text and text.strip() else None
        out = []
        for f in self.all():
            if min_severity is not None and f.severity.rank < Severity.parse(min_severity).rank:
                continue
            if sevs and f.severity not in sevs:
                continue
            if cats and f.category not in cats:
                continue
            if langs and (f.language or "").lower() not in langs:
                continue
            if kset and f.kind.value not in kset:
                continue
            if only_detected and not f.is_detected:
                continue
            if fparts and not any(p in (f.file_path or "").lower() for p in fparts):
                continue
            if needle:
                hay = " ".join([f.title, f.evidence, f.explanation, f.remediation, f.file_path or "",
                                f.rule_id or "", f.category]).lower()
                if needle not in hay:
                    continue
            out.append(f)
        return sort_findings(out)

    def search(self, text: str) -> list[Finding]:
        return self.query(text=text)

    def group_by_severity(self, findings: Optional[list[Finding]] = None) -> "OrderedDict[Severity, list[Finding]]":
        src = sort_findings(findings if findings is not None else self.all())
        groups: "OrderedDict[Severity, list[Finding]]" = OrderedDict()
        for sev in sorted(Severity, key=lambda s: -s.rank):
            items = [f for f in src if f.severity is sev]
            if items:
                groups[sev] = items
        return groups

    def counts(self) -> dict:
        c = {s.value: 0 for s in Severity}
        for f in self.all():
            c[f.severity.value] += 1
        return c

    def categories(self) -> list[str]:
        return sorted({f.category for f in self.all()})

    def languages(self) -> list[str]:
        return sorted({f.language for f in self.all() if f.language})

    def files(self) -> list[str]:
        return sorted({f.file_path for f in self.all() if f.file_path})

    # ---- relations --------------------------------------------------------
    def related(self, fid: str, *, line_window: int = 10, limit: int = 20) -> list[tuple[Finding, str]]:
        """Findings related to `fid`, each with the reason. Ordered strongest link first."""
        base = self.get(fid)
        if base is None:
            return []
        scored: list[tuple[int, Finding, str]] = []
        for f in self.all():
            if f.id == base.id:
                continue
            if base.trace_id and f.trace_id == base.trace_id:
                scored.append((0, f, "same exception chain"))
            elif base.extra.get("related_to") == f.id or f.extra.get("related_to") == base.id:
                scored.append((0, f, "explicitly linked (AI explanation)"))
            elif base.rule_id and f.rule_id == base.rule_id and f.file_path == base.file_path:
                scored.append((1, f, "same rule in same file"))
            elif base.file_path and f.file_path == base.file_path and base.line and f.line \
                    and abs(base.line - f.line) <= line_window:
                scored.append((2, f, f"same file, within {line_window} lines"))
            elif base.rule_id and f.rule_id == base.rule_id:
                scored.append((3, f, "same rule elsewhere"))
            elif base.file_path and f.file_path == base.file_path and f.category == base.category:
                scored.append((4, f, "same file and category"))
        scored.sort(key=lambda t: (t[0], -t[1].severity.rank, t[1].file_path or "", t[1].line or 10**9))
        return [(f, why) for _, f, why in scored[:limit]]

    # ---- persistence ------------------------------------------------------
    def to_json(self, indent: int = 2) -> str:
        return json.dumps({"schema": SCHEMA, "count": len(self),
                           "findings": [f.to_dict() for f in sort_findings(self.all())]},
                          indent=indent, ensure_ascii=False)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_json(cls, text: str) -> "FindingStore":
        data = json.loads(text)
        if data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported findings schema: {data.get('schema')!r}")
        store = cls()
        store.add_many(Finding.from_dict(d) for d in data.get("findings", []))
        return store

    @classmethod
    def load(cls, path: str | Path) -> "FindingStore":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def sort_findings(items: Iterable[Finding]) -> list[Finding]:
    return sorted(items, key=lambda f: (-f.severity.rank, f.file_path or "~", f.line or 10**9, f.id))
