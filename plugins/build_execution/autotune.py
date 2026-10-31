"""Automatic limits for a project, derived from its requirements.txt (read-only; nothing is executed or downloaded).

Why: a fixed 300 s / 1 GiB plan fails for projects whose dependencies are large (torch, tensorflow, opencv ...).
This module estimates the install size from the packages named in requirements.txt and returns:
  * a timeout and memory limit that fit the project,
  * for large installs, a command that puts packages on disk (/work/.deps) instead of the RAM-backed /tmp,
  * the CPU-only PyTorch index when a torch-based package is present.
Everything it changes is visible in the approval dialog (limits, command, notes) and bound to the plan hash.
Limits are only ever RAISED, never lowered, and a command that does not run `pip install` is left untouched.
"""
from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from .discovery import SourceView
from .models import Candidate, Limits

THROUGHPUT_MB_S = 1.0        # deliberately pessimistic download + unpack speed (observed 1-7 MB/s)
DEFAULT_PKG_MB = 3.0         # unknown package
RESOLVER_FACTOR = 1.25       # transitive dependencies and version back-tracking
BASE_SECONDS = 180           # image start, pip resolver, test/run time
MAX_TIMEOUT_S = 7200
MAX_MEMORY_MB = 16384
DISK_INSTALL_MB = 150        # at or above this estimate, install to disk instead of tmpfs
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

# approximate wheel download sizes in MB (linux x86_64; torch is the CPU-only build)
SIZES = {
    "torch": 200, "torchvision": 8, "torchaudio": 4, "tensorflow": 600, "tensorflow-cpu": 220, "keras": 2, "tf-keras": 2,
    "jax": 2, "jaxlib": 80, "paddlepaddle": 150, "paddleocr": 5, "onnxruntime": 15, "transformers": 10,
    "sentence-transformers": 1, "opencv-python": 70, "opencv-python-headless": 60, "opencv-contrib-python": 75,
    "scipy": 35, "numpy": 17, "pandas": 12, "scikit-learn": 12, "scikit-image": 14, "matplotlib": 9, "pillow": 6,
    "lxml": 5, "pyarrow": 40, "polars": 30, "spacy": 6, "xgboost": 250, "lightgbm": 4, "mediapipe": 35, "numba": 3,
    "playwright": 40, "easyocr": 3, "ultralytics": 1, "deepface": 1, "shapely": 3, "reportlab": 2, "grpcio": 6,
    "cryptography": 4, "sqlalchemy": 3, "pytesseract": 1, "pdfplumber": 1, "pypdf": 1, "openpyxl": 1,
}
# packages that pull other large packages with them
IMPLIES = {
    "ultralytics": ["torch", "torchvision", "opencv-python", "scipy", "matplotlib", "pandas", "pillow"],
    "deepface": ["tensorflow", "tf-keras", "opencv-python", "pandas", "pillow"],
    "easyocr": ["torch", "torchvision", "opencv-python-headless", "scikit-image", "scipy", "pillow"],
    "sentence-transformers": ["torch", "transformers", "scikit-learn", "scipy"],
    "transformers": [], "torchvision": ["torch"], "torchaudio": ["torch"], "keras": ["tensorflow"],
    "scikit-image": ["scipy", "numpy", "pillow"], "pandas": ["numpy"], "scikit-learn": ["numpy", "scipy"],
    "mediapipe": ["opencv-contrib-python", "numpy"], "paddleocr": ["paddlepaddle", "opencv-python"],
}
TORCH_FAMILY = {"torch", "torchvision", "torchaudio"}
HEAVY_RAM = {"torch", "tensorflow", "tensorflow-cpu", "jax", "jaxlib", "paddlepaddle", "xgboost"}


