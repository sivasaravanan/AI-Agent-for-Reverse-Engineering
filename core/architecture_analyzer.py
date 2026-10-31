"""Phase 2: import / reference extraction and module relationships.

Status: PARTIAL - imports and module graph are implemented (Phase 2).
Function/class mapping and architecture summaries are still TODO (Phase 3+).

Purely textual / AST based; no project code is imported or executed.
"""
from __future__ import annotations

import ast
import posixpath
import re
import sys
import warnings
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from models.analysis_result import Manifest
from models.project_info import ImportEdge, ImportSummary

MAX_EDGES_KEPT = 3000
MAX_LINE = 2000
STDLIB = set(getattr(sys, "stdlib_module_names", ()))
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram",
    "dns", "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os", "path",
    "perf_hooks", "process", "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib",
}
PY_ALIASES = {
    "yaml": ["pyyaml"], "pil": ["pillow"], "cv2": ["opencv-python", "opencv-python-headless"],
    "sklearn": ["scikit-learn"], "bs4": ["beautifulsoup4"], "dateutil": ["python-dateutil"],
    "dotenv": ["python-dotenv"], "jwt": ["pyjwt"], "mysqldb": ["mysqlclient"], "serial": ["pyserial"],
    "attr": ["attrs"], "docx": ["python-docx"], "pptx": ["python-pptx"], "win32com": ["pywin32"],
    "win32api": ["pywin32"], "skimage": ["scikit-image"], "zmq": ["pyzmq"], "git": ["gitpython"],
    "jose": ["python-jose"], "psycopg2": ["psycopg2-binary"], "magic": ["python-magic"],
}
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json")
_COMMENT = ("#", "//", "/*", "*")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def _is_comment(line: str, prefixes=_COMMENT) -> bool:
    return line.lstrip().startswith(prefixes)


# =========================================================================== extraction
def _py_imports(path: str, text: str) -> List[Tuple[int, str, int]]:
    """Return (line, module, level) tuples. level>0 is a relative import."""
    out: List[Tuple[int, str, int]] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        for no, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE or _is_comment(line):
                continue
            m = re.match(r"\s*from\s+(\.*)([\w.]*)\s+import\s+(.+)", line)
            if m:
                out.append((no, (m.group(2) or "") + "|" + m.group(3).strip(" ()\\"), len(m.group(1))))
                continue
            m = re.match(r"\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", line)
            if m:
                out.extend((no, mod.strip(), 0) for mod in m.group(1).split(","))
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name, 0) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = ",".join(a.name for a in node.names)
            out.append((node.lineno, f"{node.module or ''}|{names}", node.level))
    return out


