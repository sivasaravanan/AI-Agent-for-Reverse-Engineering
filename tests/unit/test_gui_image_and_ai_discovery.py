"""GUI preview image preparation + local AI auto-setup + live cloud model catalogue (all with fakes: no network, no daemon)."""
import json
import os
import stat

import pytest

from app_preview import PreviewOptions, approve
from app_preview.imagebuilder import DOCKERFILE, BuildPlan, build_gui_image, gui_image_ready, make_build_plan
from core.ai_settings import load_settings, save_settings
from providers.base_provider import ProviderError
from providers.discovery import (CLOUD_PROVIDERS, SPEC_BY_KEY, LocalServer, ModelCatalog, apply_cloud_choice, apply_local,
                                 detect_local, fetch_models, parse_models)


def _stub(tmp_path, body, name="fakedocker"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


# ------------------------------------------------------------------ image builder
@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
def test_build_plan_is_bound_to_command_and_dockerfile(tmp_path):
    exe = _stub(tmp_path, "exit 0")
    opts = PreviewOptions(backend="docker", tool_paths={"docker": exe})
    plan = make_build_plan(opts)
    assert plan.argv[:4] == [exe, "build", "--tag", "ase-preview-gui:latest"] and str(DOCKERFILE) in plan.argv
    assert plan.network and any("internet" in r for r in plan.risks)
    other = make_build_plan(opts, tag="x:1")
    assert other.plan_hash != plan.plan_hash


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
def test_build_refused_without_matching_approval_and_runs_when_approved(tmp_path):
    marker = tmp_path / "ran"
    exe = _stub(tmp_path, f'echo "Step 1/3"; touch {marker}; echo done')
    plan = make_build_plan(PreviewOptions(backend="docker", tool_paths={"docker": exe}))
    wrong = approve(BuildPlan(exe=exe, backend="docker", tag="other", argv=["x"], dockerfile_sha256="0"))
    res = build_gui_image(plan, wrong)
    assert not res["ok"] and "Refused" in res["notes"][0] and not marker.exists()
    lines = []
    res = build_gui_image(plan, approve(plan), progress=lines.append)
    assert res["ok"] and marker.exists() and any("Step 1/3" in l for l in lines)


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
def test_build_failure_reports_last_lines(tmp_path):
    exe = _stub(tmp_path, 'echo "ERROR: registry forbidden"; exit 1')
    plan = make_build_plan(PreviewOptions(backend="docker", tool_paths={"docker": exe}))
    res = build_gui_image(plan, approve(plan))
    assert not res["ok"] and res["exit_code"] == 1 and any("registry forbidden" in n for n in res["notes"])


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
def test_gui_image_ready_checks_image_inspect(tmp_path):
    have = _stub(tmp_path, 'case "$1" in info) echo 27.0;; image) exit 0;; esac', "have")
    miss = _stub(tmp_path, 'case "$1" in info) echo 27.0;; image) exit 1;; esac', "miss")
    ok = gui_image_ready(PreviewOptions(backend="docker", tool_paths={"docker": have}))
    no = gui_image_ready(PreviewOptions(backend="docker", tool_paths={"docker": miss}))
    assert ok["ready"] and not no["ready"] and "does not exist" in no["reason"]


# ------------------------------------------------------------------ local AI
def _transport(up, calls=None):
    def t(method, url, headers, body, timeout):
        if calls is not None:
            calls.append(url)
        for prefix, payload in up.items():
            if url.startswith(prefix):
                return 200, json.dumps(payload).encode()
        from providers.base_provider import ProviderUnavailable
        raise ProviderUnavailable("down")
    return t


def test_detect_and_configure_ollama_and_lmstudio(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    tr = _transport({"http://localhost:11434/api/tags": {"models": [{"name": "llama3.2:3b"}, {"name": "qwen2.5-coder:7b"}]},
                     "http://localhost:1234/v1/models": {"data": [{"id": "lmstudio-community/gemma"}]}})
    servers = detect_local(tr, which=lambda n: None)
    assert [s.running for s in servers] == [True, True]
    settings = {"ai": {"enabled": False, "providers": [{"name": "ollama", "kind": "ollama", "base_url": "http://localhost:11434", "model": ""}],
                       "task_routes": {"default": ["ollama", "openrouter"]}}}
    log = apply_local(settings, servers)
    ai = settings["ai"]
    by = {p["name"]: p for p in ai["providers"]}
    assert by["ollama"]["model"] == "llama3.2:3b" and by["ollama"]["fallback_models"] == ["qwen2.5-coder:7b"]
    assert by["lmstudio"]["kind"] == "openai_compatible" and by["lmstudio"]["base_url"] == "http://localhost:1234/v1"
    assert by["lmstudio"]["model"] == "lmstudio-community/gemma" and by["lmstudio"]["api_key_env"] == ""
    assert ai["task_routes"]["default"] == ["ollama", "lmstudio", "openrouter"]
    assert ai["enabled"] is False                      # the master AI switch is never flipped automatically
    assert any("LM Studio" in l for l in log)
    # a model the user picked and that still exists is kept
    by["ollama"]["model"] = "qwen2.5-coder:7b"
    apply_local(settings, servers)
    assert by["ollama"]["model"] == "qwen2.5-coder:7b"


def test_installed_but_stopped_server_is_started_only_when_allowed():
    state = {"up": False, "started": []}

    def tr(method, url, h, b, t):
        if state["up"] and "11434" in url:
            return 200, b'{"models": [{"name": "m1"}]}'
        from providers.base_provider import ProviderUnavailable
        raise ProviderUnavailable("down")

    def popen(argv, **kw):
        state["started"].append(argv)
        state["up"] = True

    which = lambda n: "/usr/bin/ollama" if n == "ollama" else None   # noqa: E731
    s = detect_local(tr, auto_start=False, which=which, popen=popen)[0]
    assert not s.running and s.installed and not state["started"] and "not running" in s.note
    s = detect_local(tr, auto_start=True, which=which, popen=popen, sleep=lambda _x: None)[0]
    assert s.running and s.started_by_us and state["started"] == [["/usr/bin/ollama", "serve"]] and s.models == ["m1"]


def test_nothing_installed_is_a_note_not_an_error():
    servers = detect_local(_transport({}), which=lambda n: None)
    assert not any(s.running for s in servers) and all("not found" in s.note for s in servers)
    settings = {"ai": {"providers": [], "task_routes": {"default": ["ollama"]}}}
    assert apply_local(settings, servers) == [] and settings["ai"]["providers"] == []


def test_ai_flags_roundtrip_and_never_hold_keys(tmp_path):
    s = load_settings(tmp_path / "none.json")
    assert s["ai"]["auto_detect_local"] and s["ai"]["auto_start_local"] and s["ai"]["fetch_cloud_models"]
    s["ai"]["auto_start_local"] = False
    p = save_settings(s, tmp_path / "a.json")
    assert load_settings(p)["ai"]["auto_start_local"] is False


# ------------------------------------------------------------------ cloud catalogue
OPENROUTER = {"data": [
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "Llama 3.3 70B (free)", "context_length": 131072,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-x", "name": "GPT X", "context_length": 200000, "pricing": {"prompt": "0.000002", "completion": "0.00001"}}]}


def test_openrouter_free_vs_paid_from_published_prices():
    ms = parse_models(SPEC_BY_KEY["openrouter"], OPENROUTER)
    by = {m.id: m for m in ms}
    assert by["meta-llama/llama-3.3-70b-instruct:free"].cost == "free"
    g = by["openai/gpt-x"]
    assert g.cost == "paid" and g.prompt_price == 2.0 and g.completion_price == 10.0 and g.context == 200000


def test_keyed_provider_needs_key_and_key_only_goes_to_its_host():
    spec = SPEC_BY_KEY["openai"]
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        fetch_models(spec, "", _transport({}))
    seen = {}

    def tr(method, url, headers, body, timeout):
        seen.update(url=url, headers=headers)
        return 200, json.dumps({"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]}).encode()
    ms = fetch_models(spec, "sk-test-123456", tr)
    assert [m.id for m in ms] == ["gpt-a", "gpt-b"] and ms[0].cost == "unknown"
    assert seen["url"].startswith("https://api.openai.com/") and seen["headers"]["Authorization"] == "Bearer sk-test-123456"
    with pytest.raises(ProviderError) as ei:
        fetch_models(spec, "sk-test-123456", lambda *a: (401, b"bad sk-test-123456"))
    assert "sk-test-123456" not in str(ei.value)


def test_anthropic_uses_x_api_key_and_gemini_prefix_is_stripped():
    seen = {}

    def tr(method, url, headers, body, timeout):
        seen.update(headers)
        return 200, json.dumps({"data": [{"id": "claude-x", "display_name": "Claude X"}]}).encode()
    ms = fetch_models(SPEC_BY_KEY["anthropic"], "k-123456", tr)
    assert seen["x-api-key"] == "k-123456" and seen["anthropic-version"] and ms[0].name == "Claude X"
    g = parse_models(SPEC_BY_KEY["gemini"], {"models": [{"name": "models/gemini-pro", "inputTokenLimit": 1000}]})
    assert g[0].id == "gemini-pro" and g[0].context == 1000


def test_catalog_refresh_cache_offline_fallback_and_search(tmp_path):
    state = {"online": True, "calls": 0}

    def tr(method, url, headers, body, timeout):
        state["calls"] += 1
        if not state["online"]:
            from providers.base_provider import ProviderUnavailable
            raise ProviderUnavailable("offline")
        return 200, json.dumps(OPENROUTER).encode()
    now = [1000.0]
    cat = ModelCatalog(tmp_path / "c.json", tr, key_for=lambda spec: "", clock=lambda: now[0])
    rep = cat.refresh()
    assert rep["openrouter"] == "2 model(s)" and "OPENAI_API_KEY" in rep["openai"] and set(cat.models) == {"openrouter"}
    assert [m.id for m in cat.search(cost="free")] == ["meta-llama/llama-3.3-70b-instruct:free"]
    assert [m.id for m in cat.search(text="gpt")] == ["openai/gpt-x"]
    calls = state["calls"]
    assert cat.refresh(["openrouter"], force=False)["openrouter"].startswith("cached") and state["calls"] == calls
    state["online"] = False
    now[0] += 99 * 3600
    rep = cat.refresh(["openrouter"], force=False)
    assert "Showing the last saved list" in rep["openrouter"] and len(cat.models["openrouter"]) == 2
    again = ModelCatalog(tmp_path / "c.json", tr, key_for=lambda spec: "")          # survives restart
    assert len(again.models["openrouter"]) == 2
    assert "sk-" not in (tmp_path / "c.json").read_text()


def test_apply_cloud_choice_adds_provider_with_env_name_only():
    settings = {"ai": {"enabled": False, "providers": [], "task_routes": {"default": ["ollama"]}}}
    e = apply_cloud_choice(settings, SPEC_BY_KEY["groq"], "llama-3.3-70b-versatile")
    assert e["api_key_env"] == "GROQ_API_KEY" and e["model"] == "llama-3.3-70b-versatile" and e["kind"] == "openai_compatible"
    assert settings["ai"]["task_routes"]["default"] == ["ollama", "groq"] and settings["ai"]["enabled"] is False
    a = apply_cloud_choice(settings, SPEC_BY_KEY["anthropic"], "claude-x")
    assert a["extra_headers"]["anthropic-version"]
    assert len({c.key for c in CLOUD_PROVIDERS}) == len(CLOUD_PROVIDERS)


# ------------------------------------------------------------------ UI
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("No display")
    yield r
    r.destroy()


def test_ai_panel_configures_local_models_and_offers_them(root, tmp_path):
    from ui.ai_panel import AIPanel
    panel = AIPanel(root, get_selected_findings=lambda: [], get_files=lambda: [], get_architecture=lambda: None,
                    read_file=lambda n: None, settings_path=tmp_path / "ai.json", transport=_transport({}), autodetect=False)
    servers = [LocalServer("ollama", "Ollama", "http://localhost:11434", running=True, models=["llama3.2:3b", "phi4"]),
               LocalServer("lmstudio", "LM Studio", "http://localhost:1234/v1", running=True, models=["gemma"])]
    panel._local_done(servers)
    assert "lmstudio" in list(panel.prov_box["values"])
    panel.prov.set("ollama")
    panel._load_provider_fields()
    assert panel.model.get() == "llama3.2:3b" and list(panel.model_box["values"]) == ["llama3.2:3b", "phi4"]
    assert "Ollama: running with 2 model(s)" in panel.avail.get() and "Enable AI" in panel.avail.get()
    panel._pick_cloud_model("openrouter", "meta-llama/llama-3.3-70b-instruct:free")
    assert panel.prov.get() == "openrouter" and panel.model.get().endswith(":free") and "OPENROUTER_API_KEY" in panel.avail.get()
    panel.update()


def test_preview_panel_shows_prepare_image_bar_and_builds(root, tmp_path, monkeypatch):
    from ui import app_preview_view as v
    view = v.AppPreviewView(root)
    view.pack()
    root.update()
    view._handle({"type": "ui_image", "st": {"ready": False, "image": "ase-preview-gui:latest", "backend": "docker",
                                             "reason": "The GUI preview image 'ase-preview-gui:latest' does not exist on this computer yet."}})
    root.update()
    assert view.img_bar.winfo_ismapped() and "does not exist" in view.img_msg.get()
    asked = []
    monkeypatch.setattr(v.messagebox, "askokcancel", lambda *a, **k: asked.append(a[1]) or True)
    monkeypatch.setattr(v, "make_build_plan", lambda opts, tag="": BuildPlan(exe="x", backend="docker", tag="t", argv=["x", "build"],
                                                                             dockerfile_sha256="ab" * 32, risks=["net"]))
    monkeypatch.setattr(v, "build_gui_image", lambda plan, appr, progress=None: (progress("Step 1"), {"ok": True, "exit_code": 0, "notes": ["Image 't' is ready."]})[1])
    view.prepare_gui_image()
    assert "x build" in asked[0] and "net" in asked[0]
    import time
    for _ in range(40):
        root.update()
        time.sleep(0.05)
    assert not view.img_bar.winfo_ismapped() and "ready" in view.status_var.get()
    view.destroy()


# ------------------------------------------------------------------ real container (opt-in)
@pytest.mark.skipif(os.environ.get("ASE_REAL_CONTAINER_TESTS") != "1",
                    reason="set ASE_REAL_CONTAINER_TESTS=1 with a running Docker/Podman and the GUI preview image to run this")
def test_real_container_gui_app_produces_screen_frames(tmp_path):
    from app_preview import PV, discover_entries, make_plan, start
    from app_preview.runtimes import status
    opts = PreviewOptions()
    if not (status(opts)["available"] and gui_image_ready(opts)["ready"]):
        pytest.skip("no usable runtime or GUI preview image")
    (tmp_path / "main.py").write_text("import tkinter as tk\nr = tk.Tk()\ntk.Label(r, text='hi').pack()\nr.after(3000, r.destroy)\nr.mainloop()\n")
    entries, _ = discover_entries(str(tmp_path))
    plan = make_plan(str(tmp_path), next(e for e in entries if e.kind == "gui"), "run", opts)
    got = []
    PV.subscribe(got.append)
    res = start(plan, approve(plan), opts).wait(120)
    assert res.status == "completed" and any(e.get("type") == "pv_frame" and e["png"][:4] == b"\x89PNG" for e in got)


# ------------------------------------------------------------------ "Refused" must say why, and must not refuse needlessly
def test_workspace_skips_virtualenvs_of_any_name_and_caches(tmp_path):
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import prepare_workspace
    src = tmp_path / "proj"
    (src / "myenv" / "lib").mkdir(parents=True)
    (src / "myenv" / "pyvenv.cfg").write_text("home = /usr\n")
    (src / "myenv" / "lib" / "big.bin").write_bytes(b"x" * 3_000_000)
    (src / ".tox").mkdir()
    (src / ".tox" / "junk").write_bytes(b"x" * 3_000_000)
    (src / "dupescope.py").write_text("print(1)\n")
    dest = tmp_path / "w"
    prepare_workspace(src, dest, Limits(max_workspace_mb=1))
    assert (dest / "dupescope.py").exists() and not (dest / "myenv").exists() and not (dest / ".tox").exists()


def test_size_refusal_names_the_biggest_folders(tmp_path):
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import WorkspaceRejected, prepare_workspace
    src = tmp_path / "proj"
    (src / "data").mkdir(parents=True)
    (src / "data" / "dump.bin").write_bytes(b"x" * 2_000_000)
    with pytest.raises(WorkspaceRejected) as ei:
        prepare_workspace(src, tmp_path / "w", Limits(max_workspace_mb=1))
    assert "data (1 MB)" in str(ei.value) and "Max project MB" in str(ei.value)


def test_unreadable_file_is_skipped_not_a_refusal(tmp_path, monkeypatch):
    import shutil
    from plugins.build_execution import sandbox
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.py").write_text("1")
    (src / "locked.db").write_text("2")
    real = shutil.copyfile

    def flaky(a, b, *k, **kw):
        if str(a).endswith("locked.db"):
            raise PermissionError("locked")
        return real(a, b, *k, **kw)
    monkeypatch.setattr(sandbox.shutil, "copyfile", flaky)
    notes = sandbox.prepare_workspace(src, tmp_path / "w", sandbox.Limits())
    assert (tmp_path / "w" / "a.py").exists() and any("skipped unreadable file locked.db" in n for n in notes)


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
def test_refused_run_streams_the_reason_to_the_console(tmp_path):
    from plugins.build_execution.discovery import custom_candidate
    from plugins.build_execution.events import BUS
    from plugins.build_execution.models import Approval as Ap, Limits, Options
    from plugins.build_execution.sandbox import build_plan, run_plan
    exe = _stub(tmp_path, 'case "$1" in info) echo 27.0;; image) exit 0;; esac')
    opts = Options(tool_paths={"docker": exe}, backend="docker")
    src = tmp_path / "proj"
    (src / "data").mkdir(parents=True)
    (src / "data" / "dump.bin").write_bytes(b"x" * 2_000_000)
    plan = build_plan(src, custom_candidate(["python", "-c", "print(1)"]), opts, limits=Limits(max_workspace_mb=1))
    seen = []
    BUS.subscribe(seen.append)
    res = run_plan(plan, Ap.for_plan(plan), opts)
    assert res.status == "refused"
    errs = [e["text"] for e in seen if e["type"] == "err"]
    assert any("larger than 1 MB" in t for t in errs)
    assert [e["type"] for e in seen if e["type"] in ("err", "end")][-1] == "end"       # reason first, then the end event


# ------------------------------------------------------------------ single script in a huge folder
def _big_folder(tmp_path):
    src = tmp_path / "Test Area"
    (src / "AI Chat").mkdir(parents=True)
    (src / "AI Chat" / "model.bin").write_bytes(b"x" * 3_000_000)
    (src / "helpers").mkdir()
    (src / "helpers" / "__init__.py").write_text("")
    (src / "helpers" / "hashing.py").write_text("import hashlib\nfrom . import util\n")
    (src / "helpers" / "util.py").write_text("X = 1\n")
    (src / "other_tool.py").write_text("print('not needed')\n")
    (src / "config.json").write_text("{}")
    (src / "dupescope.py").write_text("import os, json\nfrom helpers.hashing import h\nprint(open('config.json').read())\n")
    return src


def test_entry_script_parsing():
    from plugins.build_execution.sandbox import entry_script
    assert entry_script(["python", "./dupescope.py", "-x"]) == "dupescope.py"
    assert entry_script(["python3.12", "-u", "sub/run.py"]) == "sub/run.py"
    assert entry_script(["python", "-m", "pkg"]) == "" and entry_script(["node", "a.js"]) == "" and entry_script(["python", "../x.py"]) == ""


def test_huge_folder_with_one_script_copies_only_the_script_and_its_imports(tmp_path):
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import prepare_workspace
    src = _big_folder(tmp_path)
    dest = tmp_path / "w"
    notes = prepare_workspace(src, dest, Limits(max_workspace_mb=1), entry="dupescope.py")
    have = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert have == {"dupescope.py", "config.json", "helpers/__init__.py", "helpers/hashing.py", "helpers/util.py"}
    assert any("only dupescope.py and 4 related file" in n for n in notes)


def test_zip_with_one_script_is_narrowed_too_and_non_python_still_refused(tmp_path):
    import zipfile
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import WorkspaceRejected, prepare_workspace
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proj/run.py", "import lib\n")
        zf.writestr("proj/lib.py", "A = 1\n")
        zf.writestr("proj/data/big.bin", os.urandom(1_500_000))
    notes = prepare_workspace(z, tmp_path / "w", Limits(max_workspace_mb=1), entry="run.py")
    assert sorted(p.name for p in (tmp_path / "w").rglob("*") if p.is_file()) == ["lib.py", "run.py"] and notes
    with pytest.raises(WorkspaceRejected):
        prepare_workspace(z, tmp_path / "w2", Limits(max_workspace_mb=1), entry="")


def test_narrowing_never_hides_a_script_that_is_itself_too_big(tmp_path):
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import WorkspaceRejected, prepare_workspace
    src = tmp_path / "p"
    src.mkdir()
    (src / "huge.py").write_text("#" + "x" * 2_000_000)
    with pytest.raises(WorkspaceRejected):
        prepare_workspace(src, tmp_path / "w", Limits(max_workspace_mb=1), entry="huge.py")


# ------------------------------------------------------------------ window programs go to App Preview
def _pump(root, cond, secs=8.0):
    import time
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.03)
    root.update()


def test_build_tab_hands_window_program_to_app_preview(root, tmp_path, monkeypatch):
    from plugins.build_execution import ui_tab
    from plugins.build_execution.discovery import custom_candidate
    from ui.execution_dialog import ExecutionTab
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "dupescope.py").write_text("import tkinter as tk\nroot = tk.Tk()\n")
    (proj / "cli.py").write_text("print('hi')\n")
    got, asked, ran = [], [], []
    tab = ExecutionTab(root, get_source=lambda: str(proj), on_gui_run=lambda s, f: got.append((s, f)))
    monkeypatch.setattr(tab, "_run", lambda *a, **k: ran.append(k))             # the normal run path (approval dialog) is not under test
    monkeypatch.setattr(ui_tab.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    # window program + yes -> handed over, detection ran on a worker thread (the call itself returns at once)
    assert tab._offer_window_program(custom_candidate(["python", "./dupescope.py"]), str(proj)) is True
    assert str(tab.run_btn["state"]) == "disabled"
    _pump(root, lambda: got)
    assert got == [(str(proj), "dupescope.py")] and "opens a window" in asked[0] and str(tab.run_btn["state"]) == "normal"
    # console script -> no question, the normal run continues
    tab._offer_window_program(custom_candidate(["python", "cli.py"]), str(proj))
    _pump(root, lambda: ran)
    assert len(got) == 1 and ran == [{"retry_limits_override": None, "_skip_gui_check": True}] and len(asked) == 1
    # window program + no -> normal run continues too
    monkeypatch.setattr(ui_tab.messagebox, "askyesno", lambda *a, **k: False)
    tab._offer_window_program(custom_candidate(["python", "dupescope.py"]), str(proj))
    _pump(root, lambda: len(ran) == 2)
    assert len(ran) == 2 and len(got) == 1
    # no host hook (plain plugin use) -> never intercepts
    tab.on_gui_run = None
    assert tab._offer_window_program(custom_candidate(["python", "dupescope.py"]), str(proj)) is False


def test_preview_start_and_scan_never_block_the_ui_thread(root, tmp_path, monkeypatch):
    import threading
    import time
    from ui import app_preview_view as v
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "main.py").write_text("print(1)\nif __name__ == '__main__':\n    pass\n")
    ui_thread = threading.get_ident()
    where = {}
    real_status = v.runtime_status
    real_sv = v.ProjectView

    def slow_status(opts):
        where["status"] = threading.get_ident()
        time.sleep(0.6)                                     # a slow `docker info`
        return real_status(opts)

    class SlowSV(real_sv):
        def __init__(self, *a, **k):
            where["sv"] = threading.get_ident()
            time.sleep(0.6)                                 # a huge folder
            super().__init__(*a, **k)
    monkeypatch.setattr(v, "runtime_status", slow_status)
    monkeypatch.setattr(v, "ProjectView", SlowSV)
    seen = []
    view = v.AppPreviewView(root, approver=lambda plan, iso: seen.append(plan) or False)
    view.pack()
    t0 = time.time()
    view.set_source(str(proj))
    assert time.time() - t0 < 0.3                           # returned at once; the scan runs on a worker thread
    _pump(root, lambda: view.entries)
    assert view.entries and view.names == ["main.py"] and where["sv"] != ui_thread
    t0 = time.time()
    view.start()
    assert time.time() - t0 < 0.3
    _pump(root, lambda: seen or view.status_var.get().startswith("Could not"))
    assert where["status"] != ui_thread
    view.destroy()


def test_preview_run_script_starts_a_gui_entry_even_without_main_guard(root, tmp_path, monkeypatch):
    import time
    from ui import app_preview_view as v
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "dupescope.py").write_text("import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n")
    view = v.AppPreviewView(root, approver=lambda plan, iso: False)         # decline: we only check what would be started
    seen = []
    monkeypatch.setattr(view, "approver", lambda plan, iso: seen.append(plan) or False)
    view.pack()
    view.run_script(str(proj), "./dupescope.py")
    for _ in range(80):
        root.update()
        time.sleep(0.05)
        if seen:
            break
    assert seen and seen[0].entry.kind == "gui" and seen[0].display and seen[0].entry.argv == ["python", "dupescope.py"]
    view.destroy()


# ------------------------------------------------------------------ storage settings and clean-up
@pytest.fixture
def home(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv("AISEA_HOME", str(tmp_path / "home"))
    (tmp_path / "systmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))          # isolate from real leftovers in /tmp
    return tmp_path


def test_storage_settings_validate_persist_and_feed_limits(home):
    from plugins.build_execution import storage
    from plugins.build_execution.models import Limits
    assert storage.load()["max_workspace_mb"] == 500 and Limits().max_workspace_mb == 500
    scratch = home / "big drive" / "scratch"
    cfg = storage.save(scratch_dir=str(scratch), max_workspace_mb=900, tmp_mb=2048, keep_workspace=False)
    assert cfg["max_workspace_mb"] == 900 and scratch.is_dir()
    lim = Limits()
    assert lim.max_workspace_mb == 900 and lim.tmp_mb == 2048        # sizes are part of every new plan (and its approval hash)
    assert storage.save(max_workspace_mb=99999)["max_workspace_mb"] == 4096        # clamped
    with pytest.raises(ValueError, match="comma"):
        storage.save(scratch_dir=str(home / "a,b"))
    with pytest.raises(ValueError, match="absolute"):
        storage.save(scratch_dir="relative/dir")
    (storage.settings_path()).write_text("{ broken json")
    assert storage.load() == storage.DEFAULTS                          # a broken file never breaks a run


def test_run_uses_the_scratch_folder_and_deletes_the_copy_unless_kept(home):
    from plugins.build_execution import storage
    d = home / "scr"
    storage.save(scratch_dir=str(d))
    s = storage.new_scratch("be_ws_")
    assert s.parent == d.resolve() or s.parent == d
    (s / "x").write_text("1")
    assert storage.finish_scratch(s) == (True, "") and not s.exists()
    storage.save(keep_workspace=True)
    s2 = storage.new_scratch("be_ws_")
    removed, note = storage.finish_scratch(s2)
    assert not removed and s2.exists() and str(s2) in note
    storage.save(keep_workspace=False)
    assert storage.remove_scratch([str(s2)]) == [str(s2)] and not s2.exists()


def test_sweep_only_touches_our_old_folders(home):
    import time
    from plugins.build_execution import storage
    d = home / "scr"
    storage.save(scratch_dir=str(d))
    old, new, other = storage.new_scratch("be_ws_"), storage.new_scratch("be_pv_"), d / "my_data"
    other.mkdir()
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert storage.sweep_old_scratch(3600) == [str(old)] and new.exists() and other.exists()
    assert {i["path"] for i in storage.scratch_inventory()} == {str(new)}


def _runtime_stub(tmp_path, log):
    def run(exe, args, timeout=60):
        log.append(args)
        if args[:1] == ["ps"]:
            return 0, "abc\tbe-0123456789ab\texited\tExited (0)\ndef\tbe-pv-aabbccddeeff\trunning\tUp\nzzz\tbe-evil;rm\texited\tx\n", ""
        if args[:2] == ["image", "ls"] and "dangling=true" in args:
            return 0, "i1\ni2\n", ""
        if args[:2] == ["image", "ls"]:
            return 0, "ase-preview-gui:latest\t380MB\npython:3.12-slim\t120MB\n", ""
        if args[:2] == ["system", "df"]:
            return 0, "TYPE TOTAL ACTIVE SIZE\nImages 2 1 500MB\n", ""
        if args[:1] == ["info"]:
            return 0, "/var/lib/docker\n" if "DockerRootDir" in args[2] else "<no value>", ""
        return 0, "ok\n", ""
    return run


def test_runtime_inventory_and_clean_only_remove_our_things(tmp_path):
    from plugins.build_execution import storage
    log = []
    run = _runtime_stub(tmp_path, log)
    inv = storage.runtime_inventory("docker", run)
    assert [c["name"] for c in inv["containers"]] == ["be-0123456789ab", "be-pv-aabbccddeeff"]       # the odd name is ignored
    assert inv["gui_image"]["size"] == "380MB" and inv["dangling"] == 2 and inv["root"] == "/var/lib/docker" and "Images" in inv["df"]
    log.clear()
    rep = storage.clean_runtime("docker", run=run)
    assert ["rm", "-f", "be-0123456789ab"] in log and ["rm", "-f", "be-pv-aabbccddeeff"] not in log      # running one is kept
    assert not any(a[:2] in (["image", "prune"], ["builder", "prune"], ["image", "rm"]) for a in log)      # global things need asking
    assert any("kept running container" in r for r in rep)
    log.clear()
    storage.clean_runtime("docker", containers=False, gui_image=True, dangling_images=True, build_cache=True, run=run)
    assert ["image", "rm", "ase-preview-gui:latest"] in log and ["image", "prune", "-f"] in log and ["builder", "prune", "-f"] in log
    log.clear()
    storage.clean_runtime("docker", include_running=True, run=run)
    assert ["rm", "-f", "be-pv-aabbccddeeff"] in log


def test_storage_dialog_saves_and_cleans(root, home):
    import time
    from plugins.build_execution import storage
    from ui.storage_dialog import StorageDialog
    log = []
    leftover = storage.new_scratch("be_ws_")
    (leftover / "f").write_text("x" * 1000)
    dlg = StorageDialog(root, runner=_runtime_stub(home, log))
    _pump(root, lambda: dlg.tree.get_children() and any("be-0123" in str(dlg.tree.item(i, "values")) for i in dlg.tree.get_children()))
    kinds = [dlg.tree.item(i, "text") for i in dlg.tree.get_children()]
    assert "Project copy" in kinds and "Container" in kinds and "Image" in kinds
    dlg.size.set(750)
    dlg.scratch.set(str(home / "elsewhere"))
    dlg.save()
    assert storage.load()["max_workspace_mb"] == 750 and (home / "elsewhere").is_dir()
    dlg.c_gui.set(False)
    dlg.clean()
    _pump(root, lambda: dlg.msg.get().startswith("Done"))
    assert "removed" in dlg.msg.get() and not leftover.exists()
    dlg.destroy()


def test_missing_script_is_named_in_the_size_refusal(tmp_path):
    from plugins.build_execution.models import Limits
    from plugins.build_execution.sandbox import WorkspaceRejected, prepare_workspace
    src = tmp_path / "p"
    (src / "AI Chat").mkdir(parents=True)
    (src / "AI Chat" / "m.bin").write_bytes(b"x" * 2_000_000)
    (src / "dupescope.py").write_text("print(1)\n")
    with pytest.raises(WorkspaceRejected) as ei:
        prepare_workspace(src, tmp_path / "w", Limits(max_workspace_mb=1), entry="dupescopy.py")
    assert "'dupescopy.py' was not found" in str(ei.value) and "AI Chat" in str(ei.value)
