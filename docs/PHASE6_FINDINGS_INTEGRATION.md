# findings_plugin - Phase 6 drop-in (Findings, error explanation, debugging)

Standard library only. Python 3.11+ (developed on 3.12). Tkinter is needed only for the UI panel.

## Install (2 steps)
1. Copy the `findings_plugin/` folder next to your `app.py` (project root).
2. In `app.py`, after your `ttk.Notebook` exists:

```python
from findings_plugin import register

plugin = register(notebook,                       # adds a "Findings" tab
                  on_open_file=open_in_viewer)    # optional: (path, line) -> None

# feed it from your existing Phase 1 analyzer (dicts / objects / SyntaxError / str all work):
added = plugin.add(analyzer_results, source="analyzer.py")
if plugin.last_errors:                            # items that could not be converted (never dropped silently)
    print(plugin.last_errors)

# after a run / test command:
plugin.analyze_log_file("run.log")                # or plugin.analyze_log_text(text, "pytest")
```
`register()` with no argument is headless (no Tk needed). Updating the store from a worker thread is safe;
the panel refreshes on the Tk thread.

## Report integration (Phase 1 / 10 generators)
`plugin.html_section("Findings")` returns an escaped HTML fragment; `plugin.export_json(path)` / `export_html(path)` write files.

## Phase 7 / 8 hooks (already provided, not implemented here)
* `make_ai_finding(...)` - always `kind=ai_hypothesis`, confidence `weak`, never verified; shown with a notice in the UI and HTML.
* `register(..., on_request_ai=callback)` adds an "Ask AI to explain" button (callback receives the Finding).
* `store.set_verification(id, Verification.VERIFIED, evidence="pytest ... -> 12 passed")` - evidence text is mandatory.

## Mapping to the master-prompt layout (if you prefer to split it up)
models.py -> models/finding.py | store.py + logparse.py + knowledge.py -> core/ | ui/findings_view.py -> ui/ | report.py -> core/report_generator.py

## Run / test (Windows)
    python -m findings_plugin --log run.log --json out.json --html out.html
    python -m findings_plugin --demo --ui        # labelled demo rows, UI preview only
    python -m pytest findings_plugin\tests -q    # UI smoke test auto-skips without a display

## Limits
* Log parsing recognises Python, Java, .NET and Node.js traces, pytest FAILED/ERROR lines and generic ERROR/CRITICAL/FATAL lines. Other formats are not parsed.
* Guidance text comes from a built-in table keyed on exception names - general advice, labelled as such in every finding, not an analysis of your code.
* File/line values are copied from the log text only; if the log has none, they stay empty.
* Secret redaction is pattern-based (password/token/key/bearer/connection-string); it is not a guarantee.
