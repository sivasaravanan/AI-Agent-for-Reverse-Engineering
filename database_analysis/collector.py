"""Accumulates evidence/connections/findings during a scan (single-threaded, per analysis run)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from . import signatures as sig
from .context import FileContext
from .models import (CONFIRMED, LEVEL_RANK, SCOPE_EXAMPLE, SCOPE_TEST, STRONG, ConnectionConfig, DbDependency,
                     DbFinding, EnvReference, Evidence, MigrationInfo, OrmEntity, SchemaObject, SqlScript)
from .options import AnalysisOptions
from .redaction import is_placeholder_value, looks_templated, redact_text, snippet

# evidence kinds that prove the file touches a database (used by the credential-literal second pass)
DB_ACCESS_KINDS = {"import", "connection", "provider", "config_key", "connect_call", "service"}
_ENV_IN_TEXT = re.compile(r"\$\{(\w+)|\$([A-Za-z_]\w*)|%(\w+)%")


class Collector:
    def __init__(self, opts: AnalysisOptions):
        self.opts = opts
        self.evidence: Dict[str, List[Evidence]] = defaultdict(list)
        self.counts: Dict[str, Counter] = defaultdict(Counter)
        self._stored: Counter = Counter()
        self._seen: Set[Tuple] = set()
        self.dependencies: List[DbDependency] = []
        self.connections: List[ConnectionConfig] = []
        self.schema: List[SchemaObject] = []
        self.orm: List[OrmEntity] = []
        self.migrations: List[MigrationInfo] = []
        self.scripts: List[SqlScript] = []
        self.envs: List[EnvReference] = []
        self.findings: List[DbFinding] = []
        self.skipped: Counter = Counter()
        self.files_with_db_access: Set[str] = set()
        self.flagged_lines: Set[Tuple[str, int]] = set()
        self._rule_counts: Counter = Counter()
        self.sql_dialect_dbs: Dict[str, Counter] = defaultdict(Counter)

    # ------------------------------------------------------------ evidence
    def add(self, db: str, level: str, kind: str, ctx: FileContext, line: Optional[int],
            detail: str, raw_line: str = "") -> None:
        if ctx.scope == SCOPE_EXAMPLE and level == CONFIRMED:
            level = STRONG
        key = (db, ctx.path, line, level)
        if key in self._seen:
            return
        self._seen.add(key)
        self.counts[db][kind] += 1
        if kind in DB_ACCESS_KINDS:
            self.files_with_db_access.add(ctx.path)
        if self._stored[(db, kind)] >= self.opts.max_evidence_per_kind:
            return
        self._stored[(db, kind)] += 1
        self.evidence[db].append(Evidence(db, level, kind, ctx.path, line, detail,
                                          snippet(raw_line) if raw_line else "", ctx.scope))

    # ------------------------------------------------------------ dependencies / env
    def dependency(self, ctx: FileContext, name: str, version: str, role: str, db: Optional[str],
                   line: Optional[int], ecosystem: str, dev: bool) -> None:
        self.dependencies.append(DbDependency(name, version.strip()[:60], role, db, ctx.path, line, ecosystem, dev))
        if db:
            self.add(db, "weak", "dependency", ctx, line,
                     f"{name}{(' ' + version.strip()) if version.strip() else ''} declared in {ecosystem} manifest"
                     + (" (dev dependency)" if dev else ""))

    def env(self, name: str, kind: str, ctx: FileContext, line: int) -> None:
        if sig.DB_ENV_NAME_RE.match(name):
            rec = EnvReference(name, kind, ctx.path, line)
            if rec not in self.envs:
                self.envs.append(rec)

    # ------------------------------------------------------------ connections
    def connection(self, ctx: FileContext, line: int, db: str, kind: str, raw: str, *,
                   host: Optional[str] = None, port: Optional[str] = None, database: Optional[str] = None,
                   user: Optional[str] = None, password: Optional[str] = None, explicit: bool = True,
                   tls_disabled: bool = False) -> ConnectionConfig:
        has_pw = bool(password) and not looks_templated(password or "")

        def _safe(v: Optional[str]) -> Optional[str]:
            # structured fields are sanitised too, so a mis-parse can never carry a secret into a report
            if v is None:
                return None
            v = redact_text(v)
            if has_pw and password and password in v:
                v = v.replace(password, "****")
            return v[:200]
        host, port, database, user = _safe(host), _safe(port), _safe(database), _safe(user)
        if port and re.search(r"[;&=\s]", port):
            port = None
        placeholder = any(is_placeholder_value(v) for v in (host, database, user) if v)
        level = CONFIRMED if (explicit and not placeholder and ctx.scope != SCOPE_EXAMPLE) else STRONG
        red = redact_text(raw.strip())
        if has_pw and password and password in red:   # positional/unknown syntaxes: mask the known secret itself
            red = red.replace(password, "****")
        red = red[:300]
        env_vars = sorted({g for m in _ENV_IN_TEXT.finditer(raw) for g in m.groups() if g})
        local = (host or "").lower() in sig.LOCAL_HOSTS or db == "sqlite"
        conn = ConnectionConfig(db, sig.DB_DISPLAY.get(db, db), level, kind, ctx.path, line, ctx.scope, red,
                                host, port, database, user, has_pw, placeholder,
                                bool(tls_disabled and not local), env_vars)
        self.connections.append(conn)
        detail = f"{kind} connection setting" + (" (placeholder/example values)" if placeholder else "") + \
                 ("" if explicit else " (database type inferred heuristically)")
        self.add(db, level, "connection", ctx, line, detail, red)
        # ---- findings derived from the connection
        if has_pw and ctx.scope != SCOPE_EXAMPLE:
            self.flagged_lines.add((ctx.path, line))
            self.finding(
                "DB-SEC-001", "low" if ctx.scope == SCOPE_TEST else "high", "Hard-coded database credential",
                ctx.path, line, red,
                "A database password is written directly in the project files. Anyone with access to the "
                "source or archive can read it. The value is masked in this report.",
                "Move the secret to an environment variable or secret manager, rotate the exposed password, "
                "and keep only a placeholder in committed files.", "high", db, "database-security")
        if user and user.lower() in sig.ADMIN_USERS and db not in ("sqlite",) and ctx.scope not in (SCOPE_EXAMPLE,):
            self.finding(
                "DB-SEC-002", "low", "Connection uses a privileged/default database account", ctx.path, line, red,
                f"The connection logs in as '{user}', a well-known administrative account. Applications should "
                "use a least-privilege account.",
                "Create a dedicated application user with only the permissions the app needs.", "medium", db,
                "database-security")
        if conn.tls_disabled:
            self.finding(
                "DB-SEC-004", "low", "Transport encryption / certificate validation disabled", ctx.path, line, red,
                "The connection to a non-local host disables TLS or certificate validation, exposing credentials "
                "and data to interception.",
                "Enable TLS (e.g. sslmode=require/verify-full, Encrypt=True) and validate server certificates.",
                "medium", db, "database-security")
        return conn

    # ------------------------------------------------------------ findings
    def finding(self, rule: str, severity: str, title: str, file: Optional[str], line: Optional[int],
                evidence: str, explanation: str, remediation: str, confidence: str,
                db: Optional[str] = None, category: str = "database") -> None:
        key = (rule, file)
        if self._rule_counts[key] >= self.opts.max_findings_per_rule_per_file:
            return
        self._rule_counts[key] += 1
        self.findings.append(DbFinding("", rule, severity, category, title, file, line, None,
                                       redact_text(evidence)[:300], explanation, remediation,
                                       confidence=confidence, db=db))
