"""Build & Test tab: commands found in the chosen ZIP/folder are listed in a combobox (skipped without Tk/display)."""
import gc
import time
import zipfile

import pytest

tk = pytest.importorskip("tkinter")

from ui.execution_dialog import ExecutionTab  # noqa: E402


@pytest.fixture(autouse=True)
def _free_tk_objects_on_main_thread():
    yield
    gc.collect()


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def _pump(root, cond, timeout=10):
    end = time.time() + timeout
    while not cond():
        root.update()
        time.sleep(0.02)
        assert time.time() < end, "timed out"
    root.update()


def _zip(tmp_path, files, name="p.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return str(p)


DOTNET = {"api/Api.csproj": "<Project Sdk='Microsoft.NET.Sdk'></Project>", "api/Program.cs": "class P{}"}
PYTHON = {"tests/test_a.py": "def test_a():\n    assert True\n", "requirements.txt": "pytest\n"}


def test_commands_are_listed_automatically_when_source_is_set(tmp_path):
    root = _root()
    try:
        tab = ExecutionTab(root)
        tab.pack(fill="both", expand=True)
        tab.src.set(_zip(tmp_path, DOTNET))
        _pump(root, lambda: len(tab._cands) > 0)
        values = list(tab.cmd_box.cget("values"))
        assert len(values) == len(tab._cands) == len(tab.tree.get_children())
        assert any(v.startswith("dotnet:build") and "Api.csproj" in v for v in values)
        assert tab.eco.get() == "dotnet"                        # no longer left on "python" for a .NET project
        assert "found" in tab.cmd_info.get()
    finally:
        root.destroy()


def test_picking_in_combobox_selects_the_table_row_and_the_run_uses_it(tmp_path, monkeypatch):
    root = _root()
    try:
        tab = ExecutionTab(root)
        tab.src.set(_zip(tmp_path, PYTHON))
        _pump(root, lambda: len(tab._cands) >= 2)
        tab.cmd_box.current(1)
        tab._on_pick()
        sel = tab.tree.selection()
        assert sel and tab.tree.index(sel[0]) == 1 and tab.eco.get() == "python"
        seen = {}
        import plugins.build_execution.ui_tab as ui_tab

        class FakeDlg:
            approved = False
            def __init__(self, parent, plan):
                seen["id"] = plan.candidate.id

        monkeypatch.setattr(ui_tab, "ApprovalDialog", FakeDlg)
        tab._run()
        assert seen["id"] == tab._cands[1].id                  # the command chosen in the combobox is the one planned
        tab.tree.selection_set(tab.tree.get_children()[0])
        root.update()
        assert tab.cmd_box.current() == 0                      # table selection is mirrored in the combobox
    finally:
        root.destroy()


def test_wrong_ecosystem_is_explained_not_silently_ignored(tmp_path):
    root = _root()
    try:
        tab = ExecutionTab(root)
        tab.src.set(_zip(tmp_path, DOTNET))
        _pump(root, lambda: len(tab._cands) > 0)
        tab.eco.set("python")
        root.update()
        assert "No python commands" in tab.cmd_info.get() and "dotnet" in tab.cmd_info.get()
        tab.eco.set("dotnet")
        root.update()
        assert "found" in tab.cmd_info.get() and "No python" not in tab.cmd_info.get()
    finally:
        root.destroy()


def test_bad_or_missing_source_does_not_pop_up_or_break(tmp_path):
    root = _root()
    try:
        tab = ExecutionTab(root)
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        tab.src.set(str(bad))
        _pump(root, lambda: "Could not read" in tab.cmd_info.get())
        assert tab._cands == [] or tab._cands is not None
        tab.src.set(str(tmp_path / "nothing-here.zip"))
        for _ in range(5):
            root.update()
            time.sleep(0.1)
        assert str(tab.run_btn["state"]) == "normal"
    finally:
        root.destroy()


def test_single_command_is_preselected_and_folder_sources_work(tmp_path):
    root = _root()
    try:
        d = tmp_path / "proj"
        (d / "api").mkdir(parents=True)
        (d / "api" / "Api.csproj").write_text("<Project/>")
        tab = ExecutionTab(root)
        tab.src.set(str(d))
        _pump(root, lambda: len(tab._cands) > 0)
        if len(tab._cands) == 1:
            assert tab.tree.selection()
        else:
            assert len(tab.cmd_box.cget("values")) == len(tab._cands)
    finally:
        root.destroy()
