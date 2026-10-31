"""JSON-serialisable data models for the database-analysis plug-in.

Evidence levels (transparent, ordinal - NOT probabilities):
    confirmed - explicit configuration found (connection string/URL, provider/engine
                setting, SQLite data file header, ...).  Reachability and credentials
                are NOT verified; no live connection is ever attempted.
    strong    - driver import, dialect-specific SQL, DB container service, connect call,
                or a connection setting that is a placeholder / example / heuristic.
    weak      - dependency declared only, documentation/comment mention, port hint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CONFIRMED = "confirmed"
STRONG = "strong"
WEAK = "weak"
LEVEL_RANK = {WEAK: 1, STRONG: 2, CONFIRMED: 3}

SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}

SCOPE_PROD = "production-or-unknown"
SCOPE_TEST = "test"
SCOPE_EXAMPLE = "example"


@dataclass
class Evidence:
    db: str
    level: str
    kind: str
    file: str
    line: Optional[int]
    detail: str
    snippet: str = ""
    scope: str = SCOPE_PROD


@dataclass
class DatabaseDetection:
    db: str
    display: str
    status: str                      # highest evidence level
    rationale: str
    evidence_counts: Dict[str, int]
    evidence: List[Evidence]         # capped sample per kind (counts hold the totals)
    scopes: List[str]
    notes: List[str] = field(default_factory=list)


@dataclass
class DbDependency:
    name: str
    version: str
    role: str                        # driver | orm | migration
    db: Optional[str]
    file: str
    line: Optional[int]
    ecosystem: str
    dev: bool = False


@dataclass
class ConnectionConfig:
    db: str
    display: str
    level: str
    kind: str                        # url | jdbc | ado | pdo | sqlite-file | config-keys
    file: str
    line: Optional[int]
    scope: str
    redacted: str                    # secrets masked; raw value is never stored
    host: Optional[str] = None
    port: Optional[str] = None
    database: Optional[str] = None
    user: Optional[str] = None
    password_inline: bool = False
    placeholder: bool = False
    tls_disabled: bool = False
    env_vars: List[str] = field(default_factory=list)


@dataclass
class SchemaObject:
    kind: str                        # table, view, index, procedure, collection, ...
    name: str
    file: str
    line: Optional[int]
    source: str                      # sql | migration | orm
    columns: List[str] = field(default_factory=list)
    references: List[str] = field(default_factory=list)


@dataclass
class OrmEntity:
    orm: str
    name: str
    table: Optional[str]
    file: str
    line: Optional[int]
    fields: Optional[int] = None
    detail: str = ""


@dataclass
class MigrationInfo:
    tool: str
    file: str
    version: str = ""
    creates: List[str] = field(default_factory=list)


@dataclass
class SqlScript:
    file: str
    purpose: str                     # schema | seed | routines | migration | queries | mixed
    statements: Dict[str, int]
    dialect_hints: Dict[str, int]


@dataclass
class EnvReference:
    name: str
    kind: str                        # reference | definition
    file: str
    line: Optional[int]


@dataclass
class DbFinding:
    id: str
    rule: str
    severity: str
    category: str
    title: str
    file: Optional[str]
    line: Optional[int]
    column: Optional[int]
    evidence: str
    explanation: str
    remediation: str
    source: str = "database_analysis"
    confidence: str = "medium"       # high | medium | low
    verification_status: str = "static-detected; not verified at runtime"
    db: Optional[str] = None


@dataclass
class DatabaseAnalysisResult:
    schema_version: str = "1.0"
    plugin_version: str = ""
    databases: List[DatabaseDetection] = field(default_factory=list)
    dependencies: List[DbDependency] = field(default_factory=list)
    connections: List[ConnectionConfig] = field(default_factory=list)
    schema_objects: List[SchemaObject] = field(default_factory=list)
    orm_entities: List[OrmEntity] = field(default_factory=list)
    migrations: List[MigrationInfo] = field(default_factory=list)
    sql_scripts: List[SqlScript] = field(default_factory=list)
    env_references: List[EnvReference] = field(default_factory=list)
    findings: List[DbFinding] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    limitations: List[str] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def database(self, key: str) -> Optional[DatabaseDetection]:
        return next((d for d in self.databases if d.db == key), None)

    def status_map(self) -> Dict[str, str]:
        return {d.db: d.status for d in self.databases}

    def findings_by_severity(self) -> Dict[str, List[DbFinding]]:
        out: Dict[str, List[DbFinding]] = {}
        for f in self.findings:
            out.setdefault(f.severity, []).append(f)
        return out
