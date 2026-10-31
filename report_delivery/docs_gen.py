"""Generates documentation from the real project files (AST parsing only - nothing is imported or run)."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


def architecture_markdown(files: Dict[str, bytes]) -> str:
    L = ["# Architecture overview", "",
         "_Auto-generated from the packaged source files (AST scan; code was not executed). "
         "Replace with a hand-written document for design rationale._", "", "## Layout", "```"]
    dirs = sorted({p.rsplit("/", 1)[0] if "/" in p else "." for p in files})
    for d in dirs:
        L.append(f"{d}/  ({sum(1 for p in files if (p.rsplit('/', 1)[0] if '/' in p else '.') == d)} files)")
    L += ["```", "", "## Modules"]
    for path in sorted(files):
        if not path.endswith(".py") or path.startswith(("tests/", "test/")) or "/tests/" in path:
            continue
        try:
            tree = ast.parse(files[path].decode("utf-8-sig", "replace"))
        except SyntaxError:
            L.append(f"- `{path}` - could not be parsed (syntax error)")
            continue
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
        L.append(f"- `{path}` - {doc[0] if doc else 'no module docstring'}")
        if classes:
            L.append(f"  - classes: {', '.join(classes[:12])}{' ...' if len(classes) > 12 else ''}")
        if funcs:
            L.append(f"  - public functions: {', '.join(funcs[:12])}{' ...' if len(funcs) > 12 else ''}")
    return "\n".join(L) + "\n"


def installation_markdown(requirements: str, python_hint: str) -> str:
    reqs = [l.strip() for l in requirements.splitlines() if l.strip() and not l.strip().startswith("#")]
    L = ["# Installation (Windows 10/11)", "", f"1. Install Python {python_hint} from python.org (tick *Add python.exe to PATH*; keep *tcl/tk* selected).",
         "2. Open Command Prompt in the extracted folder.", "3. Create and activate a virtual environment:", "   ```",
         "   python -m venv .venv", "   .venv\\Scripts\\activate", "   ```", "4. Install dependencies:", "   ```",
         "   python -m pip install -r requirements.txt", "   ```", "5. Run: `scripts\\run.bat`   Test: `scripts\\test.bat`", "",
         "## Declared dependencies"] + ([f"- {r}" for r in reqs] or ["- none (standard library only)"])
    return "\n".join(L) + "\n"


def user_guide_markdown(readme: str, has_run: bool) -> str:
    head = "\n".join(readme.strip().splitlines()[:40]) if readme else "_No README was found in the project._"
    return ("# User guide\n\n_Auto-generated starter - extend with screenshots and workflows._\n\n"
            "## Starting the application\n- Run `scripts\\run.bat`" + ("" if has_run else " (generated)") +
            " or `python app.py`.\n\n## Project README (excerpt)\n\n" + head + "\n")


def known_issues_markdown(issues: Sequence[str]) -> str:
    body = [f"- {i}" for i in issues] if issues else ["- None recorded. This does not mean none exist; see the analysis report's limitations."]
    return "# Known issues\n\n" + "\n".join(body) + "\n"


def changelog_stub(version: str) -> str:
    return (f"# Changelog\n\n## {version}\n- Delivery package created. No earlier change history was found in the project, "
            "so none is claimed here.\n")


RUN_BAT = "@echo off\r\nREM Starts the application (run from the package root)\r\npython app.py %*\r\n"
TEST_BAT = "@echo off\r\nREM Runs the automated tests\r\npython -m pip install pytest\r\npython -m pytest -q\r\n"
