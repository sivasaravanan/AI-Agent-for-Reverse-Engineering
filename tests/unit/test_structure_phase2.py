import posixpath

from core.analysis_orchestrator import analyze_archive
from tests.fixtures.sample_project import build_sample_zip


def st(make_zip, files):
    return analyze_archive(make_zip(files)).structure


def kinds(items, attr="kind"):
    return {getattr(i, attr) for i in items}


# ---------------------------------------------------------------- tree
def test_tree_counts_and_vendored_collapsed(make_zip):
    s = st(make_zip, {"a/b/c.py": "x=1\n", "a/d.py": "y=1\n", "node_modules/x/i.js": "1", "node_modules/y/j.js": "1"})
    root = s.tree
    assert root.file_count == 4
    a = next(c for c in root.children if c.name == "a")
    assert a.file_count == 2 and [c.name for c in a.children] == ["b", "d.py"]
    nm = next(c for c in root.children if c.name == "node_modules")
    assert nm.vendored and nm.file_count == 2 and nm.children == []


def test_tree_excludes_unsafe_entries_and_respects_caps(make_zip):
    from core.archive_manager import ArchiveReader
    from core.project_inventory import build_tree
    z = make_zip({"../evil.py": "x", **{f"d{i}/f.py": "x" for i in range(50)}})
    with ArchiveReader(z) as r:
        t = build_tree(r.entries, max_nodes=10)
    assert all(c.name != "evil.py" for c in t.children) and t.truncated
    assert len(t.children) <= 10


def test_tree_depth_cap(make_zip):
    from core.archive_manager import ArchiveReader
    from core.project_inventory import build_tree
    deep = "/".join(f"d{i}" for i in range(30)) + "/f.py"
    with ArchiveReader(make_zip({deep: "x"})) as r:
        t = build_tree(r.entries, max_depth=5)
    node, depth = t, 0
    while node.children:
        node, depth = node.children[0], depth + 1
    assert depth == 5 and node.truncated


# ---------------------------------------------------------------- entry points
def test_entry_points_positive(make_zip):
    files = {
        "a.py": "def f():\n    pass\nif __name__ == '__main__':\n    f()\n",
        "manage.py": "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'x')\n",
        "pkg/__main__.py": "print(1)\n",
        "App.cs": "class A { static void Main(string[] a) {} }",
        "M.java": "class M { public static void main(String[] a) {} }",
        "main.go": "package main\nfunc main() {}\n",
        "package.json": '{"main": "server.js", "bin": {"x": "cli.js"}, "scripts": {"start": "node server.js"}}',
        "pyproject.toml": '[project]\nname="p"\n[project.scripts]\ntool = "pkg.cli:main"\n',
        "Dockerfile": "FROM python:3.12\nCMD [\"python\", \"a.py\"]\n",
    }
    eps = st(make_zip, files).entry_points
    got = {(e.file, e.kind) for e in eps}
    assert ("a.py", "python-main-guard") in got and ("manage.py", "django-manage") in got
    assert ("pkg/__main__.py", "python-package-main") in got and ("App.cs", "csharp-main") in got
    assert ("M.java", "java-main") in got and ("main.go", "go-main") in got
    assert ("package.json", "package-main") in got and ("pyproject.toml", "console-script") in got
    assert any(k == "container-cmd" for _, k in got)
    assert next(e for e in eps if e.kind == "python-main-guard").line == 3


def test_entry_points_negative_and_weak(make_zip):
    eps = st(make_zip, {"lib.py": "def f(): pass\n", "main.py": "print('x')\n", "package.go": "package util\nfunc main() {}\n"}).entry_points
    assert [(e.file, e.confidence) for e in eps] == [("main.py", "weak")]


def test_npm_scripts_recorded_not_executed(make_zip):
    eps = st(make_zip, {"package.json": '{"scripts": {"start": "rm -rf /"}}'}).entry_points
    assert eps[0].kind.startswith("npm-script") and "not run" in eps[0].kind


# ---------------------------------------------------------------- config inventory
def test_config_inventory(make_zip):
    files = {".env": "A=1\n", "appsettings.Development.json": "{}", "docker-compose.yml": "x: 1", "settings.py": "X=1",
             "config/db.yaml": "a: 1", "src/app.py": "x=1", "node_modules/a/config.json": "{}", ".github/workflows/ci.yml": "on: push"}
    cfg = {c.path: c.kind for c in st(make_zip, files).config_files}
    assert cfg[".env"] == "environment file" and cfg["appsettings.Development.json"] == ".NET app settings"
    assert cfg["config/db.yaml"] == "configuration file" and cfg[".github/workflows/ci.yml"] == "CI workflow"
    assert "src/app.py" not in cfg and "node_modules/a/config.json" not in cfg


