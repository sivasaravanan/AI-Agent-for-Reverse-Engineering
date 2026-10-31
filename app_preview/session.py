"""One preview run: starts the approved plan in a disposable container and publishes what happens.

Hard rules kept from the Phase 4 sandbox: no approval for the exact plan -> refused; no working runtime -> skipped (NO host
fallback); images are never pulled; only a scratch COPY of the project is mounted; the container is removed afterwards.

Events: standard ones (start/phase/out/err/stats/top/end) go to ``BUS`` so the Live Run tab shows preview runs as well; preview
events (pv_*) go to ``PV``. Subscribers must be fast and thread-safe (the Tk view only enqueues).
"""
from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from plugins.build_execution import storage as _storage
from plugins.build_execution.events import BUS
from plugins.build_execution.models import Approval, ExecutionResult
from plugins.build_execution.sandbox import CREATE_NO_WINDOW, WorkspaceRejected, _cli, _env, _watch_container, entry_script, prepare_workspace

from .planner import HELPER
from .container import container_command, exec_command, runtime_dir
from .events import PV
from .models import DISPLAY_NUM, PreviewOptions, PreviewPlan
from .runtimes import image_present, status as runtime_status
from .stream import ProtocolDecoder

LIVE_CAP = 4 * 1024 * 1024
KEY_RE = re.compile(r"^[A-Za-z0-9_+]{1,40}$")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Pump(threading.Thread):
    """Reads one pipe, separates protocol lines from plain text, forwards both, keeps the plain text (capped)."""

    def __init__(self, stream, stream_name: str, on_plain: Callable[[str, str], None], on_event: Callable[[Dict], None], cap: int):
        super().__init__(daemon=True)
        self.s, self.name_, self.on_plain, self.on_event, self.cap = stream, stream_name, on_plain, on_event, cap
        self.buf: List[str] = []
        self.size = 0
        self.truncated = False
        self.live = 0
        self.dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.proto = ProtocolDecoder()
        self.start()

    def _push(self, plain: str, events: List[Dict]) -> None:
        if plain:
            if self.size < self.cap:
                keep = plain[:self.cap - self.size]
                self.buf.append(keep)
                self.size += len(keep)
                self.truncated = self.truncated or len(keep) < len(plain)
            else:
                self.truncated = True
            if self.live < LIVE_CAP:
                self.live += len(plain)
                self._safe(self.on_plain, self.name_, plain)
            elif self.live != -1:
                self.live = -1
                self._safe(self.on_plain, self.name_, "\n[live preview stopped: output is very large; the final result keeps the capped output]\n")
        for ev in events:
            self._safe(self.on_event, ev)

    @staticmethod
    def _safe(fn, *a) -> None:
        try:
            fn(*a)
        except Exception:  # noqa: BLE001 - a viewer problem never disturbs process handling
            pass

    def run(self) -> None:
        try:
            while True:
                chunk = self.s.read1(65536)
                if not chunk:
                    break
                self._push(*self.proto.feed(self.dec.decode(chunk)))
        except (OSError, ValueError):
            pass
        self._push(*self.proto.flush())

    def text(self) -> str:
        self.join(5)
        return "".join(self.buf)


def _cli_bytes(cmd: List[str], timeout: float = 10, data: Optional[bytes] = None) -> tuple:
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout, env=_env(), stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, b"", str(exc).encode()


def png_size(data: bytes) -> tuple:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


