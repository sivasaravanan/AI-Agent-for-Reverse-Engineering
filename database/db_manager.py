"""Local analysis history (SQLite, standard library only).

* All SQL is parameterized. Stored results are the already-redacted JSON result (zlib compressed).
* Writes use short-lived connections so the worker thread and the UI thread never share one.
* ``max_history`` oldest rows are trimmed automatically.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import typing
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from models.analysis_result import AnalysisResult

SCHEMA_VERSION = 1
MAX_BLOB_BYTES = 25 * 1024 * 1024
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class HistoryError(Exception):
    """User-presentable history failure."""


@dataclass
class HistoryRow:
    id: int
    analyzed_at: str
    archive_name: str
    archive_sha256: str
    tool_version: str
    duration_seconds: float
    file_count: int
    finding_count: int
    high_count: int
    languages: str
    html_path: str
    json_path: str
    stored: bool


def file_sha256(path: str | Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    p = Path(path)
    if p.is_dir():                         # folder input: hash relative names + sizes + mtimes (cheap, stable identity)
        for f in sorted(x for x in p.rglob("*") if x.is_file() and not x.is_symlink()):
            st = f.stat()
            h.update(f"{f.relative_to(p).as_posix()}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8", "replace"))
        return "dir:" + h.hexdigest()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- dict -> dataclass
def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Union:
        inner = [a for a in args if a is not type(None)]
        return _convert(inner[0], value) if inner else value
    if origin in (list, List):
        return [_convert(args[0], v) for v in value] if args else list(value)
    if origin in (dict, Dict):
        return {k: _convert(args[1], v) for k, v in value.items()} if len(args) == 2 else dict(value)
    if dataclasses.is_dataclass(tp) and isinstance(value, dict):
        return from_dict(tp, value)
    return value


def from_dict(cls: Any, data: Dict[str, Any]) -> Any:
    """Rebuild a (nested) result dataclass from its JSON form; unknown/missing keys are tolerated."""
    hints = typing.get_type_hints(cls)
    kwargs = {f.name: _convert(hints[f.name], data[f.name]) for f in dataclasses.fields(cls) if f.name in data}
    return cls(**kwargs)


class DbManager:
    def __init__(self, path: str | Path | None = None, max_history: int = 200):
        self.path = Path(path) if path else Path("data") / "history.db"
        self.max_history = max(1, int(max_history))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as con:
                con.executescript(self._schema_sql())
                con.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"The history database could not be opened ({exc}).")

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA foreign_keys = ON")
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _schema_sql() -> str:
        try:
            return _SCHEMA_FILE.read_text(encoding="utf-8")
        except OSError:
            raise HistoryError("database/schema.sql is missing.")

    # ------------------------------------------------------------------ write
    def save_result(self, result: AnalysisResult, report_paths: Optional[Dict[str, Any]] = None,
                    archive_sha256: str = "") -> int:
        data = result.to_dict()
        blob: Optional[bytes] = zlib.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), 6)
        if len(blob) > MAX_BLOB_BYTES:
            blob = None                                  # keep the summary row, skip the oversized payload
        sev = Counter(f.severity for f in result.findings)
        paths = report_paths or {}
        try:
            with self._connect() as con:
                cur = con.execute(
                    "INSERT INTO analyses(analyzed_at, archive_name, archive_sha256, tool_version, duration_seconds, "
                    "file_count, total_size, finding_count, high_count, medium_count, low_count, languages, html_path, "
                    "json_path, result_blob) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (result.analyzed_at or datetime.now(timezone.utc).isoformat(timespec="seconds"), result.archive_name,
                     archive_sha256, result.tool_version, result.duration_seconds, result.archive.file_count,
                     result.archive.total_uncompressed, len(result.findings), sev["critical"] + sev["high"], sev["medium"],
                     sev["low"] + sev["info"], ", ".join(l.language for l in result.languages[:6]),
                     str(paths.get("html", "")), str(paths.get("json", "")), blob))
                aid = int(cur.lastrowid)
                con.executemany(
                    "INSERT INTO findings(analysis_id, finding_id, rule_id, severity, category, file, line, title, evidence) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [(aid, f.id, f.rule_id, f.severity, f.category, f.file, f.line, f.title, f.evidence[:300])
                     for f in result.findings])
                con.execute("DELETE FROM analyses WHERE id NOT IN (SELECT id FROM analyses ORDER BY id DESC LIMIT ?)",
                            (self.max_history,))
            return aid
        except sqlite3.Error as exc:
            raise HistoryError(f"The analysis could not be saved to history ({exc}).")

    def delete(self, analysis_id: int) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM analyses WHERE id = ?", (int(analysis_id),))

    def clear(self) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM analyses")

    # ------------------------------------------------------------------ read
    @staticmethod
    def _row(r: sqlite3.Row) -> HistoryRow:
        return HistoryRow(r["id"], r["analyzed_at"], r["archive_name"], r["archive_sha256"] or "", r["tool_version"] or "",
                          r["duration_seconds"] or 0.0, r["file_count"] or 0, r["finding_count"] or 0, r["high_count"] or 0,
                          r["languages"] or "", r["html_path"] or "", r["json_path"] or "", bool(r["has_blob"]))

    def list_history(self, limit: int = 200, search: str = "") -> List[HistoryRow]:
        sql = ("SELECT id, analyzed_at, archive_name, archive_sha256, tool_version, duration_seconds, file_count, "
               "finding_count, high_count, languages, html_path, json_path, result_blob IS NOT NULL AS has_blob "
               "FROM analyses")
        args: List[Any] = []
        if search.strip():
            esc = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " WHERE archive_name LIKE ? ESCAPE '\\'"
            args.append(f"%{esc}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        with self._connect() as con:
            return [self._row(r) for r in con.execute(sql, args)]

    def get_row(self, analysis_id: int) -> Optional[HistoryRow]:
        with self._connect() as con:
            r = con.execute("SELECT id, analyzed_at, archive_name, archive_sha256, tool_version, duration_seconds, file_count, "
                            "finding_count, high_count, languages, html_path, json_path, result_blob IS NOT NULL AS has_blob "
                            "FROM analyses WHERE id = ?", (int(analysis_id),)).fetchone()
        return self._row(r) if r else None

    def load(self, analysis_id: int) -> Dict[str, Any]:
        with self._connect() as con:
            r = con.execute("SELECT result_blob FROM analyses WHERE id = ?", (int(analysis_id),)).fetchone()
        if r is None:
            raise HistoryError("That history entry no longer exists.")
        if r["result_blob"] is None:
            raise HistoryError("The full result of this entry was too large to store; only its summary is kept.")
        try:
            return json.loads(zlib.decompress(r["result_blob"]).decode("utf-8"))
        except (zlib.error, ValueError) as exc:
            raise HistoryError(f"The stored result is damaged ({type(exc).__name__}).")

    def load_result(self, analysis_id: int) -> AnalysisResult:
        return from_dict(AnalysisResult, self.load(analysis_id))

    def compare(self, older_id: int, newer_id: int, cap: int = 200) -> Dict[str, Any]:
        def sigs(aid: int) -> Counter:
            with self._connect() as con:
                return Counter((r["rule_id"], r["file"], r["evidence"]) for r in
                               con.execute("SELECT rule_id, file, evidence FROM findings WHERE analysis_id = ?", (int(aid),)))
        a, b = sigs(older_id), sigs(newer_id)
        fmt = lambda c: [{"rule_id": k[0], "file": k[1], "evidence": k[2]} for k in list((c).elements())[:cap]]  # noqa: E731
        return {"new": fmt(b - a), "resolved": fmt(a - b), "unchanged": sum((a & b).values()),
                "new_count": sum((b - a).values()), "resolved_count": sum((a - b).values())}
