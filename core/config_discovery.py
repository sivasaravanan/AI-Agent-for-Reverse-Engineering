"""Phase 2: environment-variable references and database connection configuration.

Only variable NAMES are recorded for definitions; values are never stored. Connection evidence is redacted.
"""
from __future__ import annotations

import posixpath
import re
from typing import Dict, List, Mapping

from core.database_detector import RULES
from models.project_info import DbConnectionConfig, EnvVar, EnvVarUse
from security.secret_scanner import redact

MAX_LINE = 2000
STANDARD_VARS = {
    "PATH", "HOME", "USER", "USERNAME", "TEMP", "TMP", "TMPDIR", "PWD", "SHELL", "LANG", "APPDATA",
    "LOCALAPPDATA", "USERPROFILE", "COMPUTERNAME", "HOSTNAME", "NODE_ENV", "PYTHONPATH", "CI", "TERM",
}
_NAME = r"([A-Za-z_][A-Za-z0-9_]*)"
# (language, regex, default_group) - default_group: group index that is non-empty when a default is supplied
ENV_PATTERNS = [
    ("Python", re.compile(r"""(?:\bos\.)?(?:environ\.get|getenv)\(\s*["']""" + _NAME + r"""["']\s*(,)?"""), 2),
    ("Python", re.compile(r"""(?:\bos\.)?environ\[\s*["']""" + _NAME + r"""["']\s*\]"""), 0),
    ("JavaScript", re.compile(r"""process\.env\.""" + _NAME + r"""(\s*(?:\|\||\?\?))?"""), 2),
    ("JavaScript", re.compile(r"""process\.env\[\s*["']""" + _NAME + r"""["']\s*\](\s*(?:\|\||\?\?))?"""), 2),
    (".NET", re.compile(r"""GetEnvironmentVariable\(\s*"([^"]+)"\s*(\))?"""), 0),
    ("Java", re.compile(r"""System\.getenv\(\s*"([^"]+)"\s*\)"""), 0),
    ("Go", re.compile(r"""os\.(?:Getenv|LookupEnv)\(\s*"([^"]+)"\s*\)"""), 0),
    ("PHP", re.compile(r"""\bgetenv\(\s*['"]([^'"]+)['"]\s*\)"""), 0),
    ("PHP", re.compile(r"""\$_ENV\[\s*['"]([^'"]+)['"]\s*\]"""), 0),
]
_SHELL_REF = re.compile(r"\$\{([A-Za-z_]\w*)(:?-[^}]*)?\}")
_CODE_EXT = {".py": "Python", ".pyw": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
             ".cjs": "JavaScript", ".ts": "JavaScript", ".tsx": "JavaScript", ".cs": ".NET", ".vb": ".NET",
             ".java": "Java", ".go": "Go", ".php": "PHP"}
_TEMPLATE = re.compile(r"example|sample|template|dist$", re.I)
COMMENT = ("#", "//", "/*", "*", "--")
DB_NAME_HINT = re.compile(r"(?i)(database|(^|_)db($|_)|connection|dsn|mongo|postgres|mysql|sqlite|oracle|mssql|sqlserver)")
DB_FROM_NAME = [("postgres", "PostgreSQL"), ("mysql", "MySQL/MariaDB"), ("maria", "MySQL/MariaDB"),
                ("mongo", "MongoDB"), ("sqlite", "SQLite"), ("oracle", "Oracle"), ("mssql", "SQL Server"),
                ("sqlserver", "SQL Server")]


def _is_template(path: str) -> bool:
    return bool(_TEMPLATE.search(posixpath.basename(path)))


