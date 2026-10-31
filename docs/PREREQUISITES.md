# Prerequisites for institutions (Docker, Podman and analysis tools)

The application works without any of this: analysis, reports, grading and compliance are static. A **container runtime**
(Podman or Docker) is only needed to run builds and tests of uploaded projects in isolation. This page describes how the
application detects what is installed, offers to install what is missing, and copes with the usual obstacles of school,
university, company and government computers.

## What happens when the application (or the standalone EXE) starts
1. In the background it **checks whether a container runtime is installed and running** (setup doctor). Nothing is shown if one is ready.
2. If not, a window asks. Nothing is downloaded or installed until you confirm; for the full catalogue open **Institution options...**
   in that window or the **Prerequisites** tab (System group in the sidebar).
3. Every install shows an **exact plan first**: commands, download address, whether administrator rights are needed, which hosts must be
   reachable, licence terms. Approval is bound to that exact plan; a changed plan needs a new approval.
4. Afterwards the tool is detected again and the result is reported honestly (installed / needs start / needs restart / failed + why).

"Automatic" therefore means: **checked automatically at every start, offered automatically, installed with one confirmed click.**
There is no silent install. Setting `mode` to `auto` (policy or settings) lets the start-up install **Podman** without a dialog; Docker
Desktop is never installed unattended because its licence must be accepted by a person.

## What can be installed
| Item | Typical method (first usable wins) | Administrator? |
|---|---|---|
| Podman (recommended: free for organisations) | offline folder, `winget --scope user`, verified download, package manager | per-user when the product allows |
| Docker Desktop (licence for larger organisations) | offline folder, `winget`, verified download with `install --user` | no with `--user`, otherwise yes |
| WSL 2 (needed by both on Windows) | `wsl --install --no-distribution` | yes, restart usually needed |
| Ruff, SQLFluff | private virtual environment under `tools/venv` (binary packages only) | **no** |
| ESLint, TypeScript, Pyright | `npm --prefix tools/npm --ignore-scripts` | **no** |
| Node.js, JDK 17, .NET 8, Go, Python, PHP, Ollama | winget / Homebrew / apt, dnf, pacman, zypper | usually yes |

Sandbox images (python, node, .NET, Maven, Gradle) are pulled only on request (**Pull sandbox images...** or `--prereq pull-images --yes`),
from the public registry or your `registry_mirror`.

## Institutional obstacles and how they are handled
| Obstacle | What the application does |
|---|---|
| No administrator rights | prefers per-user installs (`winget --scope user`, Docker `--user`, private tool folders); otherwise explains and offers the IT request text |
| winget missing or blocked | falls back to a **checksum/signature-verified vendor download** (HTTPS, allow-listed hosts only) |
| Downloads forbidden or no internet | **offline folder**: put approved installers plus a `SHA256SUMS` file in `offline_dir`; `--prereq offline-manifest` lists what to carry in |
| Corporate proxy | uses the system/environment proxy or `proxy`/`no_proxy`; also passed to pip, npm, podman and docker pulls |
| TLS inspection (re-signed HTTPS) | Python uses the Windows certificate store; `ca_bundle` adds a PEM file; the error message says exactly this |
| Proxy needs a login (407) | clear message; credentials can be set in the proxy URL (stored as plain text - prefer system proxy) or use the offline folder |
| Internal mirrors (Artifactory/Nexus) | `mirror_base_url` (installers), `pip_index_url`, `npm_registry`, `registry_mirror` (container images, re-tagged to the standard names) |
| Software policy / audit | policy file locks settings; every download and install is written to `logs/prerequisites_audit.jsonl` (URLs, SHA-256, commands; secrets masked) |
| Licence rules | Docker's licence is shown and must be ticked; Podman needs no click-through |
| WSL / virtualization / old Windows / pending restart / low disk | **Hurdles...** (or `--prereq diagnose`) checks and tells you what to ask IT |
| Installer integrity | SHA-256 from the release (when published) and/or a valid Authenticode signature by the expected publisher; otherwise the installer is **not run** |
| Program Files is read-only (EXE) | data (history, reports, logs, settings) moves to `%LOCALAPPDATA%\AISoftwareEngineeringAgent` only when the install folder is not writable; otherwise behaviour is unchanged |
| IT must do it | **IT request...** produces a ready-to-send text: software, purpose, addresses, hosts to allow, integrity, removal |

