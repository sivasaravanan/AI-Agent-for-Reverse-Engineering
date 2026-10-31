import gc
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _no_lingering_threads_and_deterministic_tk_cleanup():
    """Worker threads must be finished, and destroyed Tk objects freed on the main thread (freeing the Tk interpreter from
    another thread aborts the whole Python process)."""
    yield
    end = time.time() + 8
    while time.time() < end:
        extra = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
        if not extra:
            break
        time.sleep(0.05)
    gc.collect()
    leftover = [t.name for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
    assert not leftover, f"worker threads still running after the test: {leftover}"
