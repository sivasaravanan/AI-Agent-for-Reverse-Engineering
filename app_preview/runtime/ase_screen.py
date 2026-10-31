"""Runs INSIDE the preview container, started once by the App Preview panel (``<runtime> exec -i ... python /ase/ase_screen.py FPS``).

One long-lived process instead of one ``docker exec`` per picture and per click:
  * it prints the virtual screen as a PNG whenever it changed:   ``ASEF <length>\\n<png bytes>``
  * it prints one information line at the start:                 ``ASEI <json>\\n``
  * it reads mouse / keyboard events from stdin, one JSON object per line, and applies them with xdotool at once:
        {"c":"move","x":1,"y":2} {"c":"down","b":1} {"c":"up","b":1} {"c":"click","x":1,"y":2,"b":1} {"c":"scroll","d":"up","n":1}
        {"c":"key","k":"ctrl+c"} {"c":"keydown","k":"a"} {"c":"keyup","k":"a"} {"c":"type","t":"text"} {"c":"fps","v":4} {"c":"snap"}
Only validated values reach xdotool (numbers, a key-name pattern, text without control characters); no shell is involved.
Standard library only.
"""
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

ENV = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
OUT = sys.stdout.buffer
KEY = re.compile(r"^[A-Za-z0-9_+]{1,40}$")
CAPTURE = (["import", "-silent", "-window", "root", "png:-"], ["import-im7.q16", "-silent", "-window", "root", "png:-"],
           ["import-im6.q16", "-silent", "-window", "root", "png:-"], ["magick", "import", "-silent", "-window", "root", "png:-"])
BURST_S = 1.5            # after an input event: look at the screen often for a moment so the result shows quickly
BURST_FPS = 8.0


def info(**kw):
    OUT.write(b"ASEI " + json.dumps(kw).encode() + b"\n")
    OUT.flush()


def run(argv, timeout=6):
    try:
        p = subprocess.run(argv, env=ENV, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, b"", str(exc).encode()


class Capture:
    def __init__(self):
        self.i = None
        self.err = ""

    def grab(self):
        order = [self.i] if self.i is not None else range(len(CAPTURE))
        for i in order:
            if not shutil.which(CAPTURE[i][0]):
                continue
            rc, out, err = run(CAPTURE[i])
            if rc == 0 and out[:4] == b"\x89PNG":
                self.i = i
                return out
            self.err = (err.decode("utf-8", "replace").strip() or f"exit {rc}")[:160]
        return None


def num(v, lo, hi):
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError
    return v


def command(c):
    """One validated event -> xdotool arguments (a list), or None."""
    k = c.get("c")
    if k == "move":
        return ["mousemove", str(num(c["x"], 0, 10000)), str(num(c["y"], 0, 10000))]
    if k == "click":
        return ["mousemove", str(num(c["x"], 0, 10000)), str(num(c["y"], 0, 10000)), "click", str(num(c.get("b", 1), 1, 3))]
    if k in ("down", "up"):
        pre = ["mousemove", str(num(c["x"], 0, 10000)), str(num(c["y"], 0, 10000))] if "x" in c and "y" in c else []
        return pre + ["mousedown" if k == "down" else "mouseup", str(num(c.get("b", 1), 1, 3))]
    if k == "scroll":
        n = num(c.get("n", 1), 1, 10)
        return ["click", "--repeat", str(n), "4" if c.get("d") == "up" else "5"]
    if k in ("key", "keydown", "keyup"):
        key = str(c.get("k", ""))
        if not KEY.match(key):
            return None
        return [{"key": "key", "keydown": "keydown", "keyup": "keyup"}[k], "--", key]
    if k == "type":
        t = str(c.get("t", ""))[:200]
        if not t or "\x00" in t:
            return None
        return ["type", "--delay", "10", "--", t]
    return None


def main():
    fps = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    fps = max(0.2, min(10.0, fps))
    tools = {t: bool(shutil.which(t)) for t in ("xdotool", "import", "openbox")}
    wm = ""
    for _ in range(80):                                    # the entry script writes the file once the screen and the desktop are up
        try:
            wm = open("/tmp/ase_desktop").read().strip()
        except OSError:
            wm = ""
        if wm:
            break
        time.sleep(0.1)
    wm = wm or "none"
    info(k="channel", wm=wm, tools=tools, fps=fps)
    cap = Capture()
    last = None
    buf = b""
    burst_until = 0.0
    nxt = 0.0
    last_err = ""
    fd = sys.stdin.fileno()
    while True:
        now = time.monotonic()
        wait = max(0.0, nxt - now)
        r, _, _ = select.select([fd], [], [], wait)
        todo = []
        if r:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    todo.append(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    pass
        force = False
        if todo:
            chain, moves = [], None
            for c in todo:
                if not isinstance(c, dict):
                    continue
                if c.get("c") == "fps":
                    try:
                        fps = max(0.2, min(10.0, float(c.get("v"))))
                    except (TypeError, ValueError):
                        pass
                    continue
                if c.get("c") == "snap":
                    force = True
                    continue
                try:
                    a = command(c)
                except (KeyError, TypeError, ValueError):
                    a = None
                if not a:
                    continue
                if c.get("c") == "move":                       # a burst of moves: only the last one matters
                    if chain and chain[-1][0] == "mousemove" and len(chain[-1]) == 3:
                        chain.pop()
                chain.append(a)
            if chain:
                argv = ["xdotool"]
                for a in chain:
                    argv += a
                run(argv, 10)
                burst_until = time.monotonic() + BURST_S
                nxt = time.monotonic() + 0.03
        now = time.monotonic()
        if now >= nxt or force:
            png = cap.grab()
            if png:
                h = hashlib.md5(png).digest()
                if h != last and len(png) < 8_000_000:
                    last = h
                    OUT.write(b"ASEF %d\n" % len(png) + png)
                    OUT.flush()
            elif cap.err != last_err:
                last_err = cap.err
                info(k="error", text=cap.err)
            rate = BURST_FPS if time.monotonic() < burst_until else fps
            nxt = time.monotonic() + 1.0 / max(0.2, rate)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
