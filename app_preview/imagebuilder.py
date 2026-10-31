"""Prepare the GUI preview image (Xvfb + Tk + screen-capture tools) with the user's own container runtime.

The preview never pulls or builds images by itself. This module is the explicit, approved way to create the one image GUI apps
need: the user sees the exact ``<runtime> build`` command and the SHA-256 of the Dockerfile, approves it, and only then it runs.
The build needs the network (base image + apt packages); it runs on the container runtime, never as project code, and the
project being previewed is not part of the build context.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from plugins.build_execution.models import Approval
from plugins.build_execution.sandbox import CREATE_NO_WINDOW, _env

from .models import GUI_IMAGE, PreviewOptions
from .runtimes import find_runtime, image_present, status

IMAGES_DIR = Path(__file__).resolve().parent / "images"
DOCKERFILE = IMAGES_DIR / "Dockerfile.gui"


def _env_dir(tag: str) -> Path:
    """Private folder holding the generated Dockerfile of a cached package image."""
    try:
        from plugins.build_execution.storage import _data_dir
        base = _data_dir()
    except Exception:  # noqa: BLE001
        base = Path.home() / ".ase_agent" / "data"
    return base / "env_images" / tag.split(":")[0]


@dataclass
class BuildPlan:
    """What the user approves before an image is built. ``plan_hash`` binds the approval to these exact values."""
    exe: str
    backend: str
    tag: str
    argv: List[str]
    dockerfile_sha256: str
    network: bool = True
    risks: List[str] = field(default_factory=list)
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if not self.plan_hash:
            self.plan_hash = hashlib.sha256("\x00".join([self.tag, self.dockerfile_sha256, *self.argv]).encode()).hexdigest()

    @property
    def command_line(self) -> str:
        return " ".join(self.argv)


def gui_image_ready(options: Optional[PreviewOptions] = None, image: str = "") -> dict:
    """{'ready': bool, 'image', 'backend', 'reason'} - never raises, never builds or starts anything."""
    options = options or PreviewOptions()
    img = image or options.gui_image or GUI_IMAGE
    st = status(options)
    if not st["available"]:
        return {"ready": False, "image": img, "backend": st["backend"], "reason": st["reason"]}
    if image_present(st["executable"], img):
        return {"ready": True, "image": img, "backend": st["backend"], "reason": ""}
    return {"ready": False, "image": img, "backend": st["backend"],
            "reason": f"The GUI preview image '{img}' does not exist on this computer yet."}


def make_build_plan(options: Optional[PreviewOptions] = None, tag: str = "") -> BuildPlan:
    options = options or PreviewOptions()
    name, exe = find_runtime(options)
    if not exe:
        raise RuntimeError("No container runtime (docker, podman, nerdctl, finch) was found, so nothing can be built.")
    if not DOCKERFILE.is_file():
        raise RuntimeError("app_preview/images/Dockerfile.gui is missing from this installation.")
    tag = tag or options.gui_image or GUI_IMAGE
    sha = hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()
    argv = [exe, "build", "--tag", tag, "--file", str(DOCKERFILE), str(IMAGES_DIR)]
    risks = ["Downloads a base image and Debian packages from the internet (network ENABLED for this build only)",
             "Builds the image on your container runtime; the project you preview is NOT part of the build"]
    return BuildPlan(exe=exe, backend=name or "", tag=tag, argv=argv, dockerfile_sha256=sha, risks=risks)


def build_gui_image(plan: BuildPlan, approval: Approval, progress: Optional[Callable[[str], None]] = None,
                    cancel: Optional[threading.Event] = None, timeout_s: int = 1800) -> dict:
    """Run the approved build. Returns {'ok', 'exit_code', 'notes'}. Refuses without an approval for exactly this plan."""
    if not isinstance(approval, Approval) or approval.plan_hash != plan.plan_hash:
        return {"ok": False, "exit_code": None, "notes": ["Refused: no approval for this exact build."]}
    say = progress or (lambda _t: None)
    cancel = cancel or threading.Event()
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    say(f"$ {plan.command_line}")
    try:
        proc = subprocess.Popen(plan.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=_env(), text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as exc:
        return {"ok": False, "exit_code": None, "notes": [f"Could not start {plan.backend}: {exc}"]}
    timer = threading.Timer(timeout_s, proc.kill)
    timer.start()
    tail: List[str] = []
    try:
        for line in proc.stdout:                      # type: ignore[union-attr]
            line = line.rstrip("\n")
            tail = (tail + [line])[-12:]
            say(line)
            if cancel.is_set():
                proc.kill()
                return {"ok": False, "exit_code": None, "notes": ["Build cancelled."]}
        code = proc.wait()
    finally:
        timer.cancel()
    if code == 0:
        return {"ok": True, "exit_code": 0, "notes": [f"Image '{plan.tag}' is ready."]}
    return {"ok": False, "exit_code": code,
            "notes": [f"The build failed (exit {code}). Last lines:"] + tail[-6:]}
