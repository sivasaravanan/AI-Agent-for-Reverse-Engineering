"""Quick run - one click, a live result, the usual hiccups handled by the tool itself (standard library only). New module, additive.

Nothing here weakens the sandbox: the run still goes through ``build_plan`` -> ``run_plan`` (disposable copy, no host folders, network off,
read-only root filesystem, dropped capabilities). Quick run only takes the chores off the user:

  * picks what to run         the root entry script (else the tests, else a syntax check); a single chosen .py file runs as it is;
                              a window program (Tk / Qt / pygame) is handed straight to App Preview;
  * fixes a mistyped script   ``python dupescopy.py`` in a project that has ``dupescope.py`` runs ``dupescope.py`` (said in the notes);
  * starts Docker / Podman    when it is installed but not running (nothing is ever installed);
  * fetches a missing image   after one question (or never asking, with "Pull a missing image automatically" in Storage & clean-up);
  * copies only what is needed  a script run in a huge project copies the script and its imports; an oversized project is trimmed, never
                              refused for size alone (``quickprep``); what was left out is listed;
  * retries                   once with a bigger timeout / memory after a timeout or an out-of-memory kill;
  * installs a missing package  when the program stops with "No module named ..." it re-runs with requirements installed (the network is
                              needed for that, so this ONE step asks for approval, exactly like the normal approval dialog);
  * clears up                 the copy and the container are removed after the run (``cleanup``; setting "Clear after every run").

Everything slow or interactive is injected through ``Hooks`` so the UI thread is never blocked and tests need no Docker.
"""
from __future__ import annotations

import dataclasses
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import storage as _storage
from .autotune import apply_autotune, retry_limits
from .discovery import ECOSYSTEM_ENV, custom_candidate
from .models import Approval, Candidate, ExecutionResult, Limits, Options, Plan
from .quickprep import QUICK, Src, nearest_script
from .run_targets import discover_all
from .runtime_start import ensure_runtime
from .sandbox import build_plan, entry_script, image_present, run_plan

MAX_ATTEMPTS = 3
PIP_NAMES = {"cv2": "opencv-python-headless", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
             "dotenv": "python-dotenv", "dateutil": "python-dateutil", "skimage": "scikit-image", "Crypto": "pycryptodome",
             "serial": "pyserial", "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf", "jwt": "pyjwt", "attr": "attrs"}
_MISSING = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([A-Za-z0-9_.]+)['\"]")
_PKG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _noop(*_a, **_k):
    return None


@dataclass
class Hooks:
    progress: Callable[[str], None] = _noop
    confirm: Callable[[str, str], bool] = lambda title, text: False          # yes / no questions (the UI shows them on its own thread)
    approve: Callable[[Plan], bool] = lambda plan: False                      # a plan that needs the NETWORK: the user approves it
    gui: Optional[Callable[[str, str], bool]] = None                          # (source, script) -> handed to App Preview


@dataclass
class QuickOutcome:
    results: List[ExecutionResult] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)            # what the tool handled by itself, in order
    message: str = ""                                            # why nothing ran / what to do next
    handed_over: bool = False                                    # a window program went to App Preview
    candidate: Optional[Candidate] = None

    @property
    def final(self) -> Optional[ExecutionResult]:
        return self.results[-1] if self.results else None


# ---------------------------------------------------------------------------------------------------------------- choosing
def pick_candidate(source) -> tuple:
    """(candidate, notes). Raises ValueError with a readable message when there is nothing to run."""
    p = Path(source)
    if p.is_file():
        import zipfile
        if not zipfile.is_zipfile(p):
            ext = p.suffix.lower()
            if ext in (".py", ".pyw"):
                return custom_candidate(["python", "./" + p.name], "python"), [f"Running the chosen file {p.name}."]
            if ext in (".js", ".mjs", ".cjs"):
                return custom_candidate(["node", "./" + p.name], "node"), [f"Running the chosen file {p.name}."]
            raise ValueError(f"A single {ext or 'extension-less'} file cannot be run directly; choose a project folder or archive.")
    cands, notes = discover_all(source)
    free = [c for c in cands if not c.needs_network]
    for pick in (lambda c: c.origin == "entry-point", lambda c: "test" in c.id.lower() and not c.id.endswith("compileall"),
                 lambda c: not c.id.endswith("compileall"), lambda c: True):
        for c in free:
            if pick(c):
                return c, notes
    raise ValueError("Nothing runnable was found in this project (no entry script, tests or build command). "
                     "Type a custom command in Build & Test.")


def fix_entry(source, cand: Candidate, actions: List[str]) -> Candidate:
    """A script name that is not in the project but is one typo away from one that is: use the real one and say so."""
    entry = entry_script(cand.argv)
    if not entry:
        return cand
    try:
        src = Src(source)
    except Exception:  # noqa: BLE001 - unreadable source: the run itself explains it
        return cand
    try:
        names = [e.rel for e in src.entries]
    finally:
        src.close()
    if entry in names or entry.lower() in {n.lower() for n in names}:
        return cand
    near = nearest_script(names, entry)
    if not near:
        return cand
    argv = []
    for a in cand.argv:
        norm = a.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        argv.append("./" + near if norm == entry else a)
    actions.append(f"'{entry}' is not in the project; it looks like a typo of '{near}', so '{near}' is run instead.")
    return dataclasses.replace(cand, argv=argv, notes=list(cand.notes) + [f"Script name corrected from {entry} to {near}."])