# ---------------------------------------------------------------- imports
def test_python_imports_internal_external_stdlib_relative(make_zip):
    files = {
        "pkg/__init__.py": "", "pkg/a.py": "import os\nimport requests\nfrom . import b\nfrom .c import X\nfrom pkg import b as bb\n",
        "pkg/b.py": "x = 1\n", "pkg/c.py": "X = 1\n", "main.py": "import pkg.a\nimport yaml\n",
        "requirements.txt": "requests==2.0\nPyYAML\n",
    }
    s = st(make_zip, files)
    by = {(e.source, e.target): e for e in s.imports}
    assert by[("pkg/a.py", "os")].category == "stdlib"
    assert by[("pkg/a.py", "requests")].category == "external"
    assert by[("pkg/a.py", ".")].resolved in ("pkg/b.py", "pkg/__init__.py") or by[("pkg/a.py", ".")].category == "internal"
    assert by[("pkg/a.py", ".c")].resolved == "pkg/c.py"
    assert by[("main.py", "pkg.a")].resolved == "pkg/a.py" and by[("main.py", "pkg.a")].line == 1
    declared = {p["name"]: p["declared"] for p in s.import_summary.external_packages}
    assert declared == {"requests": True, "yaml": True}          # yaml -> PyYAML alias


def test_undeclared_import_warning_with_location(make_zip):
    s = st(make_zip, {"a.py": "import flask\nimport numpy\n", "requirements.txt": "flask\n"})
    w = [x for x in s.warnings if x.code == "undeclared-import"]
    assert len(w) == 1 and "numpy" in w[0].message and (w[0].file, w[0].line) == ("a.py", 2)


def test_no_undeclared_warning_without_parsed_manifest(make_zip):
    s = st(make_zip, {"a.py": "import numpy\n"})
    assert not [x for x in s.warnings if x.code == "undeclared-import"]
    assert s.import_summary.external_packages[0]["declared"] is None
    assert any(x.code == "missing-manifest" for x in st(make_zip, {f"f{i}.py": "x=1\n" for i in range(3)}).warnings)


def test_js_imports(make_zip):
    files = {"src/a.js": "import x from './b';\nconst fs = require('fs');\nconst e = require('express');\nimport('./c.js');\n",
             "src/b.js": "export default 1\n", "src/c.js": "1\n", "src/lib/index.ts": "export {}\n",
             "src/t.ts": "import y from './lib';\nimport z from '@scope/pkg/sub';\n",
             "package.json": '{"dependencies": {"express": "^4"}}'}
    s = st(make_zip, files)
    by = {(e.source, e.target): e for e in s.imports}
    assert by[("src/a.js", "./b")].resolved == "src/b.js" and by[("src/a.js", "./c.js")].resolved == "src/c.js"
    assert by[("src/a.js", "fs")].category == "builtin"
    assert by[("src/t.ts", "./lib")].resolved == "src/lib/index.ts"
    assert by[("src/t.ts", "@scope/pkg/sub")].package == "@scope/pkg"
    names = {p["name"]: p["declared"] for p in s.import_summary.external_packages}
    assert names["express"] is True and names["@scope/pkg"] is False


def test_other_language_imports(make_zip):
    files = {"A.cs": "using System;\nusing MyApp.Models;\nusing Newtonsoft.Json;\nnamespace MyApp.Services {}\n",
             "M.cs": "namespace MyApp.Models { }\n",
             "p/Main.java": "import java.util.List;\nimport com.acme.util.Helper;\nimport org.springframework.boot.X;\n",
             "com/acme/util/Helper.java": "package com.acme.util;\n",
             "go.mod": "module example.com/app\n\ngo 1.21\n", "main.go": 'package main\nimport (\n\t"fmt"\n\t"example.com/app/util"\n\t"github.com/gin-gonic/gin"\n)\n',
             "util/u.go": "package util\n",
             "i.php": "<?php\nnamespace App;\nuse App\\Models\\User;\nuse Illuminate\\Support\\Str;\nrequire 'lib.php';\n", "lib.php": "<?php\n"}
    by = {(e.source, e.target): e for e in st(make_zip, files).imports}
    assert by[("A.cs", "System")].category == "stdlib"
    assert by[("A.cs", "MyApp.Models")].resolved == "M.cs"
    assert by[("A.cs", "Newtonsoft.Json")].category == "external"
    assert by[("p/Main.java", "java.util.List")].category == "stdlib"
    assert by[("p/Main.java", "com.acme.util.Helper")].resolved == "com/acme/util/Helper.java"
    assert by[("p/Main.java", "org.springframework.boot.X")].package == "org.springframework"
    assert by[("main.go", "fmt")].category == "stdlib"
    assert by[("main.go", "example.com/app/util")].resolved == "util/u.go"
    assert by[("main.go", "github.com/gin-gonic/gin")].package == "github.com/gin-gonic/gin"
    assert by[("i.php", "Illuminate\\Support\\Str")].category == "external"
    assert by[("i.php", "lib.php")].resolved == "lib.php"


