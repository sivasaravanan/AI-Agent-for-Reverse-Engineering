"""Phase 2 data containers: structure, imports, configuration and confidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TreeNode:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    file_count: int = 0
    vendored: bool = False      # directory such as node_modules; counted but not expanded
    truncated: bool = False     # children omitted because of tree size/depth caps
    children: List["TreeNode"] = field(default_factory=list)


@dataclass
class EntryPoint:
    file: str
    line: Optional[int]
    kind: str
    language: str
    evidence: str
    confidence: str  # strong | moderate | weak


@dataclass
class ConfigFile:
    path: str
    kind: str
    size: int


@dataclass
class ImportEdge:
    source: str
    line: Optional[int]
    language: str
    target: str                 # raw module / path as written
    category: str               # internal | external | stdlib | builtin | unresolved
    resolved: str = ""          # archive path when internal
    package: str = ""           # top-level package name when external


@dataclass
class ImportSummary:
    edge_count: int = 0
    files_with_imports: int = 0
    internal_edges: int = 0
    external_packages: List[Dict] = field(default_factory=list)   # {name, language, count, declared, first_file, first_line}
    most_depended_on: List[Dict] = field(default_factory=list)    # {file, count}
    cycles: List[List[str]] = field(default_factory=list)
    unused_declared: List[Dict] = field(default_factory=list)     # weak indicator only
    by_language: Dict[str, int] = field(default_factory=dict)


@dataclass
class EnvVarUse:
    file: str
    line: int
    language: str
    has_default: bool


@dataclass
class EnvVar:
    name: str
    status: str                 # defined | template-only | has-default | standard | undefined-in-archive
    references: List[EnvVarUse] = field(default_factory=list)
    defined_in: List[str] = field(default_factory=list)   # names only; values are never recorded


@dataclass
class DbConnectionConfig:
    database: str               # database name or "unknown"
    file: str
    line: int
    key: str
    form: str                   # url | connection-string | jdbc | env-reference
    evidence: str               # redacted
    confidence: str             # strong | weak
    note: str = ""


@dataclass
class ConfidenceIndicator:
    subject: str
    kind: str                   # language | framework | database
    evidence_class: str         # confirmed | strong | weak | unknown
    reasons: List[str] = field(default_factory=list)


@dataclass
class StructureWarning:
    code: str
    severity: str               # low | info | medium
    message: str
    file: str = ""
    line: Optional[int] = None


@dataclass
class StructureResult:
    tree: Optional[TreeNode] = None
    entry_points: List[EntryPoint] = field(default_factory=list)
    config_files: List[ConfigFile] = field(default_factory=list)
    imports: List[ImportEdge] = field(default_factory=list)
    imports_truncated: int = 0
    import_summary: ImportSummary = field(default_factory=ImportSummary)
    env_vars: List[EnvVar] = field(default_factory=list)
    db_connections: List[DbConnectionConfig] = field(default_factory=list)
    dependency_summary: Dict[str, Dict[str, int]] = field(default_factory=dict)
    unpinned_dependencies: List[Dict] = field(default_factory=list)
    confidence: List[ConfidenceIndicator] = field(default_factory=list)
    warnings: List[StructureWarning] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