## Policy files (for IT)
Precedence (later wins): built-in defaults < user (`data/prerequisites.json`) < `config/institution_policy.json` <
machine-wide `%ProgramData%\AISoftwareEngineeringAgent\policy.json` (Linux `/etc/aisea/policy.json`, deployable by Group Policy / Intune).
Keys present in a policy file become **read-only** in the application.

| Key | Meaning (default) |
|---|---|
| `mode` | `ask` (dialog), `auto` (Podman without dialog), `off` (never prompt) - `ask` |
| `startup_check` | check at start (`true`) |
| `snooze_days` | how long "remind me later" lasts (7) |
| `prefer_runtime` | `auto` (Podman first), `podman`, `docker` |
| `prefer_user_scope` | per-user installs where supported (`true`) |
| `allow_downloads`, `allow_installs`, `allow_winget` | master switches (`true`) |
| `require_verification` | refuse unverifiable installers (`true`) |
| `proxy`, `no_proxy`, `ca_bundle` | network settings |
| `offline_dir` | folder with approved installers + `SHA256SUMS` |
| `mirror_base_url`, `pip_index_url`, `npm_registry`, `registry_mirror` | internal mirrors |
| `allowed_hosts`, `blocked` | extra download hosts; prerequisite ids that must never be offered |
| `timeout_seconds`, `max_download_mb` | limits (60 / 2048) |

Environment: `AISEA_NO_PREREQ_CHECK=1` skips the start-up check; `AISEA_HOME` moves all data to a folder you choose;
a file `portable.flag` next to the EXE keeps data beside it.

## Command line
```
python app.py --setup-check | --setup-install [--assume-yes]      (setup doctor: container runtime + images)
python app.py --prereq check [--json]
python app.py --prereq diagnose [--network]
python app.py --prereq plan podman                                  (shows everything, changes nothing)
python app.py --prereq install podman --yes                         (docker also needs --accept-license docker)
python app.py --prereq start podman | pull-images --yes
python app.py --prereq it-request [IDS] [--out FILE] | offline-manifest | network-test
python app.py --prereq config show | config set KEY VALUE
```
`--yes` approves exactly the plan printed just before; without it nothing is installed (exit code 3).

## Safety rules
- Project or archive content never reaches a command. Only catalogue commands run; no shell is used.
- Downloads: HTTPS only, allow-listed hosts only (redirects included), size cap, resume, SHA-256 and signature checks.
- pip installs use binary wheels only (no build scripts); npm installs use `--ignore-scripts`; both go into private folders.
- Windows elevation uses a fixed, encoded PowerShell wrapper and refuses arguments with spaces or shell characters.
- The execution rule is unchanged: running a project's build/test still needs its own approval and a container; it never pulls images.

## What was verified, and what was not
Verified (Linux sandbox): planning for Windows/Linux/macOS, policy locking, proxy/CA/mirror settings, resumable and hash-checked
downloads against a local server, verification rules, approval binding and licence gating, elevation wrapper, audit log with masking,
diagnostics, IT request, start-up decision logic, CLI, UI dialogs, and **real private installs** of Ruff (pip virtual environment)
and ESLint (npm prefix) from the public registries.

**Not verified** (needs a Windows machine with the real products): winget installs of Podman/Docker/WSL, the Docker `install --user`
and Podman installer switches, UAC prompts, `podman machine` creation, Authenticode checks against the real vendor signatures, and
behaviour behind a real authenticating proxy. If a vendor changes an installer switch, the plan shows the failing command and the
log tells you; nothing is hidden.