def test_circular_imports_and_fan_in(make_zip):
    s = st(make_zip, {"a.py": "import b\n", "b.py": "import c\n", "c.py": "import a\n", "d.py": "import a\n"})
    assert s.import_summary.cycles == [["a.py", "b.py", "c.py"]]
    assert s.import_summary.most_depended_on[0] == {"file": "a.py", "count": 2}
    assert any(w.code == "import-cycle" for w in s.warnings)


def test_no_cycle_negative(make_zip):
    s = st(make_zip, {"a.py": "import b\n", "b.py": "x = 1\n"})
    assert s.import_summary.cycles == []


def test_import_in_comment_or_unparseable_python_still_handled(make_zip):
    s = st(make_zip, {"a.py": "# import secret_pkg\nimport os\n", "bad.py": "import json\ndef x(:\n"})
    targets = {(e.source, e.target) for e in s.imports}
    assert ("a.py", "secret_pkg") not in targets and ("bad.py", "json") in targets


# ---------------------------------------------------------------- env vars
def test_env_var_discovery_and_status(make_zip):
    files = {
        "a.py": "import os\nA = os.environ['DEFINED_VAR']\nB = os.getenv('WITH_DEFAULT', 'x')\nC = os.environ.get('MISSING_VAR')\nD = os.getenv('PATH')\n# os.getenv('COMMENTED')\n",
        "s.js": "const x = process.env.JS_MISSING; const y = process.env.JS_DEF || 'd';\n",
        "C.cs": 'var v = Environment.GetEnvironmentVariable("CS_VAR");\n',
        ".env": "DEFINED_VAR=supersecretvalue\n",
        ".env.example": "ONLY_EXAMPLE=changeme\n", "x.py": "import os; os.getenv('ONLY_EXAMPLE')\n",
    }
    s = st(make_zip, files)
    v = {e.name: e for e in s.env_vars}
    assert v["DEFINED_VAR"].status == "defined" and v["DEFINED_VAR"].defined_in == [".env"]
    assert v["WITH_DEFAULT"].status == "has-default" and v["PATH"].status == "standard"
    assert v["MISSING_VAR"].status == "undefined-in-archive" and v["MISSING_VAR"].references[0].line == 4
    assert v["JS_MISSING"].status == "undefined-in-archive" and v["JS_DEF"].status == "has-default"
    assert v["CS_VAR"].status == "undefined-in-archive" and "COMMENTED" not in v
    assert v["ONLY_EXAMPLE"].status == "template-only"
    import json
    from core.report_generator import to_json
    from core.analysis_orchestrator import analyze_archive
    assert "supersecretvalue" not in to_json(analyze_archive(make_zip(files)))      # values never recorded
    assert any(w.code == "env-undefined" and w.file == "a.py" and w.line == 4 for w in s.warnings)


def test_compose_and_dockerfile_env(make_zip):
    files = {"docker-compose.yml": "services:\n  web:\n    environment:\n      - APP_MODE=prod\n      - DB_PASS=${DB_PASS}\n    image: ${IMAGE:-nginx}\n",
             "Dockerfile": "FROM x\nARG BUILD_ID\nENV APP_PORT=80\n"}
    v = {e.name: e for e in st(make_zip, files).env_vars}
    assert v["DB_PASS"].status == "defined"       # defined in the compose environment list
    assert v["IMAGE"].status == "has-default"
    assert v["APP_PORT"].status == "defined" and v["BUILD_ID"].status == "defined"


