from core.analysis_orchestrator import analyze_archive


def names(items):
    return {i.name: i for i in items}


def test_languages_and_markers(make_zip):
    r = analyze_archive(make_zip({"a.py": "x=1\n", "requirements.txt": "flask\n", "b.js": "1\n", "c.go": "package main\n"}))
    langs = {l.language: l for l in r.languages}
    assert langs["Python"].confidence == "strong"          # has requirements.txt marker
    assert langs["Go"].confidence == "weak"                # one file, no go.mod
    assert "JavaScript" in langs


def test_marker_without_source_files_is_not_a_language(make_zip):
    r = analyze_archive(make_zip({"package.json": "{}"}))
    assert r.languages == []


def test_vendored_folders_excluded(make_zip):
    r = analyze_archive(make_zip({"node_modules/x/a.js": "eval('1')", "src/app.py": "x=1\n"}))
    assert [l.language for l in r.languages] == ["Python"]
    assert r.archive.vendored_file_count == 1
    assert not any(f.rule_id == "sec-eval-exec" for f in r.findings)


def test_framework_from_manifest_is_strong(make_zip):
    r = analyze_archive(make_zip({"requirements.txt": "Django==4.2\npytest\n"}))
    fw = names(r.frameworks)
    assert fw["Django"].confidence == "strong" and fw["Django"].evidence[0].line == 1
    assert "pytest" in fw


def test_framework_import_only_is_moderate(make_zip):
    r = analyze_archive(make_zip({"app.py": "from flask import Flask\n"}))
    assert names(r.frameworks)["Flask"].confidence == "moderate"


def test_no_framework_negative(make_zip):
    r = analyze_archive(make_zip({"a.py": "print('hi')\n"}))
    assert r.frameworks == []


def test_dotnet_web_sdk_and_runtime(make_zip):
    csproj = '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    r = analyze_archive(make_zip({"A.csproj": csproj, "P.cs": "class P{}"}))
    fw = names(r.frameworks)
    assert "ASP.NET Core" in fw and ".NET net8.0" in fw


def test_database_connection_string_is_strong(make_zip):
    r = analyze_archive(make_zip({"cfg.py": 'URL = "postgresql://u:p@h/db"\n'}))
    db = names(r.databases)["PostgreSQL"]
    assert db.confidence == "strong"
    assert "p@h" not in db.evidence[0].text or "REDACTED" in db.evidence[0].text


def test_driver_in_requirements_only_is_weak_not_confirmed(make_zip):
    r = analyze_archive(make_zip({"requirements.txt": "psycopg2-binary\npymysql\n", "a.py": "x=1\n"}))
    dbs = names(r.databases)
    assert dbs["PostgreSQL"].confidence == "weak" and dbs["MySQL/MariaDB"].confidence == "weak"
    assert "does not show" in dbs["PostgreSQL"].note


def test_driver_import_is_moderate(make_zip):
    r = analyze_archive(make_zip({"a.py": "import sqlite3\nc = sqlite3.connect('x')\n"}))
    assert names(r.databases)["SQLite"].confidence == "moderate"


def test_comment_or_doc_mention_is_weak(make_zip):
    r = analyze_archive(make_zip({"a.py": "# mysql://u:p@h/db could be used later\n", "README.md": "We use Oracle.\n"}))
    dbs = names(r.databases)
    assert dbs["MySQL/MariaDB"].confidence == "weak"
    assert dbs["Oracle"].confidence == "weak"


def test_no_database_negative(make_zip):
    assert analyze_archive(make_zip({"a.py": "print(1)\n"})).databases == []


def test_sqlserver_and_mongo_and_oracle_strings(make_zip):
    files = {
        "a.json": '{"c": "Server=sql01;Database=Shop;User Id=sa;Password=x;"}',
        "b.js": "mongoose.connect('mongodb://localhost/db')\n",
        "C.java": 'String u = "jdbc:oracle:thin:@host:1521:xe";\n',
    }
    dbs = names(analyze_archive(make_zip(files)).databases)
    assert dbs["SQL Server"].confidence == "strong"
    assert dbs["MongoDB"].confidence == "strong"
    assert dbs["Oracle"].confidence == "strong"


def test_sql_ddl_detected_as_schema_not_specific_db(make_zip):
    r = analyze_archive(make_zip({"s.sql": "CREATE TABLE t (id int);\n"}))
    assert "SQL schema (dialect unknown)" in names(r.databases)
    assert len(r.databases) == 1


def test_manifest_identification_and_dependencies(make_zip):
    files = {
        "requirements.txt": "flask==3.0\n# c\n-r other.txt\nrequests>=2.0 ; python_version>'3'\n",
        "package.json": '{"dependencies": {"express": "^4"}, "devDependencies": {"jest": "^29"}}',
        "pyproject.toml": '[project]\nrequires-python = ">=3.11"\ndependencies = ["rich>=13"]\n',
        "go.mod": "module m\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        "x/App.csproj": '<Project><ItemGroup><PackageReference Include="Dapper" Version="2.1.0"/></ItemGroup></Project>',
    }
    r = analyze_archive(make_zip(files))
    mf = {m.path: m for m in r.manifests}
    assert [d.name for d in mf["requirements.txt"].dependencies] == ["flask", "requests"]
    assert mf["requirements.txt"].dependencies[0].line == 1
    assert {d.name for d in mf["package.json"].dependencies} == {"express", "jest"}
    assert mf["pyproject.toml"].dependencies[0].name == "rich"
    assert mf["go.mod"].dependencies[0].spec == "v1.9.1"
    assert mf["x/App.csproj"].dependencies[0].name == "Dapper"


def test_malformed_manifest_is_a_warning(make_zip):
    r = analyze_archive(make_zip({"package.json": "{not json", "a.js": "1"}))
    assert any("package.json" in w for w in r.warnings)
    assert r.manifests[0].parse_error


def test_java_runtime_label_is_clean(make_zip):
    pom = "<project><properties><java.version>17</java.version></properties></project>"
    names_ = {d.name for d in analyze_archive(make_zip({"pom.xml": pom, "A.java": "class A{}"})).frameworks}
    assert "Java 17" in names_
