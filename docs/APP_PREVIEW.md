# App Preview (phase 17)

Run, debug, trace and test the application inside a ZIP or folder in a disposable container and watch it from the
**App Preview** view (sidebar > Actions). Nothing runs on this computer.

## Using it
1. Choose the project (ZIP/folder, or "Use analysed project"). Start options are discovered automatically (read-only).
2. Pick a mode and a start option (or type a command; it runs inside the container only).
3. **Start** shows the exact plan (command, image, network, ports, limits, risks). Nothing runs until you approve.

| Mode | What it does | Languages |
|---|---|---|
| Run | Starts the app; with "Link output to code" (default) Python output is tagged with the code that printed it | any (link: Python) |
| Trace | Records calls, lines and returns; heat map, function table, timeline | Python |
| Debug | Breakpoints (click a line number), Continue / Over / Into / Out (F5, F10, F11, Shift+F11), stack, variables, evaluate | Python |
| Test | Runs the discovered test command | pytest, unittest, npm, dotnet, maven, gradle |

Viewer tabs: **Screen** (GUI apps on a virtual display), **Web** (web apps on `127.0.0.1`), **Console**, **Trace**, **Debug**,
**Insights** (located errors + hints). Bottom: the **code block** being executed/selected and **its output**.

## Preview card (VirtualBox-style)
A framed **Preview** thumbnail is docked at the right and stays visible on every tab, with a **Details** list under it (state,
mode, image, network, ports, uptime, CPU/memory). It shows: the virtual screen of a GUI app; a page thumbnail of a web app
(title + text); a terminal view of the latest console output; "Powered off" when nothing runs. Paused or finished runs keep
the last picture under a badge. Click it to jump to the Screen / Web / Console tab. It is a thumbnail of the same snapshots
(about 2 per second), not live video.

## Runtimes
Docker, Podman, nerdctl, Finch, or *custom* (any docker-compatible program path). `auto` tries them in that order.
A runtime counts as available only if `info` answers; otherwise the run is skipped (never executed on the host).

## Images
Images are never pulled or built automatically. Defaults match Build & Test (`python:3.12-slim`, `node:20-slim`, ...). Apps
need their dependencies in the image, or tick **Install requirements first** (runs `pip install -r requirements.txt` into
`/tmp` or `npm install` inside the container; this turns the network on, shown in the approval).
GUI apps need `ase-preview-gui:latest` (Xvfb, Tk, xdotool, ImageMagick); see "GUI image on demand" below.

## Security (same rules as Phase 4, plus)
- Approval is bound to the exact plan (command, env, image, limits, ports, display, helper mount, mode).
- Scratch copy of the project only; read-only root; all capabilities dropped; non-root; no host env/credentials.
- Web apps: the published port is bound to `127.0.0.1`. Publishing needs a network, so the run has the **network enabled**
  (stated in the approval). Programs that bind only `127.0.0.1` inside the container are not reachable.
- Trace/debug mount `app_preview/runtime/` read-only at `/ase`. Debug uses stdin for debugger commands, so the program sees an empty stdin.
- Screen input is limited to validated click coordinates, `xdotool type` text and key names; no shell strings.

## Limits (honest)
- Trace, debug and code-to-output linking work for Python only (`python file.py` or `python -m module`). Other languages get
  Run/Test plus located errors in the code view.
- Not run against a real Docker/Podman here: tests use a **fake runtime** that executes on the host (verifies flags, mounts,
  protocol, events, cleanup; does not prove isolation). Verify once on Windows: a console app, `Trace`, `Debug`, a web app,
  and a GUI app after building the GUI image.
- Screen = periodic PNG snapshots (about 2/s), not a live video. Web tab shows a text snapshot, not a rendered page.
- Podman rootless/SELinux bind-mount permission differences are untested.
- Tar archives are not supported by the sandbox (ZIP/folder only), as in Build & Test.
- Tracing slows Python programs noticeably; the event cap is 300,000 (a note is shown when reached).

## GUI image on demand
The preview never pulls or builds images by itself, but it can build the GUI image for you after you approve it. If
`ase-preview-gui:latest` is missing, the Screen tab shows **Prepare GUI preview image...** and pressing Start on a GUI app offers it
too: you see the exact `<runtime> build ...` command and the Dockerfile hash, approve it, and watch the progress in the console (needs the
internet once: base image + Debian packages; your project is not part of the build). Alternatives: `scripts\build_preview_image.bat [runtime]`,
`scripts/build_preview_image.sh`, or `python -m app_preview --build-gui-image [--backend ...] [--yes]` (asks for approval unless `--yes`).
You can also type your own image (Xvfb + ImageMagick `import` + xdotool) in the **Image** box.
The virtual screen is 800x600 by default (`PreviewOptions.screen`); snapshots are scaled to fit the pane. There is no window manager, so
windows appear at the top-left without a title bar.


## ⚡ Run now
One button for "just run it and show me". It chooses the entry (window > web > console), lists every step in a single approval window
(start the runtime, build the GUI image once, install the packages the code imports into a cached `ase-env-<hash>` image once, run, show),
then opens the preview at once. A missing package found at run time is installed after one yes/no and the run repeats; an out-of-memory
kill repeats once with more memory. Everything still runs only inside the disposable container; installs happen in an image build, never
on your computer. Later runs of the same program need no internet.


## Screen options, external view, snapshots
Screen tab: **Size** (virtual screen, next start), **Rate** (snapshots per second, live), **View** (Fit/50%/100%/200%), **Pop out** (own window, F11 full screen, on top; web apps open in the browser), **Snapshot** (Save as..., Quick save into `data/snapshots`, Open/Choose folder, auto-snapshot at the end of a run). Live Run's thumbnail opens the same window when clicked.


## Lean copy (what goes into the container)
A script run never copies the whole folder or archive. It copies the script, what it imports, small data next to it, project files its code names, and what you add with **Include...**. If the program reports a missing file, the panel offers to copy exactly that file and run again. Switch it off in Storage & clean-up ("Lean copy"). Commands that are not a plain Python script keep the earlier rules (see CHANGELOG 0.9.17).


## Compact toolbars (icons, tooltips, nothing hidden)
The toolbars of App Preview show an icon per button; hover for the name and the explanation. **Aa** switches to icons with text and back (remembered as the `compact` preference). Rows wrap onto a second line when the window is narrow, so no button, box or check box is ever cut off. Nothing is hidden in either mode.
