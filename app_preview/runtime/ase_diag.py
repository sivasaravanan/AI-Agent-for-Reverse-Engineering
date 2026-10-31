"""Run INSIDE the preview container by the App Preview panel when no screen snapshot has arrived yet.
Prints a short, plain report: is the virtual screen up, which capture tools exist, which programs run, how many windows exist."""
import glob
import os
import shutil
import subprocess

ENV = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
print("DISPLAY=" + ENV["DISPLAY"])
print("X sockets: " + (", ".join(os.listdir("/tmp/.X11-unix")) if os.path.isdir("/tmp/.X11-unix") else "none (/tmp/.X11-unix missing)"))
tools = ["Xvfb", "import", "import-im7.q16", "import-im6.q16", "magick", "xdotool"]
print("tools: " + ", ".join(f"{t}={'yes' if shutil.which(t) else 'MISSING'}" for t in tools))
print("programs:")
for d in sorted(glob.glob("/proc/[0-9]*"), key=lambda p: int(p.rsplit("/", 1)[1])):
    try:
        cmd = open(d + "/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        st = open(d + "/stat").read().rsplit(")", 1)[1].split()
        ticks = int(st[11]) + int(st[12])
        print(f"  pid {d.rsplit('/', 1)[1]:>4}  cpu-ticks {ticks:>7}  {cmd[:110]}")
    except (OSError, ValueError, IndexError):
        pass


def run(argv, t=4):
    try:
        p = subprocess.run(argv, env=ENV, capture_output=True, timeout=t)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "timeout", b"", b""
    except OSError as exc:
        return "error", b"", str(exc).encode()


names = set()
HAVE_XDOTOOL = bool(shutil.which("xdotool"))
for flag in ("--name", "--class"):
    rc, out, _ = run(["xdotool", "search", "--onlyvisible", flag, "."])
    if rc == 0:
        names |= set(out.split())
print("visible windows: " + (f"{len(names)}" + ("  (the program has not opened a window yet)" if not names else "") if HAVE_XDOTOOL
                              else "unknown (xdotool is not in this image - rebuild the GUI preview image)"))
for cmd in (["import", "-silent", "-window", "root", "png:-"], ["import-im7.q16", "-silent", "-window", "root", "png:-"],
            ["import-im6.q16", "-silent", "-window", "root", "png:-"], ["magick", "import", "-silent", "-window", "root", "png:-"]):
    if not shutil.which(cmd[0]):
        continue
    rc, out, err = run(cmd)
    print(f"capture {cmd[0]}: rc={rc} bytes={len(out)} png={'yes' if out[:4] == b'\x89PNG' else 'no'} {err.decode('utf-8', 'replace').strip()[:160]}")
    if rc == 0 and out[:4] == b"\x89PNG":
        break
