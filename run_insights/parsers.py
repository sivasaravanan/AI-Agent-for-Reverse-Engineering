"""Parse compiler / test-runner / stack-trace text into located diagnostics. Pure functions, bounded work."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_TEXT = 400_000          # characters examined per run
MAX_DIAGNOSTICS = 300
WORKSPACE_PREFIXES = ("/work/", "/workspace/", "/app/", "/src/")

Frame = Tuple[str, int, str]            # (file, line, function)


@dataclass
class Diagnostic:
    tool: str                            # python | pytest | unittest | node | dotnet | java | typescript | generic
    severity: str                        # error | warning
    message: str
    file: str = ""
    line: int = 0
    col: int = 0
    code: str = ""
    frames: List[Frame] = field(default_factory=list)      # call trace, oldest call first
    test: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["frames"] = [list(f) for f in self.frames]
        return d

    @property
    def location(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" + (f":{self.col}" if self.col else "") if self.line else self.file


def normalize_path(p: str) -> str:
    """Project-relative, forward slashes: strips the container mount, drive letters and ./ prefixes."""
    p = (p or "").strip().strip("\"'")
    p = p.replace("\\", "/")
    if p.startswith("file://"):
        p = p[7:]
    for pre in WORKSPACE_PREFIXES:
        if p.startswith(pre):
            return p[len(pre):]
    m = re.match(r"^[A-Za-z]:/(.*)$", p)
    if m:                                   # a host path: keep the tail only; matching to project files is by suffix
        return m.group(1)
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/") if p.startswith("/") and "/work/" not in p else p.split("/work/", 1)[-1]


# ---------------------------------------------------------------- Python
_PY_TB = re.compile(r"^Traceback \(most recent call last\):\s*$")
_PY_FRAME = re.compile(r'^\s+File "(?P<f>[^"]+)", line (?P<l>\d+)(?:, in (?P<fn>.+))?$')
_PY_EXC = re.compile(r"^(?P<t>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|AssertionError|KeyboardInterrupt|StopIteration)"
                     r"(?::\s*(?P<m>.*))?$")
_PYTEST_FAIL = re.compile(r"^(?P<k>FAILED|ERROR) (?P<f>[^\s:]+\.py)(?:::(?P<t>\S+))?(?: - (?P<m>.*))?$")
_PYTEST_LOC = re.compile(r"^(?P<f>[^\s:]+\.py):(?P<l>\d+): (?P<m>\S.*)$")
_UT_FAIL = re.compile(r"^(?P<k>FAIL|ERROR): (?P<t>\S+) \((?P<c>[^)]+)\)")


def _parse_python(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    i, n = 0, len(lines)
    pending_test = ""
    while i < n:
        ln = lines[i]
        mt = _UT_FAIL.match(ln)
        if mt:
            pending_test = f"{mt.group('c')}"
        if _PY_TB.match(ln):
            frames: List[Frame] = []
            j = i + 1
            while j < n:
                mf = _PY_FRAME.match(lines[j])
                if mf:
                    frames.append((normalize_path(mf.group("f")), int(mf.group("l")), (mf.group("fn") or "").strip()))
                    j += 1
                    while j < n and lines[j].startswith("    ") and not _PY_FRAME.match(lines[j]):
                        j += 1                       # source echo / caret lines
                    continue
                break
            msg_line = lines[j] if j < n else ""
            me = _PY_EXC.match(msg_line.strip()) if msg_line and not msg_line.startswith(" ") else None
            if me:
                last = frames[-1] if frames else ("", 0, "")
                out.append(Diagnostic("python", "error", (me.group("m") or "").strip() or me.group("t"), last[0], last[1],
                                      code=me.group("t"), frames=frames, test=pending_test))
                pending_test = ""
                j += 1
            i = max(j, i + 1)
            continue
        i += 1
    return out


_PY_SYNTAX = re.compile(r"^(?P<t>SyntaxError|IndentationError|TabError): (?P<m>.*)$")


def _parse_python_syntax(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    for i, ln in enumerate(lines):
        mf = _PY_FRAME.match(ln)
        if not mf or (i > 0 and lines[i - 1].strip() == "Traceback (most recent call last):"):
            continue
        for j in range(i + 1, min(i + 5, len(lines))):
            ms = _PY_SYNTAX.match(lines[j].strip())
            if ms:
                out.append(Diagnostic("python", "error", ms.group("m").strip(), normalize_path(mf.group("f")),
                                      int(mf.group("l")), code=ms.group("t")))
                break
    return out


def _parse_pytest(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    locs: Dict[str, Tuple[int, str]] = {}
    for ln in lines:
        m = _PYTEST_LOC.match(ln)
        if m and "Error" in m.group("m") or (m and "assert" in m.group("m").lower()):
            locs.setdefault(normalize_path(m.group("f")), (int(m.group("l")), m.group("m").strip()))
    for ln in lines:
        m = _PYTEST_FAIL.match(ln.strip())
        if m:
            f = normalize_path(m.group("f"))
            line, detail = locs.get(f, (0, ""))
            out.append(Diagnostic("pytest", "error", (m.group("m") or detail or m.group("k").lower()).strip(), f, line,
                                  code=m.group("k"), test=m.group("t") or ""))
    return out


# ---------------------------------------------------------------- Node / TypeScript
_NODE_AT = re.compile(r"^\s+at (?:(?P<fn>[^\s(]+(?: \[as \w+\])?) \()?(?P<f>(?:file://)?[^\s():]+(?::\\)?[^\s():]*):(?P<l>\d+):(?P<c>\d+)\)?$")
_NODE_ERR = re.compile(r"^(?P<t>(?:[A-Z]\w*)?Error)(?::\s*(?P<m>.*))?$")
_NPM_ERR = re.compile(r"^npm (?:ERR!|error) (?P<m>.+)$")
_TSC = re.compile(r"^(?P<f>[^\s(]+)\((?P<l>\d+),(?P<c>\d+)\): (?P<s>error|warning) (?P<code>TS\d+): (?P<m>.*)$")


def _parse_node(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    for i, ln in enumerate(lines):
        me = _NODE_ERR.match(ln.strip()) if not ln.startswith(" ") else None
        if not me:
            continue
        frames: List[Frame] = []
        j = i + 1
        while j < len(lines) and lines[j].startswith("    at "):
            ma = _NODE_AT.match(lines[j])
            if ma:
                f = normalize_path(ma.group("f"))
                if "node_modules/" not in f and not f.startswith("node:"):
                    frames.append((f, int(ma.group("l")), ma.group("fn") or ""))
            j += 1
        if j > i + 1:
            frames.reverse()                          # node prints newest first; present oldest first like Python
            last = frames[-1] if frames else ("", 0, "")
            first_user = next((fr for fr in reversed(frames)), last)
            out.append(Diagnostic("node", "error", (me.group("m") or me.group("t") or "").strip(), first_user[0], first_user[1],
                                  code=me.group("t"), frames=frames))
    return out


def _parse_tsc(lines: List[str]) -> List[Diagnostic]:
    out = []
    for ln in lines:
        m = _TSC.match(ln.strip())
        if m:
            out.append(Diagnostic("typescript", m.group("s"), m.group("m").strip(), normalize_path(m.group("f")),
                                  int(m.group("l")), int(m.group("c")), code=m.group("code")))
    return out


# ---------------------------------------------------------------- .NET / MSBuild
_MSBUILD = re.compile(r"^(?P<f>.+?)\((?P<l>\d+)(?:,(?P<c>\d+))?(?:,\d+,\d+)?\): (?P<s>error|warning) (?P<code>[A-Za-z]+\d+): (?P<m>.*?)(?: \[(?P<p>[^\]]+)\])?$")
_MSBUILD_NOLOC = re.compile(r"^(?:\S+ : )?(?P<s>error|warning) (?P<code>(?:NU|NETSDK|MSB)\d+): (?P<m>.*?)(?: \[(?P<p>[^\]]+)\])?$")


def _parse_dotnet(lines: List[str]) -> List[Diagnostic]:
    out, seen = [], set()
    for ln in lines:
        s = ln.strip()
        m = _MSBUILD.match(s)
        if m:
            key = (m.group("f"), m.group("l"), m.group("code"))
            if key not in seen:                         # MSBuild repeats every diagnostic in its summary
                seen.add(key)
                out.append(Diagnostic("dotnet", m.group("s"), m.group("m").strip(), normalize_path(m.group("f")),
                                      int(m.group("l")), int(m.group("c") or 0), code=m.group("code")))
            continue
        m = _MSBUILD_NOLOC.match(s)
        if m:
            key = (m.group("p") or "", m.group("code"), m.group("m"))
            if key not in seen:
                seen.add(key)
                out.append(Diagnostic("dotnet", m.group("s"), m.group("m").strip(), normalize_path(m.group("p") or ""), 0,
                                      code=m.group("code")))
    return out


# ---------------------------------------------------------------- Java
_JAVAC = re.compile(r"^(?P<f>[^\s:]+\.java):(?P<l>\d+): (?P<s>error|warning): (?P<m>.*)$")
_MVN = re.compile(r"^\[(?P<s>ERROR|WARNING)\]\s+(?P<f>\S+\.java):\[(?P<l>\d+),(?P<c>\d+)\]\s*(?P<m>.*)$")
_JAVA_AT = re.compile(r"^\s+at (?P<fn>[\w.$<>]+)\((?P<f>[\w$]+\.java):(?P<l>\d+)\)$")
_JAVA_EXC = re.compile(r"^(?:Exception in thread \"[^\"]+\" )?(?P<t>(?:[a-z_][\w]*\.)+[A-Z]\w*(?:Exception|Error))(?::\s*(?P<m>.*))?$")


def _parse_java(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        m = _JAVAC.match(s)
        if m:
            out.append(Diagnostic("java", m.group("s"), m.group("m").strip(), normalize_path(m.group("f")), int(m.group("l"))))
            continue
        m = _MVN.match(s)
        if m:
            out.append(Diagnostic("java", "error" if m.group("s") == "ERROR" else "warning", m.group("m").strip(),
                                  normalize_path(m.group("f")), int(m.group("l")), int(m.group("c"))))
            continue
        me = _JAVA_EXC.match(s) if not ln.startswith((" ", "\t")) else None
        if me:
            frames: List[Frame] = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("at "):
                ma = _JAVA_AT.match(lines[j].replace("\t", "    "))
                if ma:
                    frames.append((ma.group("f"), int(ma.group("l")), ma.group("fn")))
                j += 1
            if frames:
                frames.reverse()
                out.append(Diagnostic("java", "error", (me.group("m") or me.group("t")).strip(), frames[-1][0], frames[-1][1],
                                      code=me.group("t"), frames=frames))
    return out


# ---------------------------------------------------------------- generic  file:line:col: error: message (gcc, clang, go, rust-ish)
_GENERIC = re.compile(r"^(?P<f>[^\s:()]+\.\w{1,6}):(?P<l>\d+)(?::(?P<c>\d+))?: (?P<s>error|warning|fatal error)[: ]\s*(?P<m>.+)$")


def _parse_generic(lines: List[str], known: set) -> List[Diagnostic]:
    out = []
    for ln in lines:
        m = _GENERIC.match(ln.strip())
        if m:
            f = normalize_path(m.group("f"))
            if (f, int(m.group("l"))) in known or f.endswith(".java") or f.endswith(".py"):
                continue
            out.append(Diagnostic("generic", "warning" if m.group("s") == "warning" else "error", m.group("m").strip(), f,
                                  int(m.group("l")), int(m.group("c") or 0)))
    return out


def parse_output(stdout: str, stderr: str = "") -> List[Diagnostic]:
    """All located diagnostics found in a run's output, de-duplicated, errors first."""
    text = ((stdout or "") + "\n" + (stderr or ""))[-MAX_TEXT:]
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    found: List[Diagnostic] = []
    found += _parse_python(lines)
    found += [d for d in _parse_python_syntax(lines) if not any(x.file == d.file and x.line == d.line for x in found)]
    found += _parse_pytest(lines)
    found += _parse_node(lines)
    found += _parse_tsc(lines)
    found += _parse_dotnet(lines)
    found += _parse_java(lines)
    found += _parse_generic(lines, {(d.file, d.line) for d in found})
    out, seen = [], set()
    for d in found:
        key = (d.tool, d.file, d.line, d.code, d.message[:80], d.test)
        if key not in seen:
            seen.add(key)
            out.append(d)
    out.sort(key=lambda d: (d.severity != "error",))
    return out[:MAX_DIAGNOSTICS]