@dataclass
class Profile:
    packages: list = field(default_factory=list)
    heavy: list = field(default_factory=list)          # packages that need a lot of memory or disk
    est_download_mb: int = 0
    memory_mb: int = 0
    timeout_s: int = 0
    to_disk: bool = False
    cpu_torch: bool = False
    reasons: list = field(default_factory=list)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirements(text: str) -> list:
    """Package names from requirements.txt text (versions, extras, markers, comments and option lines ignored)."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://", ".", "/")):
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            out.append(_norm(m.group(1)))
    return out


def profile_text(text: str) -> Optional[Profile]:
    names = parse_requirements(text)
    if not names:
        return None
    full, stack = set(), list(names)
    while stack:                                       # expand implied packages (each counted once)
        n = stack.pop()
        if n in full:
            continue
        full.add(n)
        stack += IMPLIES.get(n, [])
    listed = {n for n in full if n in SIZES}
    total = sum(SIZES[n] for n in listed) + DEFAULT_PKG_MB * len([n for n in names if n not in SIZES])
    total = int(total * RESOLVER_FACTOR)
    p = Profile(packages=names, est_download_mb=total)
    p.heavy = sorted(n for n in full if n in HEAVY_RAM or SIZES.get(n, 0) >= 60)
    p.cpu_torch = bool(full & TORCH_FAMILY)
    p.to_disk = total >= DISK_INSTALL_MB or bool(full & HEAVY_RAM)
    if full & HEAVY_RAM:
        p.memory_mb = 3072
        p.reasons.append("large ML packages (" + ", ".join(sorted(full & HEAVY_RAM)) + ") need about 3 GB while installing")
    elif total >= 300:
        p.memory_mb = 2048
        p.reasons.append(f"estimated install of about {total} MB")
    else:
        p.memory_mb = 1024
    p.timeout_s = min(MAX_TIMEOUT_S, int(math.ceil((BASE_SECONDS + total / THROUGHPUT_MB_S) / 60.0) * 60))
    p.reasons.append(f"estimated download about {total} MB at a pessimistic {THROUGHPUT_MB_S:g} MB/s")
    return p


def profile_source(source) -> Optional[Profile]:
    """Profile the project's root requirements.txt, or None when there is none (or the source cannot be read)."""
    try:
        src = SourceView(source)
    except Exception:  # noqa: BLE001 - bad zip / missing folder: the normal path reports it
        return None
    try:
        text = src.read("requirements.txt")
    finally:
        src.close()
    return profile_text(text) if text else None


def _runs_pip_install(argv) -> bool:
    return bool(re.search(r"\bpip3?\s+install\b|-m\s+pip\s+install\b", " ".join(argv)))


def apply_autotune(candidate: Candidate, limits: Limits, source) -> tuple:
    """Return (candidate, limits, notes). Inputs are never modified; limits only go up."""
    if candidate.ecosystem != "python" or not _runs_pip_install(candidate.argv):
        return candidate, limits, []
    prof = profile_source(source)
    if prof is None:
        return candidate, limits, []
    notes = []
    mem = max(limits.memory_mb, prof.memory_mb, candidate.min_memory_mb)
    tout = max(limits.timeout_s, prof.timeout_s, candidate.min_timeout_s)
    if mem > limits.memory_mb:
        notes.append(f"Memory raised {limits.memory_mb} -> {mem} MB")
    if tout > limits.timeout_s:
        notes.append(f"Timeout raised {limits.timeout_s} -> {tout} s")
    new_limits = Limits(**{**vars(limits), "memory_mb": mem, "timeout_s": tout, "network": True})
    argv, env = list(candidate.argv), dict(candidate.env)
    if prof.to_disk and len(argv) >= 3 and argv[0] == "sh" and argv[1] == "-c" and "/tmp/deps" in argv[2] \
            and "/work/.deps" not in argv[2]:
        script = argv[2].replace("/tmp/deps", "/work/.deps")
        script = re.sub(r"(pip3?\s+install)\b", r"\1 --no-cache-dir", script, count=1) \
            if "--no-cache-dir" not in script else script
        argv[2] = "mkdir -p /work/.deps /work/.tmp && " + script
        env.update({"PIP_NO_CACHE_DIR": "1", "TMPDIR": "/work/.tmp"})
        notes.append("Packages are installed to /work/.deps on disk instead of the RAM-backed /tmp (which counts against the "
                     "memory limit)")
    if prof.cpu_torch and "PIP_EXTRA_INDEX_URL" not in env:
        env["PIP_EXTRA_INDEX_URL"] = CPU_TORCH_INDEX
        notes.append("torch is taken from the CPU-only index (about 200 MB instead of the multi-GB CUDA build)")
    cand = dataclasses.replace(candidate, argv=argv, env=env, needs_network=True,
                               notes=list(candidate.notes) + ["Auto-tuned for this project: " + "; ".join(prof.reasons)]
                               + notes)
    return cand, new_limits, notes


