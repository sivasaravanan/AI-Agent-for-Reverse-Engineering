"""Python: in-process ast parsing (never imports/executes code), Ruff, optional Pyright."""
from __future__ import annotations

import ast
import json
import os
import platform
import re
import time
import warnings

from .base import (Adapter, Context, find_tool, make_run, rel_path, run_command, tool_version,
                   unavailable, RunResult)
from .models import Finding, ToolRun

EXCLUDES = ["node_modules", "venv", ".venv", "env", "__pycache__", ".git", "build", "dist", "site-packages"]


class PythonAdapter(Adapter):
    name = "python"
    languages = ("python",)

    def run(self, ctx: Context):
        findings: list = []
        runs: list = []
        files = ctx.files.get("python", [])
        self._ast(ctx, files, findings, runs)
        if not ctx.cancel.is_set():
            self._ruff(ctx, files, findings, runs)
        if not ctx.cancel.is_set() and ctx.options.use_pyright:
            self._pyright(ctx, files, findings, runs)
        return findings, runs

    # ---- ast (parser errors - confirmed for the host Python grammar) -------------------------
    def _ast(self, ctx, files, findings, runs):
        t0 = time.monotonic()
        checked, skipped, new = 0, 0, []
        ver = f"Python {platform.python_version()} ast"
        for f in files:
            if ctx.cancel.is_set():
                break
            try:
                if f.stat().st_size > ctx.options.max_file_bytes:
                    skipped += 1
                    continue
                data = f.read_bytes()
            except OSError:
                skipped += 1
                continue
            rel = rel_path(ctx.root, f)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    ast.parse(data, filename=rel)
                checked += 1
            except SyntaxError as exc:
                checked += 1
                new.append(Finding(
                    analyzer="python-ast", analyzer_version=ver, kind="parser_error", severity="high",
                    message=f"{exc.msg}", file=rel, line=exc.lineno, column=exc.offset,
                    rule=type(exc).__name__, language="python", confidence="high",
                    evidence=(exc.text or "").strip()[:200]))
            except (ValueError, RecursionError, MemoryError) as exc:
                checked += 1
                new.append(Finding(
                    analyzer="python-ast", analyzer_version=ver, kind="parser_error", severity="high",
                    message=f"Could not parse file: {exc}", file=rel, rule=type(exc).__name__,
                    language="python", confidence="medium"))
        findings.extend(new)
        note = "In-process ast.parse only; no project code is imported or run. " \
               f"Grammar is that of the host interpreter (Python {platform.python_version()})."
        if skipped:
            note += f" {skipped} file(s) skipped (too large or unreadable)."
        runs.append(ToolRun(analyzer="python-ast", status="findings" if new else "ok",
                            command=["ast.parse(<file>)  # in-process"], version=ver,
                            duration_s=round(time.monotonic() - t0, 3), note=note, files_checked=checked))

    # ---- ruff -------------------------------------------------------------------------------
    def _ruff(self, ctx, files, findings, runs):
        exe = find_tool("ruff", ctx.options)
        if not exe:
            runs.append(unavailable("ruff", "ruff", "Install with: pip install ruff"))
            return
        ver = tool_version(exe, ctx.root, ctx.cancel)
        cmd = [exe, "check", "--isolated", "--no-cache", "--output-format", "json",
               "--select", "E4,E7,E9,F", "--extend-exclude",
               ",".join(EXCLUDES), "."]
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            runs.append(make_run("ruff", res, cmd, ver))
            return
        try:
            items = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            runs.append(make_run("ruff", res, cmd, ver, status="error",
                                 note="Could not parse ruff JSON output (see stderr)."))
            return
        if res.exit_code not in (0, 1):
            runs.append(make_run("ruff", res, cmd, ver, status="error", note="ruff reported an internal/usage error."))
            return
        new = []
        ast_bad = {f.file for f in findings if f.analyzer == "python-ast"}
        for it in items:
            code = it.get("code")
            loc = it.get("location") or {}
            if not code or not re.match(r"^[A-Z]+\d+$", code):  # ruff reports syntax errors without a rule code
                if rel_path(ctx.root, it.get("filename", "")) in ast_bad:
                    continue  # already reported (more precisely) by ast
                kind, sev, rule = "parser_error", "high", "syntax-error"
            else:
                kind, rule = "lint_warning", code
                sev = "high" if code.startswith("E9") else "medium" if code.startswith("F") else "low"
            new.append(Finding(
                analyzer="ruff", analyzer_version=ver, kind=kind, severity=sev, message=it.get("message", ""),
                file=rel_path(ctx.root, it.get("filename", "")), line=loc.get("row"), column=loc.get("column"),
                rule=rule, language="python", confidence="high"))
        runs.append(make_run("ruff", res, cmd, ver, status="findings" if new else "ok",
                             note="Run with --isolated: project ruff config is ignored; default rule set.",
                             files_checked=len(files)))
        findings.extend(self.cap(new, ctx, runs))

    # ---- pyright ----------------------------------------------------------------------------
    def _pyright(self, ctx, files, findings, runs):
        exe = find_tool("pyright", ctx.options)
        if not exe:
            runs.append(unavailable("pyright", "pyright", "Optional. Install with: pip install pyright"))
            return
        ver = tool_version(exe, ctx.root, ctx.cancel)
        cfg = ctx.scratch / "pyrightconfig.json"
        try:  # pyright only accepts include paths relative to the config file
            inc = os.path.relpath(ctx.root, ctx.scratch).replace("\\", "/")
        except ValueError:
            runs.append(ToolRun(analyzer="pyright", status="skipped", version=ver,
                                note="Project is on a different drive than the scratch folder; pyright skipped."))
            return
        cfg.write_text(json.dumps({
            "include": [inc], "exclude": [f"**/{e}" for e in EXCLUDES],
            "typeCheckingMode": "basic", "reportMissingImports": "none",
            "reportMissingModuleSource": "none"}), encoding="utf-8")
        cmd = [exe, "--outputjson", "--project", str(cfg)]
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            runs.append(make_run("pyright", res, cmd, ver))
            return
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            runs.append(make_run("pyright", res, cmd, ver, status="error",
                                 note="Could not parse pyright JSON output (see stdout/stderr)."))
            return
        new = []
        ast_bad = {f.file for f in findings if f.analyzer == "python-ast"}
        for d in data.get("generalDiagnostics", []):
            sev = d.get("severity", "information")
            if sev == "information":
                continue
            relf = rel_path(ctx.root, d.get("file", ""))
            if not d.get("rule") and relf in ast_bad:
                continue  # pyright syntax diagnostics duplicate the ast parser error already reported
            start = (d.get("range") or {}).get("start") or {}
            new.append(Finding(
                analyzer="pyright", analyzer_version=ver,
                kind=("parser_error" if not d.get("rule") else "type_error") if sev == "error" else "lint_warning",
                severity="high" if (sev == "error" and not d.get("rule")) else "medium" if sev == "error" else "low",
                message=d.get("message", ""), file=relf,
                line=start.get("line", -1) + 1 if "line" in start else None,
                column=start.get("character", -1) + 1 if "character" in start else None,
                rule=d.get("rule", ""), language="python", confidence="medium",
                evidence="Type-checker result; missing third-party imports are not reported."))
        runs.append(make_run("pyright", res, cmd, ver, status="findings" if new else "ok",
                             note="Basic mode, project pyright config ignored, unresolved imports suppressed.",
                             files_checked=len(files)))
        findings.extend(self.cap(new, ctx, runs))
