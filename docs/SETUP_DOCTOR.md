# Setup doctor (prerequisite check and guided install)

## What happens
- **At every start** the app checks, in the background, whether a container runtime (Docker or Podman) is installed and
  running and whether the Python test image is downloaded. If everything required is present, nothing is shown.
- If something required is missing, a **Set up prerequisites** window opens. It lists what exists and what is missing, the
  **exact commands** that would run and the approximate download size. **Nothing is installed or downloaded until you tick
  items and confirm.** Windows then shows its own permission (UAC) prompt for installers.
- Also reachable from the Build & Test tab (**Set up Docker...**) and from the command line:
  `python app.py --setup-check` (exit 1 if a required item is missing) and `python app.py --setup-install` (shows the
  commands, asks `Proceed? [y/N]`). `--assume-yes` skips the question; use it only for unattended deployments after reviewing
  the printed list.
- "Don't ask when the app starts" is remembered per user in `%LOCALAPPDATA%\AISoftwareEngineeringAgent\setup_doctor.json`.

## What it can install
| Item | How | Notes |
|---|---|---|
| Docker Desktop | `winget install -e --id Docker.DockerDesktop` (Windows), Homebrew on macOS | Needs WSL 2/virtualization; a restart may be needed; first start asks you to accept Docker's terms. Free for personal, education and small business use; larger organisations need a paid licence. |
| Podman (alternative) | `winget install -e --id RedHat.Podman` | Free; create/start its machine from the dialog. |
| Start the runtime | starts Docker Desktop / `podman machine start` | Waits up to 4 minutes for it to answer. |
| Test images | `docker pull <image>` | python (about 60 MB) is required; node, .NET, Maven, Gradle are optional and large. |
| Optional analysis tools | pip (ruff, pyright), winget (Node.js LTS, .NET 8 SDK, Temurin JDK 17) | Not offered inside the standalone exe for pip packages. |

Linux: install Docker/Podman with your package manager (the dialog shows the vendor page); images can be pulled from the dialog.

## Offline or locked-down institutions
- No internet on the target PC: on another PC run `docker pull python:3.12-slim` then `docker save -o images.tar python:3.12-slim`,
  copy the file over and use **Load images from file...** (runs `docker load -i`).
- No winget or no permission to install: the dialog shows the vendor download page; ask IT to install Docker/Podman, then **Re-check**.
- A tool installed during this session is found in its usual folder and added to the app's search path, so no restart is needed.

## Safety rules
- Only commands from a fixed catalog can run (`setup_doctor/catalog.py`); `validate_action` refuses anything else, including
  other winget packages, other pip packages, `docker run` and shell programs. Project or archive content never reaches a command.
- The execution rule is unchanged: running a project's build/test still requires its own approval window and never downloads images.
  Downloads happen only here, after your confirmation.

## Standalone exe
`scripts\build_exe.bat` (or `... windowed`) builds a folder with the exe via PyInstaller. Keep the folder together and run it
from a writable location. Built and run on Linux here (setup check, analysis, batch); not yet run on Windows.


## When the Docker install fails / Docker is installed but stopped
- A failed Docker install prints the reason (WSL 2 missing, virtualization off, restart pending, no administrator window) and adds repair rows to the list: enable WSL 2, retry as administrator, BIOS / restart hints, manual download. The first usable one is pre-ticked; tick and press **Install selected** as usual.
- **▶ Start Docker** (next to Re-check) starts an installed but stopped Docker Desktop / Podman machine and waits for the engine. It is disabled (not hidden) while the runtime already runs or none is installed.
