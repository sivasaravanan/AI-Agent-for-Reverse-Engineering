import json

import pytest

import database_analysis as dba
from database_analysis.redaction import redact_text
from database_analysis.sources import sanitize_member_name
from fixtures import SECRET, build_zip, multi_db_project, negative_project


@pytest.fixture(scope="module")
def multi():
    return dba.run(multi_db_project())


def status(res):
    return res.status_map()


# ---------------------------------------------------------------- positive detection
def test_expected_databases_found(multi):
    s = status(multi)
    for db in ("postgresql", "mysql", "mongodb", "sqlserver", "oracle"):
        assert db in s, f"{db} missing: {s}"


def test_confirmed_requires_explicit_config(multi):
    s = status(multi)
    assert s["mysql"] == "confirmed"        # mysql:// URL
    assert s["oracle"] == "confirmed"       # jdbc:oracle:thin
    assert s["sqlserver"] == "confirmed"    # UseSqlServer + ADO string
    assert s["postgresql"] in ("confirmed", "strong")


def test_mongodb_without_connection_is_not_confirmed(multi):
    assert status(multi)["mongodb"] != "confirmed"


def test_connection_parsing(multi):
    my = next(c for c in multi.connections if c.db == "mysql" and c.kind == "url")
    assert (my.host, my.port, my.database, my.user) == ("mysql.internal.corp", "3306", "catalog", "shop")
    ora = next(c for c in multi.connections if c.db == "oracle")
    assert ora.host == "ora01.corp.local" and ora.port == "1521" and ora.database == "ORCLPDB1"
    ado = next(c for c in multi.connections if c.db == "sqlserver" and c.kind == "ado")
    assert ado.host == "sql01.corp.local" and ado.port == "1433" and ado.database == "Shop"
    pdo = next(c for c in multi.connections if c.kind == "pdo")
    assert pdo.db == "mysql" and pdo.database == "blog"


def test_dependencies_cite_file_and_line(multi):
    dep = next(d for d in multi.dependencies if d.name == "psycopg2-binary")
    assert dep.file == "requirements.txt" and dep.line == 1 and dep.db == "postgresql"
    assert any(d.name == "alembic" and d.role == "migration" for d in multi.dependencies)
    assert any(d.name == "ojdbc8" or "ojdbc8" in d.name for d in multi.dependencies)
    assert any(d.name == "mysql2" and d.line for d in multi.dependencies)


def test_orm_entities(multi):
    names = {(o.orm, o.name) for o in multi.orm_entities}
    assert ("SQLAlchemy", "User") in names
    assert ("EF Core", "Order") in names
    assert ("JPA/Hibernate", "Customer") in names
    assert ("Mongoose (MongoDB ODM)", "Cat") in names
    user = next(o for o in multi.orm_entities if o.name == "User")
    assert user.table == "users" and user.fields == 2
    assert next(o for o in multi.orm_entities if o.name == "Customer").table == "customers"


def test_sql_schema_and_dialects(multi):
    orders = next(s for s in multi.schema_objects if s.name == "orders")
    assert orders.kind == "table" and "id" in orders.columns and "customers" in orders.references
    sc = {s.file: s for s in multi.sql_scripts}
    assert sc["db/schema.sql"].dialect_hints.get("sqlserver", 0) >= 2
    assert sc["db/seed_mysql.sql"].dialect_hints.get("mysql", 0) >= 2
    assert sc["db/seed_mysql.sql"].purpose == "mixed"


def test_migrations_and_services(multi):
    assert any(m.tool == "Alembic" and "users" in m.creates for m in multi.migrations)
    pg = multi.database("postgresql")
    assert pg.evidence_counts.get("service") == 1


def test_go_and_php_drivers(multi):
    pg_imports = [e for e in multi.database("postgresql").evidence if e.kind in ("import", "provider")]
    assert any(e.file == "go/main.go" for e in pg_imports)


# ---------------------------------------------------------------- negative cases
def test_negative_project_nothing_confirmed():
    res = dba.run(negative_project())
    s = status(res)
    assert all(v == "weak" for v in s.values()), s
    assert not res.connections
    rules = {f.rule for f in res.findings}
    assert "DB-CFG-002" in rules  # dependency declared but unused


def test_comment_only_url_is_weak():
    res = dba.run({"a.py": "# mysql://u:p@host/db\n"})
    assert res.status_map() == {"mysql": "weak"} and not res.connections


def test_block_comment_ignored():
    res = dba.run({"A.java": "/* jdbc:mysql://h:3306/x */\nclass A {}\n"})
    assert not res.connections


def test_docs_url_is_weak_only():
    res = dba.run({"README.md": "Use postgresql://user:pw@localhost/db"})
    assert res.status_map() == {"postgresql": "weak"} and not res.connections


def test_example_env_not_confirmed():
    res = dba.run({".env.example": "DATABASE_URL=postgresql://user:pass@localhost:5432/mydb\n"})
    assert res.status_map()["postgresql"] == "strong"
    assert not [f for f in res.findings if f.rule == "DB-SEC-001"]


