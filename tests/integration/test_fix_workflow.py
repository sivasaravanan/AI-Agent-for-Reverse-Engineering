import sys
from pathlib import Path

import pytest

from core.analysis_orchestrator import analyze_archive
from core.patch_manager import Workspace, apply_and_verify, diff, propose_from_finding
from models.patch import NoFixAvailable
from tests.fixtures.sample_project import build_sample_zip

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import fix_cli  # noqa: E402


@pytest.fixture
def env(tmp_path):
    z = build_sample_zip(tmp_path / "s.zip")
    return z, analyze_archive(z), tmp_path


def test_every_fixable_sample_finding_yields_a_compiling_patch(env):
    z, result, tmp = env
    ws, _ = Workspace.create_from_zip(z, tmp / "ws")
    seen = set()
    for f in result.findings:
        try:
            p = propose_from_finding(f, ws)
        except NoFixAvailable:
            continue
        seen.add(f.rule_id)
        assert compile(p.new_text, p.file, "exec") and p.source == "rule-based" and p.finding_id == f.id
    assert {"sec-eval-exec", "sec-debug-enabled", "sec-hardcoded-secret"} <= seen


def test_end_to_end_apply_resolves_the_finding_statically(env):
    z, result, tmp = env
    ws, _ = Workspace.create_from_zip(z, tmp / "ws")
    f = next(x for x in result.findings if x.rule_id == "sec-hardcoded-secret")
    p = propose_from_finding(f, ws)
    assert "os.environ.get" in diff(p)
    out = apply_and_verify(p, True, ws)
    assert out.verification.static.target_resolved is True and not out.reverted
    assert "abcd1234efgh5678" not in (ws.root / f.file).read_text()
    assert out.verification.claim.startswith("NOT VERIFIED")


def test_syntax_error_finding_has_no_fabricated_fix(env):
    z, result, tmp = env
    ws, _ = Workspace.create_from_zip(z, tmp / "ws")
    f = next(x for x in result.findings if x.rule_id == "py-syntax")
    with pytest.raises(NoFixAvailable, match="manual"):
        propose_from_finding(f, ws)


def test_cli_list_decline_apply_and_revert(env, capsys):
    z, result, tmp = env
    ws = tmp / "ws"
    assert fix_cli.run(z, ws, list_only=True) == 0
    assert "sec-debug-enabled" in capsys.readouterr().out
    fid = next(x.id for x in result.findings if x.rule_id == "sec-debug-enabled")
    target = ws / "shop/app/main.py"
    # declined -> nothing changes
    assert fix_cli.run(z, ws, fid, input_fn=lambda _: "n") == 0
    assert "DEBUG = True" in target.read_text()
    # approved
    lines = []
    assert fix_cli.run(z, ws, fid, input_fn=lambda _: "y", out=lines.append) == 0
    assert "DEBUG = False" in target.read_text()
    manifest = next(l for l in lines if "manifest.json" in l).split(": ", 1)[1]
    assert any("NOT VERIFIED" in l for l in lines)
    assert fix_cli.run_revert(manifest) == 0 and "DEBUG = True" in target.read_text()


def test_cli_errors_are_friendly(tmp_path, capsys):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"x" * 50)
    assert fix_cli.run(bad, tmp_path / "ws", "F-0001") == 2
    assert "not a valid ZIP" in capsys.readouterr().out
    assert fix_cli.run_revert(tmp_path / "missing.json") == 2
