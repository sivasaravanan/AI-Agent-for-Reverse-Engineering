# User guide

## Start
- GUI: `python app.py` (Windows: `scripts\run.bat`). CLI: `python app.py --cli project.zip --out reports`.
- Python 3.11+ with Tkinter. `pip install -r requirements.txt` (pytest only; everything else is standard library).

## Navigating the window
Use the icon sidebar on the left (click a group name to fold it); the ◐ button in the toolbar switches light/dark. Hover a compact label for a hint. The window starts maximized. Drag the thin divider next to the sidebar to resize it, or press « to fold it to an icon rail. Click any group title (Inputs, Policy, Run options, Options...) to fold/unfold it; pages scroll when the window is small.

## Analyse a project
1. **Select ZIP / archive...** (zip, tar, tar.gz) or **Select folder...**, then **Analyze**. Tick or untick the optional phases: language analyzers, database analysis, build/test discovery.
2. Nothing from the ZIP is executed or extracted to disk. Unsafe entries (path traversal, bombs, symlinks) are skipped and listed.
3. Tabs: Dashboard, Overview, Findings, Findings (filter), Structure, Entry points, Imports, Config & Env, Confidence, Static tools,
   Databases, Findings Explorer, Build & Test, AI Assistant, Report preview, History, Settings.

## Code map and documentation
The **Code map** tab lists classes, functions and methods with location, method (ast = exact, regex = names only) and complexity. **Generate documentation** (or `--cli X --docs`) writes a Markdown draft; items it cannot know are marked TODO.

## Findings
Every finding shows severity, evidence, explanation, remediation, source, confidence and verification status. "unverified-heuristic"
means a pattern match, not proof. Tool results from analyzers are labelled parser / compiler / type / lint / heuristic.

## Build and test (Phase 4)
Commands are discovered from manifests but never run automatically. **Review and run...** shows the exact command, image, network setting
and limits; you must approve each run. Runs happen only in a disposable Docker/Podman container (network off by default). With no
container runtime the run is skipped: there is no host fallback. One-time setup: install Docker Desktop, pull the images you need.

## Live Run and Docker/Podman start
When a run needs Docker/Podman and it is installed but stopped, the app starts it (untick *Start automatically when needed* in Build & Test to prevent this; *Start Docker/Podman* does it on demand). While anything runs in the sandbox, the **Live Run** view opens automatically and shows the console stream, CPU/memory, processes and a timeline; use **Stop** to cancel, **Save...** to keep the log, and the run list to review earlier runs.

## AI assistant (Phase 7, optional)
Off by default. Enable it, choose Ollama (local) or an OpenAI-compatible / OpenRouter provider, set a model, press *Check availability*.
API keys come from the environment variable named in settings, or are typed for the session; they are never saved.
Pick the files to include. For external providers a dialog shows the exact (redacted) text and host; nothing is sent unless you consent.
AI output is an unverified suggestion. "Review as patch" sends a suggested file change into the Phase 8 workflow.


