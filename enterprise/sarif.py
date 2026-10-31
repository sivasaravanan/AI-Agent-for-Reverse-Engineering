"""SARIF 2.1.0 export so findings show up in GitHub code scanning, Azure DevOps and similar CI dashboards."""
from __future__ import annotations

from typing import Any, Dict

from core import __version__
from models.analysis_result import AnalysisResult

_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}


def to_sarif(result: AnalysisResult) -> Dict[str, Any]:
    rules, seen, results = [], set(), []
    for f in result.findings:
        if f.rule_id not in seen:
            seen.add(f.rule_id)
            rules.append({"id": f.rule_id, "name": f.rule_id, "shortDescription": {"text": f.title[:200]}})
        r: Dict[str, Any] = {"ruleId": f.rule_id, "level": _LEVEL.get(f.severity, "note"), "message": {"text": f.title[:500]},
                             "properties": {"confidence": f.confidence, "verification": f.verification, "source": f.source}}
        if f.file:
            loc: Dict[str, Any] = {"physicalLocation": {"artifactLocation": {"uri": f.file}}}
            if f.line:
                loc["physicalLocation"]["region"] = {"startLine": int(f.line)}
            r["locations"] = [loc]
        results.append(r)
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "AI Software Engineering Agent", "version": __version__, "rules": rules}}, "results": results}]}
