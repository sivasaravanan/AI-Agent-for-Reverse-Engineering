# Phase 5 - Database Analysis plug-in

Self-contained package `database_analysis/` (standard library only, Python 3.11+). It does **not** depend on
Phases 1-4 and does **not** modify any existing file.

## Install into the main app (3 steps)
1. Copy the `database_analysis/` folder next to `app.py` (project root).
2. Copy `tests/phase5/` into your `tests/` folder (optional).
3. Call it:
```python
import database_analysis as dba
result = dba.run("project.zip")                 # or folder / {path: content} / your FileProvider
html   = dba.render_html_fragment(result)       # embed in the main HTML report
data   = result.to_dict()                       # JSON-safe, goes in the JSON report
finds  = dba.to_findings(result)                # Phase 6 finding fields
```
Tkinter tab: `from database_analysis.ui_panel import add_database_tab; add_database_tab(notebook).analyze_async(zip)`.
CLI: `python -m database_analysis project.zip --json out.json --html out.html --md out.md`.
See `integration_example.py`.

## What it detects
| Area | How |
|---|---|
| Databases | SQLite, SQL Server, PostgreSQL, MySQL, MariaDB, Oracle, MongoDB (+ H2, Derby, Db2, HSQLDB via JDBC/deps) |
| Drivers/client libs | manifests: requirements/pyproject/Pipfile/setup, package.json, pom.xml, Gradle, .csproj/packages.config, composer.json, go.mod; plus source imports (Python, JS/TS, Java/Kotlin, C#/VB, PHP, Go) |
| Connection config | URLs (`postgresql://`, `mongodb+srv://`...), JDBC, ADO.NET/ODBC strings, PDO DSNs, `sqlite3.connect`, .env/ini/yaml/json/properties keys, Django `ENGINE`, EF `UseXxx()`, Hibernate/Spring/Prisma/Sequelize/Knex/TypeORM settings |
| ORM models | SQLAlchemy, Django, SQLModel, Peewee, mongoengine/beanie, TypeORM, MikroORM, Sequelize, Mongoose, Prisma, JPA/Hibernate, Spring Data MongoDB, EF Core, Eloquent, Doctrine, GORM |
| Migrations | Django, Alembic, Flyway, Liquibase, Prisma, EF Core, Rails, Laravel, Node, plain SQL |
| Schema/DDL | CREATE/ALTER of tables, views, indexes, routines... with columns & FK references; dialect-specific SQL clues |
| Containers | postgres/mysql/mariadb/mongo/mssql/oracle images in compose/Dockerfiles |
| Data files | SQLite file by header (content never opened) |

## Evidence levels (never merged into a single "yes")
* **confirmed** - explicit connection string/URL, provider/engine setting, SQLite file header. Reachability is *not* verified.
* **strong** - driver import, dialect-specific SQL, DB container, connect call, placeholder/example/heuristic connection.
* **weak** - dependency only, docs/comment mention.
A library in a manifest, or a name in a comment/README, can never reach "confirmed". Evidence from tests/examples is labelled by `scope`.

## Findings (Phase 6 compatible: id, severity, category, file, line, evidence, explanation, remediation, source, confidence, verification_status)
DB-SEC-001 hard-coded credential - DB-SEC-002 privileged account - DB-SEC-003 string-built SQL (heuristic, low confidence) -
DB-SEC-004 TLS/cert validation off - DB-DATA-001 SQLite file in archive - DB-CFG-001 driver but no config -
DB-CFG-002 unused dependency - DB-CFG-003 multiple databases - DB-CFG-004 placeholders - DB-CFG-005 dialect mismatch -
DB-CFG-006 env var referenced but undefined. Nothing is marked "verified".

## Safety
* Archive members are read in memory, never extracted; traversal/absolute/symlink/encrypted/duplicate entries skipped; file-count, size and compression-ratio limits.
* No project code executed, **no network or database connection** (tests assert this by blocking sockets).
* Passwords, tokens and keys are masked in every stored string, report and UI; raw secrets are never stored.

## Extending
`DatabaseAnalyzer(extra_detectors=[fn])` where `fn(ctx, collector)` calls `collector.add(db, level, kind, ctx, line, detail, raw_line)` -
see `test_extra_detector_extension_point`. Add built-in signatures in `signatures.py`.

## Known limitations
See `result.limitations` (also printed in reports): comments only by line/block rules; strings/docstrings not distinguished; heuristic
dialect and SQL-injection patterns can produce false positives/negatives; large (>2 MB) and binary files skipped; live-connection
features intentionally not implemented (would need explicit user approval per the master prompt).
