"""Database technology detection.

Levels (never "confirmed" - this is static analysis):
  strong   : a connection string / URL / config entry that targets the database
  moderate : the database driver is imported or used in source code
  weak     : driver only declared in a manifest, or mentioned in a comment/doc
A driver in requirements.txt is NOT proof that the database is configured or used.
"""
from __future__ import annotations

import posixpath
import re
from typing import Dict, List, Mapping, Sequence

from models.analysis_result import CONFIDENCE_RANK, Detection, Evidence, Manifest
from security.secret_scanner import redact

_I = re.IGNORECASE
# db -> [(level, compiled regex)]
RULES: Dict[str, List[tuple]] = {
    "SQLite": [
        ("strong", re.compile(r"sqlite:///?|jdbc:sqlite:|Data Source\s*=\s*[^;\"']*\.(?:db|sqlite3?)\b", _I)),
        ("moderate", re.compile(r"\bimport\s+sqlite3\b|\bsqlite3\.connect\(|Microsoft\.Data\.Sqlite|System\.Data\.SQLite|better-sqlite3|require\(\s*['\"]sqlite3['\"]", _I)),
        ("weak", re.compile(r"\bsqlite\b", _I)),
    ],
    "PostgreSQL": [
        ("strong", re.compile(r"\bpostgres(?:ql)?(?:\+\w+)?://|jdbc:postgresql:|\bHost\s*=[^;\n]+;[^\n]*Database\s*=", _I)),
        ("moderate", re.compile(r"\bpsycopg2?\b|\basyncpg\b|\bNpgsql\b|require\(\s*['\"]pg['\"]|from\s+['\"]pg['\"]|\bpgx\b|lib/pq", _I)),
        ("weak", re.compile(r"\bpostgres(?:ql)?\b", _I)),
    ],
    "MySQL/MariaDB": [
        ("strong", re.compile(r"\bmysql(?:\+\w+)?://|\bmariadb://|jdbc:(?:mysql|mariadb):|PDO\(\s*['\"]mysql:", _I)),
        ("moderate", re.compile(r"\bpymysql\b|mysql\.connector|\bMySQLdb\b|\bmysqlclient\b|MySql\.Data|MySqlConnector|mysqli_connect|new\s+mysqli|require\(\s*['\"]mysql2?['\"]", _I)),
        ("weak", re.compile(r"\b(?:mysql|mariadb)\b", _I)),
    ],
    "SQL Server": [
        ("strong", re.compile(r"jdbc:sqlserver:|\bmssql(?:\+\w+)?://|Initial Catalog\s*=|Server\s*=[^;\n]+;[^\n]*(?:Database|Initial Catalog)\s*=|ODBC Driver \d+ for SQL Server", _I)),
        ("moderate", re.compile(r"\bpyodbc\b|\bpymssql\b|System\.Data\.SqlClient|Microsoft\.Data\.SqlClient|require\(\s*['\"]mssql['\"]|\btedious\b|sqlsrv_connect", _I)),
        ("weak", re.compile(r"\bsql server\b|\bmssql\b", _I)),
    ],
    "Oracle": [
        ("strong", re.compile(r"jdbc:oracle:|\boracle(?:\+\w+)?://|\(DESCRIPTION\s*=|Data Source\s*=[^;\n]*\(HOST", _I)),
        ("moderate", re.compile(r"\bcx_Oracle\b|\boracledb\b|Oracle\.ManagedDataAccess|\bojdbc\d*\b|oci_connect", _I)),
        ("weak", re.compile(r"\boracle\b", _I)),
    ],
    "MongoDB": [
        ("strong", re.compile(r"\bmongodb(?:\+srv)?://", _I)),
        ("moderate", re.compile(r"\bpymongo\b|\bMongoClient\b|\bmongoose\b|MongoDB\.Driver", _I)),
        ("weak", re.compile(r"\bmongo(?:db)?\b", _I)),
    ],
}
# manifest dependency substring -> database (declared only => weak)
DEP_HINTS = {
    "psycopg": "PostgreSQL", "asyncpg": "PostgreSQL", "npgsql": "PostgreSQL", "org.postgresql": "PostgreSQL",
    "entityframeworkcore.sqlserver": "SQL Server", "microsoft.data.sqlclient": "SQL Server",
    "system.data.sqlclient": "SQL Server", "pyodbc": "SQL Server", "pymssql": "SQL Server",
    "mssql": "SQL Server", "tedious": "SQL Server",
    "pymysql": "MySQL/MariaDB", "mysqlclient": "MySQL/MariaDB", "mysql-connector": "MySQL/MariaDB",
    "mysql2": "MySQL/MariaDB", "mysqlconnector": "MySQL/MariaDB", "mysql.data": "MySQL/MariaDB",
    "mariadb": "MySQL/MariaDB",
    "cx_oracle": "Oracle", "cx-oracle": "Oracle", "oracledb": "Oracle", "ojdbc": "Oracle",
    "oracle.manageddataaccess": "Oracle",
    "pymongo": "MongoDB", "mongoose": "MongoDB", "mongodb": "MongoDB", "motor": "MongoDB",
    "better-sqlite3": "SQLite", "microsoft.data.sqlite": "SQLite", "system.data.sqlite": "SQLite",
    "entityframeworkcore.sqlite": "SQLite",
}
DOC_EXTENSIONS = {".md", ".txt", ".rst"}
MANIFEST_BASENAMES = {"requirements.txt", "package.json", "pyproject.toml", "pom.xml", "composer.json", "go.mod", "pipfile"}
COMMENT_PREFIXES = ("#", "//", "--", "/*", "*", "'", "<!--", "rem ")
_DDL = re.compile(r"\bCREATE\s+TABLE\b", _I)
MAX_EVIDENCE = 8
MAX_LINE = 2000