def test_placeholder_connection_downgraded():
    res = dba.run({"cfg.yml": "url: postgresql://${DB_USER}:${DB_PASS}@<host>:5432/<db>\n"})
    c = res.connections[0]
    assert c.placeholder and c.level == "strong" and not c.password_inline


def test_env_password_not_flagged():
    res = dba.run({"settings.py": "URL = 'mysql://app:${DB_PASS}@db.corp.local/x'\n"})
    assert not [f for f in res.findings if f.rule == "DB-SEC-001"]


def test_library_name_in_unrelated_text_not_detected():
    res = dba.run({"x.py": "pg = 3\nprint('oracle of delphi')\n", "notes.md": "the oracle spoke"})
    assert res.databases == []


def test_test_scope_noted():
    res = dba.run({"tests/test_db.py": "import sqlite3\nc = sqlite3.connect(':memory:')\n"})
    assert res.database("sqlite").scopes == ["test"]
    assert any("test files" in n for n in res.database("sqlite").notes)


def test_sqlite_data_file_header():
    res = dba.run({"data/app.db": b"SQLite format 3\x00" + b"\x00" * 100, "data/other.db": b"not sqlite"})
    assert res.database("sqlite").status == "confirmed"
    assert any(f.rule == "DB-DATA-001" for f in res.findings)
    assert res.skipped.get("non-SQLite .db file (not identified)") == 1


def test_node_modules_skipped():
    res = dba.run({"node_modules/pg/index.js": "require('pg'); // mysql://a:b@c/d"})
    assert res.databases == []


def test_django_engine_and_config_keys():
    res = dba.run({"settings.py": "DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':'x','PASSWORD':'hunter22'}}\nimport psycopg2\n",
                   ".env": "MYSQL_HOST=db1\nMYSQL_DATABASE=app\nMYSQL_PASSWORD=pw9\n"})
    assert res.database("postgresql").status == "confirmed"
    assert res.database("mysql").status == "confirmed"
    assert any(c.kind == "config-keys" and c.password_inline for c in res.connections)


def test_extra_detector_extension_point():
    def fake_db(ctx, col):
        if "cockroach://" in ctx.text:
            col.add("cockroachdb", "strong", "connection", ctx, 1, "custom detector")
    res = dba.run({"a.txt.py": "u='cockroach://x'"}, extra_detectors=[fake_db])
    assert res.database("cockroachdb").status == "strong"


def test_broken_detector_does_not_abort():
    def boom(ctx, col):
        raise RuntimeError("x")
    res = dba.run({"a.py": "import sqlite3"}, extra_detectors=[boom])
    assert res.database("sqlite") and any("detector error" in k for k in res.skipped)


# ---------------------------------------------------------------- secrets / redaction
def test_redaction_unit():
    assert redact_text("postgres://u:abc123@h/db") == "postgres://u:****@h/db"
    assert "abc" not in redact_text("Server=x;Password=abc;User Id=u")
    assert "abc" not in redact_text('{"password": "abc"}')
    assert redact_text("password=${DB_PASS}") == "password=${DB_PASS}"
    assert "tok" not in redact_text("MONGO_TOKEN=tok123")


def test_no_secret_in_any_output(multi):
    blobs = [dba.render_json(multi), dba.render_html(multi), dba.render_markdown(multi)]
    for b in blobs:
        assert SECRET not in b
        assert "pw1" not in b and "pw2" not in b
    assert any(f.rule == "DB-SEC-001" for f in multi.findings)


def test_findings_have_phase6_shape(multi):
    for f in dba.to_findings(multi):
        for k in ("id", "severity", "category", "file", "line", "column", "evidence", "explanation",
                  "remediation", "source", "confidence", "verification_status"):
            assert k in f
        assert f["verification_status"].startswith("static-detected")
    assert any(f.rule == "DB-SEC-003" for f in multi.findings)


def test_jdbc_not_double_parsed_and_fields_clean():
    res = dba.run({"c.properties": "url=jdbc:sqlserver://sql.corp.net:1433;databaseName=hr;user=sa;password=Rr7pass;encrypt=false"})
    assert len(res.connections) == 1
    c = res.connections[0]
    assert (c.kind, c.host, c.port, c.database, c.user) == ("jdbc", "sql.corp.net", "1433", "hr", "sa")
    assert "Rr7pass" not in dba.render_json(res)


def test_other_connection_styles_no_leak():
    res = dba.run({
        "a.cs": 'var c="Host=pg.corp.local;Port=5432;Database=x;Username=bob;Password=Zz9pass;";',
        "b.js": "const u='mongodb+srv://m:Qq8pass@cluster0.corp.net/app?retryWrites=true';"})
    assert {c.db for c in res.connections} == {"postgresql", "mongodb"}
    out = dba.render_json(res)
    assert "Zz9pass" not in out and "Qq8pass" not in out


def test_split_credential_keys_join_sibling_connection():
    res = dba.run({"application.properties": "spring.datasource.url=jdbc:oracle:thin:@//h1.corp.local:1521/X\n"
                                              "spring.datasource.username=app\nspring.datasource.password=Tt5pass\n"})
    assert "unknown-sql" not in res.status_map()
    assert any(f.rule == "DB-SEC-001" for f in res.findings)
    assert "Tt5pass" not in dba.render_json(res)
