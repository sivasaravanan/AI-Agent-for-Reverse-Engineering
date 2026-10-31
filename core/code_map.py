"""Function / class / module map and call graph (master prompt item 8).

* Python: exact, from the AST (classes, methods, functions, parameters, decorators, cyclomatic-style complexity, calls).
* JavaScript/TypeScript, Java, C#, PHP, Go: regex-based symbol extraction (names and lines only; NO call graph).
Every symbol records ``method`` = ast | regex so the report never presents a regex guess as a parse.
Calls are resolved only when the callee name is unique among the project's symbols; otherwise they stay unresolved.
Nothing is imported or executed.
"""
from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional

MAX_SYMBOLS = 20_000
MAX_CALLS = 30_000
LONG_FUNCTION_LINES = 80
COMPLEX_THRESHOLD = 12


@dataclass
class Symbol:
    file: str
    kind: str              # class | function | method | interface | struct | enum | trait
    name: str
    qualname: str
    line: int
    end_line: Optional[int] = None
    language: str = ""
    method: str = "regex"  # ast | regex
    params: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    complexity: Optional[int] = None
    doc: bool = False


@dataclass
class Call:
    file: str
    line: int
    caller: str
    callee: str
    resolved: str = ""     # qualname of the unique matching project symbol, else ""


@dataclass
class CodeMap:
    symbols: List[Symbol] = field(default_factory=list)
    calls: List[Call] = field(default_factory=list)
    modules: List[Dict[str, Any]] = field(default_factory=list)       # per file: counts, lines
    hotspots: List[Dict[str, Any]] = field(default_factory=list)      # long / complex functions
    unreferenced: List[Dict[str, Any]] = field(default_factory=list)  # Python functions never called by name (weak hint)
    limitations: List[str] = field(default_factory=lambda: [
        "Python is parsed with the AST; other languages use regex name extraction only (no call graph).",
        "Calls are matched by simple name. Dynamic dispatch, decorators, reflection and star-imports are not resolved.",
        "'Unreferenced' is a weak hint: functions used via frameworks, tests, entry points or reflection look unreferenced.",
    ])
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"symbols": [asdict(s) for s in self.symbols], "calls": [asdict(c) for c in self.calls],
                "modules": self.modules, "hotspots": self.hotspots, "unreferenced": self.unreferenced,
                "limitations": self.limitations, "truncated": self.truncated,
                "summary": self.summary()}

    def summary(self) -> Dict[str, Any]:
        kinds = Counter(s.kind for s in self.symbols)
        return {"symbols": len(self.symbols), "by_kind": dict(kinds), "calls": len(self.calls),
                "resolved_calls": sum(1 for c in self.calls if c.resolved), "files": len(self.modules),
                "hotspots": len(self.hotspots)}


# ------------------------------------------------------------------------------------------------ Python (AST)
_BRANCH = (ast.If, ast.For, ast.While, ast.AsyncFor, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp,
           ast.Assert, ast.comprehension)