def _is_comment(stripped: str) -> bool:
    return stripped.lower().startswith(COMMENT_PREFIXES)


def detect_databases(manifests: Sequence[Manifest], texts: Mapping[str, str]) -> List[Detection]:
    found: Dict[str, Detection] = {}
    ranked: Dict[str, List[tuple]] = {}

    def add(db: str, level: str, file: str, line, text: str, kind: str = "database") -> None:
        det = found.get(db)
        if det is None:
            det = found[db] = Detection(name=db, kind=kind, confidence=level)
        det.evidence_count += 1
        if CONFIDENCE_RANK[level] > CONFIDENCE_RANK[det.confidence]:
            det.confidence = level
        ranked.setdefault(db, []).append((CONFIDENCE_RANK[level], len(ranked.get(db, [])),
                                          Evidence(file, line, redact(text.strip())[:200], level)))

    for mf in manifests:
        for dep in mf.dependencies:
            low = dep.name.lower()
            for hint, db in DEP_HINTS.items():
                if hint in low:
                    add(db, "weak", dep.file, dep.line, f"declared dependency: {dep.name} {dep.spec}".strip())
                    break

    for path, text in texts.items():
        base = posixpath.basename(path).lower()
        ext = posixpath.splitext(base)[1]
        if base in MANIFEST_BASENAMES or re.fullmatch(r"requirements[\w.\-]*\.txt", base):
            continue  # handled through dependency hints above
        is_doc = ext in DOC_EXTENSIONS
        for no, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            commented = stripped.startswith("--") if ext == ".sql" else _is_comment(stripped)
            for db, rules in RULES.items():
                for level, rx in rules:
                    if rx.search(line):
                        eff = "weak" if (commented or is_doc) else level
                        add(db, eff, path, no, line)
                        break  # strongest matching level for this db on this line
            if ext == ".sql" and not commented and _DDL.search(line):
                add("SQL schema (dialect unknown)", "moderate", path, no, line, kind="schema")

    out = list(found.values())
    for det in out:
        items = sorted(ranked[det.name], key=lambda t: (-t[0], t[1]))
        det.evidence = [e for _, _, e in items[:MAX_EVIDENCE]]
        if det.confidence == "weak":
            det.note = ("Only weak indicators: a declared driver or a mention in comments/docs. "
                        "This does not show the database is configured or used.")
        elif det.confidence == "moderate":
            det.note = "Driver or schema usage found in code, but no connection configuration was identified."
        else:
            det.note = "A connection string or URL targeting this database was found (static evidence only)."
    out.sort(key=lambda d: (-CONFIDENCE_RANK[d.confidence], d.name))
    return out
