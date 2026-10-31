"""Shared fakes: a scriptable machine (Env), a config factory and a local HTTP server. Nothing here touches the real system."""
from __future__ import annotations

import gc
import hashlib
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from prerequisites import Env, RunResult, load_config


@pytest.fixture(autouse=True)
def _no_lingering_threads():
    yield
    end = time.time() + 8
    while time.time() < end:
        extra = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive() and not t.daemon]
        if not extra:
            break
        time.sleep(0.05)
    # daemon workers of UI tabs finish quickly with fakes; give them a moment so Tk objects are freed on the main thread
    end = time.time() + 5
    while time.time() < end and any(t.is_alive() for t in threading.enumerate() if t is not threading.main_thread()):
        time.sleep(0.05)
    gc.collect()


class FakeRun:
    """Scripted command runner. ``rules`` maps a substring of the command line to (rc, output). Records every call."""

    def __init__(self, rules=None, default=(0, "")):
        self.rules, self.default, self.calls = dict(rules or {}), default, []

    def lookup(self, cmd):
        line = " ".join(str(c) for c in cmd)
        for key, val in self.rules.items():
            if key in line:
                return val
        return self.default

    def quick(self, cmd, timeout=20):
        self.calls.append(("quick", list(cmd)))
        rc, out = self.lookup(cmd)
        return rc, out, ""

    def long(self, cmd, timeout=1800, on_line=None, cancel=None, env=None, cwd=None, interactive=False):
        self.calls.append(("long", list(cmd)))
        if cancel is not None and cancel.is_set():
            return RunResult(None, "", cancelled=True)
        rc, out = self.lookup(cmd)
        for ln in out.splitlines():
            if on_line:
                on_line(ln)
        return RunResult(rc, out)

    def commands(self, kind="long"):
        return [" ".join(map(str, c)) for k, c in self.calls if k == kind]


def make_env(platform="windows", arch="amd64", admin=False, tools=None, python="/usr/bin/python3", root=None, run=None,
             frozen=False, build=22631, interactive=False) -> Env:
    tools = dict(tools or {})
    run = run or FakeRun()
    env = Env(platform=platform, arch=arch, which=lambda exe: tools.get(exe), quick=run.quick, run_long=run.long, is_admin=admin,
              root=Path(root) if root else Path("."), frozen=frozen, python=python, interactive=interactive, windows_build=build)
    env._run = run            # type: ignore[attr-defined]
    env._tools = tools        # type: ignore[attr-defined]
    return env


def make_cfg(tmp_path: Path, **values):
    user = tmp_path / "user_prereq.json"
    user.write_text(json.dumps(values), encoding="utf-8")
    return load_config(user_path=user, shipped_path=tmp_path / "no_shipped.json", machine_path=tmp_path / "no_machine.json")


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


PAYLOAD = bytes(range(256)) * 4096          # 1 MiB, deterministic
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/file":
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
                body = PAYLOAD[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
            else:
                body = PAYLOAD
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://evil.invalid/payload.exe")
            self.end_headers()
        elif self.path == "/auth":
            self.send_response(407)
            self.end_headers()
        elif self.path == "/forbidden":
            self.send_response(403)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    do_HEAD = do_GET  # noqa: N815


@pytest.fixture
def http_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(5)