def _complexity(fn: ast.AST) -> int:
    n = 1
    for node in ast.walk(fn):
        if isinstance(node, _BRANCH):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.Try):
            n += len(node.handlers)
    return n


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, file: str, cm: CodeMap):
        self.file, self.cm = file, cm
        self.stack: List[str] = []
        self.kinds: List[str] = []

    def _qual(self, name: str) -> str:
        return ".".join([*self.stack, name]) if self.stack else name

    def _func(self, node) -> None:
        in_class = bool(self.kinds) and self.kinds[-1] == "class"
        params = [a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]
        if node.args.vararg:
            params.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            params.append("**" + node.args.kwarg.arg)
        sym = Symbol(self.file, "method" if in_class else "function", node.name, self._qual(node.name), node.lineno,
                     getattr(node, "end_lineno", None), "Python", "ast", params,
                     [_name_of(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list],
                     _complexity(node), ast.get_docstring(node) is not None)
        if len(self.cm.symbols) < MAX_SYMBOLS:
            self.cm.symbols.append(sym)
        else:
            self.cm.truncated = True
        for sub in ast.walk(node):          # calls made directly by this function (nested defs attribute to themselves later)
            pass
        self.stack.append(node.name)
        self.kinds.append("function")
        self._collect_calls(node, sym.qualname)
        self.generic_visit(node)
        self.kinds.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if len(self.cm.symbols) < MAX_SYMBOLS:
            self.cm.symbols.append(Symbol(self.file, "class", node.name, self._qual(node.name), node.lineno,
                                          getattr(node, "end_lineno", None), "Python", "ast",
                                          [_name_of(b) for b in node.bases if _name_of(b)],
                                          [_name_of(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list],
                                          None, ast.get_docstring(node) is not None))
        self.stack.append(node.name)
        self.kinds.append("class")
        self.generic_visit(node)
        self.kinds.pop()
        self.stack.pop()

    def _collect_calls(self, fn, caller: str) -> None:
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call):
                callee = _name_of(n.func)
                if callee and len(self.cm.calls) < MAX_CALLS:
                    self.cm.calls.append(Call(self.file, n.lineno, caller, callee))
                elif callee:
                    self.cm.truncated = True
            stack.extend(ast.iter_child_nodes(n))


# ------------------------------------------------------------------------------------------------ other languages
_RX: Dict[str, List[tuple]] = {
    "JavaScript": [("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
                   ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.M)),
                   ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M))],
    "Java": [("class", re.compile(r"^\s*(?:public|protected|private|abstract|final|static|\s)*\b(?:class|record)\s+([A-Za-z_]\w*)", re.M)),
             ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+([A-Za-z_]\w*)", re.M)),
             ("enum", re.compile(r"^\s*(?:public\s+)?enum\s+([A-Za-z_]\w*)", re.M)),
             ("method", re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?[\w<>\[\],.? ]+?\s+([a-z_]\w*)\s*\([^;{)]*\)\s*(?:throws\s+[\w., ]+)?\{", re.M))],
    "C#": [("class", re.compile(r"^\s*(?:public|internal|protected|private|static|abstract|sealed|partial|\s)*\b(?:class|record)\s+([A-Za-z_]\w*)", re.M)),
           ("interface", re.compile(r"^\s*(?:public|internal)?\s*interface\s+([A-Za-z_]\w*)", re.M)),
           ("struct", re.compile(r"^\s*(?:public|internal)?\s*struct\s+([A-Za-z_]\w*)", re.M)),
           ("enum", re.compile(r"^\s*(?:public|internal)?\s*enum\s+([A-Za-z_]\w*)", re.M)),
           ("method", re.compile(r"^\s*(?:public|protected|private|internal)\s+(?:static\s+|virtual\s+|override\s+|async\s+|abstract\s+)*[\w<>\[\],.? ]+?\s+([A-Z_]\w*)\s*\([^;{)]*\)\s*\{?\s*$", re.M))],
    "VB.NET": [("class", re.compile(r"^\s*(?:Public|Friend|Private)?\s*(?:Partial\s+)?Class\s+(\w+)", re.M | re.I)),
               ("function", re.compile(r"^\s*(?:Public|Private|Friend|Protected)?\s*(?:Shared\s+)?(?:Function|Sub)\s+(\w+)", re.M | re.I))],
    "PHP": [("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)", re.M)),
            ("interface", re.compile(r"^\s*interface\s+([A-Za-z_]\w*)", re.M)),
            ("trait", re.compile(r"^\s*trait\s+([A-Za-z_]\w*)", re.M)),
            ("function", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|static\s+)*function\s+&?([A-Za-z_]\w*)\s*\(", re.M))],
    "Go": [("struct", re.compile(r"^type\s+([A-Za-z_]\w*)\s+struct\b", re.M)),
           ("interface", re.compile(r"^type\s+([A-Za-z_]\w*)\s+interface\b", re.M)),
           ("function", re.compile(r"^func\s+([A-Za-z_]\w*)\s*\(", re.M)),
           ("method", re.compile(r"^func\s+\([^)]*\)\s*([A-Za-z_]\w*)\s*\(", re.M))],
}
_RX["TypeScript"] = _RX["JavaScript"]
_EXT = {".py": "Python", ".pyw": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
        ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".cs": "C#", ".vb": "VB.NET", ".php": "PHP", ".go": "Go"}


def _lang(path: str) -> str:
    dot = path.rfind(".")
    return _EXT.get(path[dot:].lower(), "") if dot >= 0 else ""


def build_code_map(texts: Mapping[str, str]) -> CodeMap:
    cm = CodeMap()
    for path in sorted(texts):
        lang = _lang(path)
        if not lang:
            continue
        text = texts[path]
        lines = text.count("\n") + 1
        before = len(cm.symbols)
        if lang == "Python":
            try:
                tree = ast.parse(text, filename=path)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                cm.modules.append({"file": path, "language": lang, "lines": lines, "symbols": 0, "note": "not parseable (syntax error)"})
                continue
            _PyVisitor(path, cm).visit(tree)
        else:
            for kind, rx in _RX.get(lang, []):
                for m in rx.finditer(text):
                    if len(cm.symbols) >= MAX_SYMBOLS:
                        cm.truncated = True
                        break
                    line = text.count("\n", 0, m.start(1)) + 1
                    name = m.group(1)
                    if kind == "method" and name in ("if", "for", "while", "switch", "catch", "return", "new"):
                        continue
                    cm.symbols.append(Symbol(path, kind, name, name, line, None, lang, "regex"))
        cm.modules.append({"file": path, "language": lang, "lines": lines, "symbols": len(cm.symbols) - before})
    _resolve(cm)
    return cm


def _resolve(cm: CodeMap) -> None:
    by_name: Dict[str, List[Symbol]] = defaultdict(list)
    for s in cm.symbols:
        if s.method == "ast" and s.kind in ("function", "method", "class"):
            by_name[s.name].append(s)
    called = set()
    for c in cm.calls:
        cands = by_name.get(c.callee, [])
        if len(cands) == 1:
            c.resolved = f"{cands[0].file}::{cands[0].qualname}"
        if cands:
            called.add(c.callee)
    for s in cm.symbols:
        if s.method == "ast" and s.kind in ("function", "method"):
            size = (s.end_line or s.line) - s.line + 1
            if size >= LONG_FUNCTION_LINES or (s.complexity or 0) >= COMPLEX_THRESHOLD:
                cm.hotspots.append({"file": s.file, "name": s.qualname, "line": s.line, "lines": size, "complexity": s.complexity})
            if (s.name not in called and not s.name.startswith("_") and not s.name.startswith("test")
                    and "route" not in s.decorators and not s.decorators):
                cm.unreferenced.append({"file": s.file, "name": s.qualname, "line": s.line})
    cm.hotspots.sort(key=lambda h: (-(h["complexity"] or 0), -h["lines"]))
    cm.hotspots = cm.hotspots[:100]
    cm.unreferenced = cm.unreferenced[:200]


def callers_of(cm: CodeMap, qualified: str) -> List[Call]:
    return [c for c in cm.calls if c.resolved == qualified]


def callees_of(cm: CodeMap, file: str, qualname: str) -> List[Call]:
    return [c for c in cm.calls if c.file == file and c.caller == qualname]
