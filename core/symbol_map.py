"""Function / class / module mapping (master-prompt objective 8). Text and AST only - nothing is imported or run.

Python is parsed with ``ast`` (exact lines). Other languages use line-based regexes: these are heuristics and are
labelled ``method='regex'`` so they are never presented as parser-confirmed.
"""
from __future__ import annotations

import ast
import re
import warnings
from typing import Dict, List, Mapping

MAX_SYMBOLS_PER_FILE = 300
MAX_FILES = 2000

_RX = {
    "javascript": [("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
                   ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
                   ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"))],
    "java": [("class", re.compile(r"^\s*(?:public|protected|private|abstract|final|static|\s)*\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")),
             ("function", re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],\s?]+\s+([a-z_]\w*)\s*\([^;]*$"))],
    "csharp": [("class", re.compile(r"^\s*(?:public|internal|protected|private|abstract|sealed|static|partial|\s)*\b(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)")),
               ("function", re.compile(r"^\s*(?:public|internal|protected|private)\s+(?:static\s+|async\s+|virtual\s+|override\s+)*[\w<>\[\],?]+\s+([A-Za-z_]\w*)\s*\([^;]*$"))],
    "vbnet": [("class", re.compile(r"^\s*(?:Public|Friend|Private|\s)*\b(?:Class|Module|Interface|Structure)\s+([A-Za-z_]\w*)", re.I)),
              ("function", re.compile(r"^\s*(?:Public|Friend|Private|Protected|Shared|\s)*\b(?:Sub|Function)\s+([A-Za-z_]\w*)", re.I))],
    "go": [("class", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
           ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("))],
    "php": [("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|static\s+)*function\s+&?([A-Za-z_]\w*)\s*\("))],
}
_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "javascript", ".tsx": "javascript", ".java": "java", ".cs": "csharp", ".vb": "vbnet", ".go": "go",
        ".php": "php"}
_LANG_NAME = {"python": "Python", "javascript": "JavaScript/TypeScript", "java": "Java", "csharp": "C#",
              "vbnet": "VB.NET", "go": "Go", "php": "PHP"}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "else", "using", "lock", "foreach"}


def _py_symbols(text: str) -> List[dict]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    out: List[dict] = []

    def walk(body, owner: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.append({"kind": "class", "name": node.name, "line": node.lineno, "owner": owner, "method": "ast"})
                walk(node.body, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append({"kind": "method" if owner else "function", "name": node.name, "line": node.lineno,
                            "owner": owner, "method": "ast"})
            elif isinstance(node, (ast.If, ast.Try)):
                walk(getattr(node, "body", []) + getattr(node, "orelse", []), owner)
    walk(tree.body, "")
    return out


def _rx_symbols(lang: str, text: str) -> List[dict]:
    out: List[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 400 or line.lstrip().startswith(("//", "#", "*", "/*", "'")):
            continue
        for kind, rx in _RX[lang]:
            m = rx.match(line)
            if m and m.group(1) not in _KEYWORDS:
                out.append({"kind": kind, "name": m.group(1), "line": i, "owner": "", "method": "regex"})
                break
    return out


def map_symbols(texts: Mapping[str, str]) -> Dict:
    """Return {'files': [...], 'totals': {...}, 'truncated': bool, 'notes': [...]} (JSON friendly)."""
    files, totals, truncated = [], {"classes": 0, "functions": 0, "files": 0}, False
    for path in sorted(texts):
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        lang = _EXT.get(ext)
        if not lang:
            continue
        if len(files) >= MAX_FILES:
            truncated = True
            break
        syms = _py_symbols(texts[path]) if lang == "python" else _rx_symbols(lang, texts[path])
        if not syms:
            continue
        if len(syms) > MAX_SYMBOLS_PER_FILE:
            syms, truncated = syms[:MAX_SYMBOLS_PER_FILE], True
        totals["classes"] += sum(1 for s in syms if s["kind"] == "class")
        totals["functions"] += sum(1 for s in syms if s["kind"] != "class")
        totals["files"] += 1
        files.append({"file": path, "language": _LANG_NAME[lang], "method": syms[0]["method"], "symbols": syms})
    return {"files": files, "totals": totals, "truncated": truncated,
            "notes": ["Python symbols come from ast (exact). Other languages use line-based regexes and may miss "
                      "or misreport declarations (method='regex')."]}