# ---------------------------------------------------------------- db connection config
def test_db_connection_config_and_redaction(make_zip):
    files = {"cfg.py": 'DATABASE_URL = "postgresql://u:topsecret@h/db"\n',
             "appsettings.json": '{"ConnectionStrings": {"Default": "Server=s;Database=d;User Id=sa;Password=hunter2pw;"}}',
             ".env.example": "DATABASE_URL=postgresql://user:password@localhost/db\n"}
    s = st(make_zip, files)
    cfg = {(c.file, c.database): c for c in s.db_connections}
    pg = cfg[("cfg.py", "PostgreSQL")]
    assert pg.key == "DATABASE_URL" and pg.form == "url" and pg.confidence == "strong" and "topsecret" not in pg.evidence
    ss = cfg[("appsettings.json", "SQL Server")]
    assert ss.key == "Default" and "hunter2pw" not in ss.evidence
    assert cfg[(".env.example", "PostgreSQL")].confidence == "weak"


def test_db_env_reference_is_weak_and_unresolved_warning(make_zip):
    s = st(make_zip, {"a.py": "import os\nurl = os.environ['DATABASE_URL']\n"})
    c = s.db_connections[0]
    assert c.form == "env-reference" and c.confidence == "weak" and c.database == "unknown"
    assert any(w.code == "db-env-unresolved" for w in s.warnings)
    assert [i.evidence_class for i in s.confidence if i.kind == "database"] == ["unknown"]


def test_db_driver_not_treated_as_configured(make_zip):
    s = st(make_zip, {"requirements.txt": "psycopg2\n", "a.py": "import psycopg2\n"})
    assert s.db_connections == []
    ind = {i.subject: i for i in s.confidence}
    assert ind["PostgreSQL"].evidence_class == "weak"
    assert any(w.code == "db-driver-without-config" for w in s.warnings)


def test_db_confirmed_requires_config_and_code_use(make_zip):
    s = st(make_zip, {"a.py": 'import psycopg2\nURL = "postgresql://u:p@h/db"\n'})
    assert {i.subject: i.evidence_class for i in s.confidence}["PostgreSQL"] == "confirmed"


def test_db_manifest_alone_never_confirms(make_zip):
    s = st(make_zip, {"A.csproj": '<Project><ItemGroup><PackageReference Include="Microsoft.Data.SqlClient" Version="5"/></ItemGroup></Project>',
                      "c.json": '{"c": "Server=s;Database=d;"}'})
    assert {i.subject: i.evidence_class for i in s.confidence}["SQL Server"] == "strong"


# ---------------------------------------------------------------- confidence + dependencies
def test_framework_confidence_classes(make_zip):
    s = st(make_zip, {"requirements.txt": "flask\ndjango\n", "app.py": "from flask import Flask\n"})
    c = {i.subject: i.evidence_class for i in s.confidence if i.kind == "framework"}
    assert c["Flask"] == "confirmed" and c["Django"] == "strong"
    assert any("no usage found" in r for i in s.confidence if i.subject == "Django" for r in i.reasons)


def test_language_confidence_weak_for_single_file(make_zip):
    c = {i.subject: i.evidence_class for i in st(make_zip, {"x.go": "package x\n"}).confidence if i.kind == "language"}
    assert c["Go"] == "weak"


def test_unpinned_dependencies(make_zip):
    s = st(make_zip, {"requirements.txt": "a==1\nb\nc>=2\n", "package.json": '{"dependencies": {"x": "*", "y": "^1"}}'})
    assert {d["name"] for d in s.unpinned_dependencies} == {"b", "x"}
    assert s.dependency_summary["python"] == {"manifests": 1, "dependencies": 3, "unpinned": 1}


def test_unused_declared_is_weak_indicator(make_zip):
    s = st(make_zip, {"requirements.txt": "requests\nflask\n", "a.py": "import flask\n"})
    assert [u["name"] for u in s.import_summary.unused_declared] == ["requests"]


def test_sample_project_structure(tmp_path):
    s = analyze_archive(build_sample_zip(tmp_path / "s.zip")).structure
    assert {"go-main", "java-main", "aspnet-host"} <= kinds(s.entry_points)
    assert any(c.key == "DATABASE_URL" for c in s.db_connections)
    assert s.tree.file_count == 19
    assert {"python", "javascript", "dotnet", "java", "go", "php"} <= set(s.dependency_summary)


def test_phase1_output_unchanged_by_phase2(make_zip):
    r = analyze_archive(make_zip({"a.py": "def x(:\n"}))
    assert r.findings[0].rule_id == "py-syntax" and r.schema_version == "1.0"
    assert not [f for f in r.findings if f.category == "structure"]