def retry_limits(status: str, exit_code, limits: Limits) -> Optional[Limits]:
    """Larger limits for one retry after a timeout or an out-of-memory kill, or None when a retry would not help."""
    if status == "timeout" and limits.timeout_s < MAX_TIMEOUT_S:
        return Limits(**{**vars(limits), "timeout_s": min(MAX_TIMEOUT_S, limits.timeout_s * 2)})
    if exit_code == 137 and limits.memory_mb < MAX_MEMORY_MB:
        return Limits(**{**vars(limits), "memory_mb": min(MAX_MEMORY_MB, limits.memory_mb * 2)})
    return None


def apply_preview_install(source, limits: Limits, argv: list, env: dict) -> tuple:
    """App Preview variant: argv is ['sh', '-c', '<pip install --target /tmp/ase-site ... && exec "$@"', 'ase', ...program].
    Returns (limits, argv, env, notes); inputs are not modified and limits only go up."""
    if len(argv) < 4 or argv[0] != "sh" or argv[1] != "-c" or not _runs_pip_install([argv[2]]):
        return limits, argv, env, []
    prof = profile_source(source)
    if prof is None:
        return limits, argv, env, []
    mem, tout = max(limits.memory_mb, prof.memory_mb), max(limits.timeout_s, prof.timeout_s)
    notes = []
    if mem > limits.memory_mb:
        notes.append(f"Memory raised {limits.memory_mb} -> {mem} MB")
    if tout > limits.timeout_s:
        notes.append(f"Timeout raised {limits.timeout_s} -> {tout} s")
    new_limits = Limits(**{**vars(limits), "memory_mb": mem, "timeout_s": tout})
    argv, env = list(argv), dict(env)
    if prof.to_disk and "/tmp/ase-site" in argv[2] and "/work/.ase-site" not in argv[2]:
        line = argv[2].replace("/tmp/ase-site", "/work/.ase-site")
        if "--no-cache-dir" not in line:
            line = re.sub(r"(pip3?\s+install)\b", r"\1 --no-cache-dir", line, count=1)
        argv[2] = "mkdir -p /work/.ase-site /work/.tmp && " + line
        env = {k: (v.replace("/tmp/ase-site", "/work/.ase-site") if isinstance(v, str) else v) for k, v in env.items()}
        env.update({"PIP_NO_CACHE_DIR": "1", "TMPDIR": "/work/.tmp"})
        notes.append("Packages are installed to /work/.ase-site on disk instead of the RAM-backed /tmp "
                     "(which counts against the memory limit)")
    if prof.cpu_torch and "PIP_EXTRA_INDEX_URL" not in env:
        env["PIP_EXTRA_INDEX_URL"] = CPU_TORCH_INDEX
        notes.append("torch is taken from the CPU-only index (about 200 MB instead of the multi-GB CUDA build)")
    if notes:
        notes.insert(0, "Auto-tuned for this project: " + "; ".join(prof.reasons))
    return new_limits, argv, env, notes
