"""Discover candidate build/test commands from manifests. READ-ONLY: nothing is executed or extracted.

Candidates are only suggestions. Shell scripts, Makefiles, wrapper scripts (gradlew, mvnw) and install hooks
are never offered automatically.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

from .models import Candidate

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "bin", "obj", "target", "dist", "build"}
DEFAULT_IMAGES = {
    "python": "python:3.12-slim",
    "node": "node:20-slim",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
    "maven": "maven:3.9-eclipse-temurin-17",
    "gradle": "gradle:8-jdk17",
}
ECOSYSTEM_ENV = {
    "python": {"PYTHONDONTWRITEBYTECODE": "1", "PIP_CACHE_DIR": "/tmp/pip-cache", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    "node": {"npm_config_cache": "/tmp/npm-cache", "npm_config_update_notifier": "false"},
    "dotnet": {"DOTNET_CLI_HOME": "/tmp", "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
               "NUGET_PACKAGES": "/tmp/nuget"},
    "maven": {"MAVEN_OPTS": "-Dmaven.repo.local=/tmp/m2"},
    "gradle": {"GRADLE_USER_HOME": "/tmp/gradle"},
}
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare", "pretest", "posttest", "prebuild", "postbuild")
SUSPICIOUS = re.compile(r"\b(curl|wget|Invoke-WebRequest|powershell|sudo|chmod\s+\+x|eval|base64|nc\s+-|rm\s+-rf)\b", re.I)
MAX_NAMES = 50_000


class SourceView:
    """Uniform read-only view over a ZIP or a folder (single top-level folder is stripped)."""

    def __init__(self, source):
        self.path = Path(source)
        self._zip = None
        self._prefix = ""
        if self.path.is_dir():
            self.names = self._walk()
        else:
            self._zip = zipfile.ZipFile(self.path)  # BadZipFile propagates to the caller
            raw = [i.filename.replace("\\", "/") for i in self._zip.infolist() if not i.is_dir()]
            raw = [n for n in raw if self._safe(n)][:MAX_NAMES]
            tops = {PurePosixPath(n).parts[0] for n in raw}
            if len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in raw):
                self._prefix = tops.pop() + "/"
            self.names = [n[len(self._prefix):] for n in raw
                          if not (set(PurePosixPath(n[len(self._prefix):]).parts[:-1]) & SKIP_DIRS)]

    @staticmethod
    def _safe(n: str) -> bool:
        return not (n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in PurePosixPath(n).parts or "\x00" in n)

    def _walk(self) -> list:
        out = []
        for d, dirs, files in os.walk(self.path, followlinks=False):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not (Path(d) / x).is_symlink()]
            for f in files:
                p = Path(d) / f
                if not p.is_symlink():
                    out.append(p.relative_to(self.path).as_posix())
                if len(out) >= MAX_NAMES:
                    return out
        return out

    def read(self, name: str, limit: int = 1_000_000):
        try:
            if self._zip is None:
                return (self.path / name).read_bytes()[:limit].decode("utf-8", "replace")
            return self._zip.read(self._prefix + name)[:limit].decode("utf-8", "replace")
        except (KeyError, OSError, zipfile.BadZipFile):
            return None

    def close(self):
        if self._zip:
            self._zip.close()


def _line_of(text: str, needle: str):
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return None


def _rel(name: str) -> str:
    return "./" + name  # never lets a file name be parsed as an option


def discover_candidates(source) -> tuple:
    """Return (candidates, notes). Raises zipfile.BadZipFile / FileNotFoundError for bad sources."""
    src = SourceView(source)
    try:
        return _discover(src)
    finally:
        src.close()


def _discover(src: SourceView) -> tuple:
    names, cands, notes = src.names, [], []
    root_files = {n for n in names if "/" not in n}
    lower = {n.lower(): n for n in names}

    # ---- Python -----------------------------------------------------------------------------
    py_tests = [n for n in names if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", n)]
    if py_tests:
        env = dict(ECOSYSTEM_ENV["python"])
        cands.append(Candidate("python:unittest", "python", ["python", "-m", "unittest", "discover", "-v"],
                               "Run unittest-style tests (standard library)", py_tests[0], origin="convention",
                               risks=["Test code from the project executes inside the container"], env=env))
        cands.append(Candidate("python:pytest", "python", ["python", "-m", "pytest", "-q"],
                               "Run pytest (pytest must already exist in the image)", py_tests[0], origin="convention",
                               risks=["Test code from the project executes inside the container"],
                               notes=["The stock python image has no pytest and the network is off: expect "
                                      "'No module named pytest' unless you use a prepared image."], env=env))
        if "requirements.txt" in root_files:
            cands.append(Candidate(
                "python:pip-pytest", "python",
                ["sh", "-c", "pip install --no-input --target /tmp/deps -r requirements.txt pytest && "
                             "PYTHONPATH=/tmp/deps python -m pytest -q"],
                "Install requirements into the container, then run pytest", "requirements.txt", origin="convention",
                needs_network=True,
                risks=["Network access is required (explicit approval)",
                       "pip may build source distributions, which run arbitrary setup code (inside the container)",
                       "Test code from the project executes inside the container"], env=env))

        if "requirements.txt" in root_files:
            # Disk-backed variant for large dependency sets (torch, tensorflow, opencv ...). /tmp is a RAM-backed tmpfs
            # that counts against the container memory limit, so the default 'pip-pytest' can be OOM-killed (exit 137).
            # Here packages and pip's temp files go to /work (a bind-mounted scratch folder on disk), without pip cache.
            req_text = src.read("requirements.txt") or ""
            heavy_env = dict(env)
            heavy_env.update({"PIP_NO_CACHE_DIR": "1", "TMPDIR": "/work/.tmp"})
            heavy_notes = ["Packages are installed to /work/.deps on disk instead of the RAM-backed /tmp, with pip's cache off.",
                           "Memory limit is raised to at least 3072 MB and the timeout to at least 1800 s (shown in the plan).",
                           "The scratch folder on the host needs several GB free while it runs; it is deleted afterwards."]
            if re.search(r"(?im)^\s*(torch|torchvision|torchaudio|ultralytics|deepface|easyocr)\b", req_text):
                heavy_env["PIP_EXTRA_INDEX_URL"] = "https://download.pytorch.org/whl/cpu"
                heavy_notes.append("torch is taken from the CPU-only index (about 200 MB instead of the multi-GB CUDA build).")
            cands.append(Candidate(
                "python:pip-pytest-heavy", "python",
                ["sh", "-c", "mkdir -p /work/.deps /work/.tmp && "
                             "pip install --no-input --no-cache-dir --target /work/.deps -r requirements.txt pytest && "
                             "PYTHONPATH=/work/.deps python -m pytest -q"],
                "Install large requirements to disk (not RAM), then run pytest", "requirements.txt", origin="convention",
                needs_network=True, min_memory_mb=3072, min_timeout_s=1800,
                risks=["Network access is required (explicit approval)",
                       "pip may build source distributions, which run arbitrary setup code (inside the container)",
                       "Test code from the project executes inside the container"],
                notes=heavy_notes, env=heavy_env))
    # ---- Node -------------------------------------------------------------------------------
    if "package.json" in root_files:
        text = src.read("package.json") or ""
        try:
            scripts = (json.loads(text).get("scripts") or {})
            if not isinstance(scripts, dict):
                raise ValueError
        except (ValueError, AttributeError):
            scripts = {}
            notes.append("package.json could not be parsed; no Node commands offered.")
        risks = []
        for k in LIFECYCLE:
            if k in scripts:
                risks.append(f"Lifecycle hook '{k}' runs automatically: {str(scripts[k])[:120]}")
        for k, v in scripts.items():
            m = SUSPICIOUS.search(str(v))
            if m:
                risks.append(f"Script '{k}' contains '{m.group(1)}': {str(v)[:120]}")
        env = dict(ECOSYSTEM_ENV["node"])
        base = ["Project scripts execute inside the container"] + risks
        has_lock = "package-lock.json" in root_files
        if "test" in scripts:
            cands.append(Candidate("node:npm-test", "node", ["npm", "test"], "Run the 'test' script",
                                   "package.json", _line_of(text, '"test"'), risks=base, env=env,
                                   notes=["node_modules is not copied; without network, dependencies are missing."]))
            install = "npm ci --ignore-scripts" if has_lock else "npm install --ignore-scripts"
            cands.append(Candidate("node:npm-install-test", "node", ["sh", "-c", install + " && npm test"],
                                   "Install dependencies (install hooks disabled), then run 'test'",
                                   "package.json", _line_of(text, '"test"'), needs_network=True, env=env,
                                   risks=["Network access is required (explicit approval)"] + base))
        if "build" in scripts:
            cands.append(Candidate("node:npm-build", "node", ["npm", "run", "build"], "Run the 'build' script",
                                   "package.json", _line_of(text, '"build"'), risks=base, env=env))

    # ---- .NET -------------------------------------------------------------------------------
    projs = [n for n in names if n.lower().endswith((".csproj", ".vbproj", ".fsproj"))]
    slns = [n for n in root_files if n.lower().endswith(".sln")]
    if projs or slns:
        target = slns[0] if len(slns) == 1 else sorted(projs, key=lambda n: (n.count("/"), n))[0]
        risks = ["MSBuild executes project-defined targets and build events inside the container"]
        for p in projs[:20]:
            t = src.read(p, 200_000) or ""
            if re.search(r"<Exec\b|PreBuildEvent|PostBuildEvent", t):
                risks.append(f"{p} defines <Exec>/build events")
        env = dict(ECOSYSTEM_ENV["dotnet"])
        cands.append(Candidate("dotnet:build", "dotnet", ["dotnet", "build", _rel(target), "--no-restore"],
                               "Build without restoring packages", target, origin="convention", risks=risks, env=env,
                               notes=["Packages are not restored offline: expect NETSDK1004 unless obj/ is present."]))
        cands.append(Candidate("dotnet:restore-test", "dotnet", ["dotnet", "test", _rel(target)],
                               "Restore packages, build and run tests", target, origin="convention",
                               needs_network=True, risks=["Network access is required (explicit approval)"] + risks,
                               env=env))

    # ---- Java -------------------------------------------------------------------------------
    if "pom.xml" in root_files:
        env = dict(ECOSYSTEM_ENV["maven"])
        r = ["Maven plugins and test code execute inside the container"]
        cands.append(Candidate("maven:test-offline", "maven", ["mvn", "-B", "-o", "test"], "mvn test (offline)",
                               "pom.xml", origin="convention", risks=r, env=env,
                               notes=["Offline mode needs a local repository; expect missing-artifact errors."]))
        cands.append(Candidate("maven:test", "maven", ["mvn", "-B", "test"], "mvn test (downloads dependencies)",
                               "pom.xml", origin="convention", needs_network=True, env=env,
                               risks=["Network access is required (explicit approval)"] + r))
    gradle = [n for n in root_files if n in ("build.gradle", "build.gradle.kts")]
    if gradle:
        env = dict(ECOSYSTEM_ENV["gradle"])
        r = ["Gradle build scripts and test code execute inside the container"]
        cands.append(Candidate("gradle:test-offline", "gradle", ["gradle", "test", "--offline", "--no-daemon"],
                               "gradle test (offline)", gradle[0], origin="convention", risks=r, env=env))
        cands.append(Candidate("gradle:test", "gradle", ["gradle", "test", "--no-daemon"],
                               "gradle test (downloads dependencies)", gradle[0], origin="convention",
                               needs_network=True, env=env,
                               risks=["Network access is required (explicit approval)"] + r))

    skipped = sorted({n for n in names if re.search(r"(\.sh|\.bat|\.cmd|\.ps1)$", n, re.I)
                      or os.path.basename(n) in ("Makefile", "gradlew", "mvnw", "Dockerfile", "docker-compose.yml")})
    if skipped:
        notes.append("Not offered automatically (project-supplied scripts): " + ", ".join(skipped[:12])
                     + (" ..." if len(skipped) > 12 else "") + ". Use a custom command if you must run one.")
    if not cands:
        notes.append("No build/test commands could be derived from the manifests found.")
    return cands, notes


def custom_candidate(argv: list, ecosystem: str = "python", needs_network: bool = False) -> Candidate:
    """A user-typed command. Still goes through the same plan/approval/sandbox path."""
    if not argv or any((not isinstance(a, str)) or "\x00" in a for a in argv):
        raise ValueError("Custom command must be a non-empty list of strings.")
    return Candidate(id="custom:" + ecosystem, ecosystem=ecosystem, argv=list(argv),
                     description="User-supplied command", origin="user-supplied", needs_network=needs_network,
                     risks=["User-supplied command: not derived from a manifest"],
                     env=dict(ECOSYSTEM_ENV.get(ecosystem, {})))