def find_env_vars(texts: Mapping[str, str]) -> List[EnvVar]:
    refs: Dict[str, List[EnvVarUse]] = {}
    defs: Dict[str, List[str]] = {}
    for path, text in texts.items():
        base = posixpath.basename(path).lower()
        ext = posixpath.splitext(base)[1]
        # definitions (names only)
        if base.startswith(".env"):
            for line in text.splitlines():
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=", line)
                if m:
                    defs.setdefault(m.group(1), []).append(path)
        elif base == "dockerfile":
            for line in text.splitlines():
                m = re.match(r"\s*(?:ENV|ARG)\s+([A-Za-z_]\w*)", line, re.I)
                if m:
                    defs.setdefault(m.group(1), []).append(path)
        elif base.startswith("docker-compose"):
            for line in text.splitlines():
                m = re.match(r"\s*-\s*([A-Za-z_]\w*)=", line) or re.match(r"\s{2,}([A-Z][A-Z0-9_]+):\s", line)
                if m:
                    defs.setdefault(m.group(1), []).append(path)
        # references
        lang = _CODE_EXT.get(ext)
        shell_like = base.startswith("docker-compose") or base == "dockerfile" or ext in (".sh", ".yml", ".yaml")
        for no, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE or line.lstrip().startswith(COMMENT):
                continue
            if lang:
                for plang, rx, dgroup in ENV_PATTERNS:
                    if not (plang == lang or (plang == "JavaScript" and lang == "JavaScript")):
                        continue
                    for m in rx.finditer(line):
                        has_default = bool(dgroup and m.group(dgroup))
                        refs.setdefault(m.group(1), []).append(EnvVarUse(path, no, lang, has_default))
            elif shell_like and ("docker-compose" in base or base == "dockerfile" or ext == ".sh"):
                for m in _SHELL_REF.finditer(line):
                    refs.setdefault(m.group(1), []).append(EnvVarUse(path, no, "shell/compose", bool(m.group(2))))
    out: List[EnvVar] = []
    for name in sorted(set(refs) | set(defs)):
        uses = refs.get(name, [])
        where = sorted(set(defs.get(name, [])))
        real = [p for p in where if not _is_template(p)]
        if real:
            status = "defined"
        elif where:
            status = "template-only"
        elif name in STANDARD_VARS:
            status = "standard"
        elif uses and all(u.has_default for u in uses):
            status = "has-default"
        elif uses:
            status = "undefined-in-archive"
        else:
            status = "defined"   # defined but never referenced in scanned code
        out.append(EnvVar(name=name, status=status, references=uses[:25], defined_in=where))
    out.sort(key=lambda v: (v.status != "undefined-in-archive", v.name))
    return out


def find_db_connections(texts: Mapping[str, str], env_vars: List[EnvVar]) -> List[DbConnectionConfig]:
    out: List[DbConnectionConfig] = []
    skip_names = {"requirements.txt", "package.json", "pyproject.toml", "pom.xml", "composer.json", "go.mod", "pipfile"}
    for path, text in texts.items():
        base = posixpath.basename(path).lower()
        ext = posixpath.splitext(base)[1]
        if base in skip_names or ext in (".md", ".txt", ".rst") or re.fullmatch(r"requirements[\w.\-]*\.txt", base):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE or line.lstrip().startswith(COMMENT):
                continue
            for db, rules in RULES.items():
                level, rx = rules[0]       # the "strong" rule
                m = rx.search(line)
                if not m:
                    continue
                km = re.search(r"""["']?([A-Za-z_][\w.\-]*)["']?\s*[:=]\s*["']?$""", line[: m.start()])
                form = "jdbc" if m.group(0).lower().startswith("jdbc:") else ("url" if "://" in m.group(0) else "connection-string")
                tmpl = _is_template(path)
                out.append(DbConnectionConfig(
                    database=db, file=path, line=no, key=km.group(1) if km else "", form=form,
                    evidence=redact(line.strip())[:200], confidence="weak" if tmpl else "strong",
                    note="template/example file" if tmpl else ""))
                break
    for v in env_vars:
        if DB_NAME_HINT.search(v.name) and v.references and v.status != "standard":
            db = next((label for hint, label in DB_FROM_NAME if hint in v.name.lower()), "unknown")
            u = v.references[0]
            out.append(DbConnectionConfig(
                database=db, file=u.file, line=u.line, key=v.name, form="env-reference",
                evidence=f"reads environment variable {v.name}", confidence="weak",
                note=f"value comes from the environment ({v.status}); target database is not visible statically"))
    out.sort(key=lambda c: (c.confidence != "strong", c.database, c.file, c.line))
    return out[:300]
