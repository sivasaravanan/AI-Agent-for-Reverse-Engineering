"""Fixed catalog of the prerequisites this application can use, and the exact commands that install them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Image tags mirror plugins.build_execution.discovery.DEFAULT_IMAGES (imported lazily so this package stays standalone).
FALLBACK_IMAGES = {"python": "python:3.12-slim", "node": "node:20-slim", "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
                   "maven": "maven:3.9-eclipse-temurin-17", "gradle": "gradle:8-jdk17"}
IMAGE_APPROX_MB = {"python": 60, "node": 90, "dotnet": 800, "maven": 550, "gradle": 750}
REQUIRED_IMAGES = ("python",)

WINGET_IDS = {
    "docker": "Docker.DockerDesktop",
    "podman": "RedHat.Podman",
    "node": "OpenJS.NodeJS.LTS",
    "dotnet": "Microsoft.DotNet.SDK.8",
    "jdk": "EclipseAdoptium.Temurin.17.JDK",
}
PIP_PACKAGES = ("ruff", "pyright")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]{0,200}(:[A-Za-z0-9._-]{1,128})?$")
DOWNLOAD_URLS = {"docker": "https://www.docker.com/products/docker-desktop/",
                 "podman": "https://podman.io/docs/installation"}


def default_images() -> dict:
    try:
        from plugins.build_execution.discovery import DEFAULT_IMAGES
        return dict(DEFAULT_IMAGES)
    except Exception:  # noqa: BLE001
        return dict(FALLBACK_IMAGES)


@dataclass(frozen=True)
class Action:
    """One command the user can approve. ``argv`` is executed directly (never through a shell)."""
    id: str
    label: str
    argv: Tuple[str, ...]
    kind: str = "run"                  # run | launch (start a program and return)
    admin: bool = False                # Windows will show its own permission (UAC) prompt
    approx_mb: int = 0
    note: str = ""
    wait_for_daemon: bool = False      # after launch: wait until the container runtime answers
    sets_path: bool = False            # after success: look for the tool in its usual install folder
    elevate: bool = False              # Windows: run through the permission (UAC) window so an installer gets administrator rights
    ok_codes: Tuple[int, ...] = (0,)   # exit codes that count as success (3010 = success, restart needed)

    @property
    def display(self) -> str:
        return " ".join(self.argv)


@dataclass
class Item:
    id: str
    title: str
    status: str                        # ok | missing | optional | info
    detail: str = ""
    required: bool = False
    actions: List[Action] = field(default_factory=list)
    url: str = ""                      # manual download page when no automatic action exists

    @property
    def fixable(self) -> bool:
        return self.status in ("missing", "optional") and bool(self.actions)


def winget_action(key: str, label: str, approx_mb: int, note: str = "", admin: bool = True) -> Action:
    return Action(id=f"install:{key}", label=label,
                  argv=("winget", "install", "-e", "--id", WINGET_IDS[key], "--accept-package-agreements",
                        "--accept-source-agreements"),
                  admin=admin, approx_mb=approx_mb, note=note, sets_path=True)


def qualify_image(runtime: str, image: str) -> str:
    """Podman needs a registry host; Docker resolves short names itself."""
    if runtime != "podman":
        return image
    if "/" in image:
        first = image.split("/")[0]
        if "." in first or ":" in first or first == "localhost":
            return image                              # already names a registry
        return "docker.io/" + image
    return "docker.io/library/" + image


def pull_action(runtime_exe: str, runtime: str, eco: str, image: str) -> Action:
    return Action(id=f"pull:{eco}", label=f"Download the {eco} test image ({image})",
                  argv=(runtime_exe, "pull", qualify_image(runtime, image)), approx_mb=IMAGE_APPROX_MB.get(eco, 500),
                  note="Used only to run approved builds/tests in isolation.")


def pip_action(python_exe: str, pkg: str, user: bool = True) -> Action:
    """``--user`` is refused by pip inside a virtual environment, so it is only added outside one."""
    argv = (python_exe, "-m", "pip", "install") + (("--user",) if user else ()) + (pkg,)
    return Action(id=f"pip:{pkg}", label=f"Install {pkg} (extra static checks)", argv=argv, approx_mb=40, sets_path=True,
                  note="" if user else "Installed into the active virtual environment.")


def load_image_action(runtime_exe: str, tar_path: str) -> Action:
    return Action(id="load:image", label=f"Load images from file {tar_path}", argv=(runtime_exe, "load", "-i", tar_path),
                  note="For computers without internet access: export on another machine with 'docker save'.")


def start_docker_desktop(exe: str) -> Action:
    return Action(id="start:docker", label="Start Docker Desktop", argv=(exe,), kind="launch", wait_for_daemon=True,
                  note="The first start asks you to accept Docker's terms and may ask to enable WSL 2 (a restart can be needed).")


def podman_machine_actions(podman_exe: str) -> List[Action]:
    return [Action(id="podman:init", label="Create the Podman machine", argv=(podman_exe, "machine", "init"), approx_mb=600),
            Action(id="podman:start", label="Start the Podman machine", argv=(podman_exe, "machine", "start"),
                   wait_for_daemon=True)]


def podman_start_action(podman_exe: str) -> Action:
    return Action(id="podman:start", label="Start the Podman machine", argv=(podman_exe, "machine", "start"),
                  wait_for_daemon=True)


def macos_brew_action(key: str) -> Optional[Action]:
    if key == "docker":
        return Action(id="install:docker", label="Install Docker Desktop with Homebrew",
                      argv=("brew", "install", "--cask", "docker"), approx_mb=600, sets_path=True)
    if key == "podman":
        return Action(id="install:podman", label="Install Podman with Homebrew", argv=("brew", "install", "podman"),
                      approx_mb=100, sets_path=True)
    return None


def wsl_install_action() -> Action:
    return Action(id="fix:wsl", label="Enable WSL 2 (the Windows feature Docker Desktop needs)", argv=("wsl", "--install", "--no-distribution"),
                  admin=True, elevate=True, ok_codes=(0, 3010), approx_mb=100,
                  note="Needs administrator rights. Windows usually asks for a restart afterwards; restart, then install Docker again.")


def docker_admin_action() -> Action:
    return Action(id="install:docker:admin", label="Install Docker Desktop as administrator", approx_mb=600, admin=True, elevate=True, sets_path=True,
                  argv=("winget", "install", "-e", "--id", WINGET_IDS["docker"], "--accept-package-agreements", "--accept-source-agreements", "--silent"),
                  ok_codes=(0, 3010), note="Opens the Windows permission window and runs Docker's installer silently with administrator rights.")


def docker_cli_start_action(exe: str) -> Action:
    return Action(id="start:docker", label="Start Docker Desktop", argv=(exe, "desktop", "start"), wait_for_daemon=True,
                  note="Starts Docker Desktop through its command line.")
