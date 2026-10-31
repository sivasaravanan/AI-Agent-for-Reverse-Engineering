"""Build & Test publishes snapshots of the container's virtual screen as 'screen' events (no Docker: a fake runtime script)."""
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.build_execution.events import BUS  # noqa: E402
from plugins.build_execution.sandbox import _snapshot, _watch_screen  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


@pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell script as the fake runtime")
def test_snapshot_and_watcher_publish_png_frames(tmp_path):
    exe = tmp_path / "fakedocker"
    exe.write_bytes(b"#!/bin/sh\n[ \"$1\" = exec ] && printf '\\211PNG\\r\\n\\032\\nfake-image-bytes'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    assert _snapshot(str(exe), "c1") == PNG
    bad = tmp_path / "baddocker"
    bad.write_text("#!/bin/sh\necho not a png\n")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    assert _snapshot(str(bad), "c1") == b""
    got = []
    unsub = BUS.subscribe(lambda ev: got.append(ev) if ev["type"] == "screen" else None)
    stop = threading.Event()
    t = threading.Thread(target=_watch_screen, args=(str(exe), "c1", "r1", stop, 0.05), daemon=True)
    try:
        t.start()
        for _ in range(100):
            if got:
                break
            time.sleep(0.1)
    finally:
        stop.set()
        t.join(5)
        unsub()
    assert got and got[0]["png"] == PNG and got[0]["run"] == "r1"
    assert len(got) == 1                                   # identical consecutive frames are published once
