"""JavaScript / TypeScript: ESLint (project config ignored) and the TypeScript compiler (--noEmit)."""
from __future__ import annotations

import json
import re

from .base import Adapter, Context, find_tool, make_run, rel_path, run_command, tool_version, unavailable
from .models import Finding, ToolRun

IGNORES = ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.git/**", "**/vendor/**", "**/*.min.js"]
RULES = {
    "no-dupe-keys": "error", "no-dupe-args": "error", "no-unreachable": "error", "no-const-assign": "error",
    "no-redeclare": "error", "no-func-assign": "error", "no-self-assign": "warn", "no-unsafe-finally": "error",
    "no-cond-assign": "error", "no-dupe-else-if": "error", "no-duplicate-case": "error", "use-isnan": "error",
    "valid-typeof": "error", "no-import-assign": "error", "no-unused-vars": "warn",
}
TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) "
                      r"(?P<code>TS\d+): (?P<msg>.*)$")
UNRESOLVED_TS = {"TS2307", "TS2792", "TS7016", "TS2304", "TS2580", "TS2584", "TS2688"}


class JavaScriptAdapter(Adapter):
    name = "javascript"
    languages = ("javascript", "typescript")

    def run(self, ctx: Context):
        findings: list = []
        runs: list = []
        if ctx.files.get("javascript"):
            self._eslint(ctx, findings, runs)
        if ctx.files.get("typescript") and not ctx.cancel.is_set():
            self._tsc(ctx, findings, runs)
        return findings, runs

    # ---- ESLint -----------------------------------------------------------------------------
    def _eslint(self, ctx, findings, runs):
        exe = find_tool("eslint", ctx.options)
        if not exe:
            runs.append(unavailable("eslint", "eslint", "Install in your own environment, e.g. npm i -g eslint"))
            return
        ver = tool_version(exe, ctx.root, ctx.cancel)
        m = re.search(r"(\d+)\.", ver)
        major = int(m.group(1)) if m else 0
        if major >= 9:
            cfg = ctx.scratch / "eslint.config.mjs"
            cfg.write_text(
                "export default [{files:['**/*.js','**/*.jsx','**/*.mjs'],languageOptions:{ecmaVersion:'latest',"
                "sourceType:'module',parserOptions:{ecmaFeatures:{jsx:true}}},rules:" + json.dumps(RULES) + "},"
                "{files:['**/*.cjs'],languageOptions:{ecmaVersion:'latest',sourceType:'commonjs'},rules:"
                + json.dumps(RULES) + "},{ignores:" + json.dumps(IGNORES) + "}];", encoding="utf-8")
            cmd = [exe, "--no-config-lookup", "-c", str(cfg), "--format", "json", "--no-warn-ignored", "."]
        elif major == 8:
            cfg = ctx.scratch / "eslintrc.json"
            cfg.write_text(json.dumps({"root": True, "env": {"es2022": True},
                                       "parserOptions": {"ecmaVersion": "latest", "sourceType": "module",
                                                         "ecmaFeatures": {"jsx": True}}, "rules": RULES}),
                           encoding="utf-8")
            cmd = [exe, "--no-eslintrc", "-c", str(cfg), "--format", "json", "--ext", ".js,.jsx,.mjs,.cjs",
                   "--ignore-pattern", "node_modules", "--ignore-pattern", "dist", "--ignore-pattern", "build", "."]
        else:
            runs.append(ToolRun(analyzer="eslint", status="skipped", version=ver,
                                note=f"Unsupported ESLint version '{ver}' (need 8 or 9+)."))
            return
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            runs.append(make_run("eslint", res, cmd, ver))
            return
        try:
            data = json.loads(res.stdout or "[]")
            if res.exit_code not in (0, 1):
                raise ValueError("eslint exit code %s" % res.exit_code)
        except (json.JSONDecodeError, ValueError):
            runs.append(make_run("eslint", res, cmd, ver, status="error",
                                 note="ESLint failed or produced unparseable output; see stderr."))
            return
        new = []
        for fr in data:
            rel = rel_path(ctx.root, fr.get("filePath", ""))
            for msg in fr.get("messages", []):
                fatal = msg.get("fatal") or msg.get("ruleId") is None
                if fatal:
                    new.append(Finding(
                        analyzer="eslint", analyzer_version=ver, kind="parser_error", severity="high",
                        message=msg.get("message", ""), file=rel, line=msg.get("line"), column=msg.get("column"),
                        rule="parse-error", language="javascript", confidence="medium",
                        evidence="Parsed as an ES module (CommonJS for .cjs); a classic script may be a false positive."))
                else:
                    new.append(Finding(
                        analyzer="eslint", analyzer_version=ver, kind="lint_warning",
                        severity="medium" if msg.get("severity") == 2 else "low", message=msg.get("message", ""),
                        file=rel, line=msg.get("line"), column=msg.get("column"), rule=msg.get("ruleId") or "",
                        language="javascript", confidence="high"))
        runs.append(make_run("eslint", res, cmd, ver, status="findings" if new else "ok",
                             note="Project ESLint config is ignored and not executed; small built-in rule set. "
                                  "Type-aware and plugin rules are not applied.",
                             files_checked=len(ctx.files.get("javascript", []))))
        findings.extend(self.cap(new, ctx, runs))

    # ---- tsc --------------------------------------------------------------------------------
    def _tsc(self, ctx, findings, runs):
        exe = find_tool("tsc", ctx.options)
        if not exe:
            runs.append(unavailable("tsc", "tsc", "Install in your own environment, e.g. npm i -g typescript"))
            return
        ver = tool_version(exe, ctx.root, ctx.cancel)
        ts_files = ctx.files["typescript"]
        tsconfig = ctx.root / "tsconfig.json"
        if tsconfig.exists():
            cmd = [exe, "--noEmit", "--pretty", "false", "-p", str(tsconfig)]
            mode = "project tsconfig.json"
        else:
            cfg = ctx.scratch / "tsconfig.json"
            mm = re.search(r"(\d+)\.", ver)
            resolution = "bundler" if mm and int(mm.group(1)) >= 5 else "node"  # 'node' is deprecated in TS 6
            cfg.write_text(json.dumps({
                "compilerOptions": {"noEmit": True, "skipLibCheck": True, "target": "ES2020", "module": "ESNext",
                                    "moduleResolution": resolution, "jsx": "preserve", "types": []},
                "files": [str(f) for f in ts_files]}), encoding="utf-8")
            cmd = [exe, "--pretty", "false", "-p", str(cfg)]
            mode = "generated tsconfig (no tsconfig.json in project)"
        res = run_command(cmd, ctx.root, ctx.options.timeout_s, ctx.cancel)
        if res.outcome != "done":
            runs.append(make_run("tsc", res, cmd, ver))
            return
        new: list = []
        last = None
        cfg_errors: list = []
        generated = not tsconfig.exists()
        for line in (res.stdout + "\n" + res.stderr).splitlines():
            m = TSC_LINE.match(line)
            if m and generated and m.group("file").replace("\\", "/").endswith("tsconfig.json"):
                cfg_errors.append(line.strip())  # error in OUR generated config, not in the project
                last = None
                continue
            if m:
                code = m.group("code")
                num = int(code[2:])
                err = m.group("sev") == "error"
                unresolved = code in UNRESOLVED_TS
                last = Finding(
                    analyzer="tsc", analyzer_version=ver,
                    kind=("parser_error" if num < 2000 else "compiler_error") if err else "compiler_warning",
                    severity="high" if err and not unresolved else "medium" if err else "low",
                    message=m.group("msg"), file=rel_path(ctx.root, m.group("file")),
                    line=int(m.group("line")), column=int(m.group("col")), rule=code, language="typescript",
                    confidence="medium" if unresolved else "high",
                    evidence="Likely an unresolved dependency/type declaration (packages are not installed by this tool)."
                    if unresolved else "")
                new.append(last)
            elif last is not None and line.startswith(" "):
                last.message += " " + line.strip()
            else:
                last = None
        note = (f"--noEmit using {mode}. Dependencies are not installed, so unresolved-module "
                "errors are reported with reduced confidence.")
        status = "findings" if new else ("ok" if res.exit_code == 0 else "error")
        if cfg_errors:
            status, note = "error", "tsc rejected the generated configuration: " + " | ".join(cfg_errors)
        runs.append(make_run("tsc", res, cmd, ver, status=status, note=note, files_checked=len(ts_files)))
        findings.extend(self.cap(new, ctx, runs))