_JS_PATTERNS = [
    re.compile(r"""\bimport\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+[^'"]*?\sfrom\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
]


def _js_imports(text: str) -> List[Tuple[int, str]]:
    out, seen = [], set()
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE or _is_comment(line):
            continue
        for rx in _JS_PATTERNS:
            for m in rx.finditer(line):
                key = (no, m.group(1))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def _regex_lines(text: str, rx: "re.Pattern[str]") -> List[Tuple[int, str]]:
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE or _is_comment(line, ("//", "/*", "*", "#")):
            continue
        m = rx.match(line)
        if m:
            out.append((no, m.group(1)))
    return out


_CS_USING = re.compile(r"\s*(?:global\s+)?using\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;")
_CS_NS = re.compile(r"\s*namespace\s+([A-Za-z_][\w.]*)")
_JAVA_IMPORT = re.compile(r"\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;")
_PHP_USE = re.compile(r"\s*use\s+(?:function\s+|const\s+)?([A-Za-z_\\][\w\\]*)(?:\s+as\s+\w+)?\s*;")
_PHP_NS = re.compile(r"\s*namespace\s+([A-Za-z_\\][\w\\]*)\s*;")
_PHP_REQ = re.compile(r"""\b(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?['"]([^'"]+\.php)['"]""")


def _go_imports(text: str) -> List[Tuple[int, str]]:
    out, in_block = [], False
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("import ("):
            in_block = True
            continue
        if in_block:
            if s == ")":
                in_block = False
                continue
            m = re.match(r'(?:[\w.]+\s+)?"([^"]+)"', s)
        else:
            m = re.match(r'import\s+(?:[\w.]+\s+)?"([^"]+)"', s)
        if m:
            out.append((no, m.group(1)))
    return out


# =========================================================================== resolution helpers
class _PyIndex:
    def __init__(self, files: Iterable[str]):
        self.files = set(files)
        self.by_dotted: Dict[str, List[str]] = defaultdict(list)
        for f in self.files:
            stem = f[:-3] if f.endswith(".py") else f[:-4]
            parts = stem.split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                if parts[i:]:
                    self.by_dotted[".".join(parts[i:])].append(f)

    def root_key(self, f: str) -> str:
        stem = f[:-3]
        parts = stem.split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def resolve_abs(self, dotted: str, importer: str) -> Optional[str]:
        cands = self.by_dotted.get(dotted, [])
        if not cands:
            return None
        imp_dir = posixpath.dirname(importer)
        # 1) sibling module of the importer (script-style imports)
        for c in cands:
            if posixpath.dirname(c) == imp_dir or c.startswith(imp_dir + "/") and imp_dir:
                if self.root_key(c).endswith(dotted):
                    return c
        # 2) full path from archive root
        for c in cands:
            if self.root_key(c) == dotted:
                return c
        # 3) unique suffix match, else the closest one
        if len(cands) == 1:
            return cands[0]
        return max(cands, key=lambda c: len(posixpath.commonprefix([c, importer])))

    def resolve_rel(self, importer: str, level: int, module: str, names: str) -> Optional[str]:
        base = posixpath.dirname(importer)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        parts = [p for p in module.split(".") if p]
        target = "/".join([p for p in [base] + parts if p])
        for cand in (f"{target}.py", f"{target}/__init__.py"):
            if cand in self.files:
                return cand
        if not module:  # from . import x
            for n in names.split(","):
                for cand in (f"{base}/{n.strip()}.py", f"{base}/{n.strip()}/__init__.py"):
                    if cand.lstrip("/") in self.files:
                        return cand.lstrip("/")
            if f"{base}/__init__.py".lstrip("/") in self.files:
                return f"{base}/__init__.py".lstrip("/")
        return None


def _js_resolve(importer: str, target: str, files: Set[str]) -> Optional[str]:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), target)) if target.startswith(".") \
        else target.lstrip("/")
    if base in files:
        return base
    for ext in JS_EXTS:
        if base + ext in files:
            return base + ext
    for ext in JS_EXTS:
        if f"{base}/index{ext}" in files:
            return f"{base}/index{ext}"
    return None


def _js_package(target: str) -> str:
    parts = target.split("/")
    return "/".join(parts[:2]) if target.startswith("@") and len(parts) > 1 else parts[0]


# =========================================================================== main entry point
def analyze_imports(texts: Mapping[str, str], manifests: Sequence[Manifest]) -> Tuple[List[ImportEdge], ImportSummary]:
    files = set(texts)
    py_files = [f for f in texts if f.endswith(".py")]
    pyidx = _PyIndex(py_files)
    cs_ns: Dict[str, str] = {}
    php_ns: Dict[str, str] = {}
    java_paths = {f for f in texts if f.endswith(".java")}
    go_module = ""
    for f, t in texts.items():
        if f.endswith(".cs") or f.endswith(".vb"):
            for _, ns in _regex_lines(t, _CS_NS):
                cs_ns.setdefault(ns, f)
        elif f.endswith(".php"):
            for _, ns in _regex_lines(t, _PHP_NS):
                php_ns.setdefault(ns, f)
        elif posixpath.basename(f).lower() == "go.mod":
            m = re.search(r"^module\s+(\S+)", t, re.M)
            if m and not go_module:
                go_module = m.group(1)

    edges: List[ImportEdge] = []

    def add(src, line, lang, target, cat, resolved="", pkg=""):
        edges.append(ImportEdge(src, line, lang, target, cat, resolved, pkg))

    for f in sorted(texts):
        t = texts[f]
        ext = posixpath.splitext(f)[1].lower()
        if ext in (".py", ".pyw"):
            for line, mod, level in _py_imports(f, t):
                module, _, names = mod.partition("|")
                label = ("." * level) + module
                if level:
                    r = pyidx.resolve_rel(f, level, module, names)
                    add(f, line, "Python", label, "internal" if r else "unresolved", r or "")
                    continue
                top = module.split(".")[0]
                local = pyidx.resolve_abs(module, f) if module else None
                sibling = bool(local) and posixpath.dirname(local) == posixpath.dirname(f)
                if local and (sibling or pyidx.root_key(local) == module):
                    add(f, line, "Python", module, "internal", local)
                elif top in STDLIB:
                    add(f, line, "Python", module, "stdlib", pkg=top)
                elif local:
                    add(f, line, "Python", module, "internal", local)
                else:
                    add(f, line, "Python", module, "external", pkg=top)
        elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
            lang = "TypeScript" if ext in (".ts", ".tsx") else "JavaScript"
            for line, target in _js_imports(t):
                if target.startswith((".", "/")):
                    r = _js_resolve(f, target, files)
                    add(f, line, lang, target, "internal" if r else "unresolved", r or "")
                elif target.startswith("node:") or target.split("/")[0] in NODE_BUILTINS:
                    add(f, line, lang, target, "builtin", pkg=target.replace("node:", "").split("/")[0])
                elif target.startswith(("@/", "~/", "#")):
                    add(f, line, lang, target, "unresolved")
                else:
                    add(f, line, lang, target, "external", pkg=_js_package(target))
        elif ext in (".cs", ".vb"):
            for line, ns in _regex_lines(t, _CS_USING):
                hit = next((cs_ns[d] for d in cs_ns if d == ns or d.startswith(ns + ".") or ns.startswith(d + ".")), None)
                lang = "C#" if ext == ".cs" else "VB.NET"
                if hit:
                    add(f, line, lang, ns, "internal", hit)
                elif ns.split(".")[0] == "System":
                    add(f, line, lang, ns, "stdlib", pkg="System")
                else:
                    add(f, line, lang, ns, "external", pkg=".".join(ns.split(".")[:2]))
        elif ext == ".java":
            for line, imp in _regex_lines(t, _JAVA_IMPORT):
                as_path = imp.replace(".", "/")
                hit = next((p for p in java_paths if p.endswith(as_path + ".java")), None) or \
                    next((p for p in java_paths if ("/" + as_path + "/") in ("/" + p)), None)
                if hit:
                    add(f, line, "Java", imp, "internal", hit)
                elif imp.startswith(("java.", "javax.")):
                    add(f, line, "Java", imp, "stdlib", pkg=imp.split(".")[0])
                else:
                    add(f, line, "Java", imp, "external", pkg=".".join(imp.split(".")[:2]))
        elif ext == ".go":
            for line, imp in _go_imports(t):
                if go_module and (imp == go_module or imp.startswith(go_module + "/")):
                    sub = imp[len(go_module):].strip("/")
                    hit = next((p for p in files if p.endswith(".go") and posixpath.dirname(p).endswith(sub)), "")
                    add(f, line, "Go", imp, "internal", hit)
                elif "." not in imp.split("/")[0]:
                    add(f, line, "Go", imp, "stdlib", pkg=imp.split("/")[0])
                else:
                    add(f, line, "Go", imp, "external", pkg="/".join(imp.split("/")[:3]))
        elif ext == ".php":
            for line, ns in _regex_lines(t, _PHP_USE):
                hit = next((php_ns[d] for d in php_ns if ns.startswith(d)), None)
                if hit:
                    add(f, line, "PHP", ns, "internal", hit)
                else:
                    add(f, line, "PHP", ns, "external", pkg=ns.split("\\")[0])
            for line, rel in _regex_lines(t, _PHP_REQ):
                r = posixpath.normpath(posixpath.join(posixpath.dirname(f), rel))
                add(f, line, "PHP", rel, "internal" if r in files else "unresolved", r if r in files else "")
    return _finish(edges, manifests)


# =========================================================================== summary
def _declared_names(manifests: Sequence[Manifest], ecosystem: str) -> Set[str]:
    return {_norm(d.name) if ecosystem == "python" else d.name.lower()
            for m in manifests if m.ecosystem == ecosystem for d in m.dependencies}


def _py_declared(top: str, declared: Set[str]) -> bool:
    n = _norm(top)
    if n in declared or any(_norm(a) in declared for a in PY_ALIASES.get(top.lower(), [])):
        return True
    return any(d.startswith(n + "-") or d == n for d in declared)


def _finish(edges: List[ImportEdge], manifests: Sequence[Manifest]) -> Tuple[List[ImportEdge], ImportSummary]:
    s = ImportSummary(edge_count=len(edges))
    s.files_with_imports = len({e.source for e in edges})
    s.by_language = dict(Counter(e.language for e in edges))
    internal = [e for e in edges if e.category == "internal" and e.resolved]
    s.internal_edges = len(internal)
    py_declared = _declared_names(manifests, "python")
    js_declared = _declared_names(manifests, "javascript")
    has_py_manifest = any(m.ecosystem == "python" and m.dependencies for m in manifests)
    has_js_manifest = any(m.ecosystem == "javascript" and m.dependencies for m in manifests)
    ext: Dict[Tuple[str, str], Dict] = {}
    for e in edges:
        if e.category != "external":
            continue
        key = (e.language, e.package)
        rec = ext.get(key)
        if rec is None:
            declared: Optional[bool] = None
            if e.language == "Python" and has_py_manifest:
                declared = _py_declared(e.package, py_declared)
            elif e.language in ("JavaScript", "TypeScript") and has_js_manifest:
                declared = e.package.lower() in js_declared or f"@types/{e.package.lower()}" in js_declared
            rec = ext[key] = {"name": e.package, "language": e.language, "count": 0, "declared": declared,
                              "first_file": e.source, "first_line": e.line}
        rec["count"] += 1
    s.external_packages = sorted(ext.values(), key=lambda r: (-r["count"], r["name"]))
    # fan-in
    fan = Counter(e.resolved for e in internal if e.resolved != e.source)
    s.most_depended_on = [{"file": f, "count": c} for f, c in fan.most_common(10)]
    s.cycles = _cycles(internal)
    # declared but never imported (weak indicator)
    used_py = {_norm(r["name"]) for r in ext.values() if r["language"] == "Python"}
    used_py |= {_norm(a) for r in ext.values() if r["language"] == "Python" for a in PY_ALIASES.get(r["name"].lower(), [])}
    used_js = {r["name"].lower() for r in ext.values() if r["language"] in ("JavaScript", "TypeScript")}
    for m in manifests:
        for d in m.dependencies:
            if d.scope not in ("runtime",):
                continue
            if m.ecosystem == "python" and not any(u == _norm(d.name) or u.startswith(_norm(d.name)) or _norm(d.name).startswith(u)
                                                    for u in used_py):
                s.unused_declared.append({"name": d.name, "file": d.file, "line": d.line, "ecosystem": "python"})
            elif m.ecosystem == "javascript" and d.name.lower() not in used_js:
                s.unused_declared.append({"name": d.name, "file": d.file, "line": d.line, "ecosystem": "javascript"})
    s.unused_declared = s.unused_declared[:100]
    kept = sorted(edges, key=lambda e: (e.source, e.line or 0))
    return kept, s


def _cycles(internal: List[ImportEdge], cap: int = 20) -> List[List[str]]:
    """Strongly connected components (iterative Tarjan) with more than one node, or self-loops."""
    graph: Dict[str, Set[str]] = defaultdict(set)
    for e in internal:
        graph[e.source].add(e.resolved)
        graph.setdefault(e.resolved, set())
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on: Set[str] = set()
    stack: List[str] = []
    result: List[List[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    advanced = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in graph[node]:
                    result.append(sorted(comp))
    result.sort(key=lambda c: (-len(c), c))
    return [c[:12] for c in result[:cap]]