# ---------------------------------------------------------------------------------------------------------------- image
def pull_image(exe: str, image: str, progress: Callable[[str], None], cancel: threading.Event, timeout_s: int = 1800) -> tuple:
    """``docker pull`` with live progress lines. (ok, message). The only network use of Quick run itself (the runtime downloads the image)."""
    import os
    import time
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        proc = subprocess.Popen([exe, "pull", image], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", **kw)
    except OSError as exc:
        return False, f"Could not start {exe}: {exc}"
    t0, last = time.monotonic(), ""
    try:
        for line in proc.stdout:                                          # type: ignore[union-attr]
            line = line.strip()
            if line and line != last:
                last = line
                progress(f"pulling {image}: {line[:120]}")
            if cancel.is_set() or time.monotonic() - t0 > timeout_s:
                proc.kill()
                return False, "The image download was cancelled or took too long."
        rc = proc.wait(30)
    except Exception as exc:  # noqa: BLE001
        try:
            proc.kill()
        except OSError:
            pass
        return False, f"The image download failed: {exc}"
    return (rc == 0), (f"Image {image} is ready." if rc == 0 else f"'{exe} pull {image}' failed (exit {rc}): {last}")


def ensure_image(exe: str, image: str, hooks: Hooks, cfg: dict, cancel: threading.Event, actions: List[str],
                 pull: Optional[Callable] = None, present: Optional[Callable] = None) -> tuple:
    """(ok, message). Pulls a missing image after one question, or silently when 'Pull a missing image automatically' is on."""
    pull = pull or pull_image
    present = present or image_present
    if present(exe, image):
        return True, ""
    if not cfg.get("auto_pull") and not hooks.confirm(
            "Image needed", f"The container image '{image}' is not on this computer yet.\n\nDownload it now? (one time; the container "
            "runtime fetches it, your project is not involved)\n\nTip: tick 'Pull a missing image automatically' in Storage & clean-up to "
            "skip this question."):
        return False, (f"Skipped: image '{image}' is not available. Download it once with:  {Path(exe).name} pull {image}")
    ok, msg = pull(exe, image, hooks.progress, cancel)
    if ok:
        actions.append(f"downloaded the container image {image}")
    return ok, msg


# ---------------------------------------------------------------------------------------------------------------- self-healing
def pip_run_candidate(source, cand: Candidate, module: str) -> Optional[Candidate]:
    """The same script, run after installing the missing package(s) into /tmp/deps. None when it cannot be done safely."""
    entry = entry_script(cand.argv)
    if not entry or cand.id.startswith("python:quick-pip") or "pip install" in " ".join(cand.argv):
        return None
    top = module.split(".")[0]
    try:
        src = Src(source)
        try:
            names = [e.rel for e in src.entries]
            has_req = "requirements.txt" in names
        finally:
            src.close()
    except Exception:  # noqa: BLE001
        names, has_req = [], False
    if f"{top}.py" in names or any(n.startswith(top + "/") for n in names):
        return None                                                         # a project module: installing from PyPI would be wrong
    pkg = PIP_NAMES.get(top, top)
    if not _PKG_OK.match(pkg):
        return None
    target = "-r requirements.txt" if has_req else shlex.quote(pkg)
    line = f"pip install --no-input --target /tmp/deps {target} && PYTHONPATH=/tmp/deps python {shlex.quote('./' + entry)}"
    return Candidate(f"python:quick-pip:{entry}", "python", ["sh", "-c", line],
                     f"Install {'requirements.txt' if has_req else pkg}, then run {entry}", entry, None, "entry-point", True,
                     ["Network access is required (explicit approval)",
                      "pip may build source distributions, which run arbitrary setup code (inside the container)",
                      "Project code executes inside the container"],
                     [f"Added by Quick run because the program stopped with: No module named '{module}'."], dict(ECOSYSTEM_ENV["python"]))


def heal(res: ExecutionResult, cand: Candidate, lim: Limits, source, actions: List[str], tried_pip: bool) -> tuple:
    """(next candidate, next limits, tried_pip) or (None, None, tried_pip) when there is nothing sensible to change."""
    bigger = retry_limits(res.status, res.exit_code, lim)
    if bigger is not None:
        what = (f"timeout {lim.timeout_s}s -> {bigger.timeout_s}s" if res.status == "timeout"
                else f"memory {lim.memory_mb} MB -> {bigger.memory_mb} MB")
        actions.append(f"the run ended with '{res.status}': trying once more with {what}.")
        return cand, bigger, tried_pip
    if res.status == "failed" and not tried_pip:
        m = _MISSING.search(res.stderr or "") or _MISSING.search(res.stdout or "")
        if m:
            nxt = pip_run_candidate(source, cand, m.group(1))
            if nxt is not None:
                actions.append(f"the program needs the package '{m.group(1)}': running again with the requirements installed first "
                               "(needs the network - you approve that step).")
                return nxt, lim, True
    if res.status == "failed":
        gone = [n for n in res.notes if n.startswith("Left out")]
        if gone and re.search(r"FileNotFoundError|No such file or directory", (res.stderr or "") + (res.stdout or "")):
            actions.append("the program could not find a file that was too large to copy in - see the 'Left out' note; raise "
                           "'Largest data file (MB)' in Storage & clean-up and run again.")
    return None, None, tried_pip


# ---------------------------------------------------------------------------------------------------------------- the run
def quick_run(source, candidate: Optional[Candidate] = None, hooks: Optional[Hooks] = None, cancel: Optional[threading.Event] = None,
              image: str = "", options: Optional[Options] = None, limits: Optional[Limits] = None) -> QuickOutcome:
    """Run the project with no questions unless one is really needed. Never raises: every outcome is a QuickOutcome."""
    hooks = hooks or Hooks()
    cancel = cancel or threading.Event()
    out = QuickOutcome()
    token = QUICK.set(True)                         # prepare_workspace: copy trimmed instead of refusing for size
    try:
        return _quick(source, candidate, hooks, cancel, image, options, limits, out)
    except Exception as exc:  # noqa: BLE001
        out.message = f"Quick run stopped: {type(exc).__name__}: {exc}"
        return out
    finally:
        QUICK.reset(token)


def _quick(source, candidate, hooks: Hooks, cancel, image, options, limits, out: QuickOutcome) -> QuickOutcome:
    say = hooks.progress
    cfg = _storage.load()
    options = options or Options(auto_start=True)
    options.auto_start = True
    if not str(source or "").strip() or not Path(str(source)).exists():
        out.message = "No project selected (or it no longer exists). Choose a folder, archive or file first."
        return out
    # 1. what to run
    say("Quick run: looking for what to run...")
    try:
        if candidate is None:
            candidate, notes = pick_candidate(source)
            out.actions.append(f"chose: {candidate.display}")
    except (ValueError, OSError, Exception) as exc:  # noqa: BLE001 - readable message, never a traceback
        out.message = str(exc) or "Nothing to run."
        return out
    candidate = fix_entry(source, candidate, out.actions)
    out.candidate = candidate
    script = entry_script(candidate.argv)
    # 2. a window program belongs in App Preview
    if hooks.gui is not None and script and candidate.ecosystem == "python":
        try:
            from app_preview.discovery import custom_entry, entry_uses_gui
            if entry_uses_gui(source, custom_entry(list(candidate.argv))) and hooks.gui(str(source), script):
                out.handed_over = True
                out.actions.append(f"{script} opens a window: handed to App Preview (live screen).")
                return out
        except Exception:  # noqa: BLE001 - detection only
            pass
    # 3. container runtime (started when installed but stopped)
    say("Quick run: checking the container runtime...")
    iso = ensure_runtime(Options(backend=options.backend, tool_paths=dict(options.tool_paths), auto_start=True), progress=say, cancel=cancel)
    if iso.get("started") and iso.get("available"):
        out.actions.append(f"{iso['backend']} was not running: started it.")
    if not iso.get("available"):
        out.message = "Docker / Podman is not available: " + str(iso.get("reason") or "") + ("\n" + iso["hint"] if iso.get("hint") else "")
        return out
    exe = iso["executable"]
    # 4. plan (+ auto-tune limits for big installs)
    lim = limits or Limits()
    tried_pip = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cand = candidate
        tuned_lim = lim
        if cand.ecosystem == "python":
            cand, tuned_lim, tuned = apply_autotune(cand, lim, source)
            if tuned:
                out.actions.append("auto-tuned: " + "; ".join(tuned))
        plan = build_plan(source, cand, options, image=image, limits=tuned_lim)
        ok, msg = ensure_image(exe, plan.image, hooks, cfg, cancel, out.actions)
        if not ok:
            out.message = msg
            return out
        if plan.limits.network and not hooks.approve(plan):                 # the only step that needs the user: the network
            out.message = "Not approved - nothing was executed (the plan needs the network)."
            return out
        say(f"Quick run: running {cand.display} ...")
        res = run_plan(plan, Approval.for_plan(plan), options, progress=say, cancel=cancel)
        out.results.append(res)
        if res.ok or cancel.is_set() or attempt == MAX_ATTEMPTS:
            break
        nxt, nlim, tried_pip = heal(res, cand, plan.limits, source, out.actions, tried_pip)
        if nxt is None:
            break
        candidate, lim = nxt, nlim
    return out


def summary(out: QuickOutcome) -> str:
    """Plain text for the status line / console."""
    lines = []
    if out.actions:
        lines.append("Handled automatically:")
        lines += [f"  - {a}" for a in out.actions]
    if out.message:
        lines.append(out.message)
    f = out.final
    if f is not None:
        lines.append(f"Result: {f.status}  exit={f.exit_code}  {f.duration_s}s")
    return "\n".join(lines)
