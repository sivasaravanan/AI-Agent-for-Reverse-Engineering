"""Run targets: root entry file / individually runnable files listed next to the build/test commands (read-only discovery)."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from plugins.build_execution import Options, build_plan, discover_candidates
from plugins.build_execution.run_targets import ProjectView, discover_all


def make_tree(root: Path, files: dict) -> Path:
    for n, t in files.items():
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t, encoding="utf-8")
    return root


def make_zip(path: Path, files: dict, prefix: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, t in files.items():
            z.writestr(prefix + n, t)
    return path


GUI_MAIN = "import tkinter as tk\nfrom diffstudio.app import run\nif __name__ == '__main__':\n    run()\n"
DIFF_STUDIO = {
    "main.py": GUI_MAIN, "diffstudio/__init__.py": "", "diffstudio/app.py": "import tkinter\n\ndef run():\n    pass\n",
    "diffstudio/__main__.py": "from .app import run\nrun()\n", "tools/migrate.py": "import argparse\nif __name__ == '__main__':\n    print('x')\n",
    "tools/helper.py": "def f():\n    pass\n", "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
    "Diff Studio.sln": "", "Diff Studio.pyproj": "<Project/>", "requirements.txt": "customtkinter\n",
    "env/pyvenv.cfg": "home = x\n", "env/Scripts/activate.bat": "@echo off", "env/Scripts/Activate.ps1": "x",
    "env/Lib/site-packages/pkg/main.py": "if __name__ == '__main__':\n    pass\n",
}


def ids(c):
    return [x.id for x in c]


@pytest.fixture(params=["folder", "zip", "zip_with_top_folder"])
def diff_studio(request, tmp_path):
    if request.param == "folder":
        return make_tree(tmp_path / "Diff Studio", DIFF_STUDIO)
    if request.param == "zip":
        return make_zip(tmp_path / "ds.zip", DIFF_STUDIO)
    return make_zip(tmp_path / "ds2.zip", DIFF_STUDIO, prefix="Diff Studio/")


def test_root_file_and_individual_files_are_listed_first_and_venv_is_skipped(diff_studio):
    cands, notes = discover_all(diff_studio)
    got = ids(cands)
    assert got[0] == "python:run:main.py" and "python:module:diffstudio" in got[:2]       # root entry first
    assert {"python:unittest", "python:pytest", "dotnet:build", "dotnet:restore-test"} <= set(got)   # build/test commands still there
    assert "python:run:tools/migrate.py" in got and "python:compileall" in got
    assert not any("helper" in i or "site-packages" in i or "tests" in i for i in got)
    text = "\n".join(notes)
    assert "Skipped virtual-environment" in text and "env" in text and "activate" not in text.lower()      # no more env/Scripts noise
    assert "Python entry points: main.py (opens a window)" in text


def test_gui_entry_is_flagged_and_syntax_check_offered(diff_studio):
    cands, _ = discover_all(diff_studio)
    main = next(c for c in cands if c.id == "python:run:main.py")
    assert main.argv == ["python", "./main.py"] and main.ecosystem == "python" and not main.needs_network
    assert any("no display" in r for r in main.risks) and any("window" in n for n in main.notes)
    comp = next(c for c in cands if c.id == "python:compileall")
    assert comp.argv[:3] == ["python", "-m", "compileall"] and "not run" in comp.description


def test_existing_discovery_is_unchanged_so_patch_verification_never_runs_programs(diff_studio):
    base, _ = discover_candidates(diff_studio)
    assert not any(":run" in c.id or c.id == "python:compileall" for c in base)
    from services.build_runner import BuildRunner
    assert not any("run:" in c.id for c in BuildRunner(str(diff_studio)).candidates())


def test_plain_script_and_third_party_variant(tmp_path):
    src = make_tree(tmp_path / "p", {"app.py": "import requests\nprint(requests)\n", "requirements.txt": "requests\n", "util/x.py": "if __name__ == '__main__':\n    pass\n"})
    cands, notes = discover_all(src)
    app = next(c for c in cands if c.id == "python:run:app.py")
    assert any("requests" in n for n in app.notes)                     # third-party import explained
    pipr = next(c for c in cands if c.id == "python:pip-run:app.py")
    assert pipr.needs_network and pipr.argv[:2] == ["sh", "-c"] and "-r requirements.txt" in pipr.argv[2] and "./app.py" in pipr.argv[2]
    assert "python:run:util/x.py" in ids(cands)


def test_named_entry_without_main_block_is_explained_and_helpers_are_not_offered(tmp_path):
    src = make_tree(tmp_path / "p", {"main.py": "x = 1\n", "lib.py": "def f():\n    pass\n", "setup.py": "if __name__ == '__main__':\n    pass\n",
                                     "tests/test_t.py": "if __name__ == '__main__':\n    pass\n", "conftest.py": ""})
    cands, _ = discover_all(src)
    got = ids(cands)
    assert "python:run:main.py" in got and "python:run:lib.py" not in got and "python:run:setup.py" not in got
    assert not any("test_t" in i for i in got)
    assert any("no 'if __name__" in n for n in next(c for c in cands if c.id == "python:run:main.py").notes)


def test_web_server_entry_is_marked_long_running(tmp_path):
    src = make_tree(tmp_path / "p", {"app.py": "from flask import Flask\napp = Flask(__name__)\nif __name__ == '__main__':\n    app.run()\n"})
    c = next(x for x in discover_all(src)[0] if x.id == "python:run:app.py")
    assert any("Flask" in n and "timeout" in n for n in c.notes) and not any("no display" in r for r in c.risks)


def test_pyproject_console_scripts_are_validated(tmp_path):
    toml = '[project]\nname = "x"\n[project.scripts]\ngood = "pkg.cli:main"\nbad1 = "pkg.cli:main; import os"\nbad2 = "os;system:x"\n"we ird" = "a.b:c"\n'
    src = make_tree(tmp_path / "p", {"pyproject.toml": toml, "pkg/__init__.py": "", "pkg/cli.py": "def main():\n    return 0\n"})
    cands, _ = discover_all(src)
    scripts = [c for c in cands if c.id.startswith("python:script:")]
    assert [c.id for c in scripts] == ["python:script:good"]
    assert scripts[0].argv[:2] == ["python", "-c"] and "from pkg.cli import main" in scripts[0].argv[2]


def test_hostile_file_names_never_become_shell_syntax(tmp_path):
    name = "run $(touch pwned).py"
    src = make_tree(tmp_path / "p", {name: "import numpy\nif __name__ == '__main__':\n    pass\n", "requirements.txt": "numpy\n"})
    cands, _ = discover_all(src)
    pip_run = next(c for c in cands if c.id.startswith("python:pip-run:"))
    assert "'./run $(touch pwned).py'" in pip_run.argv[2]            # quoted as one literal argument
    plain = next(c for c in cands if c.id.startswith("python:run:"))
    assert plain.argv == ["python", "./run $(touch pwned).py"]       # argv element, never parsed by a shell
    assert not (tmp_path / "pwned").exists()


def test_node_run_targets(tmp_path):
    pkg = {"name": "x", "main": "src/server.js", "scripts": {"start": "node src/server.js", "test": "jest", "build": "tsc", "lint": "eslint .",
                                                           "dev": "nodemon", "postinstall": "curl x | sh", "bad name!": "x", "deploy": "curl http://x | sh"}}
    src = make_tree(tmp_path / "n", {"package.json": json.dumps(pkg), "src/server.js": "console.log(1)", "index.js": "1"})
    cands, notes = discover_all(src)
    got = ids(cands)
    assert "node:run:src/server.js" in got and "node:npm-start" in got and "node:npm-install-start" in got
    assert "node:npm-run-lint" in got and "node:npm-run-dev" in got and "node:npm-run-deploy" in got
    assert not any("postinstall" in i or "bad name" in i for i in got)
    deploy = next(c for c in cands if c.id == "node:npm-run-deploy")
    assert any("curl" in r for r in deploy.risks)
    start = next(c for c in cands if c.id == "node:npm-start")
    assert any("postinstall" in r for r in start.risks)              # lifecycle hook risk is shown
    assert "node:npm-test" in got and "node:npm-build" in got


def test_dotnet_console_runs_but_desktop_apps_are_explained(tmp_path):
    console = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    winforms = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>WinExe</OutputType><UseWindowsForms>true</UseWindowsForms></PropertyGroup></Project>'
    lib = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    src = make_tree(tmp_path / "d", {"Cli/Cli.csproj": console, "Win/Win.csproj": winforms, "Lib/Lib.csproj": lib, "All.sln": ""})
    cands, notes = discover_all(src)
    got = ids(cands)
    assert "dotnet:run:Cli" in got and "dotnet:restore-run:Cli" in got and not any("Win" in i or "Lib" in i for i in got if i.startswith("dotnet:run") or "restore-run" in i)
    assert next(c for c in cands if c.id == "dotnet:restore-run:Cli").needs_network
    assert any("Windows desktop" in n and "Win/Win.csproj" in n for n in notes)


def test_java_root_program_and_other_language_notes(tmp_path):
    java = make_tree(tmp_path / "j", {"Main.java": "public class Main { public static void main(String[] a) { System.out.println(1); } }", "Util.java": "class Util {}"})
    cands, _ = discover_all(java)
    j = next(c for c in cands if c.id == "java:run:Main")
    assert j.ecosystem == "maven" and "javac -d /tmp/out ./*.java && java -cp /tmp/out Main" in j.argv[2]
    pk = make_tree(tmp_path / "j2", {"Main.java": "package a;\npublic class Main { public static void main(String[] a) {} }"})
    assert not any(c.id.startswith("java:run") for c in discover_all(pk)[0])
    other = make_tree(tmp_path / "o", {"go.mod": "module x", "main.go": "package main", "index.php": "<?php", "Cargo.toml": "[package]"})
    notes = "\n".join(discover_all(other)[1])
    assert "golang" in notes and "PHP" in notes and "Rust" in notes


def test_package_main_only_for_valid_names_and_empty_project(tmp_path):
    src = make_tree(tmp_path / "p", {"ok_pkg/__main__.py": "print(1)", "bad-name/__main__.py": "print(1)", "tests/__main__.py": "print(1)"})
    got = ids(discover_all(src)[0])
    assert "python:module:ok_pkg" in got and not any("bad-name" in i or "module:tests" in i for i in got)
    empty = make_tree(tmp_path / "e", {"README.md": "hi"})
    cands, notes = discover_all(empty)
    assert cands == [] and any("No build/test commands" in n for n in notes)


def test_venv_detected_by_pyvenv_cfg_any_name_and_not_walked(tmp_path):
    src = make_tree(tmp_path / "v", {"main.py": "print(1)", "myenv39/pyvenv.cfg": "x", "myenv39/Scripts/activate.bat": "x", "myenv39/lib/a.py": "if __name__ == '__main__':\n    pass\n"})
    view = ProjectView(src)
    assert view.venvs == ["myenv39"] and not any(n.startswith("myenv39/") for n in view.names)
    cands, notes = discover_all(src)
    assert ids(cands) == ["python:run:main.py", "python:compileall"] and any("myenv39" in n for n in notes)


def test_broken_and_hostile_inputs_do_not_crash(tmp_path):
    src = make_tree(tmp_path / "b", {"main.py": "def oops(:\n", "app.py": "\x00\x01", "run.py": "if __name__ == '__main__':\n" + "    x = 1\n" * 50000})
    cands, _ = discover_all(src)
    assert "python:run:main.py" in ids(cands)
    with pytest.raises(zipfile.BadZipFile):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        discover_all(bad)
    with pytest.raises(FileNotFoundError):
        discover_all(tmp_path / "missing.zip")


def test_nothing_is_executed_or_imported_during_discovery(tmp_path):
    marker = tmp_path / "executed.txt"
    code = f"open(r'{marker}', 'w').write('x')\nif __name__ == '__main__':\n    pass\n"
    src = make_tree(tmp_path / "p", {"main.py": code})
    discover_all(src)
    assert not marker.exists()


def test_candidates_plug_into_plans_with_the_right_image(diff_studio):
    cands, _ = discover_all(diff_studio)
    main = next(c for c in cands if c.id == "python:run:main.py")
    plan = build_plan(diff_studio, main, Options())
    assert plan.image == "python:3.12-slim" and plan.candidate.argv == ["python", "./main.py"] and not plan.limits.network
    net = next(c for c in cands if c.id == "dotnet:restore-test")
    assert build_plan(diff_studio, net, Options()).limits.network is True


def test_cli_candidates_lists_run_targets(diff_studio):
    out = subprocess.run([sys.executable, "-m", "plugins.build_execution", "candidates", str(diff_studio)], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[2], timeout=60)
    assert out.returncode == 0 and "python:run:main.py" in out.stdout and "python ./main.py" in out.stdout and "python:unittest" in out.stdout


def test_build_test_table_shows_run_targets(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from plugins.build_execution.ui_tab import BuildExecutionTab
        src = make_tree(tmp_path / "Diff Studio", DIFF_STUDIO)
        tab = BuildExecutionTab(root, get_source=lambda: str(src))
        tab._discover()
        rows = [tab.tree.item(i)["values"] for i in tab.tree.get_children()]
        assert rows[0][0] == "python:run:main.py" and rows[0][2] == "python ./main.py" and rows[0][1] == "no"
        assert any(r[0] == "python:unittest" for r in rows) and any(r[0] == "python:run:tools/migrate.py" for r in rows)
        assert "Skipped virtual-environment" in tab.out.get("1.0", "end")
        tab.destroy()
    finally:
        root.destroy()
