## 0.9.21 - Docker install failure explained and repaired; Start Docker button (includes 0.9.20, the usable machine window)
- **Why "Docker Desktop install" failed (winget 0x8A150006 / installer exit -5)**: the setup dialog now says it in words instead of a bare exit code, after looking at the machine (read-only): WSL 2 installed?, virtualization on?, restart pending?, running as administrator?, Docker perhaps already installed? (`setup_doctor/recovery.py`).
- **Repair rows appear in the dialog, the first usable one pre-ticked**: *Enable WSL 2* (administrator window, restart afterwards), *Retry installing Docker Desktop as administrator* (runs `winget ... --silent` through the Windows permission window, which the hidden first attempt could not show), *Turn on virtualization in the BIOS / UEFI* and *Restart Windows first* (information rows with the vendor page), *Install Docker Desktop yourself* (docker.com). Exit 3010 (restart needed) now counts as success with a note.
- **▶ Start Docker button** (always shown, enabled when a runtime is installed but not running; the label follows the runtime: Docker / Podman): starts Docker Desktop (also a per-user install under `%LOCALAPPDATA%\Programs\Docker`, or `docker desktop start`) or the Podman machine and waits until the engine answers, then re-checks.
- Safety: still only fixed catalog commands. New allowed commands: `wsl --install --no-distribution`, `winget install ... --silent` (Docker only), `docker desktop start`; the administrator wrapper refuses arguments with spaces or shell characters; nothing runs without the usual confirmation.
- Honest limit: I could not run Windows here. The diagnosis, the new actions and the dialog are covered by tests with a simulated Windows; whether WSL / the administrator retry fixes your particular PC depends on the cause shown in the log (BIOS virtualization and a pending restart cannot be fixed from software).
- Files: new `setup_doctor/recovery.py`, `tests/setup_doctor/test_docker_recovery_start.py`; changed `setup_doctor/catalog.py`, `setup_doctor/checks.py`, `setup_doctor/installer.py`, `ui/setup_dialog.py`, `docs/SETUP_DOCTOR.md`, `CHANGELOG.md`. The 0.9.20 files (machine window with live mouse / keyboard, desktop, fast screen channel) are included again because your upload did not contain them.

## 0.9.20 - the virtual machine window is really usable: desktop, live mouse + keyboard, fast screen channel
Why the machine window "could not be accessed": clicks and keys reached the program only when a hidden switch was on (it was off), every click started its own `docker exec` (about a second behind), and the machine had no desktop - windows sat at the top-left corner with no title bar, could not be moved or closed, and dialogs piled up.
- **A desktop inside the machine** (Openbox, added to `app_preview/images/Dockerfile.gui`; started by `app_preview/runtime/ase_gui_entry.sh`): title bars, minimise / maximise / close, move and resize by dragging, dialogs in front of their window, Alt+Tab, root menu on right-click of the background. Apps open centred like on a real desktop. An image built before this release still runs everything as before; the status bar then shows "🪟 none" and the console says to press **Prepare GUI preview image** once.
- **Live input like a VirtualBox window** (`ui/guest_input.py`): the machine window captures mouse and keyboard by default (Input > Capture Mouse and Keyboard, or the 🖱 status icon). Press / release / drag / wheel / right-click and every key down / up with its own name (Shift, Ctrl, Alt, arrows, F-keys, typing) go to the program at once; keys held when the window loses focus are released. F11 and Ctrl+T / Ctrl+E / Ctrl+Shift+S stay with the window.
- **One fast channel instead of one command per picture / click** (`app_preview/runtime/ase_screen.py`, `PreviewSession._screen_channel`): a single long-lived process in the container sends the screen when it changes and applies input immediately; after you click or type it looks at the screen several times a second so the result shows at once. Measured on a real container: click -> typed text -> button -> program output in 0.2 s. If the channel cannot start the older snapshot path takes over automatically, with a note in the console.
- The machine window always opens with **Fit** scaling; **Start** is never a dead button: when another machine holds App Preview it offers to power that one off and start this one.
- Safety unchanged: only the session's validated `input_event` / the helper's validated commands (numbers, key-name pattern, text without control characters) reach xdotool; no shell; starting still asks for approval of the exact plan.
- Honest limit: this is a container with a virtual screen and a desktop - not a hypervisor with a full guest operating system. It runs Linux programs (Tkinter, pygame, ...); Windows programs and a whole OS are not possible here.
- Files: new `app_preview/runtime/ase_screen.py`, `ui/guest_input.py`, `tests/unit/test_machine_input.py` (7, one real-container test opt-in); changed `app_preview/session.py`, `app_preview/runtime/ase_gui_entry.sh`, `app_preview/images/Dockerfile.gui`, `ui/vm_window.py`, `core/phase_registry.py`, `CHANGELOG.md`, `docs/VM_MANAGER.md`.

