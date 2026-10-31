"""Auto-tuning of limits/command from requirements.txt (pure functions; no Docker, no network)."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.build_execution import Limits, apply_autotune, build_plan, custom_candidate, retry_limits  # noqa: E402
from plugins.build_execution.autotune import parse_requirements, profile_text  # noqa: E402

CMD = ["sh", "-c", "pip install --no-input --target /tmp/deps -r requirements.txt && PYTHONPATH=/tmp/deps python ./main.py"]


def _zip(tmp_path, req):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("main.py", "print(1)")
        if req is not None:
            f.writestr("requirements.txt", req)
    return str(z)


def test_parse_requirements_ignores_versions_extras_comments_options():
    txt = "# c\nRequests[security]>=2.0 ; python_version>'3'\n-r other.txt\nPillow==10.0  # img\ngit+https://x/y.git\n\nopencv_python\n"
    assert parse_requirements(txt) == ["requests", "pillow", "opencv-python"]


def test_heavy_project_gets_disk_install_cpu_torch_and_bigger_limits(tmp_path):
    z = _zip(tmp_path, "easyocr\nopencv-python\nreportlab\nopenpyxl\n")
    cand = custom_candidate(CMD, "python")
    c2, lim, notes = apply_autotune(cand, Limits(), z)
    assert lim.memory_mb == 3072 and lim.timeout_s > 300 and lim.timeout_s % 60 == 0 and lim.network
    script = c2.argv[2]
    assert "/tmp/deps" not in script and script.startswith("mkdir -p /work/.deps /work/.tmp && ")
    assert "--no-cache-dir" in script and "PYTHONPATH=/work/.deps" in script
    assert c2.env["PIP_EXTRA_INDEX_URL"].endswith("/whl/cpu") and c2.env["TMPDIR"] == "/work/.tmp"
    assert cand.argv == CMD and "TMPDIR" not in cand.env          # the input candidate is not modified
    assert build_plan(z, c2, limits=lim).limits.memory_mb == 3072


def test_small_project_keeps_command_and_never_lowers_limits(tmp_path):
    z = _zip(tmp_path, "requests\nflask\n")
    c2, lim, _ = apply_autotune(custom_candidate(CMD, "python"), Limits(memory_mb=2048, timeout_s=900), z)
    assert c2.argv == CMD and lim.memory_mb == 2048 and lim.timeout_s == 900


def test_non_pip_command_or_no_requirements_untouched(tmp_path):
    z = _zip(tmp_path, None)
    cand = custom_candidate(CMD, "python")
    assert apply_autotune(cand, Limits(), z)[0] is cand
    z2 = _zip(tmp_path, "torch\n")
    other = custom_candidate(["python", "main.py"], "python")
    assert apply_autotune(other, Limits(), z2)[0] is other


def test_profile_sizes_are_sane():
    p = profile_text("tensorflow\ndeepface\n")
    assert p.memory_mb == 3072 and p.to_disk and "tensorflow" in p.heavy
    assert profile_text("") is None


def test_retry_limits_double_once_and_stop_at_caps():
    assert retry_limits("timeout", None, Limits(timeout_s=600)).timeout_s == 1200
    assert retry_limits("failed", 137, Limits(memory_mb=2048)).memory_mb == 4096
    assert retry_limits("failed", 1, Limits()) is None
    assert retry_limits("timeout", None, Limits(timeout_s=7200)) is None


def test_app_preview_install_is_tuned_and_options_can_disable(tmp_path):
    from app_preview.models import PreviewOptions
    from app_preview.planner import make_plan
    from app_preview.discovery import discover_entries
    z = _zip(tmp_path, "easyocr\nopencv-python\n")
    entries = discover_entries(z)
    entries = entries[0] if isinstance(entries, tuple) else entries
    entry = next(e for e in entries if e.install)
    plan = make_plan(z, entry, "run", PreviewOptions(), install=True)
    assert plan.limits.memory_mb >= 3072 and plan.limits.timeout_s >= 900
    assert "/tmp/ase-site" not in plan.argv[2] and "/work/.ase-site" in plan.argv[2]
    assert plan.env["PYTHONPATH"] == "/work/.ase-site" and plan.env["PIP_EXTRA_INDEX_URL"].endswith("/whl/cpu")
    off = make_plan(z, entry, "run", PreviewOptions(autotune=False), install=True)
    assert "/tmp/ase-site" in off.argv[2] and off.limits.memory_mb == 1536
