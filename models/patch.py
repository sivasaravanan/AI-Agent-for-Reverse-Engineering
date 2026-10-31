"""Phase 8 data containers: patches, backups and verification results."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set


class PatchError(Exception):
    """Raised with a user-presentable message when a patch operation is refused."""


class NoFixAvailable(PatchError):
    """No safe, automatic fix exists for this finding."""


@dataclass
class Patch:
    id: str
    file: str                       # path relative to the workspace root ('/' separators)
    workspace_root: str
    original_text: str
    new_text: str
    original_sha256: str
    rationale: str = ""
    caution: str = ""
    source: str = "manual"          # manual | rule-based | ai-unverified
    finding_id: str = ""
    rule_id: str = ""
    status: str = "proposed"        # proposed | applied | reverted | rolled-back
    diff_hash: str = ""             # set when the diff has been generated for display
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BackupInfo:
    patch_id: str
    file: str
    target_path: str
    backup_path: str
    manifest_path: str
    original_sha256: str
    new_sha256: str
    applied_at: str


@dataclass
class RunSummary:
    """Result of one isolated test run (produced by a Phase 4 runner)."""
    exit_code: int
    passed: Set[str] = field(default_factory=set)
    failed: Set[str] = field(default_factory=set)
    output: str = ""
    timed_out: bool = False


@dataclass
class StaticComparison:
    target_resolved: Optional[bool] = None     # None = patch not tied to a rule
    resolved: List[Dict[str, Any]] = field(default_factory=list)
    new: List[Dict[str, Any]] = field(default_factory=list)
    unchanged_count: int = 0


@dataclass
class TestComparison:
    __test__ = False
    ran: bool = False
    baseline_passed: int = 0
    baseline_failed: int = 0
    after_passed: int = 0
    after_failed: int = 0
    regressions: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)
    timed_out: bool = False


@dataclass
class VerificationResult:
    level: str = "none"             # none | static-checked | test-verified | failed
    static: StaticComparison = field(default_factory=StaticComparison)
    tests: TestComparison = field(default_factory=TestComparison)
    skipped_reasons: List[str] = field(default_factory=list)
    rollback_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    claim: str = "Not verified."


@dataclass
class ApplyOutcome:
    patch: Patch
    backup: Optional[BackupInfo]
    verification: VerificationResult
    reverted: bool = False
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