## 0.9.19 - compact App Preview toolbars: icons with tooltips, nothing hidden
- Every button, label and check box of the App Preview toolbars and of the Screen tab bar is now `icon + tooltip`. The tooltip carries the full name and the explanation. The new **Aa** button (option, remembered) switches between **icons only** (default, compact) and **icons with text**; nothing is removed in either mode and no command changed.
- The rows **wrap instead of being cut off** (`ui/compact.py`, `FlowFrame`): at any window width every widget (including the 🖥 Virtual machines menu that used to fall off the right edge) stays visible and reachable; the Start / Project boxes still take the free room of their row.
- Fewer rows, less white space: the options row merged into the controls row, the Screen tab's two bars into one, tighter padding on the Details list and panes. The runtime / status lines wrap instead of running off the edge.
- Existing behaviour untouched: the Include... counter now shows as `➕ (n)` (tooltip: Include...). Files changed: `ui/app_preview_view.py`, `ui/lean_copy.py` (counter), `core/phase_registry.py`, `CHANGELOG.md`, `docs/APP_PREVIEW.md`, `tests/unit/test_lean_copy.py` (counter assertions); new: `ui/compact.py`, `tests/unit/test_compact_toolbar.py`.

## 0.9.18 - VirtualBox-style windows: Manager, machine window, snapshot tree, settings
- New **🖥 Virtual machines ▾** menu in App Preview (next to "Clear project & copies"); everything that existed (screen Snapshot menu, Pop out, Preview card, Start / Run now ...) is unchanged.
- **Manager** (`ui/vm_manager.py`): list of machines with state (Running / Paused / Powered Off / Aborted), filter box, toolbar New / Settings / Remove / Start / Stop / Show / Snapshot / Snapshots, right-click menu, F2 rename, Del remove, Enter start/show; tabs **Details** (VirtualBox-like sections - click a heading to open that Settings page - and a live Preview), **Snapshots**, **Log**. A machine is created automatically for every program you run, or with New / "Add the program selected in App Preview"; Clone and Rename are there too.
- **Machine window** (`ui/vm_window.py`): menu bar Machine / View / Input / Devices / Help, toolbar, the virtual screen or (for programs without a window) the console, and a status bar with clickable icons (uptime, snapshots, shared files, network, storage, mouse/keyboard capture) and a state badge. Scale Fit/50/100/200, full screen (F11), always on top, hold screen, resize window to the screen, capture mouse and keyboard, send Ctrl-Alt-Del / Esc / Enter / Tab / Alt+F4 / Ctrl+C / Ctrl+V / F5, type text. Closing a running machine asks: keep running, power off, or cancel.
- **Snapshots** (`ui/vm_snapshots.py`, `app_preview/vmstore.py`): a tree like VirtualBox's (a snapshot taken while another is current becomes its child) ending in "Current State"; Take (name + description), Restore (optionally snapshotting the current state first), Delete (children move up), Properties, Save picture. A snapshot keeps the screen picture, console text, settings and start command. The container's memory/disk is not saved (runs are disposable), so Restore puts settings and start command back and shows the recorded picture; the next Start runs fresh. Optional automatic snapshot when a run ends (last 10 kept). Stored under `data/vm/`.
- **Settings** (`ui/vm_settings.py`): General (name, project, mode, start command, runtime, image) | System (time limit, memory, processors, processes, install step) | Display (size, rate, scale) | Storage | Network | Shared Folders | User Interface. Values are validated; a typo never reaches a run.
- The existing screen **Snapshot** menu gained two entries: "Take machine snapshot..." and "Snapshots of this machine...".
- Safety unchanged: starting a machine goes through the panel's ordinary Start, so the approval of the exact plan is asked every time; nothing runs on the host; ids are checked before they become paths.
- Existing files changed only additively: `ui/app_preview_view.py` (mixin, one menu button, one init call), `core/phase_registry.py` (phase 19), `CHANGELOG.md`.
- Tests: `tests/unit/test_vm_manager.py` (9).

