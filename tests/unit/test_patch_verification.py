import pytest

from core.patch_manager import (
    RollbackPolicy, Workspace, apply_and_verify, compare_runs, compare_static, diff, propose,
)
from models.patch import PatchError, RunSummary


class FakeRunner:
    """Test double standing in for a Phase 4 isolated runner. Executes nothing."""
    def __init__(self, runs, isolated=True, boom=False):
        self.runs, self.isolated, self.boom, self.calls = list(runs), isolated, boom, 0

    def run(self, root):
        self.calls += 1
        if self.boom:
            raise RuntimeError("sandbox exploded")
        return self.runs.pop(0)


@pytest.fixture
def ws(tmp_path, make_zip):
    z = make_zip({"m.py": "import os\nx = eval(s)\nDEBUG = True\n", "t.py": "pass\n"})
    return Workspace.create_from_zip(z, tmp_path / "ws")[0]


def patch_for(ws, new_text, rule=""):
    p = propose("m.py", new_text, ws)
    p.rule_id = rule
    diff(p)
    return p


GOOD = "import os\nx = eval(s)\nDEBUG = False\n"


def test_static_only_is_reported_as_not_verified(ws):
    out = apply_and_verify(patch_for(ws, GOOD, "sec-debug-enabled"), True, ws)
    v = out.verification
    assert v.level == "static-checked" and v.static.target_resolved is True and not out.reverted
    assert v.claim.startswith("NOT VERIFIED") and "tests were not run" in v.claim
    assert any("No isolated test runner" in r for r in v.skipped_reasons)
    assert (ws.root / "m.py").read_text() == GOOD


def test_non_isolated_runner_is_refused_and_never_called(ws):
    r = FakeRunner([RunSummary(0)], isolated=False)
    out = apply_and_verify(patch_for(ws, GOOD), True, ws, runner=r)
    assert r.calls == 0 and out.verification.level == "static-checked"
    assert any("not isolated" in s for s in out.verification.skipped_reasons)


def test_refusal_happens_before_any_runner_call(ws):
    r = FakeRunner([RunSummary(0), RunSummary(0)])
    p = propose("m.py", GOOD, ws)            # diff never shown
    with pytest.raises(PatchError):
        apply_and_verify(p, True, ws, runner=r)
    with pytest.raises(PatchError):
        apply_and_verify(patch_for(ws, GOOD), False, ws, runner=r)
    assert r.calls == 0


def test_syntax_error_introduced_triggers_automatic_rollback(ws):
    out = apply_and_verify(patch_for(ws, "import os\nx = eval(s\n"), True, ws)
    assert out.reverted and out.patch.status == "rolled-back" and out.verification.level == "failed"
    assert "rolled back" in out.verification.claim
    assert (ws.root / "m.py").read_text() == "import os\nx = eval(s)\nDEBUG = True\n"      # original restored


def test_rollback_can_be_disabled_but_is_still_reported(ws):
    out = apply_and_verify(patch_for(ws, "import os\nx = eval(s\n"), True, ws, policy=RollbackPolicy(auto_revert=False))
    assert not out.reverted and out.verification.level == "failed" and "Rollback was not automatic" in out.verification.claim
    assert (ws.root / "m.py").read_text().endswith("eval(s\n")


def test_new_medium_finding_warns_without_rollback(ws):
    out = apply_and_verify(patch_for(ws, GOOD + "h = hashlib.md5(b)\nr = requests.get(u, verify=False)\n"), True, ws)
    assert not out.reverted and out.verification.warnings and out.verification.level == "static-checked"


def test_target_finding_persisting_is_reported(ws):
    out = apply_and_verify(patch_for(ws, "import os\nx = eval(s)\nDEBUG = True\ny = 2\n", "sec-eval-exec"), True, ws)
    assert out.verification.static.target_resolved is False
    assert any("still appears" in w for w in out.verification.warnings)


def test_isolated_tests_no_regression_is_test_verified_with_modest_claim(ws):
    r = FakeRunner([RunSummary(1, {"a", "b"}, {"c"}), RunSummary(1, {"a", "b"}, {"c"})])
    out = apply_and_verify(patch_for(ws, GOOD), True, ws, runner=r)
    v = out.verification
    assert v.level == "test-verified" and r.calls == 2 and not v.tests.regressions
    assert "does not prove the fix is correct" in v.claim


def test_test_regression_triggers_rollback(ws):
    r = FakeRunner([RunSummary(0, {"a", "b"}, set()), RunSummary(1, {"a"}, {"b"})])
    out = apply_and_verify(patch_for(ws, GOOD), True, ws, runner=r)
    assert out.reverted and out.verification.tests.regressions == ["b"] and out.verification.level == "failed"
    assert "DEBUG = True" in (ws.root / "m.py").read_text()


def test_runner_failure_never_crashes_and_never_verifies(ws):
    out = apply_and_verify(patch_for(ws, GOOD), True, ws, runner=FakeRunner([], boom=True))
    v = out.verification
    assert v.level == "static-checked" and any("failed to execute" in s for s in v.skipped_reasons)
    assert not v.tests.ran and v.claim.startswith("NOT VERIFIED")


def test_timeout_is_not_verified(ws):
    r = FakeRunner([RunSummary(0, {"a"}, set()), RunSummary(1, {"a"}, set(), timed_out=True)])
    assert apply_and_verify(patch_for(ws, GOOD), True, ws, runner=r).verification.level == "static-checked"


def test_compare_helpers_are_pure():
    c = compare_runs(RunSummary(1, {"a", "b"}, {"c"}), RunSummary(0, {"a", "c"}, {"b"}))
    assert c.regressions == ["b"] and c.fixed == ["c"] and c.baseline_failed == 1
    from models.patch import Patch
    p = Patch("i", "a.py", "/x", "", "", "", rule_id="sec-debug-enabled")
    s = compare_static(p, "DEBUG = True\n", "DEBUG = False\n")
    assert s.target_resolved is True and [x["rule_id"] for x in s.resolved] == ["sec-debug-enabled"] and not s.new
