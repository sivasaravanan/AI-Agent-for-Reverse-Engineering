"""Log / traceback / test-output analysis (Phase 6).

Parses Python, Java, .NET and Node.js stack traces, pytest FAILED lines and generic
ERROR/CRITICAL log lines. Every file/line value comes from the text itself; if the
text does not contain a location the field stays None. Parsed log lines are redacted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .knowledge import enrich
from .models import Category, Confidence, Finding, Kind, Severity
from .util import detect_language, redact

MAX_EVIDENCE = 4000

_PY_FRAME = re.compile(r'^\s*File "(?P<file>.+?)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
_EXC_LINE = re.compile(r'^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$')
_STD_PY = re.compile(r"/(?:lib/python[\d.]*|python[\d.]*/lib)/")
_CHAIN = ("direct cause of the following exception", "During handling of the above exception")

_FRAME_ANY = re.compile(r"^\s+at\s+\S")
_HDR = re.compile(r'^(?:Unhandled [Ee]xception[.:]\s*)?(?:Exception in thread "[^"]*"\s+)?'
                  r'(?P<type>[A-Za-z_][\w.$]*)(?::\s*(?P<msg>.*))?\s*$')
_CONT = re.compile(r"^(?:\s+at\s|\s*\.\.\. \d+ (?:more|common)|Caused by:|\s*--- End of|\s*Suppressed:)")
_DOTNET = re.compile(r"^\s+at\s+(?P<sym>.+?)\s+in\s+(?P<file>.+?):line\s+(?P<line>\d+)\s*$")
_NODE = re.compile(r"^\s+at\s+(?:(?P<sym>.+?)\s+\()?(?P<file>[^()]+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_JAVA = re.compile(r"^\s+at\s+(?P<sym>[\w$.<>/]+)\((?P<file>[\w$.\-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)\s*$")

_PYTEST = re.compile(r"^(?P<what>FAILED|ERROR)\s+(?P<path>[^\s:]+\.py)(?:::(?P<name>\S+))?(?:\s+-\s+(?P<msg>.*))?$")
_LEVEL = re.compile(r"(?<![A-Za-z_])(?P<lvl>CRITICAL|FATAL|ERROR)(?![A-Za-z_])[\]\s:|\-]*(?P<msg>\S.*)?$")
_PATH_IN_TEXT = re.compile(r"(?P<file>(?:[A-Za-z]:)?[\w./\\\-]+\.(?:py|js|jsx|ts|tsx|cs|vb|java|php|go|sql|kt))[:(](?P<line>\d+)")
_EXC_IN_TEXT = re.compile(r"\b([A-Za-z_][\w.]*(?:Error|Exception))\b")


@dataclass
class LogAnalysis:
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    line_count: int = 0
    truncated: bool = False


def _trim(s: str, n: int = MAX_EVIDENCE) -> str:
    return s if len(s) <= n else s[:n] + "\n... [evidence truncated]"


def _short(s: str, n: int = 200) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class _Collector:
    def __init__(self, source_name: str, max_findings: int) -> None:
        self.source_name, self.max = source_name, max_findings
        self.items: "dict[str, Finding]" = {}
        self.over_limit = False

    def add(self, f: Finding, log_line: int) -> None:
        f.extra["log_name"] = self.source_name
        f.extra["log_line"] = log_line
        prev = self.items.get(f.id)
        if prev is not None:
            prev.extra["occurrences"] = prev.extra.get("occurrences", 1) + 1
            return
        if len(self.items) >= self.max:
            self.over_limit = True
            return
        f.extra["occurrences"] = 1
        self.items[f.id] = f


def _py_external(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return "site-packages" in p or "dist-packages" in p or p.startswith("<") or bool(_STD_PY.search(p))


def _scan_python(lines, consumed, col: _Collector, notes) -> None:
    n, i = len(lines), 0
    last_end, last_trace = -1, None
    while i < n:
        if i in consumed or not lines[i].strip().startswith("Traceback (most recent call last):"):
            i += 1
            continue
        j, frames = i + 1, []
        while j < n:
            m = _PY_FRAME.match(lines[j])
            if m:
                frames.append(m)
                j += 1
            elif lines[j].startswith((" ", "\t")):
                j += 1
            else:
                break
        em = _EXC_LINE.match(lines[j].rstrip()) if j < n else None
        if not em:
            notes.append(f"Truncated traceback starting at log line {i + 1}: exception line missing.")
            consumed.update(range(i, j))
            i = max(j, i + 1)
            continue
        exc, msg = em.group("type"), em.group("msg") or ""
        between = "\n".join(lines[last_end + 1:i]) if last_end >= 0 else ""
        chained = last_trace is not None and any(p in between for p in _CHAIN)
        trace_id = last_trace if chained else f"{col.source_name}#L{i + 1}"
        last_trace, last_end = trace_id, j
        pick = None
        for m in reversed(frames):
            if not _py_external(m.group("file")):
                pick = m
                break
        if pick is None and frames:
            pick = frames[-1]
        file_path = pick.group("file") if pick else None
        line_no = int(pick.group("line")) if pick else None
        if line_no is not None and line_no < 1:
            line_no = None
        short = exc.split(".")[-1]
        parser = short in ("SyntaxError", "IndentationError", "TabError")
        excerpt = redact("\n".join(lines[i:j + 1]))
        f = Finding(title=_short(redact(f"{exc}: {msg}" if msg else exc)), severity=Severity.HIGH,
                    category=Category.SYNTAX if parser else Category.RUNTIME,
                    kind=Kind.PARSER_ERROR if parser else Kind.RUNTIME_ERROR,
                    file_path=file_path, line=line_no, evidence=_trim(excerpt),
                    source="log-analyzer", confidence=Confidence.CONFIRMED,
                    language=detect_language(file_path) or "python", rule_id=exc, trace_id=trace_id)
        if pick is not None and _py_external(file_path):
            f.extra["frame_is_external"] = True
        enrich(f, exc)
        if f.debug_steps and f.file_path and f.line:
            f.debug_steps.insert(0, f"Open {f.file_path} at line {f.line} (innermost frame in the log).")
        col.add(f, i + 1)
        consumed.update(range(i, j + 1))
        i = j + 1

    # stand-alone SyntaxError blocks without a Traceback header
    for k in range(n):
        if k in consumed:
            continue
        m = re.match(r"^(SyntaxError|IndentationError|TabError)\b:?\s*(.*)$", lines[k])
        if not m:
            continue
        fm, lo = None, max(0, k - 4)
        for b in range(k - 1, lo - 1, -1):
            fm = _PY_FRAME.match(lines[b])
            if fm:
                lo = b
                break
        file_path = fm.group("file") if fm else None
        line_no = int(fm.group("line")) if fm else None
        f = Finding(title=_short(redact(f"{m.group(1)}: {m.group(2)}".strip(": "))), severity=Severity.HIGH,
                    category=Category.SYNTAX, kind=Kind.PARSER_ERROR, file_path=file_path,
                    line=line_no if line_no and line_no > 0 else None,
                    evidence=_trim(redact("\n".join(lines[lo:k + 1]))), source="log-analyzer",
                    confidence=Confidence.CONFIRMED, language=detect_language(file_path) or "python",
                    rule_id=m.group(1))
        enrich(f, m.group(1))
        col.add(f, k + 1)
        consumed.update(range(lo, k + 1))


def _parse_frame(line: str):
    """Return (runtime, symbol, file, line, col) or None for unparseable frames."""
    m = _DOTNET.match(line)
    if m:
        return "dotnet", m.group("sym"), m.group("file"), int(m.group("line")), None
    m = _NODE.match(line)
    if m:
        return "node", m.group("sym") or "", m.group("file"), int(m.group("line")), int(m.group("col"))
    m = _JAVA.match(line)
    if m:
        return "java", m.group("sym"), m.group("file"), int(m.group("line")), None
    return None


def _frame_external(runtime: str, sym: str, file: str) -> bool:
    if runtime == "java":
        return sym.startswith(("java.", "javax.", "jdk.", "sun.", "com.sun.", "org.junit."))
    if runtime == "dotnet":
        return sym.startswith(("System.", "Microsoft."))
    f = file.replace("\\", "/")
    return "node_modules" in f or f.startswith("node:") or "internal/" in f


_RT_LANG = {"java": "java", "dotnet": "csharp", "node": "javascript"}


def _scan_stack_traces(lines, consumed, col: _Collector) -> None:
    n, i = len(lines), 0
    while i < n:
        if i in consumed or i + 1 >= n or not _FRAME_ANY.match(lines[i + 1]):
            i += 1
            continue
        hm = _HDR.match(lines[i].rstrip())
        if not hm or not hm.group("type").split(".")[-1].endswith(("Exception", "Error", "Throwable")):
            i += 1
            continue
        j = i + 1
        while j < n and j not in consumed and _CONT.match(lines[j]):
            j += 1
        block = lines[i:j]
        top = []
        for ln in block[1:]:
            if ln.startswith("Caused by:"):
                break
            if _FRAME_ANY.match(ln):
                p = _parse_frame(ln)
                if p:
                    top.append(p)
        pick = next((p for p in top if not _frame_external(p[0], p[1], p[2])), top[0] if top else None)
        exc, msg = hm.group("type"), hm.group("msg") or ""
        file_path = pick[2] if pick else None
        runtime = pick[0] if pick else None
        causes = [ln.strip() for ln in block if ln.startswith("Caused by:")]
        f = Finding(title=_short(redact(f"{exc}: {msg}" if msg else exc)), severity=Severity.HIGH,
                    category=Category.RUNTIME, kind=Kind.RUNTIME_ERROR, file_path=file_path,
                    line=pick[3] if pick else None, column=pick[4] if pick and pick[4] else None,
                    evidence=_trim(redact("\n".join(block))), source="log-analyzer",
                    confidence=Confidence.CONFIRMED,
                    language=detect_language(file_path) or _RT_LANG.get(runtime or ""), rule_id=exc,
                    trace_id=f"{col.source_name}#L{i + 1}")
        if causes:
            f.extra["caused_by"] = [redact(c) for c in causes][:5]
        if pick is not None and _frame_external(*pick[:3]):
            f.extra["frame_is_external"] = True
        enrich(f, exc)
        if f.debug_steps and f.file_path and f.line:
            f.debug_steps.insert(0, f"Open {f.file_path} at line {f.line} (first application frame in the stack).")
        col.add(f, i + 1)
        consumed.update(range(i, j))
        i = j


def _scan_pytest_and_levels(lines, consumed, col: _Collector) -> None:
    for k, raw in enumerate(lines):
        if k in consumed:
            continue
        line = raw.rstrip()
        pm = _PYTEST.match(line)
        if pm:
            msg = redact(pm.group("msg") or "")
            failed = pm.group("what") == "FAILED"
            title = f"{'Test failed' if failed else 'Test error'}: {pm.group('name') or pm.group('path')}"
            if msg:
                title += f" - {msg}"
            f = Finding(title=_short(title), severity=Severity.HIGH, category=Category.TEST,
                        kind=Kind.TEST_FAILURE, file_path=pm.group("path"), line=None,
                        evidence=_trim(redact(line)), source="log-analyzer", confidence=Confidence.CONFIRMED,
                        language="python", rule_id="pytest-" + pm.group("what").lower())
            enrich(f, (msg.split(":")[0].strip() if msg else None))
            col.add(f, k + 1)
            continue
        lm = _LEVEL.search(line)
        if not lm or not (lm.group("msg") or "").strip():
            continue
        msg = redact(lm.group("msg").strip())
        pth = _PATH_IN_TEXT.search(msg)
        file_path = pth.group("file") if pth else None
        line_no = int(pth.group("line")) if pth and int(pth.group("line")) >= 1 else None
        crit = lm.group("lvl") in ("CRITICAL", "FATAL")
        f = Finding(title=_short(msg), severity=Severity.HIGH if crit else Severity.MEDIUM,
                    category=Category.RUNTIME, kind=Kind.RUNTIME_ERROR, file_path=file_path, line=line_no,
                    evidence=_trim(redact(line)), source="log-analyzer", confidence=Confidence.STRONG,
                    language=detect_language(file_path), rule_id=f"log-{lm.group('lvl').lower()}")
        em = _EXC_IN_TEXT.search(msg)
        if em:
            enrich(f, em.group(1))
        col.add(f, k + 1)


def analyze_log_text(text: str, source_name: str = "log", *, max_findings: int = 500) -> LogAnalysis:
    """Analyze log/console text and return structured findings (nothing is executed)."""
    lines = text.splitlines()
    res = LogAnalysis(line_count=len(lines))
    col, consumed = _Collector(source_name, max_findings), set()
    _scan_python(lines, consumed, col, res.notes)
    _scan_stack_traces(lines, consumed, col)
    _scan_pytest_and_levels(lines, consumed, col)
    res.findings = list(col.items.values())
    if col.over_limit:
        res.truncated = True
        res.notes.append(f"Stopped at {max_findings} unique findings; the log contains more.")
    if not res.findings:
        res.notes.append("No errors recognised. Absence of findings does not prove the run was healthy.")
    return res


def analyze_log_file(path: str | Path, *, max_bytes: int = 5_000_000) -> LogAnalysis:
    """Read a log file (tail only if huge; UTF-8/UTF-16 aware) and analyze it."""
    p = Path(path)
    size = p.stat().st_size
    with p.open("rb") as fh:
        head = fh.read(4)
        skipped = size > max_bytes
        if skipped:
            fh.seek(size - max_bytes)
        else:
            fh.seek(0)
        data = fh.read()
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    note = None
    if skipped:
        text = text.split("\n", 1)[-1]  # drop the partial first line
        note = f"Log is {size:,} bytes; only the last {max_bytes:,} bytes were analysed."
    res = analyze_log_text(text, p.name)
    if note:
        res.truncated = True
        res.notes.insert(0, note)
    return res
