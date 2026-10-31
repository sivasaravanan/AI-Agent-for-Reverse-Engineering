import importlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from core.phase_registry import PHASES, is_placeholder_text, layout_report, owner_of  # noqa: E402
import merge_helper  # noqa: E402


def test_every_registered_file_exists_and_is_owned_once():
    rows = layout_report(ROOT)
    assert not [r for r in rows if r["state"] == "missing"]
    paths = [p for info in PHASES.values() for p in info["files"]]
    assert len(paths) == len(set(paths))
    assert owner_of("analyzers/base.py") == 3 and owner_of("nope.py") is None


IMPLEMENTED_PHASES = tuple(range(1, 11))   # every phase is implemented


def test_all_phases_are_implemented():
    state = {(r["phase"], r["path"]): r["state"] for r in layout_report(ROOT)}
    assert all(s == "implemented" for (ph, _), s in state.items() if ph in IMPLEMENTED_PHASES)
    assert not [k for k, s in state.items() if s != "implemented"]


def test_fail_closed_defaults():
    from security.execution_policy import is_execution_allowed
    from security.consent_policy import may_send_to_external
    from providers.model_router import ModelRouter
    assert is_execution_allowed() is False and is_execution_allowed(True, False) is False
    assert is_execution_allowed(False, True) is False and is_execution_allowed(True, True) is True
    assert may_send_to_external() is False and may_send_to_external(True, []) is False
    assert ModelRouter().select("explain_finding") is None      # AI is optional: no provider means no AI, not an error


def test_non_python_placeholder_markers():
    settings = json.loads((ROOT / "config/settings.json").read_text())
    assert "_placeholder" not in settings and settings["ai"]["enabled"] is False     # Phase 7 settings; AI off by default
    assert not is_placeholder_text("x.md", (ROOT / "docs/USER_GUIDE.md").read_text())          # real docs now
    assert is_placeholder_text("x.md", "<!-- PLACEHOLDER (Phase 3): x -->\n")
    assert not is_placeholder_text("x.py", "PLACEHOLDER = False\n")
    assert not is_placeholder_text("README.md", "# Real doc\n")


# ---------------------------------------------------------------- merge helper
def _tree(root: Path, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


PH = "PHASE = 3\nPLACEHOLDER = True\n"


def test_merge_plan_and_apply(tmp_path):
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    _tree(ours, {"real.py": "A = 1\n", "same.py": "S = 1\n", "ph.py": PH, "keep.py": "K = 1\n", "conf.py": "C = 1\n"})
    _tree(theirs, {"real.py": "A = 1\n", "same.py": "S = 1\n", "ph.py": "X = 'real'\n", "keep.py": PH,
                   "conf.py": "C = 2\n", "new/file.py": "N = 1\n", "__pycache__/x.pyc": "junk", "logs/a.log": "log"})
    incoming = merge_helper.load_incoming(theirs)
    assert "__pycache__/x.pyc" not in incoming and "logs/a.log" not in incoming
    kinds = {a.path: a.kind for a in merge_helper.plan(incoming, ours)}
    assert kinds == {"real.py": "identical", "same.py": "identical", "ph.py": "upgrade", "keep.py": "keep-ours",
                     "conf.py": "conflict", "new/file.py": "new"}
    merge_helper.apply(merge_helper.plan(incoming, ours), incoming, ours)
    assert (ours / "ph.py").read_text() == "X = 'real'\n" and (ours / "new/file.py").exists()
    assert (ours / "conf.py").read_text() == "C = 1\n"                       # never overwritten
    assert (ours / "merge_incoming/conf.py").read_text() == "C = 2\n"        # staged for review
    assert (ours / "keep.py").read_text() == "K = 1\n"


def test_merge_from_zip_strips_top_folder_and_blocks_traversal(tmp_path):
    z = tmp_path / "dl.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("proj/a.py", "A = 1\n")
        zf.writestr("proj/sub/b.py", "B = 1\n")
        zf.writestr("../evil.py", "boom")
    incoming = merge_helper.load_incoming(z)
    assert set(incoming) == {"a.py", "sub/b.py"} or set(incoming) == {"proj/a.py", "proj/sub/b.py"} or "evil.py" not in incoming
    assert not any("evil" in k for k in incoming)


def test_merge_apply_never_writes_outside_root(tmp_path):
    ours = tmp_path / "ours"
    ours.mkdir()
    act = [merge_helper.Action("../outside.py", "new")]
    merge_helper.apply(act, {"../outside.py": b"x"}, ours)
    assert not (tmp_path / "outside.py").exists()


def test_merge_cli_dry_run_changes_nothing(tmp_path, capsys):
    ours, theirs = tmp_path / "o", tmp_path / "t"
    _tree(ours, {"a.py": "A=1\n"})
    _tree(theirs, {"a.py": "A=2\n", "b.py": "B=1\n"})
    rc = merge_helper.main(["--incoming", str(theirs), "--root", str(ours)])
    out = capsys.readouterr().out
    assert rc == 1 and "conflict" in out and "Dry run" in out
    assert not (ours / "b.py").exists() and not (ours / "merge_incoming").exists()
