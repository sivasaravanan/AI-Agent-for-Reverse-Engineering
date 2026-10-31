"""Code blocks and the output they produce.

A "code block" is the function / method / class that contains a line (module level code counts as the file's own block).
Python files are split exactly with ``ast``; JavaScript/TypeScript/Java/C#/Go/C-like files use a brace heuristic.
``OutputIndex`` keeps what the program printed together with the position that printed it, so the panel can show
"this block -> this output". Pure functions, bounded work, standard library only.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

MAX_SOURCE_CHARS = 2_000_000
MAX_RECORDS = 20000
MAX_TEXT = 3_000_000


@dataclass(frozen=True)
class Block:
    kind: str            # module | class | function | method
    name: str
    qualname: str
    start: int           # 1-based, inclusive
    end: int

    @property
    def label(self) -> str:
        return {"module": "(module level)", "class": f"class {self.qualname}", "method": f"{self.qualname}()",
                "function": f"{self.qualname}()"}[self.kind]


def _python_blocks(text: str) -> List[Block]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    out: List[Block] = []

    def walk(node, prefix: str, in_class: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{ch.name}"
                start = min([ch.lineno] + [d.lineno for d in getattr(ch, "decorator_list", [])])
                kind = "class" if isinstance(ch, ast.ClassDef) else ("method" if in_class else "function")
                out.append(Block(kind, ch.name, q, start, getattr(ch, "end_lineno", ch.lineno) or ch.lineno))
                walk(ch, q + ".", isinstance(ch, ast.ClassDef))
            else:
                walk(ch, prefix, in_class)
    walk(tree, "", False)
    return out


_BRACE_DECL = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|final\s+|abstract\s+|default\s+)*"
    r"(?:(?P<cls>class|interface|struct|enum)\s+(?P<cname>[A-Za-z_$][\w$]*)"
    r"|(?:function\*?\s+(?P<fname>[A-Za-z_$][\w$]*)\s*\(|func\s+(?:\([^)]*\)\s*)?(?P<gname>[A-Za-z_]\w*)\s*\()"
    r"|(?:const|let|var)\s+(?P<aname>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    r"|(?:async\s+|static\s+|get\s+|set\s+)*(?P<jname>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$"
    r"|(?:[\w<>\[\],.?]+\s+)+(?P<mname>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+)?\s*\{?\s*$)")
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do", "try", "using", "lock", "foreach"}


def _brace_blocks(text: str) -> List[Block]:
    lines = text.splitlines()
    out: List[Block] = []
    for i, line in enumerate(lines):
        m = _BRACE_DECL.match(line)
        if not m:
            continue
        name = m.group("cname") or m.group("fname") or m.group("gname") or m.group("aname") or m.group("jname") or m.group("mname")
        if not name or name in _KEYWORDS:
            continue
        depth, opened, end = 0, False, i
        for j in range(i, min(len(lines), i + 4000)):
            seg = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//.*$', "", lines[j])
            depth += seg.count("{") - seg.count("}")
            opened = opened or "{" in seg
            end = j
            if opened and depth <= 0:
                break
            if not opened and j > i + 2:
                break
        if opened:
            out.append(Block("class" if m.group("cls") else "function", name, name, i + 1, end + 1))
    return out


def blocks_for(path: str, text: str) -> List[Block]:
    """All blocks of a file, outermost first. A file without recognisable blocks yields an empty list."""
    if len(text) > MAX_SOURCE_CHARS:
        return []
    low = path.lower()
    if low.endswith((".py", ".pyw")):
        return _python_blocks(text)
    if low.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".cs", ".go", ".c", ".cc", ".cpp", ".h", ".hpp", ".kt", ".php", ".swift", ".rs")):
        return _brace_blocks(text)
    return []


def innermost(blocks: List[Block], line: int) -> Optional[Block]:
    """The smallest block containing ``line``; None means module level."""
    best: Optional[Block] = None
    for b in blocks:
        if b.start <= line <= b.end and (best is None or (b.end - b.start) < (best.end - best.start)):
            best = b
    return best


def block_text(text: str, block: Optional[Block], file_name: str = "") -> Tuple[str, int]:
    """(source of the block, first line number). Module level shows the whole file."""
    lines = text.splitlines()
    if block is None:
        return text, 1
    return "\n".join(lines[block.start - 1:block.end]), block.start


class OutputIndex:
    """Output records: (file, line, function, stream, text, timestamp). Bounded in count and size."""

    def __init__(self) -> None:
        self.records: List[Tuple[str, int, str, str, str, float]] = []
        self.size = 0
        self.dropped = 0

    def add(self, f: str, l: int, fn: str, stream: str, text: str, ts: float = 0.0) -> None:
        if not text:
            return
        if len(self.records) >= MAX_RECORDS or self.size >= MAX_TEXT:
            self.dropped += 1
            return
        if self.records:                                    # merge consecutive writes from the same position (print() writes in pieces)
            pf, pl, pfn, ps, pt, pts = self.records[-1]
            if (pf, pl, pfn, ps) == (f, l, fn, stream) and len(pt) < 8000:
                self.records[-1] = (pf, pl, pfn, ps, pt + text, ts or pts)
                self.size += len(text)
                return
        self.records.append((f, l, fn, stream, text, ts))
        self.size += len(text)

    def for_block(self, file: str, block: Optional[Block]) -> List[Tuple[int, str, str]]:
        """[(line, stream, text)] printed from inside ``block`` of ``file`` (the whole file for module level)."""
        out = []
        for f, l, _fn, s, t, _ts in self.records:
            if f != file:
                continue
            if block is None or block.start <= l <= block.end:
                out.append((l, s, t))
        return out

    def summary(self, resolve: Callable[[str], Optional[str]]) -> List[Dict]:
        """One row per (file, block): lines of output and bytes. ``resolve(file)`` returns that file's source text or None."""
        cache: Dict[str, List[Block]] = {}
        rows: Dict[Tuple[str, str], Dict] = {}
        for f, l, fn, _s, t, _ts in self.records:
            if f not in cache:
                text = resolve(f)
                cache[f] = blocks_for(f, text) if text else []
            b = innermost(cache[f], l)
            key = (f, b.qualname if b else "")
            row = rows.setdefault(key, {"file": f, "block": b.label if b else "(module level)", "line": b.start if b else 1,
                                        "lines": 0, "chars": 0})
            row["lines"] += t.count("\n")
            row["chars"] += len(t)
        return sorted(rows.values(), key=lambda r: (r["file"], r["line"]))

    def clear(self) -> None:
        self.records, self.size, self.dropped = [], 0, 0
