"""C#/VB.NET (dotnet build, approval required), Java (javac), SQL (sqlfluff)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Adapter, Context, find_tool, make_run, rel_path, run_command, tool_version, unavailable
from .models import Finding, ToolRun

# ---------------------------------------------------------------------------------------------
# .NET
# ---------------------------------------------------------------------------------------------
MSBUILD_LOC = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)(?:,\d+,\d+)?\): (?P<sev>error|warning) "
                         r"(?P<code>[A-Za-z]+\d+): (?P<msg>.*?)(?: \[(?P<proj>[^\]]+)\])?$")
MSBUILD_NOLOC = re.compile(r"^(?P<file>.+?) ?: (?P<sev>error|warning) (?P<code>[A-Za-z]+\d+): (?P<msg>.*?)"
                           r"(?: \[(?P<proj>[^\]]+)\])?$")


def parse_msbuild(text: str, root: Path, ver: str) -> list:
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        m = MSBUILD_LOC.match(line)
        has_loc = bool(m)
        if not m:
            m = MSBUILD_NOLOC.match(line)
        if not m:
            continue
        err = m.group("sev") == "error"
        code = m.group("code")
        lang = "vbnet" if code.upper().startswith("BC") else "csharp"
        f = Finding(
            analyzer="dotnet-build", analyzer_version=ver, kind="compiler_error" if err else "compiler_warning",
            severity="high" if err else "low", message=m.group("msg"), file=rel_path(root, m.group("file")),
            line=int(m.group("line")) if has_loc else None, column=int(m.group("col")) if has_loc else None,
            rule=code, language=lang, confidence="high" if has_loc else "medium",
            evidence="" if has_loc else "Project-level diagnostic (no source location).")
        key = (f.file, f.line, f.column, f.rule, f.message)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


class DotNetAdapter(Adapter):
    name = "dotnet"
    languages = ("csharp", "vbnet")

    def run(self, ctx: Context):
        if "dotnet" not in ctx.options.approved_tools:
            return [], [ToolRun(
                analyzer="dotnet-build", status="skipped",
                note="Skipped: 'dotnet build' executes MSBuild targets/tasks and build events from the uploaded "
                     "project, so it needs explicit approval (Options.approved_tools={'dotnet'}). Without an "
                     "isolated runner (Phase 4) approved builds run on this machine.")]
        exe = find_tool("dotnet", ctx.options)
        if not exe:
            return [], [unavailable("dotnet-build", "dotnet", "Install the .NET SDK from https://dotnet.microsoft.com/download")]
        ver = tool_version(exe, ctx.root, ctx.cancel)
        slns = sorted(ctx.root.glob("*.sln"))
        projs = sorted(p for p in ctx.root.rglob("*") if p.suffix.lower() in (".csproj", ".vbproj"))
        targets = slns[:1] if len(slns) == 1 else projs[:10]
        if not targets:
            return [], [ToolRun(analyzer="dotnet-build", status="skipped", version=ver,
                                note="No .sln/.csproj/.vbproj found; a bare .cs/.vb file cannot be built by itself.")]
        findings, runs = [], []
        for t in targets:
            if ctx.cancel.is_set():
                break
            cmd = [exe, "build", str(t), "--no-restore", "-nologo", "-v:q", "-clp:NoSummary"]
            res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
            text = res.stdout + "\n" + res.stderr
            found = parse_msbuild(text, ctx.root, ver) if res.outcome == "done" else []
            note = ""
            status = ""
            if "NETSDK1004" in text or "project.assets.json" in text:
                status, note = "error", ("Packages are not restored. This tool never restores/installs packages; "
                                         "run 'dotnet restore' yourself in a safe environment, then re-run.")
            elif res.outcome == "done" and res.exit_code not in (0, 1) and not found:
                status, note = "error", "dotnet build failed without compiler diagnostics; see stderr."
            elif res.outcome == "done":
                status = "findings" if found else "ok"
            runs.append(make_run("dotnet-build", res, cmd, ver, status=status, note=note))
            findings.extend(found)
        return self.cap(findings, ctx, runs), runs


# ---------------------------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------------------------
JAVAC_LINE = re.compile(r"^(?P<file>.+?\.java):(?P<line>\d+): (?P<sev>error|warning): (?P<msg>.*)$")
SYMBOL_HINT = re.compile(r"(does not exist|cannot find symbol|cannot access)", re.I)


def parse_javac(text: str, root: Path, ver: str) -> list:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = JAVAC_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        src, caret, j = "", None, i + 1
        # javac prints the source line, then a caret line (and sometimes "symbol:" detail lines)
        extra = []
        while j < len(lines) and not JAVAC_LINE.match(lines[j]) and not re.match(r"^\d+ (error|warning)s?$", lines[j]):
            ln = lines[j]
            if caret is None and ln.strip() == "^":
                caret = ln.index("^") + 1
            elif caret is None and not src:
                src = ln.strip()
            else:
                extra.append(ln.strip())
            j += 1
        err = m.group("sev") == "error"
        unresolved = bool(SYMBOL_HINT.search(m.group("msg")))
        msg = m.group("msg") + ((" " + " ".join(x for x in extra if x)) if extra else "")
        out.append(Finding(
            analyzer="javac", analyzer_version=ver, kind="compiler_error" if err else "compiler_warning",
            severity="high" if err and not unresolved else "medium" if err else "low", message=msg.strip(),
            file=rel_path(root, m.group("file")), line=int(m.group("line")), column=caret, language="java",
            confidence="medium" if unresolved else "high",
            evidence=src if not unresolved else (src + "  [likely missing classpath/dependency]").strip()))
        i = j
    return out


class JavaAdapter(Adapter):
    name = "java"
    languages = ("java",)

    def run(self, ctx: Context):
        exe = find_tool("javac", ctx.options)
        if not exe:
            return [], [unavailable("javac", "javac", "Install a JDK (e.g. Temurin/OpenJDK 17+).")]
        ver = tool_version(exe, ctx.root, ctx.cancel, ("-version",))
        files = ctx.files.get("java", [])
        argfile = ctx.scratch / "javac.args"
        argfile.write_text("\n".join('"' + str(f).replace("\\", "\\\\") + '"' for f in files), encoding="utf-8")
        outdir = ctx.scratch / "javac_out"
        outdir.mkdir(exist_ok=True)
        cmd = [exe, "-proc:none", "-Xlint:all", "-Xmaxerrs", "500", "-encoding", "UTF-8", "-d", str(outdir),
               "@" + str(argfile)]
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            return [], [make_run("javac", res, cmd, ver)]
        found = parse_javac(res.stderr + "\n" + res.stdout, ctx.root, ver)
        status = "findings" if found else ("ok" if res.exit_code == 0 else "error")
        run = make_run("javac", res, cmd, ver, status=status, files_checked=len(files),
                       note="-proc:none (annotation processors are not run); no classpath is supplied, so missing "
                            "dependencies surface as reduced-confidence errors.")
        return self.cap(found, ctx, [run]), [run]


# ---------------------------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------------------------
class SqlAdapter(Adapter):
    name = "sql"
    languages = ("sql",)

    def run(self, ctx: Context):
        exe = find_tool("sqlfluff", ctx.options)
        if not exe:
            return [], [unavailable("sqlfluff", "sqlfluff", "Optional. Install with: pip install sqlfluff")]
        ver = tool_version(exe, ctx.root, ctx.cancel)
        dialect = ctx.options.sql_dialect
        cmd = [exe, "lint", "--dialect", dialect, "--templater", "raw", "--ignore-local-config", "--format", "json",
               "--nocolor", "--exclude-rules", "layout,capitalisation,aliasing,convention", "."]
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            return [], [make_run("sqlfluff", res, cmd, ver)]
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            return [], [make_run("sqlfluff", res, cmd, ver, status="error", note="Unparseable sqlfluff output; see stderr.")]
        found = []
        for fr in data:
            rel = rel_path(ctx.root, fr.get("filepath", ""))
            for v in fr.get("violations", []):
                code = v.get("code", "")
                prs = code == "PRS"
                found.append(Finding(
                    analyzer="sqlfluff", analyzer_version=ver, kind="parser_error" if prs else "lint_warning",
                    severity="high" if prs else "low", message=v.get("description", ""), file=rel,
                    line=v.get("start_line_no"), column=v.get("start_line_pos"), rule=code, language="sql",
                    confidence="medium" if prs else "high",
                    evidence=f"Parsed with dialect '{dialect}'; a parse failure may just mean a different SQL dialect."
                    if prs else ""))
        status = "findings" if found else ("ok" if res.exit_code in (0, 1) else "error")
        run = make_run("sqlfluff", res, cmd, ver, status=status, files_checked=len(ctx.files.get("sql", [])),
                       note=f"Dialect '{dialect}', raw templater, project config ignored; style rule groups excluded.")
        return self.cap(found, ctx, [run]), [run]