## 0.9.17 - lean copy: a folder or archive is never copied as a whole
- A script run (`python script.py`, App Preview, Quick run, Build & Test) now copies into the disposable container ONLY what the script needs, whatever the project's size and for folders and ZIP archives alike (new setting **Lean copy**, default on, in Storage & clean-up; off = the earlier behaviour):
  - the script and the project modules it imports (transitively, with package `__init__` files),
  - small data / config files next to it,
  - project files and small folders its code names (string literals such as `"settings.json"`, `"assets/logo.png"`, `"templates"`; never `.env`, keys or certificates; a folder over 300 files / 20 MB is reported, not copied),
  - anything you add by hand with the new **Include...** button (tick files or folders of the project; a counter shows how many).
- On demand: when a run fails and the program's own error says a file was not found (`No such file or directory`, `TemplateNotFound`, `can't open file`, `No module named 'pkg.sub'` for a module that is in the project, Tk/pygame image errors), the panel names the project files that match and asks once: "Copy the missing files and run again?" A missing PyPI package is still offered as an install, as before. At most 3 automatic repeats per start.
- The console note after the copy says exactly what was copied and what to do if the program needs more; the approval window lists the lean copy among the plan notes.
- Not narrowed: commands that are not a plain Python script (`python -m pkg`, tests, node, maven, ...) cannot be traced to their files, so they keep the earlier copy rules (whole project if it fits, trimmed or refused above the limit).
- Tests: `tests/unit/test_lean_copy.py` (9). Verified on a real container: the first run fails on a file only a runtime path names, the offer copies it, the second run completes.

## 0.9.16 - Build & Test and Live Run hidden
- The two pages are no longer shown: App Preview is the one run view. They are not deleted: a run started elsewhere shows Live Run again, `_select_tab` still opens either, and `"show_legacy_tabs": true` in `data/ui_prefs.json` keeps both visible.

## 0.9.15 - one run view (step 2 of merging Build & Test / Live Run / App Preview)
- App Preview now offers everything the other two tabs did: build and lint commands from Build & Test discovery next to run and test commands (type your own as before), **Set up Docker...**, **Clear project & copies**, Live Run's earlier-runs picker, **Find** and **Wrap** in the console, CPU/memory/processes/timeline, Stop, Quick run (Run now).
- Sidebar: Actions lists App Preview as the run view; Build & Test and Live Run are kept (unchanged) under More. A run started in App Preview stays in App Preview; runs started elsewhere still switch to Live Run.
- Tests added to `tests/unit/test_run_workflow.py` (7 in total).

## 0.9.14 - one run workflow (step 1 of merging Build & Test / Live Run / App Preview)
- App Preview: choosing a command in **Start** starts it (approval of the exact plan is still asked; switch off with "Start when a command is chosen"). A project with `requirements.txt` ticks "Install requirements" by itself (the approval shows the network step).
- **Clear leftovers before / after** (default on): before a run every leftover project copy and stopped container of ours is deleted (never while another run is active); the same happens when the application closes, after stopping a running preview. Your own files are never touched.
- New **Resources** tab (CPU / memory, processes, timeline) in App Preview. Live Run uses the same `ui/run_panels.py` panel, so there is one implementation.
- Tests: `tests/unit/test_run_workflow.py` (4).

## 0.9.13 - screen options, external preview, snapshots; Live Run picture fixed
- App Preview > Screen tab, reorganised: **Size** (presets or your own WxH, 320x240..3840x2160, applies at the next start), **Rate** (0.5-10 snapshots/s, applies at once), **View** (Fit / 50% / 100% / 200%, scrolls when larger), **Pop out** (the screen in its own window: drag to another monitor, F11 full screen, keep on top; a web app opens in the browser), **Snapshot** menu (Save as..., Quick save to a timestamped file, Open/Choose folder, auto-snapshot when a run ends). Settings are remembered. New shared module `ui/screen_tools.py`.
- Fixed: Live Run's Preview stayed black ("Running - no output yet") for App Preview runs because frames only travel on the preview bus; Live Run now listens for them (and web pages), and clicking its thumbnail opens the same external window. Late frames no longer flip a finished run back to "running".
- Fixed: Build & Test window programs now publish `screen` events (`_snapshot` / `_watch_screen` were missing; `tests/unit/test_screen_watcher.py` could not even import).
- Fixed: the copy note said "Project is larger than 500 MB (453 MB)". The real trigger was the quick-copy size (100 MB); the note now says so. Over-limit wording unchanged.
- Fixed: App Preview status stayed on "Starting run in container..." while running; mouse input limits follow the screen size of the run itself.
- Tests: `tests/unit/test_screen_tools.py` (10).

## 0.9.12 - hand-over starts at once; no second "pick a command"
- Build & Test > Review and run on a window program hands over to App Preview, which now **starts that program immediately** with the command you already chose. Before, it first read the whole project list and, on a big folder, showed an empty Start box ("Nothing to start") while still scanning. The list is read in the background and no longer replaces the program that was chosen.
- App Preview's project listing and entry discovery now skip virtual-environment folders (any folder with `pyvenv.cfg`, such as `AI Chat/env`) like Build & Test already did, so big folders are read in seconds instead of walking tens of thousands of package files.
- Pressing **Start** with an empty Start box no longer shows "Nothing to start": while the list is still being read it starts by itself when the list is ready; when the list is ready it takes the entry the panel would offer (the File... choice if there is one). The message appears only if the project has no runnable file at all, and then says to use File... or type a command.
- Tests: hand-over before the scan ends, listing skips virtual environments, Start with an empty box.

## 0.9.11 - screen capture that explains itself
- GUI preview: when no screen snapshot arrives, the console now says why instead of staying at "Running - no output yet". After 6 s without a snapshot a small check runs inside the container (`app_preview/runtime/ase_diag.py`) and prints: whether the virtual screen is up, which capture tools exist, the running programs with their CPU, how many windows the program has opened (none yet = the program is still starting or busy, e.g. scanning files before it draws its window) and the result of a test capture. It repeats at most once a minute.
- Capture is more robust: tries `import`, `import-im7.q16`, `import-im6.q16`, `magick import` and remembers the one that works; each attempt has a 6 s limit; failing captures back off (up to 3 s) instead of hammering the runtime every 0.15 s.
- The GUI image recipe now FAILS THE BUILD when the screen cannot be captured, rather than giving a black preview later. If your image was built before this version and the console reports a missing tool, use **Prepare GUI preview image** again (or Storage > clean the GUI image, then rebuild).
- Honest note: a window program that does heavy work before it opens its window shows nothing until it does; the console now tells you that is the case ("visible windows: 0") rather than leaving you guessing.

## 0.9.10 - File... picker (run one file, see only that file)
- App Preview: new **File...** button. Pick ONE program file: its folder becomes the project, but the file tree lists only that file and the project files it imports (the rest of the folder, such as a big "AI Chat" folder, is neither listed nor copied), and the start entry is set to that file. **Whole folder** shows everything again.
- **⚡ Run now** runs the file picked with File... (no typing of the file name, so a typo such as `dupescopy.py` for `dupescope.py` cannot happen); if the scan did not list the file, it is still started as a plain Python script.
- Tests: `tests/unit/test_quick_run.py` (file picker focus, re-pick in the same folder, named file not in the scan).

## 0.9.9 - ⚡ Run now (one click to a live preview)
- New **⚡ Run now** button in App Preview (`ui/quick_run.py`, `app_preview/quick.py`). One click: it picks what to start (window program, else web app, else console script), works out what the program needs from its own code, and shows ONE approval window listing every step. After you approve it:
  - starts Docker/Podman if it is installed but stopped;
  - builds the GUI preview image if a window program needs it and it is missing;
  - installs the third-party packages the script (and the project files it imports) uses into a small **cached image** `ase-env-<hash>` (names mapped such as cv2 to opencv-python-headless, versions taken from requirements.txt when listed; Windows-only modules are skipped). Built once with the network on for that build only; later runs are offline and start at once;
  - runs the program in the usual disposable container and shows the screen / web page / console as soon as the first frame appears (screen polling is now fast until the first frame).
- Automatic remedies after a run: a `ModuleNotFoundError` for a package that is not in the image offers one yes/no to install it and runs again; an out-of-memory kill (exit 137) repeats once with double memory. At most two automatic repeats; no endless loops.
- Big folders still work through the existing single-script narrowing; runs started by Run now need no second approval window (the one approval covers the exact steps shown, and the run itself is still bound to its plan hash).
- Honest limits: the first run of a new program still needs the internet for the one-time image steps; timeouts are not retried automatically (a window left open is not an error); Podman/nerdctl/Finch and Windows/macOS paths are untested here.

## 0.9.8 - storage settings and clean-up for containers
- New **Storage & clean-up...** window (buttons in Build & Test and App Preview; `ui/storage_dialog.py`, `plugins/build_execution/storage.py`):
  - Settings (`data/storage.json`): scratch folder for the disposable project copy (default system temp; pick a roomy drive), delete the copy after every run (default on) or keep it for inspection, largest project copied in (MB), container /tmp size (MB). The two sizes are the defaults of every new plan, so they are part of what you approve.
  - Inventory: leftover project copies with sizes, containers made by this application (`be-*`), the GUI preview image, dangling images, the runtime's `system df` and its storage folder.
  - Clean up: leftover project copies; stopped containers of this application (running ones only if you also tick that); the GUI preview image; dangling images and the build cache (global to the whole runtime, so they ask for confirmation). Only objects this application created are ever removed by name.
  - Docker's own storage location is shown with how to move it (Docker Desktop disk image location / daemon.json `data-root` / Podman); the application does not change it.
- After every run the project copy is deleted (was already so) and the container is removed (`--rm` plus a forced remove); the start-up sweep now also deletes copies left by a crashed run once they are an hour old.
- A size refusal for `python name.py` now says when `name.py` does not exist in the project (typo) instead of only "too large".

## 0.9.7 - fix: window freezing on big projects
- App Preview: listing the project's files, hashing a ZIP, `docker info` and the image check no longer run on the window thread (they ran on it before, so a big folder or a slow Docker Desktop froze the whole application). Scanning, Start and the image check now run on worker threads; the UI shows "Scanning the project..." / "Checking the container runtime...".
- Build & Test: the check "does this script open a window?" (reads the project) runs on a worker thread; the Run button is disabled meanwhile and the normal run continues afterwards.
- Measured: with a 20,000-file folder the longest UI stall while handing a script to App Preview and starting it was 0.18 s (real Docker).

## 0.9.6 - window programs run in App Preview from Build & Test
- Build & Test: "Review and run..." on a Python script that opens a window (Tk / Qt / pygame, found by reading its imports and local modules) now offers to run it in App Preview instead of failing with `libtk8.6.so: cannot open shared object file`. The plain container image has no screen and no Tk libraries; App Preview uses the GUI image with a virtual screen and shows the window live. The exact plan is approved there. Answer No to keep the old behaviour.
- App Preview: new `AppPreviewView.run_script(source, script)` used by that hand-over; a script without a main guard is started as a window program.
- `ui/main_window.py` (additive: `on_gui_run` wiring and `_gui_to_preview`), `ui/execution_dialog.py` and `plugins/build_execution/ui_tab.py` (additive hook).

## 0.9.5 - one script inside a huge folder
- When a project is larger than the size limit and the command starts a Python script (`python ./dupescope.py`, also from App Preview), only that script, the project-local modules it imports (found by reading the code, with their package `__init__.py`) and small data/config files next to it are copied into the container, instead of refusing the whole folder. The console says exactly what was copied. Applies to folders and ZIPs, Build & Test and App Preview. If the script itself is too big, or the command is not a plain `python script.py`, the project is still refused with the reason.
- Nothing else changes: projects under the limit are still copied whole, the container is still the only place anything runs.

## 0.9.4 - fix: "Refused" with no reason
- A run that was refused, skipped or failed to start now prints the reason in the Live Run / App Preview console (it was only stored in the result, so the console showed just "finished: refused").
- Project copy for container runs: any virtualenv (a folder containing `pyvenv.cfg`, whatever its name) and cache folders (`.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.idea`, `.vs`, `.gradle`, `.hg`, `.svn`) are left out; a locked/unreadable/over-long-path file is skipped with a note instead of refusing the whole project. A real size refusal names the biggest folders and the App Preview panel has a new "Max project MB" box (default 500).
- Security refusals (unsafe archive paths, zip bombs, missing approval) are unchanged.

## 0.9.3 - GUI preview image on demand; local AI auto-setup; live cloud model catalogue
- App Preview: GUI apps now work out of the box after one approved step. Screen tab / Start offer "Prepare GUI preview image..." (exact build command and Dockerfile hash shown, approval required, progress in the console); `python -m app_preview --build-gui-image`; `scripts/build_preview_image.sh` (the existing `.bat` is kept as it is). New `app_preview/imagebuilder.py`. Screen snapshot scales to fit the pane (fractional scales), dark monitor backdrop, default virtual screen 800x600, the entry file opens in the code view when a run starts.
- AI: Ollama and LM Studio are detected, started when installed but stopped (optional), configured and given their model lists automatically; new "Detect local AI" and "Cloud models..." buttons, model box lists live models. New `providers/discovery.py`: live model catalogue (OpenRouter public list with Free/Paid prices; OpenAI, Anthropic, Gemini, Groq, Mistral, Together, DeepSeek, xAI, Fireworks, Cerebras with your key), disk cache for offline use, keys never stored. New settings flags `auto_detect_local`, `auto_start_local`, `fetch_cloud_models` (default on; the master AI switch stays off until you turn it on).
- Docs: `docs/APP_PREVIEW.md` gets a "GUI image on demand" section; user guide and known issues updated.

## 0.9.2 - fix: application failed to start (App Preview)
- `ui/app_preview_view.py`: `AppPreviewView.__init__` used `on_source_changed` without declaring it, so building the main window raised `TclError: unknown option "-on_source_changed"` and the application would not open. The parameter is now declared (a project picked inside the panel is announced to the host, which updates Build & Test, analysis and the rest).
- `app_preview/discovery.py`: launcher scripts named `run_*.py` / `start_*.py` / `launch_*.py` without a main guard are listed again (the `RUN_LIKE` pattern was defined but not applied), including the extra "via imports" GUI row.
- Restored `docs/APP_PREVIEW.md` and `scripts/build_preview_image.bat` (listed in the phase 17 registry but missing from the project).
- New regression tests: `tests/app_preview/test_host_wiring.py`.

## 0.9.1 - App Preview: embedded VirtualBox-style Preview card
- New docked **Preview** card (framed thumbnail with monitor icon, like the VirtualBox Manager) beside the viewer tabs, visible whatever tab is open: GUI apps show their virtual screen, web apps a page thumbnail, console apps a terminal view of the latest output; idle shows "Powered off", paused/finished runs keep the last picture under a status badge. Click it to open the full-size tab.
- New **Details** list under it: state, mode, start option, image, network, published ports, uptime, CPU/memory.
- New file `ui/preview_card.py`; `ui/app_preview_view.py` feeds it (additive).

## 0.9.0 - App Preview: run, debug, trace and test in containers with a viewer panel
- New "App Preview" view (sidebar > Actions) and `app_preview/` package. Pick a ZIP or folder (or use the analysed project), choose Run / Debug / Trace / Test and a start option (found automatically for Python, Node, .NET, static sites, plus any custom command), review the exact plan, approve, and watch the app like a VM viewer.
- Viewer: virtual screen for GUI apps (Xvfb snapshots, optional mouse/keyboard forwarding), live page of web apps (published on 127.0.0.1 only, status, headers, text snapshot, open in browser), console, trace tables and timeline, debugger (breakpoints, step into/over/out, call stack, variables, evaluate), located errors and hints.
- Code view with current line, breakpoints, error marks and an execution heat map; a "Code block" pane (the function/class being executed or clicked) with an "Output of this block" pane that shows what that code printed (Python).
- Runtimes: Docker, Podman, nerdctl, Finch or any docker-compatible program. Same hardening as Build & Test (approval per exact plan, no host fallback, no automatic image pulls, scratch copy, dropped capabilities, non-root, read-only root). Optional install of requirements/npm packages inside the container (network, shown in the approval).
- Preview runs also appear in Live Run; finished runs flow into the report and the Findings log analysis like Build & Test runs. `python -m app_preview PROJECT --list|--entry ID --mode ...` for the command line. `scripts/build_preview_image.bat` builds the local GUI image.
- Existing files changed only additively: `ui/main_window.py` (new tab, follows the chosen project, theme), `ui/theme.py` (sidebar entry and icon), `core/phase_registry.py` (phase 17).

## 0.8.0 - auto-start Docker/Podman and embedded live run preview
- Installed-but-stopped Docker/Podman is started automatically when a run needs it (Windows: `docker desktop start` / Docker Desktop; macOS: Docker app; Linux: systemd user/system unit; Podman: `podman machine start`). Opt-out checkbox + "Start Docker/Podman" button in Build & Test. Never installs anything; a missing Podman machine is reported, not created. Library default stays off (`Options.auto_start`).
- New "Live Run" view (sidebar > Actions): status chip, timer, live console (stdout/stderr), container CPU/memory graphs, process list, phase timeline, Find/Copy/Save, Stop, history of the session's runs. Opens automatically when any sandboxed run starts (build, test, custom debug/trace command, patch verification); can be switched off.
- Patch verification now also benefits from auto-start.

## 0.7.3 - maximized, resizable, scrollable, collapsible layout
- Starts maximized. Sidebar: drag the splitter to resize, « folds it to an icon rail, groups fold/unfold, list scrolls; state is remembered.
- Pages scroll (bars appear only when needed; wheel / Shift+wheel) and stretch to fill larger windows. Tables and text boxes everywhere get scrollbars.
- Every group box (Inputs, Policy, Run options, Dashboard sections, AI provider, ...) folds by clicking its title; the Options strip folds too.
- Build & Test: commands table and output are split with a draggable divider.

## 0.7.2 - one source for everything
- Build & Test no longer has its own ZIP/folder picker: it uses the project chosen in the toolbar and discovers commands automatically when opened or when the project changes (read-only; nothing is run).

## 0.7.1 - compact icon UI
- New `ui/theme.py` (light/dark theme, compact ttk styles, tooltips, code-drawn window icon) and `ui/sidebar.py` (icon navigation in collapsible groups; replaces the 28-tab strip, tab names unchanged). Theme choice is remembered in `data/ui_prefs.json`.
- Slim icon toolbar and status bar with thin progress; tighter padding across all panels; icons on panel buttons. No behaviour changes.

## 0.7.0 - Stages B, C, D: institution features
- Stage B (education): batch grading of student ZIP/tar/folder submissions (or an LMS bulk ZIP), rubric scoring (static criteria + manual scores CSV), similarity review with starter-template exclusion, per-student reports, CSV/summary export. `--grade DIR`, "Batch grading" tab.
- Stage C (enterprise): policy gates (JSON), licence detection, CycloneDX 1.5 SBOM, SARIF 2.1.0 export, tamper-evident hash-chained audit log, trends from history, portfolio scan, CI gate (`--ci`, exit 0/1/2), `--portfolio`, `--verify-audit`, "Compliance" tab.
- Stage D (shared use): local shared-folder workspace with roles (admin/reviewer/analyst/viewer), PBKDF2 passwords with lockout, shared history, approval workflow (separation of duties, hash-bound, expiring) that can gate applying a fix, HMAC-signed reports, `scripts/team_cli.py`, "Team" tab.
- Fixes found while testing: audit log now redacts secret-looking values; lockout counter persisted correctly.

## 0.6.0 - Stage A: wider inputs and analysis
- Inputs: ZIP, .tar/.tar.gz/.tgz/.tar.bz2/.tar.xz and plain folders (via a safe temporary ZIP; links, traversal and vendored dirs skipped; temp files removed). .7z/.rar are rejected with a clear message.
- New analyzers: HTML, CSS, PHP (`php -l` + heuristics) and Go (`gofmt -e` + heuristics). Missing tools are reported as unavailable, never faked.
- Code map: functions/classes/methods and call graph (exact for Python via AST; other languages names only), complexity hotspots; new "Code map" tab and report section.
- Documentation generator (`--docs`, "Generate documentation" button): Markdown from analysis facts, unknowns marked TODO.
- 443 automated tests.

## 0.5.0 - phases 9 and 10 merged
- Dashboard, filterable findings, report preview, history database, validated settings (Phase 9).
- Phase 10 report set on every analysis; delivery packager with secret scan, SHA-256 manifest and test evidence (`--deliver`).
- Approval dialog now shows the Phase 4 plan.

## 0.4.0 - phases 3-7 merged, 8 wired
- Added/wired Phase 3 static analysis, Phase 4 sandboxed execution, Phase 5 database analysis, Phase 6 findings, Phase 7 AI providers.
- Report has sections for every phase; AI notes are labelled unverified.
- Fixed secret redaction for prefixed key names; fixed Tk access from worker thread.

# Changelog

## 0.3.0 - Phase 8: human-approved code correction
- Added: safe workspace extraction, patch proposal/diff/approve/backup/apply/revert, static before/after comparison,
  isolated-runner verification hook with baseline comparison and rollback policy, rule-based fix suggestions,
  review dialog (GUI), `scripts/fix_cli.py`, `models/patch.py`. Replaced the Phase 8 placeholder `core/patch_manager.py`
  (same public names: propose, diff, apply, revert).
- Changed (additive only): "Propose fix..." button and workspace handling in `ui/main_window.py`; version 0.3.0.
- 163 automated tests.

## 0.2.0 - Phase 2: project structure and dependencies
- Added: directory tree, entry points, config-file inventory, import/module graph (6 language families),
  circular-import detection, declared-vs-imported dependency checks, environment-variable discovery (names only),
  database connection-config discovery (redacted), confidence classes, configuration warnings, dependency summary.
- Added: GUI tabs (Structure, Entry points, Imports, Config & Env, Confidence, Config warnings), HTML report sections,
  `structure` key in JSON (schema_version unchanged; all Phase 1 keys and behavior unchanged).
- Added: placeholders for all files of phases 3-10, `core/phase_registry.py`, `scripts/phase_status.py`,
  `scripts/merge_helper.py`, `PHASES.md`, `docs/MERGE_GUIDE.md`.
- Changed (additive only): `Evidence.level` optional field; `AnalysisResult.structure` optional field; version 0.2.0.
- 110 automated tests.

## 0.1.0 - Phase 1: working ZIP analyzer
- ZIP selection, validation and safe in-memory inventory (nothing is extracted to disk).
- Limits: entry count, declared total size, per-member size, compression ratio; rejects traversal,
  absolute/drive/UNC paths, NUL/control characters, symlinks and encrypted members.
- Language detection (extensions + marker files), framework/runtime detection, database clue detection with
  strong / moderate / weak evidence levels, dependency manifest identification and light parsing.
- Python syntax check via `ast.parse` (never imports/executes), TODO/FIXME/HACK scan, heuristic security warnings.
- HTML (escaped, CSP-restricted) and JSON reports with secret redaction.
- Tkinter UI with worker thread, progress, cancel, findings grid and detail panel; `--cli` mode.
- 66 automated tests (unit, integration, security, UI smoke).
