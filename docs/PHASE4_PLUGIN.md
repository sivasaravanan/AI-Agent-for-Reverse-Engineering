# Phase 4 plugin - Controlled Build & Test Execution

Self-contained (standard library only). Imports nothing from Phase 1-3; nothing existing is modified.
Works alongside `plugins/static_analysis` (Phase 3) - separate folder, no shared code.

## Install
1. Copy the `plugins/build_execution/` folder into your existing `plugins/` folder (and optionally `tests/test_build_execution_plugin.py`).
2. Hook it up - pick one:

```python
# A) Tkinter tab (includes the approval dialog)
from plugins.build_execution import attach_tab
attach_tab(notebook, get_source=lambda: self.current_zip_path)

# B) Code
from plugins.build_execution import discover_candidates, build_plan, Approval, run_plan, isolation_status
cands, notes = discover_candidates(zip_path)         # read-only; executes nothing
plan = build_plan(zip_path, cands[0])                # exact command, image, limits, network flag
# show `plan` to the user; ONLY after they approve:
result = run_plan(plan, Approval.for_plan(plan))     # ExecutionResult: status, exit_code, stdout, stderr, notes...

# C) CLI
python -m plugins.build_execution check
python -m plugins.build_execution candidates project.zip
python -m plugins.build_execution run project.zip --candidate node:npm-test
```

## One-time setup on Windows (manual action by you)
- Install Docker Desktop (or Podman) and start it.
- Pull the images you want (the plugin NEVER pulls): `docker pull python:3.12-slim`, `node:20-slim`,
  `mcr.microsoft.com/dotnet/sdk:8.0`, `maven:3.9-eclipse-temurin-17`, `gradle:8-jdk17`. Override per ecosystem via `Options(images={...})`.

## How the safety rules are enforced
| Requirement | Behaviour |
|---|---|
| Explicit approval | `run_plan` refuses unless `Approval.plan_hash` equals the plan's hash (hash covers command, env, image, limits, network, source). Changing anything after approval => `refused`. |
| Never run on host | No container runtime / daemon down / image missing => `skipped` with the reason. There is no host fallback. |
| Network off by default | `--network none`. Candidates that need downloads (`npm ci`, `pip install`, `dotnet restore`, `mvn`) are separate, flagged, and turn the network on only inside the plan the user approves. |
| No host access / secrets | Only a scratch COPY is mounted at `/work`; no host env vars are passed; `.git`, `node_modules`, venvs and symlinks are not copied. |
| Limits | `--cpus`, `--memory` (+swap), `--pids-limit`, `nofile` ulimit, tmpfs size, wall-clock timeout (container killed), output cap per stream, workspace size cap. |
| Restricted permissions | `--read-only` root FS, `--cap-drop ALL`, `no-new-privileges`, non-root user 65534. |
| Logs / exit codes | Returned in `ExecutionResult` (+ optional JSON in `Options.log_dir`). 125/126/127/137 are explained in `notes`. |
| Cleanup | Scratch workspace deleted and container force-removed in a `finally` block; `workspace_cleaned` reports it. |
| Failure never crashes the app | `run_plan` never raises; every outcome is a result status. |
| Install hooks / scripts | Never automatic. Lifecycle hooks and suspicious script contents are listed as risks; `.sh/.bat/Makefile/gradlew/mvnw` are only mentioned, never offered. `npm ci/install` use `--ignore-scripts`. |

## Verification status (be aware)
- 31 automated tests pass, BUT they use a **fake container runtime** that records the command line it is given. They prove the gating, command construction, limits handling, timeout/cancel/kill calls, truncation, cleanup and failure handling.
- They do **not** prove the isolation itself, because no Docker/Podman exists in the build sandbox. Please run once on your machine: `python -m plugins.build_execution check`, then a harmless test run, and confirm `--network none` really blocks access (e.g. custom command `python -c "import urllib.request as u; u.urlopen('http://example.com')"` should fail).
- The Tkinter tab/approval dialog only passed a compile check (no display here).

## Known limitations
- Docker Desktop on Windows/macOS runs containers in a VM; bind-mount behaviour and exit codes depend on it. Rootful Docker/daemon compromise is outside this plugin's control.
- Images are referenced by tag, not digest.
- Offline runs usually fail on missing dependencies; that is reported as a normal failed run, not hidden.
- Test-result parsing/baseline comparison is Phase 8; this plugin returns raw output.
- Maven/Gradle/.NET candidates were validated only by discovery tests, never against real toolchains.
