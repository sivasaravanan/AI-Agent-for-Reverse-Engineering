"""The disk-backed 'pip-pytest-heavy' command: raises limits, keeps packages off the RAM-backed /tmp, leaves defaults alone."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.build_execution import build_plan, discover_candidates  # noqa: E402
from run_insights.hints import hints_for  # noqa: E402


def _zip(tmp_path, req):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("tests/test_a.py", "x")
        f.writestr("requirements.txt", req)
    return str(z)


def test_heavy_candidate_uses_disk_and_raises_limits(tmp_path):
    z = _zip(tmp_path, "torch\nultralytics\n")
    cands, _ = discover_candidates(z)
    heavy = next(c for c in cands if c.id == "python:pip-pytest-heavy")
    cmd = " ".join(heavy.argv)
    assert "/work/.deps" in cmd and "--no-cache-dir" in cmd and "/tmp/deps" not in cmd
    assert heavy.needs_network and heavy.env["TMPDIR"] == "/work/.tmp"
    assert heavy.env["PIP_EXTRA_INDEX_URL"].endswith("/whl/cpu")
    plan = build_plan(z, heavy)
    assert plan.limits.memory_mb == 3072 and plan.limits.timeout_s == 1800


def test_default_pip_candidate_and_limits_unchanged(tmp_path):
    z = _zip(tmp_path, "requests\n")
    cands, _ = discover_candidates(z)
    base = next(c for c in cands if c.id == "python:pip-pytest")
    assert "/tmp/deps" in " ".join(base.argv)
    assert build_plan(z, base).limits.memory_mb == 1024
    heavy = next(c for c in cands if c.id == "python:pip-pytest-heavy")
    assert "PIP_EXTRA_INDEX_URL" not in heavy.env


def test_hint_for_killed_pip():
    assert any("pip-pytest-heavy" in h["text"] for h in hints_for("failed", 137, "Downloading torch\nKilled"))
