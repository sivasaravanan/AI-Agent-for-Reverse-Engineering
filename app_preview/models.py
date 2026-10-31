"""Data models for the App Preview panel (standard library only)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

from plugins.build_execution.models import Candidate, Limits

MODES = ("run", "debug", "trace", "test")
KINDS = ("console", "web", "gui", "test")
BACKENDS = ("auto", "docker", "podman", "nerdctl", "finch", "custom")
GUI_IMAGE = "ase-preview-gui:latest"          # built locally from app_preview/images/Dockerfile.gui, never pulled
DISPLAY_NUM = ":99"


@dataclass
class PreviewOptions:
    backend: str = "auto"                         # auto | docker | podman | nerdctl | finch | custom
    custom_exe: str = ""                          # a docker-CLI compatible executable (used when backend == "custom")
    tool_paths: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)    # ecosystem -> image override
    gui_image: str = GUI_IMAGE
    limits: Limits = field(default_factory=lambda: Limits(timeout_s=900, memory_mb=1536))
    auto_start: bool = False
    autotune: bool = True                         # size limits / install location from requirements.txt (raises only)
    live_stats: bool = True
    screen: str = "800x600"
    fps: float = 2.0                              # screen capture rate for GUI apps
    web_refresh_s: float = 3.0
    max_trace_events: int = 300_000
    log_dir: Optional[str] = None


@dataclass
class Entry:
    """Something that can be started: a console program, a web app, a GUI app or a test command."""
    id: str
    title: str
    kind: str                                     # console | web | gui | test
    ecosystem: str                                # python | node | dotnet | maven | gradle | static | custom
    argv: List[str]
    file: str = ""                                # entry file (project relative), "" when unknown
    line: int = 0
    port: Optional[int] = None                    # container port of a web app
    description: str = ""
    env: dict = field(default_factory=dict)
    needs_network: bool = False                   # dependency install needs it (web apps need it for publishing anyway)
    install: Optional[List[str]] = None           # argv of an optional dependency-install step run before the entry
    risks: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def traceable(self) -> bool:
        """Trace and debug use the in-container Python helper: only plain Python scripts / -m modules."""
        return self.ecosystem == "python" and len(self.argv) >= 2 and self.argv[0] == "python" and (
            self.argv[1] == "-m" or self.argv[1].endswith(".py"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PreviewPlan:
    """Everything the user approves. ``plan_hash`` binds an approval to these exact values."""
    entry: Entry
    mode: str
    image: str
    limits: Limits
    source: str
    source_id: str
    argv: List[str]                               # final command executed INSIDE the container
    env: dict = field(default_factory=dict)
    ports: List[Tuple[int, int]] = field(default_factory=list)       # (host, container); host side is always 127.0.0.1
    display: bool = False                         # virtual screen (Xvfb) for GUI apps
    interactive: bool = False                     # stdin attached (debug control channel)
    mount_helper: bool = False                    # read-only mount of the tracer / screen helper at /ase
    breakpoints: List[str] = field(default_factory=list)
    install: bool = False
    risks: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if not self.plan_hash:
            blob = json.dumps({"argv": self.argv, "env": self.env, "image": self.image, "limits": asdict(self.limits),
                               "source": self.source_id, "ports": self.ports, "display": self.display,
                               "interactive": self.interactive, "helper": self.mount_helper, "mode": self.mode},
                              sort_keys=True)
            self.plan_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def candidate(self) -> Candidate:
        """Duck-typed view used by the existing approval dialog."""
        return Candidate(id=f"preview:{self.mode}:{self.entry.id}", ecosystem=self.entry.ecosystem, argv=list(self.argv),
                         description=self.entry.title, source_file=self.entry.file, origin="preview",
                         needs_network=self.limits.network, risks=list(self.risks), notes=list(self.notes), env=dict(self.env))
