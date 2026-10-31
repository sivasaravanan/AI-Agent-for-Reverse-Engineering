"""SQL DDL / dialect-clue analysis (text only - SQL is never executed)."""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

from . import signatures as sig
from .collector import Collector
from .context import FileContext
from .models import SchemaObject, SqlScript, STRONG

_CREATE = re.compile(
    r"(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+|LOCAL\s+)?"
    r"(?:(?:TEMP(?:ORARY)?|UNIQUE|CLUSTERED|NONCLUSTERED|MATERIALIZED|UNLOGGED|VIRTUAL)\s+)*"
    r"(?P<kind>TABLE|VIEW|INDEX|PROCEDURE|PROC|FUNCTION|TRIGGER|SEQUENCE|SCHEMA|DATABASE|TYPE|EXTENSION|PACKAGE(?:\s+BODY)?)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\[\]`\"\w.$#]+)")
_ALTER = re.compile(r"(?is)\bALTER\s+TABLE\s+(?:ONLY\s+|IF\s+EXISTS\s+)*(?P<name>[\[\]`\"\w.$#]+)")
_INSERT = re.compile(r"(?is)\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?P<name>[\[\]`\"\w.$#]+)")
_DROP = re.compile(r"(?is)\bDROP\s+(?:TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|TRIGGER)\b")
_CONSTRAINT_WORDS = {"primary", "foreign", "constraint", "unique", "key", "index", "check", "fulltext",
                     "period", "exclude", "like", "spatial"}
_REFERENCES = re.compile(r"(?is)\bREFERENCES\s+(?P<t>[\[\]`\"\w.$#]+)")


def _clean(name: str) -> str:
    return re.sub(r"[\[\]`\"]", "", name).strip(".")


def _paren_body(text: str, start: int, limit: int = 20000) -> Optional[str]:
    i = text.find("(", start)
    if i < 0 or i - start > 200:
        return None
    depth, end = 0, min(len(text), i + limit)
    for j in range(i, end):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return None


def _split_top(body: str) -> List[str]:
    parts, depth, cur = [], 0, []
    for c in body:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append("".join(cur))
    return parts


def parse_columns(body: str) -> Tuple[List[str], List[str]]:
    cols, refs = [], []
    for part in _split_top(body):
        p = part.strip()
        if not p:
            continue
        first = re.split(r"\s+", p, maxsplit=1)[0]
        for r in _REFERENCES.finditer(p):
            refs.append(_clean(r.group("t")))
        if _clean(first).lower() in _CONSTRAINT_WORDS:
            continue
        cols.append(_clean(first))
    return cols[:200], sorted(set(refs))


def scan_sql_text(ctx: FileContext, col: Collector, source: str = "sql") -> Dict[str, int]:
    text = ctx.code_text
    counts: Counter = Counter()
    routines = 0
    for m in _CREATE.finditer(text):
        kind = re.sub(r"\s+", " ", m.group("kind").lower())
        kind = {"proc": "procedure"}.get(kind, kind)
        name = _clean(m.group("name"))
        line = text.count("\n", 0, m.start()) + 1
        columns: List[str] = []
        refs: List[str] = []
        if kind == "table":
            body = _paren_body(text, m.end())
            if body is not None:
                columns, refs = parse_columns(body)
            counts["create_table"] += 1
        elif kind in ("procedure", "function", "trigger", "package", "package body"):
            routines += 1
            counts["create_routine"] += 1
        else:
            counts["create_other"] += 1
        col.schema.append(SchemaObject(kind, name, ctx.path, line, source, columns, refs))
    for m in _ALTER.finditer(text):
        counts["alter_table"] += 1
        col.schema.append(SchemaObject("alter_table", _clean(m.group("name")), ctx.path,
                                       text.count("\n", 0, m.start()) + 1, source))
    counts["insert"] += len(_INSERT.findall(text))
    counts["drop"] += len(_DROP.findall(text))
    counts["routines"] = routines
    return dict(counts)


def scan_dialect_clues(ctx: FileContext, col: Collector) -> Dict[str, int]:
    """Adds STRONG evidence per (database, clue). Returns clue counts per database for the script summary."""
    sql_file = ctx.category == "sql"
    hits: Dict[Tuple[str, str], List] = {}
    for n, line in ctx.iter_code():
        if len(line) > 3000:
            continue
        for db, clues in sig.SQL_CLUES_COMPILED.items():
            for label, rx_i, rx_c, sql_only in clues:
                if sql_only and not sql_file:
                    continue
                if (rx_i if sql_file else rx_c).search(line):
                    rec = hits.setdefault((db, label), [0, n, line])
                    rec[0] += 1
    per_db: Counter = Counter()
    for (db, label), (cnt, n, line) in hits.items():
        per_db[db] += cnt
        col.add(db, STRONG, "sql_dialect", ctx, n, f"dialect-specific SQL: {label} ({cnt}x in file)", line)
        col.sql_dialect_dbs[db][ctx.path] += cnt
    return dict(per_db)


def detect_sql(ctx: FileContext, col: Collector, migration_path: bool = False) -> None:
    if ctx.category == "sql":
        stmts = scan_sql_text(ctx, col)
        hints = scan_dialect_clues(ctx, col)
        creates = stmts.get("create_table", 0)
        inserts = stmts.get("insert", 0)
        routines = stmts.get("routines", 0)
        if migration_path:
            purpose = "migration"
        elif creates and inserts:
            purpose = "mixed"
        elif creates:
            purpose = "schema"
        elif routines and not creates:
            purpose = "routines"
        elif inserts:
            purpose = "seed"
        else:
            purpose = "queries"
        col.scripts.append(SqlScript(ctx.path, purpose, stmts, hints))
    elif ctx.category == "source":
        scan_dialect_clues(ctx, col)
