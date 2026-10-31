# Phase 3 plugin - Language-Specific Static Analysis

Self-contained: it imports nothing from your Phase 1/2 code, and your code imports it. Nothing existing is modified.

## Install (3 steps)
1. Copy the `plugins/` folder into the project root (next to `app.py`). Optional: copy `tests/test_static_analysis_plugin.py`.
2. No new Python dependencies (standard library only). External analyzers are optional and are never installed by the plugin.
3. Hook it into the app - pick one:

```python
# A) Tkinter tab (in app.py / main_window.py, where your ttk.Notebook exists)
from plugins.static_analysis import attach_tab
attach_tab(notebook, get_source=lambda: self.current_zip_path)   # get_source optional

# B) Code only (no GUI) - e.g. from your orchestrator
from plugins.static_analysis import analyze, Options
report = analyze(zip_or_folder, Options(), progress=print)
report.findings      # list[Finding]  (severity, kind, file, line, column, rule, confidence, ...)
report.tool_runs     # list[ToolRun]  (analyzer, version, command, exit_code, stdout, stderr, status)
report.write_json("reports/static.json"); report.write_html("reports/static.html")

# C) CLI
python -m plugins.static_analysis project.zip --json out.json --html out.html
python -m plugins.static_analysis --list-tools
```
If your findings model is different, map `Finding.to_dict()` into it.

## Analyzers
| Language | Tools | Notes |
|---|---|---|
| Python | `ast` (built in), Ruff, Pyright (optional) | `ast.parse` only: nothing is imported or run. Ruff/Pyright project config is ignored. |
| JavaScript | ESLint 8/9 | Project ESLint config is ignored (it can execute code). Small built-in rule set. |
| TypeScript | `tsc --noEmit` | Uses the project's `tsconfig.json` if present, else a generated one. |
| C# / VB.NET | `dotnet build` | **Skipped unless approved** (`Options(approved_tools={"dotnet"})` / UI checkbox). Never restores packages. |
| Java | `javac` | `-proc:none`, no classpath supplied. |
| SQL | sqlfluff (optional) | Dialect option; raw templater; project config ignored. |

Add your own: subclass `plugins.static_analysis.Adapter` (set `name`, `languages`, implement `run(ctx)`) and call `register_adapter(MyAdapter)`.

## Honesty rules built in
- Every finding has a `kind`: `parser_error`, `compiler_error`, `compiler_warning`, `type_error`, `lint_warning`, `heuristic`. Lint output is never labelled a compiler error.
- Missing tools => `ToolRun.status == "unavailable"` with an install hint. Nothing is faked, nothing is installed.
- Unresolved import/symbol errors (dependencies are not installed) get `confidence = "medium"`.
- `verification` is always `tool-reported`; this plugin never claims a finding is confirmed or fixed.

## Safety
- ZIPs: path traversal / absolute paths / bomb ratio / size and entry limits reject the archive; symlinks and encrypted members skipped; only source + manifest file types are written, to a temp folder that is deleted afterwards.
- Tools run without a shell, no stdin, hard timeout, cancellable, no console window on Windows, with credential-looking env vars (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*AUTH*`...) removed.

## Known limitations
- Tkinter tab compiles but was NOT exercised in a GUI here (no Tkinter/display in the build sandbox).
- `javac` and `dotnet` paths were verified only through parser unit tests on sample output (neither is installed in the build sandbox).
- Approved `dotnet build` still runs on the host; real isolation is Phase 4.
- Tool stdout/stderr stored in reports may echo source lines; secret redaction is Phase 5.
- No cross-tool de-duplication beyond Python syntax errors.
