"""Data models for the Phase 4 controlled build/test execution plugin (standard library only)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _stored(name: str) -> int:
    try:
        from . import storage
        return int(storage.default(name))
    except Exception:  # noqa: BLE001 - a broken settings file must never break a run
        return {"tmp_mb": 1024, "max_workspace_mb": 500}[name]


@dataclass
class Limits:
    cpus: float = 1.0
    memory_mb: int = 1024
    pids: int = 256
    tmp_mb: int = field(default_factory=lambda: _stored("tmp_mb"))              # Storage settings (default 1024)
    timeout_s: int = 300
    max_output_bytes: int = 1_000_000
    max_workspace_mb: int = field(default_factory=lambda: _stored("max_workspace_mb"))   # Storage settings (default 500)
    network: bool = False          # DISABLED by default; enabling it is part of what the user approves


@dataclass
class Options:
    backend: str = "auto"                      # auto | docker | podman
    tool_paths: dict = field(default_factory=dict)   # {"docker": r"C:\...\docker.exe"}
    images: dict = field(default_factory=dict)       # override default image per ecosystem
    limits: Limits = field(default_factory=Limits)
    log_dir: Optional[str] = None              # if set, every result is also written there as JSON
    auto_start: bool = False                   # start an installed-but-stopped Docker/Podman when a run needs it (UI turns this on)
    live_stats: bool = True                    # sample container CPU/memory/process list while a run is in progress


@dataclass
class Candidate:
    id: str                    # e.g. "node:npm-test"
    ecosystem: str             # python | node | dotnet | maven | gradle | custom
    argv: list                 # executed INSIDE the container, never on the host, never via a host shell
    description: str
    source_file: str = ""      # manifest the command was derived from
    source_line: Optional[int] = None
    origin: str = "manifest"   # manifest | convention | user-supplied
    needs_network: bool = False
    risks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    min_memory_mb: int = 0     # 0 = no minimum. Raises the plan's memory limit (shown in the approval dialog) for heavy installs
    min_timeout_s: int = 0     # 0 = no minimum. Raises the plan's timeout for long downloads

    @property
    def display(self) -> str:
        return " ".join(self.argv)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    """Everything the user is asked to approve. plan_hash binds an approval to these exact values."""
    candidate: Candidate
    image: str
    limits: Limits
    source: str
    source_id: str             # sha256 of the zip, or the folder path
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if not self.plan_hash:
            blob = json.dumps({"argv": self.candidate.argv, "env": self.candidate.env, "image": self.image,
                               "limits": asdict(self.limits), "source": self.source_id}, sort_keys=True)
            self.plan_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Approval:
    """Create ONLY after the user has reviewed the plan (UI dialog / CLI prompt)."""
    plan_hash: str
    approved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def for_plan(cls, plan: Plan) -> "Approval":
        return cls(plan_hash=plan.plan_hash)


@dataclass
class ExecutionResult:
    status: str                # completed | failed | timeout | cancelled | refused | skipped | error
    candidate_id: str = ""
    argv: list = field(default_factory=list)
    image: str = ""
    backend: str = ""
    container_name: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    duration_s: float = 0.0
    network_enabled: bool = False
    limits: dict = field(default_factory=dict)
    container_command: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    workspace_cleaned: Optional[bool] = None
    started: str = ""
    finished: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.exit_code == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def write_json(self, path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return p
