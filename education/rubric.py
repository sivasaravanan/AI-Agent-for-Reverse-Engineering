"""Rubric definition and deterministic automatic checks.

A rubric is JSON: {"name": str, "criteria": [{"id", "title", "points", "check": {"type": ..., ...}}]}.
Check types (all static): file_exists, source_files_min, findings_max, no_secrets, manifest_present, doc_ratio_min,
hotspots_max, language_present, contains_text, manual.  ``manual`` criteria are never auto-scored.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_CRITERIA = 100
MAX_POINTS = 1000
CHECK_TYPES = ("file_exists", "source_files_min", "findings_max", "no_secrets", "manifest_present", "doc_ratio_min",
               "hotspots_max", "language_present", "contains_text", "manual")
SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".vb", ".php", ".go", ".c", ".cpp", ".h", ".hpp",
              ".rb", ".kt", ".swift", ".rs", ".sql", ".html", ".css"}


class RubricError(ValueError):
    pass


@dataclass
class Criterion:
    id: str
    title: str
    points: float
    check: Dict[str, Any]

    @property
    def manual(self) -> bool:
        return self.check.get("type") == "manual"


@dataclass
class Rubric:
    name: str
    criteria: List[Criterion] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(c.points for c in self.criteria)

    @property
    def auto_total(self) -> float:
        return sum(c.points for c in self.criteria if not c.manual)

    @property
    def manual_total(self) -> float:
        return sum(c.points for c in self.criteria if c.manual)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "criteria": [{"id": c.id, "title": c.title, "points": c.points, "check": c.check} for c in self.criteria]}


def default_rubric() -> Rubric:
    return Rubric("Default programming assignment (static checks)", [
        Criterion("readme", "README present", 10, {"type": "file_exists", "patterns": ["README*", "readme*"]}),
        Criterion("deps", "Dependencies declared", 5, {"type": "manifest_present"}),
        Criterion("tests", "Tests included", 15, {"type": "file_exists", "patterns": ["test_*.py", "*_test.*", "*.spec.*", "*.test.*", "tests/*", "test/*", "*Test.java", "*Tests.cs"]}),
        Criterion("size", "At least 3 source files", 5, {"type": "source_files_min", "min": 3}),
        Criterion("serious", "No critical/high findings", 20, {"type": "findings_max", "severities": ["critical", "high"], "max": 0, "per_extra_penalty": 5}),
        Criterion("secrets", "No hard-coded secrets", 10, {"type": "no_secrets"}),
        Criterion("docs", "Functions documented (Python, >=50%)", 10, {"type": "doc_ratio_min", "min": 0.5}),
        Criterion("complexity", "At most 2 complexity hotspots", 10, {"type": "hotspots_max", "max": 2, "per_extra_penalty": 2}),
        Criterion("correct", "Correctness / design (instructor review)", 15, {"type": "manual"}),
    ])


def _num(v: Any, what: str, lo: float = 0, hi: float = MAX_POINTS) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise RubricError(f"{what} must be a number between {lo} and {hi}")
    return float(v)


def load_rubric(source: Any) -> Rubric:
    """Load from a path, a JSON string or a dict. Raises RubricError with a clear message."""
    if isinstance(source, (str, Path)) and not (isinstance(source, str) and source.lstrip().startswith("{")):
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RubricError(f"Rubric could not be read: {exc}")
    elif isinstance(source, str):
        try:
            data = json.loads(source)
        except ValueError as exc:
            raise RubricError(f"Rubric is not valid JSON: {exc}")
    else:
        data = source
    if not isinstance(data, dict) or not isinstance(data.get("criteria"), list) or not data["criteria"]:
        raise RubricError("Rubric needs a non-empty 'criteria' list")
    if len(data["criteria"]) > MAX_CRITERIA:
        raise RubricError(f"Too many criteria (max {MAX_CRITERIA})")
    crits, seen = [], set()
    for i, c in enumerate(data["criteria"], 1):
        if not isinstance(c, dict):
            raise RubricError(f"Criterion {i} must be an object")
        cid = str(c.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", cid):
            raise RubricError(f"Criterion {i}: 'id' must be 1-40 letters, digits, _ . -")
        if cid in seen:
            raise RubricError(f"Duplicate criterion id '{cid}'")
        seen.add(cid)
        chk = c.get("check")
        if not isinstance(chk, dict) or chk.get("type") not in CHECK_TYPES:
            raise RubricError(f"Criterion '{cid}': check.type must be one of {', '.join(CHECK_TYPES)}")
        if chk["type"] == "contains_text":
            pat = chk.get("regex") or ""
            if not pat and not chk.get("text"):
                raise RubricError(f"Criterion '{cid}': contains_text needs 'text' or 'regex'")
            if len(pat) > 200:
                raise RubricError(f"Criterion '{cid}': regex longer than 200 characters")
            try:
                re.compile(pat)
            except re.error as exc:
                raise RubricError(f"Criterion '{cid}': bad regex ({exc})")
        if chk["type"] == "file_exists" and not (isinstance(chk.get("patterns"), list) and chk["patterns"]):
            raise RubricError(f"Criterion '{cid}': file_exists needs a 'patterns' list")
        crits.append(Criterion(cid, str(c.get("title") or cid)[:120], _num(c.get("points"), f"Criterion '{cid}' points"), chk))
    r = Rubric(str(data.get("name") or "Rubric")[:120], crits)
    if r.total <= 0 or r.total > MAX_POINTS:
        raise RubricError(f"Total points must be between 1 and {MAX_POINTS}")
    return r


# ------------------------------------------------------------------ evaluation
@dataclass
class Evaluation:
    criterion: str
    title: str
    max_points: float
    points: Optional[float]          # None = awaiting manual grade
    status: str                      # pass | partial | fail | manual
    evidence: str = ""


def _match_any(names: List[str], patterns: List[str]) -> List[str]:
    out = []
    for n in names:
        low = n.lower()
        base = low.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(base, p.lower()) or fnmatch.fnmatch(low, p.lower()) for p in patterns):
            out.append(n)
    return out


def _scaled(max_pts: float, over: int, per_penalty: Optional[float]) -> float:
    if over <= 0:
        return max_pts
    if per_penalty is None:
        return 0.0
    return max(0.0, max_pts - per_penalty * over)


def evaluate(rubric: Rubric, result: Dict[str, Any], names: List[str], texts: Dict[str, str]) -> List[Evaluation]:
    """``result`` is AnalysisResult.to_dict(); ``names`` all usable file names; ``texts`` the scanned text files."""
    out: List[Evaluation] = []
    findings = result.get("findings") or []
    cm = result.get("code_map") or {}
    for c in rubric.criteria:
        t, chk, mp = c.check["type"], c.check, c.points
        if t == "manual":
            out.append(Evaluation(c.id, c.title, mp, None, "manual", "Awaiting instructor grade"))
            continue
        pts, ev = 0.0, ""
        if t == "file_exists":
            hit = _match_any([n for n in names], list(chk["patterns"]))
            pts, ev = (mp, f"found {hit[0]}" + (f" (+{len(hit) - 1} more)" if len(hit) > 1 else "")) if hit else (0.0, "none found")
        elif t == "manifest_present":
            n = len(result.get("manifests") or [])
            pts, ev = (mp, f"{n} manifest file(s)") if n else (0.0, "no dependency manifest")
        elif t == "source_files_min":
            n = sum(1 for x in names if "." + x.rsplit(".", 1)[-1].lower() in SOURCE_EXT) if names else 0
            need = int(chk.get("min", 1))
            pts, ev = (mp if n >= need else 0.0), f"{n} source file(s), need {need}"
        elif t == "findings_max":
            sev = set(chk.get("severities") or ["critical", "high"])
            n = sum(1 for f in findings if f.get("severity") in sev)
            allowed = int(chk.get("max", 0))
            pts = _scaled(mp, n - allowed, chk.get("per_extra_penalty"))
            ev = f"{n} finding(s) with severity {'/'.join(sorted(sev))} (allowed {allowed})"
        elif t == "no_secrets":
            n = sum(1 for f in findings if any(k in f.get("rule_id", "").lower() for k in ("secret", "credential")))
            pts, ev = (mp if n == 0 else 0.0), f"{n} possible secret finding(s)"
        elif t == "doc_ratio_min":
            syms = [s for s in (cm.get("symbols") or []) if s.get("method") == "ast" and s.get("kind") in ("function", "method")]
            if not syms:
                pts, ev = mp, "no Python functions to check (full marks; instructor may adjust)"
            else:
                ratio = sum(1 for s in syms if s.get("doc")) / len(syms)
                pts, ev = (mp if ratio >= float(chk.get("min", 0.5)) else 0.0), f"{ratio:.0%} of {len(syms)} Python functions documented"
        elif t == "hotspots_max":
            n = len(cm.get("hotspots") or [])
            allowed = int(chk.get("max", 0))
            pts, ev = _scaled(mp, n - allowed, chk.get("per_extra_penalty")), f"{n} complexity hotspot(s) (allowed {allowed})"
        elif t == "language_present":
            want = str(chk.get("language", "")).lower()
            got = any(l.get("language", "").lower() == want for l in result.get("languages") or [])
            pts, ev = (mp if got else 0.0), f"{chk.get('language')}: {'detected' if got else 'not detected'}"
        elif t == "contains_text":
            globs = chk.get("files") or ["*"]
            pat = re.compile(chk["regex"]) if chk.get("regex") else None
            needle = chk.get("text")
            hit = None
            for n in _match_any(list(texts), list(globs)):
                body = texts[n][:300_000]
                if (pat and pat.search(body)) or (needle and needle in body):
                    hit = n
                    break
            pts, ev = (mp if hit else 0.0), (f"found in {hit}" if hit else "not found")
        status = "pass" if pts >= mp else ("partial" if pts > 0 else "fail")
        out.append(Evaluation(c.id, c.title, mp, round(pts, 2), status, ev))
    return out