class PreviewSession:
    def __init__(self, plan: PreviewPlan, approval: Optional[Approval], options: Optional[PreviewOptions] = None,
                 on_result: Optional[Callable[[ExecutionResult], None]] = None):
        self.plan, self.approval = plan, approval
        self.options = options or PreviewOptions()
        self.on_result = on_result
        self.run_id = uuid.uuid4().hex[:8]
        self.cancel = threading.Event()
        self.result: Optional[ExecutionResult] = None
        self.state = "idle"
        self.exe: Optional[str] = None
        self.name = ""
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stdin_lock = threading.Lock()
        self._quit = False
        self._inputs: "list[tuple]" = []
        self._inputs_lock = threading.Lock()
        self._chan: Optional[subprocess.Popen] = None       # the long-lived screen / input channel inside the container
        self._chan_ok = False                               # True once it has delivered a picture
        self._chan_lock = threading.Lock()
        self.desktop = ""                                   # "openbox" when the guest has a window manager, "none" when it has not
        self.web_url = f"http://127.0.0.1:{plan.ports[0][0]}/" if plan.ports else ""

    @property
    def screen_size(self) -> tuple:
        """(width, height) of THIS run's virtual screen: what the approved plan started, not what the panel is set to now."""
        m = re.match(r"^\s*(\d+)x(\d+)", str(self.plan.env.get("ASE_SCREEN") or self.options.screen))
        return (int(m.group(1)), int(m.group(2))) if m else (800, 600)

    # ------------------------------------------------------------------ public
    def start(self) -> "PreviewSession":
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"preview-{self.run_id}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self.cancel.set()

    def wait(self, timeout: Optional[float] = None) -> Optional[ExecutionResult]:
        if self._thread:
            self._thread.join(timeout)
        return self.result

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def send(self, cmd: Dict) -> bool:
        """Debugger command (continue/step/next/return/quit/set_break/clear_break/eval/vars). Only in debug mode."""
        if self.plan.mode != "debug" or not self.proc or not self.proc.stdin:
            return False
        if cmd.get("cmd") == "quit":
            self._quit = True
        try:
            with self._stdin_lock:
                self.proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            if cmd.get("cmd") in ("continue", "step", "next", "return"):
                self._pv("pv_state", state="running")
            return True
        except (OSError, ValueError):
            return False

    def input_event(self, kind: str, **kw) -> bool:
        """Synthetic mouse/keyboard input for the virtual screen (GUI apps only). Validated, never a shell string.
        kinds: click, move, down, up, scroll, key, keydown, keyup, type. Sent at once over the screen channel when it is up,
        otherwise queued for the slower one-command-per-event path."""
        if not self.plan.display:
            return False
        w, h = self.screen_size
        msg: Optional[Dict] = None
        item = None
        try:
            if kind in ("click", "move", "down", "up"):
                x, y = int(kw.get("x", -1)), int(kw.get("y", -1))
                if kind != "up" or "x" in kw:
                    if not (0 <= x < w and 0 <= y < h):
                        return False
                btn = int(kw.get("button", 1))
                btn = btn if btn in (1, 2, 3) else 1
                if kind == "click":
                    msg = {"c": "click", "x": x, "y": y, "b": btn}
                    item = ("click", ["xdotool", "mousemove", str(x), str(y), "click", str(btn)])
                elif kind == "move":
                    msg = {"c": "move", "x": x, "y": y}
                    item = ("move", ["xdotool", "mousemove", str(x), str(y)])
                else:
                    msg = {"c": kind, "b": btn}
                    if "x" in kw:
                        msg["x"], msg["y"] = x, y
                    item = (kind, (["xdotool", "mousemove", str(x), str(y)] if "x" in kw else ["xdotool"]) + ["mousedown" if kind == "down" else "mouseup", str(btn)])
            elif kind == "scroll":
                n = max(1, min(10, int(kw.get("n", 1))))
                up = str(kw.get("direction", "down")) == "up"
                msg = {"c": "scroll", "d": "up" if up else "down", "n": n}
                item = ("scroll", ["xdotool", "click", "--repeat", str(n), "4" if up else "5"])
            elif kind == "type":
                text = str(kw.get("text", ""))[:200]
                if not text or "\x00" in text:
                    return False
                msg = {"c": "type", "t": text}
                item = ("type", ["xdotool", "type", "--delay", "20", "--", text])
            elif kind in ("key", "keydown", "keyup"):
                key = str(kw.get("key", ""))
                if not KEY_RE.match(key):
                    return False
                msg = {"c": kind, "k": key}
                item = (kind, ["xdotool", {"key": "key", "keydown": "keydown", "keyup": "keyup"}[kind], "--", key])
            else:
                return False
        except (TypeError, ValueError):
            return False
        if msg is not None and self._chan_send(msg):
            return True
        if kind == "move":
            return True                                   # a pointer move is only useful live; never queued for the slow path
        with self._inputs_lock:
            self._inputs.append(item)
        return True

    def _chan_send(self, msg: Dict) -> bool:
        p = self._chan
        if not (self._chan_ok and p is not None and p.stdin and p.poll() is None):
            return False
        try:
            with self._chan_lock:
                p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                p.stdin.flush()
            return True
        except (OSError, ValueError):
            self._chan_ok = False
            return False

    # ------------------------------------------------------------------ events
    def _bus(self, type_: str, **d) -> None:
        BUS.emit(type_, self.run_id, **d)

    def _pv(self, type_: str, **d) -> None:
        PV.emit(type_, self.run_id, **d)

    def _log(self, m: str) -> None:
        self._bus("phase", text=m)

    def _plain(self, stream: str, text: str) -> None:
        self._bus("out" if stream == "stdout" else "err", text=text)

    def _event(self, ev: Dict) -> None:
        k = ev.get("k")
        if k == "batch":
            self._pv("pv_trace", items=ev.get("ev") or [])
        elif k == "out":
            self._pv("pv_out", f=ev.get("f") or "", l=int(ev.get("l") or 0), fn=ev.get("fn") or "", s=ev.get("s") or "stdout",
                     t=ev.get("t") or "", ts=ev.get("ts") or 0)
        elif k == "stop":
            self._pv("pv_state", state="paused")
            self._pv("pv_stop", **{x: ev.get(x) for x in ("f", "l", "fn", "why", "value", "stack", "vars")})
        elif k == "vars":
            self._pv("pv_vars", f=ev.get("f"), l=ev.get("l"), fn=ev.get("fn"), vars=ev.get("vars") or {})
        elif k == "eval":
            self._pv("pv_eval", expr=ev.get("expr"), v=ev.get("v"), ok=bool(ev.get("ok")))
        elif k == "break":
            self._pv("pv_break", f=ev.get("f"), l=ev.get("l"), ok=bool(ev.get("ok")), err=ev.get("err") or "")
        elif k == "exc_final":
            self._pv("pv_exc", et=ev.get("et"), e=ev.get("e"), frames=ev.get("frames") or [])
        elif k in ("hello", "note", "end"):
            self._pv("pv_note", info=ev)

    # ------------------------------------------------------------------ the run
    def _run(self) -> None:
        plan, options = self.plan, self.options
        res = ExecutionResult(status="error", candidate_id=plan.candidate.id, argv=list(plan.argv), image=plan.image,
                              network_enabled=plan.limits.network, limits=dict(vars(plan.limits)),
                              started=datetime.now(timezone.utc).isoformat())
        self.result = res
        BUS.register_cancel(self.run_id, self.cancel)
        t0 = time.monotonic()
        scratch: Optional[Path] = None
        exe = None
        stop_aux = threading.Event()
        aux: List[threading.Thread] = []
        out_p = err_p = None
        try:
            if self.approval is None or self.approval.plan_hash != plan.plan_hash:
                res.status = "refused"
                res.notes.append("Refused: no approval for this exact command/image/limits/ports/network setting.")
                return
            if not plan.argv or any((not isinstance(a, str)) or "\x00" in a for a in plan.argv):
                res.status = "refused"
                res.notes.append("Refused: invalid command.")
                return
            self._bus("start", title=f"[{plan.mode}] {plan.entry.title}", argv=list(plan.argv), image=plan.image,
                      network=plan.limits.network, timeout_s=plan.limits.timeout_s, candidate=plan.candidate.id)
            self._pv("pv_state", state="starting", mode=plan.mode, kind=plan.entry.kind, url=self.web_url)
            st = runtime_status(options)
            if not st["available"] and options.auto_start and st.get("executable") and st.get("backend") in ("docker", "podman"):
                from plugins.build_execution.models import Options as _O
                from plugins.build_execution.runtime_start import ensure_runtime
                st = ensure_runtime(_O(backend=st["backend"], tool_paths=dict(options.tool_paths), auto_start=True), progress=self._log, cancel=self.cancel)
            res.backend = st.get("backend") or ""
            if not st["available"]:
                res.status = "skipped"
                res.notes.append("Skipped, nothing was executed: " + st["reason"])
                return
            exe = st["executable"]
            self.exe = exe
            if not image_present(exe, plan.image):
                res.status = "skipped"
                hint = ("press 'Prepare GUI preview image' on the Screen tab (or run: python -m app_preview --build-gui-image)" if plan.display
                        else f"run: {res.backend} pull {plan.image}")
                res.notes.append(f"Skipped, nothing was executed: image '{plan.image}' is not available locally. "
                                 f"Images are never pulled automatically; {hint}")
                return
            scratch = _storage.new_scratch("be_pv_")
            workspace, helper = scratch / "work", scratch / "ase"
            self._log("Copying project into a disposable workspace...")
            try:
                res.notes.extend(prepare_workspace(plan.source, workspace, plan.limits, entry=entry_script(plan.entry.argv)))
                for n in res.notes:
                    if n.startswith("Project is larger"):
                        self._log(n)
                if plan.mount_helper:
                    helper.mkdir()
                    for f in runtime_dir().iterdir():
                        if f.is_file() and f.suffix in (".py", ".sh"):
                            shutil.copyfile(f, helper / f.name)
                            os.chmod(helper / f.name, 0o644)
                    os.chmod(helper, 0o755)
            except (WorkspaceRejected, ValueError, OSError) as exc:
                res.status = "refused"
                res.notes.append(f"Refused: {exc}")
                return
            if "," in str(workspace) or "," in str(helper):
                res.status = "refused"
                res.notes.append("Refused: scratch path contains a comma and cannot be mounted safely.")
                return
            self.name = "be-pv-" + uuid.uuid4().hex[:10]
            res.container_name = self.name
            cmd = container_command(exe, plan, workspace.resolve(), helper.resolve(), self.name)
            res.container_command = [str(x) for x in cmd]
            self._log(f"Starting {plan.mode} in container {self.name} (network {'ENABLED' if plan.limits.network else 'disabled'})...")
            kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if plan.interactive else subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(), **kw)
            except OSError as exc:
                res.status = "error"
                res.notes.append(f"Could not start {res.backend}: {exc}")
                return
            proc = self.proc
            out_p = _Pump(proc.stdout, "stdout", self._plain, self._event, plan.limits.max_output_bytes)
            err_p = _Pump(proc.stderr, "stderr", self._plain, self._event, plan.limits.max_output_bytes)
            self._pv("pv_state", state="running")
            if options.live_stats:
                aux.append(threading.Thread(target=_watch_container, args=(exe, self.name, self.run_id, stop_aux), daemon=True))
            if plan.display:
                aux.append(threading.Thread(target=self._screen_main, args=(stop_aux,), daemon=True))
            if plan.ports:
                aux.append(threading.Thread(target=self._web, args=(stop_aux,), daemon=True))
            for t in aux:
                t.start()
            outcome = "done"
            while proc.poll() is None:
                if self.cancel.is_set():
                    outcome = "cancelled"
                elif time.monotonic() - t0 > plan.limits.timeout_s:
                    outcome = "timeout"
                if outcome != "done":
                    _cli([exe, "kill", self.name], 20)
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                time.sleep(0.1)
            stop_aux.set()
            for t in aux:
                t.join(3)
            res.stdout, res.stderr = out_p.text(), err_p.text()
            res.output_truncated = out_p.truncated or err_p.truncated
            code = proc.returncode
            if outcome == "timeout":
                res.status = "timeout"
                res.notes.append(f"Timed out after {plan.limits.timeout_s}s; the container was killed.")
            elif outcome == "cancelled":
                res.status = "cancelled"
                res.notes.append("Stopped by user; the container was killed.")
            else:
                res.exit_code = code
                res.status = "completed" if code == 0 else "failed"
                if self._quit:
                    res.status = "cancelled"
                    res.notes.append("The debugging session was ended by the user.")
                elif code == 125:
                    res.status = "error"
                    res.notes.append(f"{res.backend} could not start the container (see the console).")
                elif code in (126, 127):
                    res.notes.append("The command could not be started inside the image (not found / not executable).")
                elif code == 137:
                    res.notes.append("Process was killed (exit 137) - commonly the memory limit was exceeded.")
            if res.output_truncated:
                res.notes.append(f"Output truncated to {plan.limits.max_output_bytes} bytes per stream.")
        except Exception as exc:  # noqa: BLE001 - the host application must never crash because a preview failed
            res.status = "error"
            res.notes.append(f"Unexpected error: {type(exc).__name__}: {exc}")
        finally:
            stop_aux.set()
            BUS.unregister_cancel(self.run_id)
            if self.proc and self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            if exe and self.name:
                _cli([exe, "rm", "-f", self.name], 20)
            if scratch is not None:
                res.workspace_cleaned, kept_note = _storage.finish_scratch(scratch)
                if kept_note:
                    res.notes.append(kept_note)
            res.duration_s = round(time.monotonic() - t0, 3)
            res.finished = datetime.now(timezone.utc).isoformat()
            self._diagnostics(res)
            self.state = "ended"
            self._pv("pv_state", state="ended", status=res.status)
            try:
                if res.status in ("refused", "skipped", "error"):      # say WHY in the console, not only the status word
                    for n in res.notes:
                        self._bus("err", text=f"{n}\n")
                self._bus("end", status=res.status, exit_code=res.exit_code, notes=list(res.notes), duration_s=res.duration_s)
            except Exception:  # noqa: BLE001
                pass
            if options.log_dir:
                try:
                    res.write_json(Path(options.log_dir) / f"preview_{self.run_id}.json")
                except OSError:
                    pass
            if self.on_result:
                try:
                    self.on_result(res)
                except Exception:  # noqa: BLE001
                    pass

    def _diagnostics(self, res: ExecutionResult) -> None:
        try:
            from run_insights import build_trace
            tr = build_trace(res)
            self._pv("pv_diag", summary=tr.summary, diagnostics=[d.to_dict() for d in tr.diagnostics], hints=tr.hints)
        except Exception:  # noqa: BLE001 - insights are a bonus, never a failure
            pass

    # ------------------------------------------------------------------ helpers running next to the container
    CAPTURE = (["import", "-silent", "-window", "root", "png:-"], ["import-im7.q16", "-silent", "-window", "root", "png:-"],
               ["import-im6.q16", "-silent", "-window", "root", "png:-"], ["magick", "import", "-silent", "-window", "root", "png:-"])
    CAPTURE_TIMEOUT_S = 6
    DIAG_AFTER_S = 6.0
    DIAG_EVERY_S = 60.0

    def _capture(self, state: Dict) -> tuple:
        """One screen snapshot. Tries the known ImageMagick command names; remembers the one that works. (rc, png bytes, error text)."""
        order = [state["cmd"]] if state.get("cmd") is not None else list(range(len(self.CAPTURE)))
        rc, data, err = None, b"", b""
        for i in order:
            rc, data, err = _cli_bytes(exec_command(self.exe, self.name, self.CAPTURE[i]), self.CAPTURE_TIMEOUT_S)
            if rc == 0 and data[:4] == b"\x89PNG":
                state["cmd"] = i
                return rc, data, err
            text = err.decode("utf-8", "replace").lower()
            if rc is None or not any(k in text for k in ("not found", "no such file", "executable file", "not installed")):
                break                                                    # a real failure of the tool: trying other names will not help
        return rc, data, err

    def _screen_diagnostics(self, why: str) -> None:
        """Tell the user, in the console, why nothing is on the screen yet (runs one small script inside the container)."""
        self._pv("pv_note", info={"k": "note", "text": f"No screen snapshot yet ({why}). Checking inside the container..."})
        rc, out, err = _cli_bytes(exec_command(self.exe, self.name, ["python", f"{HELPER}/ase_diag.py"]), 12)
        text = out.decode("utf-8", "replace").strip() or err.decode("utf-8", "replace").strip() or f"the diagnostic could not run (rc={rc})"
        self._pv("pv_note", info={"k": "note", "text": "Screen check:\n" + "\n".join("    " + ln for ln in text.splitlines()[:40])})

    # ------------------------------------------------------------------ the screen channel (one process, pictures out / input in)
    CHANNEL_FIRST_FRAME_S = 10.0

    def _channel_reader(self, p: subprocess.Popen, got: threading.Event) -> None:
        out = p.stdout
        try:
            while True:
                head = out.readline()
                if not head:
                    return
                if head.startswith(b"ASEF "):
                    n = int(head[5:].strip() or 0)
                    if not 0 < n < 8_000_000:
                        return
                    data = out.read(n)
                    if len(data) != n:
                        return
                    w, hh = png_size(data)
                    self._chan_ok = True
                    got.set()
                    self._pv("pv_frame", png=data, w=w, h=hh)
                elif head.startswith(b"ASEI "):
                    try:
                        d = json.loads(head[5:].decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if d.get("k") == "channel":
                        self.desktop = str(d.get("wm") or "none")
                        tools = d.get("tools") or {}
                        self._pv("pv_note", info={"k": "desktop", "wm": self.desktop, "tools": tools})
                        if self.desktop == "none":
                            self._pv("pv_note", info={"k": "note", "text": "This preview image has no desktop (window manager): windows sit at the top-left "
                                                      "and cannot be moved. Press 'Prepare GUI preview image' again to add one."})
                        if not tools.get("xdotool"):
                            self._pv("pv_note", info={"k": "note", "text": "xdotool is missing in this image, so clicks and keys cannot be sent. Rebuild the GUI preview image."})
                    elif d.get("k") == "error":
                        self._pv("pv_note", info={"k": "note", "text": "Screen capture: " + str(d.get("text") or "")[:200]})
        except (OSError, ValueError):
            return

    def _screen_channel(self, stop: threading.Event) -> bool:
        """Start the in-container helper and relay its pictures. True = ended because the run ended; False = unusable (use the old path).
        The container may not be up for the first moments: a start that ends at once is simply tried again until the first picture is due."""
        kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        t0 = time.monotonic()
        ever = False
        while not stop.is_set():
            cmd = [self.exe, "exec", "-i", "-e", f"DISPLAY={DISPLAY_NUM}", self.name, "python", f"{HELPER}/ase_screen.py", f"{float(self.options.fps):g}"]
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env(), **kw)
            except OSError:
                return False
            self._chan = p
            got = threading.Event()
            threading.Thread(target=self._channel_reader, args=(p, got), daemon=True).start()
            fps = float(self.options.fps)
            try:
                while not stop.wait(0.2):
                    if p.poll() is not None:
                        break
                    if got.is_set():
                        ever = True
                    elif time.monotonic() - t0 > self.CHANNEL_FIRST_FRAME_S:
                        return False
                    if got.is_set() and float(self.options.fps) != fps:
                        fps = float(self.options.fps)
                        self._chan_send({"c": "fps", "v": fps})
                else:
                    return True                                  # the run ended
            finally:
                self._chan_ok = False
                self._chan = None
                try:
                    if p.stdin:
                        p.stdin.close()
                except OSError:
                    pass
                try:
                    p.terminate()
                except OSError:
                    pass
            if ever or time.monotonic() - t0 > self.CHANNEL_FIRST_FRAME_S:
                return False                                     # it worked once and then died: the older path takes over
            stop.wait(0.3)
        return True

    def _screen_main(self, stop: threading.Event) -> None:
        """Pictures + input for the virtual screen: the fast channel first; if it cannot run, the older exec-per-picture path."""
        ok = False
        try:
            ok = self._screen_channel(stop)
        except Exception:  # noqa: BLE001 - never disturb the run
            ok = False
        if not ok and not stop.is_set():
            self._pv("pv_note", info={"k": "note", "text": "The fast screen channel is not available in this image: using the slower snapshot path."})
            self._frames(stop)

    def _frames(self, stop: threading.Event) -> None:
        """Snapshots of the virtual screen (``<runtime> exec ... import -window root png:-``) plus synthetic input.
        Polls fast until the first frame; when capturing keeps failing it backs off and reports the reason (and a container check)."""
        last_hash, state = None, {"cmd": None}
        t_begin, fails, last_diag, last_err = time.monotonic(), 0, None, ""
        while not stop.is_set():
            t = time.monotonic()
            with self._inputs_lock:
                todo, self._inputs = self._inputs, []
            for _kind, argv in todo:
                _cli_bytes(exec_command(self.exe, self.name, argv), 10)
            rc, data, err = self._capture(state)
            if rc == 0 and data[:4] == b"\x89PNG":
                fails, last_diag = 0, None
                h = hash(data)
                if h != last_hash and len(data) < 8_000_000:
                    last_hash = h
                    w, hh = png_size(data)
                    self._pv("pv_frame", png=data, w=w, h=hh)
            else:
                fails += 1
                last_err = ("timed out" if rc is None else f"exit {rc}") + (": " + err.decode("utf-8", "replace").strip()[:160] if err else "")
                waited = time.monotonic() - t_begin
                if last_hash is None and waited >= self.DIAG_AFTER_S and (last_diag is None or time.monotonic() - last_diag >= self.DIAG_EVERY_S):
                    last_diag = time.monotonic()
                    self._screen_diagnostics(last_err)
            if last_hash is None and fails == 0:
                wait = 0.15                                          # first frame: poll fast so the window shows the moment it is drawn
            elif fails:
                wait = min(3.0, 0.3 * (2 ** min(fails, 4)))          # failing: back off, do not hammer the runtime
            else:
                interval = 1.0 / max(0.2, min(10.0, float(self.options.fps)))      # read every round: the Rate box takes effect at once
                wait = max(0.05, interval - (time.monotonic() - t))
            stop.wait(wait)

    def _web(self, stop: threading.Event) -> None:
        """Poll the published port on 127.0.0.1 and publish status, headers and the page text for the Web tab."""
        up = False
        while not stop.wait(1.0 if not up else max(1.0, float(self.options.web_refresh_s))):
            status, headers, body = 0, {}, b""
            try:
                with _NO_PROXY.open(self.web_url, timeout=2.5) as r:
                    status, headers, body = r.status, dict(r.headers), r.read(200_000)
            except urllib.error.HTTPError as e:
                status, headers = e.code, dict(e.headers or {})
                try:
                    body = e.read(200_000)
                except OSError:
                    pass
            except (OSError, ValueError):
                if up:
                    up = False
                    self._pv("pv_web", url=self.web_url, up=False, status=0, headers={}, body="")
                continue
            up = True
            self._pv("pv_web", url=self.web_url, up=True, status=status, headers=headers, body=body.decode("utf-8", "replace"))
