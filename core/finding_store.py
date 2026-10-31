"""Finding store and queries (Phase 6), wrapping ``findings_plugin.FindingStore``.

Accepts main-app ``models.finding.Finding`` objects (and dicts / SyntaxError / str via the plugin adapters).
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional

from findings_plugin import FindingStore as _Store
from findings_plugin import Severity, Verification, coerce_many


class FindingStore:
    def __init__(self, findings: Optional[Iterable[Any]] = None, default_source: str = "analyzer"):
        self.store = _Store()
        self.errors: list = []
        self.findings = []
        if findings:
            self.add(findings, default_source)

    def add(self, items: Iterable[Any], source: str = "analyzer") -> int:
        converted, self.errors = coerce_many(list(items), default_source=source)
        n = self.store.add_many(converted)
        self.findings = self.store.all()
        return n

    def __len__(self) -> int:
        return len(self.store)

    def group_by_severity(self):
        return self.store.group_by_severity()

    def filter(self, file: Optional[str] = None, category: Optional[str] = None, language: Optional[str] = None,
               severities: Optional[Iterable[str]] = None):
        sev = [Severity.parse(s) for s in severities] if severities else None
        return self.store.query(files=[file] if file else None, categories=[category] if category else None,
                                languages=[language] if language else None, severities=sev)

    def search(self, text: str):
        return self.store.search(text)

    def related(self, finding_id: str):
        return self.store.related(finding_id)

    def counts(self) -> dict:
        return self.store.counts()

    def set_verification(self, fid: str, status: Verification, evidence: str) -> None:
        self.store.set_verification(fid, status, evidence)
