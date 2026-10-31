"""What the application can use, how to detect it and the ways to install it per platform.

Install methods are tried in this order by the planner: offline folder -> winget (Windows) -> vendor download (checksum/signature
verified) -> package manager -> private no-administrator install (pip venv / npm prefix) -> manual instructions.
Silent-install switches follow the vendors' documentation; they are listed in every plan before anything runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

WINDOWS, LINUX, DARWIN = "windows", "linux", "darwin"


@dataclass(frozen=True)
class Method:
    kind: str            # winget | direct | venv-pip | npm-local | brew | apt | dnf | pacman | zypper | feature
    ref: str = ""        # winget id / package(s) / direct key / pip or npm package
    user_scope: bool = False     # can install without administrator rights


@dataclass(frozen=True)
class Prereq:
    id: str
    name: str
    category: str                # container-runtime | runtime | analyzer | ai | windows-feature
    summary: str
    why: str
    exes: Tuple[str, ...] = ()
    version_args: Tuple[str, ...] = ("--version",)
    methods: Dict[str, Tuple[Method, ...]] = field(default_factory=dict)
    needs: Tuple[str, ...] = ()
    license_ack: bool = False
    license_note: str = ""
    manual_url: str = ""


@dataclass(frozen=True)
class Direct:
    filename: str
    urls: Dict[str, str]                         # arch -> URL
    publishers: Tuple[str, ...]                  # expected Authenticode subject fragments
    machine_args: Tuple[str, ...]
    user_args: Tuple[str, ...] = ()
    license_args: Tuple[str, ...] = ()           # appended only after the user accepted the licence in the approval dialog
    checksum: str = ""                           # "" | "github-latest:<repo>" (API digest, then 'shasums' asset)
    user_scope: bool = False


DIRECT: Dict[str, Direct] = {
    "docker": Direct(
        "Docker Desktop Installer.exe",
        {"amd64": "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe",
         "arm64": "https://desktop.docker.com/win/main/arm64/Docker%20Desktop%20Installer.exe"},
        ("Docker Inc",), ("install", "--quiet", "--backend=wsl-2", "--no-windows-containers"),
        ("install", "--quiet", "--user", "--backend=wsl-2", "--no-windows-containers"), ("--accept-license",), "", True),
    "podman": Direct(
        "podman-installer-windows-{arch}.exe",
        {"amd64": "https://github.com/containers/podman/releases/latest/download/podman-installer-windows-amd64.exe",
         "arm64": "https://github.com/containers/podman/releases/latest/download/podman-installer-windows-arm64.exe"},
        ("Red Hat", "Podman"), ("/install", "/quiet", "/norestart"), (), (), "github-latest:containers/podman", False),
    "ollama": Direct(
        "OllamaSetup.exe", {"amd64": "https://ollama.com/download/OllamaSetup.exe"}, ("Ollama",),
        ("/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"), ("/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"), (), "", True),
}

_DOCKER_LICENCE = ("Docker Desktop is free for personal use, education and small businesses; larger organisations need a paid "
                   "subscription (Docker Subscription Service Agreement). Podman is free for everyone. Your institution decides.")


def _pm(win=(), lin_apt="", lin_dnf="", lin_pac="", lin_zyp="", brew="") -> Dict[str, Tuple[Method, ...]]:
    out: Dict[str, Tuple[Method, ...]] = {}
    if win:
        out[WINDOWS] = tuple(win)
    lin = tuple(m for m in (Method("apt", lin_apt) if lin_apt else None, Method("dnf", lin_dnf) if lin_dnf else None,
                            Method("pacman", lin_pac) if lin_pac else None, Method("zypper", lin_zyp) if lin_zyp else None) if m)
    if lin:
        out[LINUX] = lin
    if brew:
        out[DARWIN] = (Method("brew", brew),)
    return out


CATALOG: Tuple[Prereq, ...] = (
    Prereq("wsl2", "Windows Subsystem for Linux 2", "windows-feature",
           "Windows feature that Podman and Docker Desktop use to run Linux containers.",
           "Needed by the container runtimes on Windows. Installing it needs administrator rights and usually a restart.",
           methods={WINDOWS: (Method("feature", "wsl"),)}, manual_url="https://learn.microsoft.com/windows/wsl/install"),
    Prereq("podman", "Podman", "container-runtime",
           "Free, daemonless container engine (recommended for institutions: no licence fees).",
           "Phase 4/8 run builds and tests inside disposable containers. Without Podman or Docker nothing from a ZIP is ever executed.",
           ("podman",), needs=("wsl2",),
           methods={WINDOWS: (Method("winget", "RedHat.Podman", True), Method("direct", "podman")),
                    **_pm(lin_apt="podman", lin_dnf="podman", lin_pac="podman", lin_zyp="podman", brew="podman")},
           manual_url="https://podman.io/docs/installation"),
    Prereq("docker", "Docker Desktop", "container-runtime",
           "Container engine with a graphical manager.",
           "Same purpose as Podman. Requires accepting Docker's licence terms.",
           ("docker",), needs=("wsl2",), license_ack=True, license_note=_DOCKER_LICENCE,
           methods={WINDOWS: (Method("winget", "Docker.DockerDesktop"), Method("direct", "docker", True)),
                    **_pm(lin_apt="docker.io", lin_dnf="moby-engine", lin_pac="docker", lin_zyp="docker", brew="cask:docker")},
           manual_url="https://docs.docker.com/get-started/get-docker/"),
    Prereq("python", "Python 3.11+", "runtime", "Python interpreter.",
           "Used to create the private tool environment for Ruff/SQLFluff when this app runs as a standalone EXE.",
           ("python",), methods=_pm(win=(Method("winget", "Python.Python.3.12", True),), lin_apt="python3 python3-venv",
                                    lin_dnf="python3", lin_pac="python", lin_zyp="python3", brew="python"),
           manual_url="https://www.python.org/downloads/"),
    Prereq("node", "Node.js (LTS)", "runtime", "JavaScript runtime and npm.",
           "Needed for ESLint, TypeScript and Pyright, and for the Node build/test commands.", ("node", "npm"),
           methods=_pm(win=(Method("winget", "OpenJS.NodeJS.LTS"),), lin_apt="nodejs npm", lin_dnf="nodejs npm",
                       lin_pac="nodejs npm", lin_zyp="nodejs npm", brew="node"), manual_url="https://nodejs.org/en/download"),
    Prereq("jdk", "Java JDK 17", "runtime", "Java compiler (javac).", "Needed for Java static analysis.", ("javac",), ("-version",),
           methods=_pm(win=(Method("winget", "EclipseAdoptium.Temurin.17.JDK"),), lin_apt="openjdk-17-jdk-headless",
                       lin_dnf="java-17-openjdk-devel", lin_pac="jdk17-openjdk", lin_zyp="java-17-openjdk-devel", brew="openjdk@17"),
           manual_url="https://adoptium.net/"),
    Prereq("dotnet", ".NET SDK 8", "runtime", ".NET compiler and build tools.",
           "Needed for C#/VB.NET diagnostics (builds only ever run after you approve them).", ("dotnet",),
           methods=_pm(win=(Method("winget", "Microsoft.DotNet.SDK.8"),), lin_apt="dotnet-sdk-8.0", lin_dnf="dotnet-sdk-8.0",
                       lin_pac="dotnet-sdk", lin_zyp="dotnet-sdk-8.0", brew="cask:dotnet-sdk"),
           manual_url="https://dotnet.microsoft.com/download"),
    Prereq("go", "Go", "runtime", "Go toolchain.", "Needed for Go static analysis.", ("go",), ("version",),
           methods=_pm(win=(Method("winget", "GoLang.Go"),), lin_apt="golang-go", lin_dnf="golang", lin_pac="go", lin_zyp="go", brew="go"),
           manual_url="https://go.dev/dl/"),
    Prereq("php", "PHP CLI", "runtime", "PHP command line.", "Needed for PHP syntax checks.", ("php",),
           methods=_pm(lin_apt="php-cli", lin_dnf="php-cli", lin_pac="php", lin_zyp="php8-cli", brew="php"),
           manual_url="https://www.php.net/downloads"),
    Prereq("ruff", "Ruff", "analyzer", "Fast Python linter.", "Phase 3 Python lint findings.", ("ruff",), needs=("python",),
           methods={p: (Method("venv-pip", "ruff", True),) for p in (WINDOWS, LINUX, DARWIN)}),
    Prereq("sqlfluff", "SQLFluff", "analyzer", "SQL linter.", "Phase 3 SQL lint findings.", ("sqlfluff",), needs=("python",),
           methods={p: (Method("venv-pip", "sqlfluff", True),) for p in (WINDOWS, LINUX, DARWIN)}),
    Prereq("eslint", "ESLint", "analyzer", "JavaScript linter.", "Phase 3 JavaScript lint findings.", ("eslint",), needs=("node",),
           methods={p: (Method("npm-local", "eslint@9", True),) for p in (WINDOWS, LINUX, DARWIN)}),
    Prereq("typescript", "TypeScript compiler", "analyzer", "tsc.", "Phase 3 TypeScript diagnostics.", ("tsc",), needs=("node",),
           methods={p: (Method("npm-local", "typescript", True),) for p in (WINDOWS, LINUX, DARWIN)}),
    Prereq("pyright", "Pyright", "analyzer", "Python type checker.", "Optional Phase 3 type diagnostics.", ("pyright",), needs=("node",),
           methods={p: (Method("npm-local", "pyright", True),) for p in (WINDOWS, LINUX, DARWIN)}),
    Prereq("ollama", "Ollama", "ai", "Local AI model runner.", "Optional: AI explanations without sending code to a hosted provider.",
           ("ollama",), methods={WINDOWS: (Method("winget", "Ollama.Ollama", True), Method("direct", "ollama", True)),
                                 DARWIN: (Method("brew", "ollama"),)}, manual_url="https://ollama.com/download"),
)

BY_ID: Dict[str, Prereq] = {p.id: p for p in CATALOG}
CONTAINER_RUNTIMES = ("podman", "docker")
SANDBOX_IMAGES = ("python:3.12-slim", "node:20-slim", "mcr.microsoft.com/dotnet/sdk:8.0", "maven:3.9-eclipse-temurin-17", "gradle:8-jdk17")


def get(pid: str) -> Prereq:
    try:
        return BY_ID[pid]
    except KeyError as exc:
        raise KeyError(f"Unknown prerequisite '{pid}'. Known: {', '.join(BY_ID)}") from exc
