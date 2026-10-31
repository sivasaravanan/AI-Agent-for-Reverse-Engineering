# Engineering Analysis Summary - db_sample_project

## Overview
- Findings: critical 0, high 6, medium 1, low 1, info 2
- Dependencies: 0  |  Static analyzers run: 0
- Build/test runs: none provided
- Section coverage: inventory=provided, dependencies=not-provided, static_analysis=not-provided, build_test=not-provided, findings=provided, database_analysis=provided

## Top findings
- **[high] Hard-coded database credential** - `app/main.py:11` (static-detected; not verified at runtime)
- **[high] Hard-coded database credential** - `docker-compose.yml:5` (static-detected; not verified at runtime)
- **[high] Hard-coded database credential** - `dotnet/appsettings.json:1` (static-detected; not verified at runtime)
- **[high] Hard-coded database credential** - `java/application.properties:2` (static-detected; not verified at runtime)
- **[high] Hard-coded database credential** - `php/conn.php:1` (static-detected; not verified at runtime)
- **[high] Hard-coded database credential** - `web/db.js:4` (static-detected; not verified at runtime)
- **[medium] SQL built by string concatenation/interpolation (heuristic)** - `app/main.py:13` (static-detected; not verified at runtime)
- **[low] Transport encryption / certificate validation disabled** - `dotnet/appsettings.json:1` (static-detected; not verified at runtime)
- **[info] Multiple database technologies detected** - `None:None` (static-detected; not verified at runtime)
- **[info] MongoDB: driver used but no connection configuration found** - `web/db.js:2` (static-detected; not verified at runtime)

## Known limitations
- Static text analysis only: no project code is executed and no database connection is ever attempted.
- 'confirmed' means explicit configuration was found - not that the database is reachable, that credentials work, or that the code path is exercised at runtime.
- Comments are skipped using simple per-language rules; string literals and docstrings are not distinguished from code, so a connection string inside a docstring can still be reported.
- Files larger than the configured size limit, binary files (except SQLite file headers), and third-party folders (node_modules, venv, vendor, bin/obj, ...) are not scanned.
- Dialect detection from SQL keywords is heuristic; ORM entity discovery covers common patterns only.
- SQLite/SQL Server data files are detected by name/header but their contents are never opened or parsed.

## Recommended next steps
1. Review the 0 critical and 6 high severity findings first.
2. 10 finding(s) are detected-but-unverified; confirm them manually or with tests before acting.
3. No build/test results were provided: run the project's tests in an isolated environment (Phase 4) to verify behaviour.
4. No language-specific static analysis results were provided (Phase 3); only heuristic findings are available.
5. Rotate any exposed database credentials and move secrets to environment variables or a secret store.
