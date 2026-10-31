#!/usr/bin/env python3
"""In-container helper for the App Preview panel: traces or debugs a Python program. Python 3.8+, standard library only.

    python ase_tracer.py --mode out|trace|debug --root /work [--break path:line ...] [--max-events N] -- script.py [args...]

Modes: ``out`` only reports which code printed what (no tracing overhead), ``trace`` also records calls/lines/returns,
``debug`` stops at breakpoints and steps under the control of the panel.

Protocol: every structured event is ONE line on stderr: ``\\x1eASE\\x1e{json}``. The program's own stdout/stderr still reach the
real streams unchanged. In debug mode, commands are read from stdin as one JSON object per line (the program itself gets an
empty stdin) and answered with events:

    {"cmd":"continue"|"step"|"next"|"return"|"quit"}            resume
    {"cmd":"set_break","file":"a.py","line":12}  {"cmd":"clear_break",...}
    {"cmd":"eval","expr":"x + 1"}                               -> {"k":"eval",...}
    {"cmd":"vars","frame":1}                                    -> {"k":"vars",...}

Events (``k``): hello, call, line, ret, exc, out, stop, vars, eval, end. Positions are paths relative to --root.
Only files under --root are traced; the standard library and installed packages are skipped for speed.
"""
import bdb
import json
import os
import runpy
import sys
import threading
import time
import traceback

MARK = "\x1eASE\x1e"
REPR_MAX = 200
_lock = threading.Lock()
_real_err = sys.__stderr__
_real_out = sys.__stdout__


