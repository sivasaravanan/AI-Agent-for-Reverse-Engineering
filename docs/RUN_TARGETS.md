# Run targets in Build & Test

Build & Test lists, for the project chosen in the toolbar (ZIP, tar or folder), every command it can offer. Besides the build/test
commands taken from manifests it now also understands **how the project is started**:

| Shown as | Meaning |
|---|---|
| `python:run:main.py` | root entry file (`main.py`, `app.py`, `run.py`, ...) or any file with an `if __name__ == "__main__"` block, run as an individual file |
| `python:module:<package>` | `python -m <package>` for a root package that has `__main__.py` |
| `python:script:<name>` | a console script declared in `pyproject.toml` (`module:function`, validated) |
| `python:pip-run:<file>` | same file, after installing `requirements.txt` into the container (needs network; only offered when third-party imports are found) |
| `python:compileall` | syntax-check every `.py` file; nothing is run (useful for GUI-only projects) |
| `node:run:<file>`, `node:npm-start`, `node:npm-run-<script>` | Node entry file, `start` script, other declared scripts (lifecycle hooks and unsafe names are never offered) |
| `dotnet:run:<project>` / `dotnet:restore-run:<project>` | console/web projects (`OutputType Exe`); Windows desktop projects are explained, not offered |
| `java:run:<Class>` | single-file programs in the project root (`javac` + `java`) |

Root entry files are listed first, then the build/test commands, then other runnable files. Rows keep the same columns (Id, Network, Command, Risks).

What the list does *not* do:
- Nothing is executed, imported or extracted while listing. Python files are only parsed; text is only read.
- Virtual-environment folders (any folder holding `pyvenv.cfg`, e.g. `env/`, `.venv/`) are skipped entirely, so their `activate` scripts and packages no longer appear.
- Every run still needs your approval of the exact plan and happens only in the disposable container (no host fallback).
  GUI programs (tkinter, Qt, ...) cannot show a window there and wait until the timeout; the row says so in its Risks column.
- Go, PHP and Rust projects are recognised, but no default image exists: the notes tell you which image and custom command to use.
- Patch verification and reports keep using only build/test commands (`discover_candidates` is unchanged), so a program is never started automatically.

Command line: `python -m plugins.build_execution candidates <zip|folder>` lists the same commands; `run <zip|folder> --candidate python:run:main.py` plans one.
