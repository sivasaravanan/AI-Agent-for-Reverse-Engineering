"""Rule-based, deterministic hints for common sandbox failures. Heuristics: they suggest, they do not prove."""
from __future__ import annotations

import re
from typing import Dict, List, Sequence

Hint = Dict[str, str]          # {"level": "info" | "warn", "text": str}


def _h(level: str, text: str) -> Hint:
    return {"level": level, "text": text}


def hints_for(status: str, exit_code, text: str, network_enabled: bool = False, diagnostics: Sequence = ()) -> List[Hint]:
    out: List[Hint] = []
    t = text or ""
    low = t.lower()

    if status == "timeout":
        out.append(_h("warn", "The run hit the time limit and was stopped. Raise 'Timeout (s)' for a slow build, or check for a "
                              "test that waits for input or a network service."))
    if exit_code == 137 or "oom-killed" in low or re.search(r"\bkilled\b", low) and exit_code not in (0, None):
        out.append(_h("warn", "The process was killed, most likely by the memory limit. Raise 'Memory (MB)' and run again."))
    if exit_code == 137 and re.search(r"pip install|Downloading\s", t):
        out.append(_h("info", "A pip install was killed while downloading. /tmp inside the sandbox lives in RAM and counts toward the "
                              "memory limit, so large packages (torch, tensorflow, opencv) fill it. Use the 'pip-pytest-heavy' "
                              "command (installs to disk, raises the limit) and make sure Docker Desktop itself is allowed enough memory."))
    if re.search(r"libtk8\.6|libtcl8\.6|No module named '_tkinter'|no display name and no \$DISPLAY|couldn't connect to display", t, re.I):
        out.append(_h("warn", "This program opens a window, but the plain Python image has no Tk and no screen. Run it in the App Preview view "
                              "(it uses the preview GUI image with a virtual screen; build it once: docker build -t ase-preview-gui:latest "
                              "-f app_preview/images/Dockerfile.gui app_preview/images). Build & Test cannot show windows."))
    if exit_code == 127 or "command not found" in low or "executable file not found" in low:
        out.append(_h("warn", "The program is not in the container image. Pick an image that contains it (Image override) "
                              "or a different command."))
    m = re.search(r"No module named ['\"]?([\w.]+)", t)
    if m:
        mod = m.group(1).split(".")[0]
        if mod == "pytest":
            out.append(_h("info", "pytest is not in the plain Python image. Use 'python:unittest', or approve the 'pip-pytest' "
                                  "command (it turns the network on for that run), or use an image that already has pytest."))
        else:
            out.append(_h("info", f"The module '{mod}' is not installed in the image. The network is "
                                  + ("on" if network_enabled else "off by default") + ", so dependencies are not installed "
                                  "automatically. Install them from a command you approve with the network on, or use an image "
                                  "that has them."))
    if re.search(r"ENOTFOUND|EAI_AGAIN|getaddrinfo|Temporary failure in name resolution|Network is unreachable|"
                 r"Connection refused|Could not resolve host|Unable to load the service index|NU1301|NU1101", t, re.I):
        out.append(_h("info", "A download was attempted but the sandbox network is off. That is expected for a normal run. "
                              "Choose the command variant marked 'restore' (needs network and your explicit approval) if you "
                              "want packages downloaded."))
    if re.search(r"Unable to find image|pull access denied|manifest unknown|image .* not known|No such image", t, re.I):
        out.append(_h("warn", "The container image is not downloaded. Use 'Set up Docker...' to download it; the app never "
                              "downloads images during a run."))
    if re.search(r"Read-only file system|EROFS|Permission denied|EACCES", t):
        out.append(_h("info", "The sandbox is read-only except /tmp, and runs as an unprivileged user. Tests that write files "
                              "next to the source will fail; point them at /tmp."))
    if re.search(r"collected 0 items|no tests ran|Ran 0 tests|No tests were found|0 passed", t, re.I) and exit_code not in (0,):
        out.append(_h("info", "No tests were found. Check the test folder name and file pattern, or try 'python -m unittest "
                              "discover -v' as a custom command."))
    if re.search(r"MSB1003|Specify a project or solution file", t):
        out.append(_h("info", "dotnet could not find a project file in the working folder. Use the command that names the "
                              ".csproj/.sln path."))
    if re.search(r"SyntaxError|IndentationError|TabError", t):
        out.append(_h("info", "A Python syntax error stops the whole run before any test starts. Fix the first error listed."))
    errs = [d for d in diagnostics if getattr(d, "severity", "") == "error"]
    if len(errs) > 1 and any(getattr(d, "tool", "") in ("dotnet", "java", "typescript") for d in errs):
        out.append(_h("info", f"{len(errs)} errors were reported. Compilers often report follow-on errors; fix the first one and "
                              "run again before reading the rest."))
    if exit_code not in (0, None) and not out and not diagnostics:
        out.append(_h("info", "The command failed but no known error pattern was recognised. Read the raw output."))
    return out