### Local AI is configured for you; cloud models are fetched live
- **Local:** when the AI view opens (and on **Detect local AI**) the app looks for **Ollama** (`localhost:11434`, or `OLLAMA_HOST`) and **LM Studio** (`localhost:1234`). Running servers are added as providers `ollama` / `lmstudio` (no key), their installed models are listed in the **Model** box, the first model is selected when none is chosen, the rest become fallbacks, and local providers go first in the route. If a server is installed but stopped, it is started (tick box "Start Ollama / LM Studio if installed but stopped"; untick to stop that). A running server with no model tells you what to do (`ollama pull llama3.2`, or download a model in LM Studio). The master **Enable AI** switch is never flipped for you.
- **Cloud:** **Cloud models...** lists models fetched live from OpenRouter (no key needed; **Free / Paid** and $/1M-token prices come from OpenRouter's published prices) and, when the provider's key is in its environment variable (or typed as the session key), OpenAI, Anthropic, Google Gemini, Groq, Mistral, Together, DeepSeek, xAI, Fireworks, Cerebras. Filter by provider, Free/Paid and text; double-click to use a model (provider entry, key variable name and route are filled in; add the key in the environment or the session box). Lists are cached in `data/model_catalog.json` (no keys) and used when you are offline. Providers other than OpenRouter do not publish per-model prices in their model lists, so their models show cost "unknown"; several have free plans with limits.
- Listing models sends no project data. Sending code to any non-local provider still needs your per-request consent.

## Fixing code (Phase 8)
Select a finding, **Propose fix...**, choose an empty workspace folder (the ZIP is never modified), read the diff, tick approval, Apply.
A backup is saved; **Revert** restores it. If a container runtime exists, tests run before and after and a regression rolls the patch back.
Without one the result says NOT VERIFIED.

## History and settings (Phase 9)
Results are stored in `data/history.db` (redacted). Settings: output folder, history, size limits; saved to `data/user_settings.json`.

## Reports and delivery (Phase 10)
Each analysis writes `<name>_<time>.html/.json` plus a `<name>_<time>_reports/` folder (summary, dependency, static-analysis, security,
build/test, inventory). **Delivery package...** (or `python app.py --deliver out --run-tests`) builds a secret-scanned, checksummed ZIP of this
application with a manifest, checklist and (optionally) real test evidence. Analysed projects are never included.

## Institutions: grading, compliance and teams (Stages B-D)
**Batch grading** - tab *Batch grading* or `--grade DIR [--rubric r.json] [--template starter.zip] [--manual-scores m.csv]`. One archive/folder per student (or one LMS bulk ZIP). Output: per-student reports, `grades.csv`, a summary and a similarity review list. Nothing submitted is ever run.

**Compliance** - tab *Compliance*: choose a policy JSON (see `samples/policy_strict.json`), then *Check policy + licences*, *Export SBOM* (CycloneDX), *Export SARIF*, *Trend*, *Portfolio scan...*, *Verify audit log*.
CI: `python app.py --ci project.zip --policy policy.json --out out` exits 0 (pass), 1 (policy failed), 2 (error) and writes `ci.sarif`, `sbom.cdx.json`, `policy_result.json`. Portfolio: `--portfolio DIR`. Verify the audit chain: `--verify-audit audit.jsonl`.

**Teams** - tab *Team*: pick a shared folder, create the first admin, sign in. Roles: admin (all), reviewer (approve, sign), analyst (analyse, propose fixes, request approval), viewer (read). While signed in, analyses go to the shared history and every action is audited. Approvals are bound to the exact diff hash, expire after 72 hours, and a person cannot approve their own request. Command line: `python scripts/team_cli.py WORKSPACE ...` (passwords via `ASE_PASSWORD`, never on the command line).

## Run targets and prerequisites
Build & Test lists the project's **root entry file** (for example `python ./main.py`) and other individually runnable files next to the build/test commands; pick one and use
*Review and run...* (see `docs/RUN_TARGETS.md`). **Prerequisites** (System group) shows what is installed and installs Docker/Podman/tools after showing the exact steps; **Hurdles...**,
**IT request...** and **Settings...** help on locked-down computers (see `docs/PREREQUISITES.md`).

## Storage and clean-up (Build & Test, App Preview)
- Open **Storage & clean-up...** (next to "Set up Docker..." in Build & Test, and in the options row of App Preview).
- **Where the project copy goes:** every run copies the project into a disposable folder and mounts that copy in the container. Set the *Scratch folder* to a drive with room (empty = system temp). The copy is deleted when the run ends; tick "Keep the project copy" only to inspect it.
- **Sizes:** "Largest project copied in" (default 500 MB; a single Python script in a bigger folder is still run by copying just that script and its imports) and "Container /tmp size". They appear in the approval dialog.
- **Clean up:** the list shows leftovers and what the runtime stores. Tick what to remove and press *Clean selected*. Dangling images and the build cache are shared by every project in Docker/Podman, so they ask first. Docker's own disk location is changed in Docker Desktop (Settings > Resources > Advanced) or `data-root` in daemon.json.

## ⚡ Run now (fastest way to a preview)
Pick the ZIP / folder / file, open **App Preview**, press **⚡ Run now**. Read the one approval window (it lists every step), press OK, and the program opens in the preview. The first run of a program can take a few minutes (one-time image steps need the internet); the next runs start in about a second. If the program needs a package that is missing, you are asked once and it is installed for you.

**Run one file:** press **File...** in App Preview and pick the program (for example `dupescope.py`). Only it and the files it imports are shown and copied; then press **⚡ Run now**. **Whole folder** brings back the full list.