def emit(ev):
    line = MARK + json.dumps(ev, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    with _lock:
        try:
            _real_err.write(line)
            _real_err.flush()
        except (OSError, ValueError):
            pass


def short(v, n=REPR_MAX):
    try:
        s = repr(v)
    except Exception as exc:  # noqa: BLE001 - a broken __repr__ must not break the program being inspected
        s = "<repr failed: %s>" % type(exc).__name__
    return s if len(s) <= n else s[:n - 3] + "..."


class Ctx:
    root = os.getcwd()
    batch = None          # set by main(): output events flush pending trace events first so the order is preserved

    @classmethod
    def rel(cls, path):
        if not path or path.startswith("<"):          # <frozen runpy>, <string>, <stdin>: never project code
            return None
        try:
            p = os.path.abspath(path)
            if "site-packages" in p or "dist-packages" in p:
                return None
            if p == cls.root or p.startswith(cls.root + os.sep):
                return os.path.relpath(p, cls.root).replace(os.sep, "/")
        except (ValueError, OSError):
            pass
        return None


class TeeOut:
    """Wraps stdout/stderr: passes text through and reports it together with the code position that produced it."""

    def __init__(self, real, stream):
        self._real, self._stream = real, stream

    def write(self, s):
        n = self._real.write(s)
        if s:
            if Ctx.batch is not None:
                Ctx.batch.flush()
            f, l, fn = current_pos()
            emit({"k": "out", "s": self._stream, "t": s[-4000:], "f": f, "l": l, "fn": fn, "ts": round(time.time(), 3)})
        return n

    def __getattr__(self, name):
        return getattr(self._real, name)


def current_pos():
    """Position (relpath, line, function) of the innermost project frame - the code that is producing output right now."""
    f = sys._getframe(2)
    while f is not None:
        rel = Ctx.rel(f.f_code.co_filename)
        if rel is not None:
            return rel, f.f_lineno, f.f_code.co_name
        f = f.f_back
    return "", 0, ""


def frame_info(frame):
    f = Ctx.rel(frame.f_code.co_filename)
    return {"f": f, "l": frame.f_lineno, "fn": frame.f_code.co_name}


class Batch:
    """Collects high-rate trace events and sends them in batches so tracing does not flood the pipe."""

    def __init__(self, cap):
        self.items, self.cap, self.total, self.last = [], cap, 0, time.time()
        self.dropped = 0

    def add(self, ev):
        if self.total >= self.cap:
            self.dropped += 1
            return
        self.total += 1
        self.items.append(ev)
        if len(self.items) >= 200 or time.time() - self.last > 0.1:
            self.flush()

    def flush(self):
        if self.items:
            emit({"k": "batch", "ev": self.items})
            self.items = []
        self.last = time.time()


class Tracer(bdb.Bdb):
    """One class for both modes. In trace mode it never stops; in debug mode it stops at breakpoints and steps."""

    def __init__(self, mode, cap, breaks):
        bdb.Bdb.__init__(self)
        self.mode, self.batch, self.depth = mode, Batch(cap), 0
        self.wait = mode == "debug"
        self.pending_break = list(breaks)
        self._last_line = None
        self.main_file = None

    # ---- filtering: only project files
    def is_project(self, frame):
        return Ctx.rel(frame.f_code.co_filename) is not None

    def stop_here(self, frame):
        if self.mode == "trace":
            return False
        return bdb.Bdb.stop_here(self, frame) and self.is_project(frame)

    # ---- trace mode events
    def trace_dispatch(self, frame, event, arg):
        if self.mode == "trace":
            return self._trace(frame, event, arg)
        return bdb.Bdb.trace_dispatch(self, frame, event, arg)

    def _trace(self, frame, event, arg):
        if not self.is_project(frame):
            return None if event == "call" else self._trace
        info = frame_info(frame)
        if event == "call":
            self.depth += 1
            a = {}
            try:
                names = frame.f_code.co_varnames[:frame.f_code.co_argcount]
                a = {n: short(frame.f_locals.get(n), 60) for n in names[:6]}
            except Exception:  # noqa: BLE001
                pass
            self.batch.add(dict(info, k="call", args=a, d=self.depth))
        elif event == "line":
            self.batch.add(dict(info, k="line"))
        elif event == "return":
            self.batch.add(dict(info, k="ret", v=short(arg, 80), d=self.depth))
            self.depth = max(0, self.depth - 1)
        elif event == "exception":
            self.batch.add(dict(info, k="exc", e=short(arg[1], 160), et=getattr(arg[0], "__name__", "")))
        return self._trace

    # ---- debug mode
    def user_line(self, frame):
        if not self.is_project(frame):
            self.set_step()
            return
        self._stop(frame, "breakpoint" if self.break_here(frame) else "step")

    def user_return(self, frame, value):
        if self.is_project(frame) and self.wait and frame.f_code.co_name != "<module>":
            self._stop(frame, "return", value=short(value, 80))

    def user_exception(self, frame, exc_info):
        if self.is_project(frame) and self.wait:
            self._stop(frame, "exception", value="%s: %s" % (exc_info[0].__name__, short(exc_info[1], 160)))

    def stack(self, frame):
        out, f = [], frame
        while f is not None:
            if self.is_project(f):
                out.append(frame_info(f))
            f = f.f_back
        return out

    def variables(self, frame):
        loc = {k: short(v) for k, v in list(frame.f_locals.items())[:80] if not k.startswith("__")}
        glo = {k: short(v) for k, v in list(frame.f_globals.items())[:80]
               if not k.startswith("__") and not callable(v) and type(v).__name__ != "module"}
        return {"locals": loc, "globals": glo}

    def _stop(self, frame, why, value=""):
        self.batch.flush()
        emit(dict(frame_info(frame), k="stop", why=why, value=value, stack=self.stack(frame), vars=self.variables(frame)))
        while True:
            cmd = read_command()
            if cmd is None or cmd.get("cmd") == "quit":
                emit({"k": "end", "why": "debugger quit"})
                for st in (sys.__stdout__, sys.__stderr__):
                    try:
                        st.flush()
                    except (OSError, ValueError):
                        pass
                os._exit(130)
            c = cmd.get("cmd")
            if c == "continue":
                self.set_continue()
                return
            if c == "step":
                self.set_step()
                return
            if c == "next":
                self.set_next(frame)
                return
            if c == "return":
                self.set_return(frame)
                return
            if c == "set_break":
                self.add_break(cmd.get("file", ""), int(cmd.get("line", 0)))
            elif c == "clear_break":
                self.clear_break(self.abs(cmd.get("file", "")), int(cmd.get("line", 0)))
            elif c == "vars":
                n = max(0, int(cmd.get("frame", 0)))
                fl, f = [], frame
                while f is not None:
                    if self.is_project(f):
                        fl.append(f)
                    f = f.f_back
                fr = fl[n] if n < len(fl) else frame
                emit(dict(frame_info(fr), k="vars", vars=self.variables(fr)))
            elif c == "eval":
                try:
                    res = short(eval(compile(str(cmd.get("expr", "")), "<debug>", "eval"), frame.f_globals, frame.f_locals), 2000)
                    emit({"k": "eval", "expr": cmd.get("expr"), "v": res, "ok": True})
                except Exception as exc:  # noqa: BLE001 - user expressions may fail in any way
                    emit({"k": "eval", "expr": cmd.get("expr"), "v": "%s: %s" % (type(exc).__name__, exc), "ok": False})

    def abs(self, rel):
        return os.path.abspath(os.path.join(Ctx.root, rel))

    def add_break(self, rel, line):
        err = self.set_break(self.abs(rel), line)
        emit({"k": "break", "f": rel, "l": line, "ok": err is None, "err": err or ""})


_stdin = None


def read_command():
    try:
        line = _stdin.readline()
    except (OSError, ValueError):
        return None
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return {}


def main(argv):
    global _stdin
    mode, root, cap, breaks, i = "trace", os.getcwd(), 200000, [], 0
    while i < len(argv) and argv[i] != "--":
        a = argv[i]
        if a == "--mode":
            mode = argv[i + 1]
            i += 1
        elif a == "--root":
            root = argv[i + 1]
            i += 1
        elif a == "--max-events":
            cap = int(argv[i + 1])
            i += 1
        elif a == "--break":
            breaks.append(argv[i + 1])
            i += 1
        i += 1
    target = argv[i + 1:]
    if not target or mode not in ("out", "trace", "debug"):
        sys.stderr.write("usage: ase_tracer.py --mode out|trace|debug [--root DIR] [--break file:line] -- script.py [args]\n")
        return 2
    Ctx.root = os.path.abspath(root)
    os.chdir(Ctx.root)
    as_module = target[0] == "-m" and len(target) > 1
    script = os.path.abspath(target[1] if as_module else target[0])
    sys.argv = target[1:] if as_module else target
    sys.path.insert(0, Ctx.root if as_module else os.path.dirname(script))
    tr = Tracer(mode, cap, breaks)
    Ctx.batch = tr.batch
    emit({"k": "hello", "mode": mode, "py": sys.version.split()[0], "script": ("-m " + target[1]) if as_module else (Ctx.rel(script) or target[0])})
    if mode == "debug":
        import io
        _stdin = sys.stdin
        sys.stdin = io.StringIO("")                    # the program gets an empty stdin; the control channel is separate
        for b in breaks:
            f, _, l = b.rpartition(":")
            try:
                tr.add_break(f, int(l))
            except ValueError:
                pass
    sys.stdout = TeeOut(sys.stdout, "stdout")
    sys.stderr = TeeOut(sys.stderr, "stderr")
    code = 0
    try:
        if mode == "debug":
            tr.run(_runner_code(as_module, target))
        elif mode == "out":
            _run_plain(script, as_module, target)
        else:
            _run_traced(tr, script, as_module, target)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:  # noqa: BLE001 - report every uncaught error as a structured event
        tb = traceback.extract_tb(exc.__traceback__)
        frames = [{"f": Ctx.rel(t.filename), "l": t.lineno, "fn": t.name, "code": t.line or ""} for t in tb if Ctx.rel(t.filename)]
        tr.batch.flush()
        emit({"k": "exc_final", "et": type(exc).__name__, "e": short(exc, 400), "frames": frames})
        traceback.print_exc()
        code = 1
    finally:
        tr.batch.flush()
        if tr.batch.dropped:
            emit({"k": "note", "text": "trace event limit reached; %d later events were not recorded" % tr.batch.dropped})
        emit({"k": "end", "code": code})
    return code


def _runner_code(as_module, target):
    if as_module:
        return "__import__('runpy').run_module(%r, run_name='__main__', alter_sys=True)" % target[1]
    return "__import__('runpy').run_path(%r, run_name='__main__')" % os.path.abspath(target[0])


def _run_plain(script, as_module, target):
    if as_module:
        runpy.run_module(target[1], run_name="__main__", alter_sys=True)
    else:
        runpy.run_path(script, run_name="__main__")


def _run_traced(tr, script, as_module, target):
    sys.settrace(tr.trace_dispatch)
    threading.settrace(tr.trace_dispatch)
    try:
        if as_module:
            runpy.run_module(target[1], run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(script, run_name="__main__")
    finally:
        sys.settrace(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
