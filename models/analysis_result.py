"""Plain data containers for analysis output (JSON friendly)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from models.finding import Finding
from models.project_info import StructureResult

CONFIDENCE_RANK = {"weak": 1, "moderate": 2, "strong": 3}


@dataclass
class Evidence:
    file: str
    line: Optional[int]
    text: str
    level: str = ""   # Phase 2: strong | moderate | weak evidence level (database detector)


@dataclass
class Detection:
    """A detected framework, runtime or database with transparent evidence.

    confidence: strong   - declared in a manifest / explicit connection config
                moderate - used in code or implied by a marker file
                weak     - mentioned only (comment, docs, unused dependency)
    """

    name: str
    kind: str
    confidence: str
    evidence: List[Evidence] = field(default_factory=list)
    evidence_count: int = 0
    note: str = ""


@dataclass
class LanguageStat:
    language: str
    files: int
    bytes: int
    percent_of_bytes: float
    confidence: str
    evidence: List[str] = field(default_factory=list)


@dataclass
class Dependency:
    name: str
    spec: str
    file: str
    line: Optional[int]
    ecosystem: str
    scope: str = "runtime"


@dataclass
class Manifest:
    path: str
    ecosystem: str
    kind: str  # manifest | lockfile | build | config
    dependencies: List[Dependency] = field(default_factory=list)
    parse_error: str = ""


@dataclass
class ArchiveSummary:
    file_count: int = 0
    dir_count: int = 0
    total_uncompressed: int = 0
    total_compressed: int = 0
    vendored_file_count: int = 0
    scanned_text_files: int = 0
    unscanned_files: int = 0
    skipped_entries: List[Dict[str, str]] = field(default_factory=list)
    skipped_total: int = 0


@dataclass
class AnalysisResult:
    schema_version: str = "1.0"
    tool_name: str = "AI Software Engineering Agent"
    tool_version: str = ""
    archive_name: str = ""
    analyzed_at: str = ""
    duration_seconds: float = 0.0
    archive: ArchiveSummary = field(default_factory=ArchiveSummary)
    languages: List[LanguageStat] = field(default_factory=list)
    frameworks: List[Detection] = field(default_factory=list)
    databases: List[Detection] = field(default_factory=list)
    manifests: List[Manifest] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    structure: Optional[StructureResult] = None   # Phase 2 (additive)
    # --- Phases 3-8 (additive; all optional) ---
    static_analysis: Optional[Dict[str, Any]] = None      # Phase 3: tool runs, tool availability, notes
    execution: Optional[Dict[str, Any]] = None            # Phase 4: isolation status + DISCOVERED candidates (nothing is run)
    execution_results: List[Dict[str, Any]] = field(default_factory=list)   # Phase 4: approved runs (appended by UI/CLI)
    database_analysis: Optional[Dict[str, Any]] = None    # Phase 5: redacted DB analysis
    findings_overview: Optional[Dict[str, Any]] = None    # Phase 6: counts by severity/category/source
    ai_notes: List[Dict[str, Any]] = field(default_factory=list)   # Phase 7: UNVERIFIED AI output (metadata + text)
    patches: List[Dict[str, Any]] = field(default_factory=list)    # Phase 8: applied/rolled-back patch outcomes
    code_map: Optional[Dict[str, Any]] = None             # symbols, call graph, hotspots (Stage A)
    input_kind: str = "zip"                                # zip | folder | tar

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
