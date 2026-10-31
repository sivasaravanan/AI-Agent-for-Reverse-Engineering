"""ORM / entity-model and migration detection (text only)."""
from __future__ import annotations

import re
from typing import List, Optional

from .collector import Collector
from .context import FileContext
from .models import MigrationInfo, OrmEntity, SchemaObject

_PY_CLASS = re.compile(r"^(?P<ind>\s*)class\s+(?P<name>\w+)\s*(?:\((?P<bases>[^)]*)\))?\s*:")


def _body(lines: List[str], start: int, indent: int, limit: int = 120) -> str:
    out = []
    for j in range(start + 1, min(len(lines), start + 1 + limit)):
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        out.append(l)
    return "\n".join(out)


def _add(col: Collector, ctx: FileContext, orm: str, name: str, table: Optional[str], line: int,
         fields: Optional[int], detail: str = "") -> None:
    col.orm.append(OrmEntity(orm, name, table, ctx.path, line, fields, detail))
    col.schema.append(SchemaObject("collection" if "Mongo" in orm or "Mongoose" in orm else "table",
                                   table or name, ctx.path, line, "orm"))


def _python(ctx: FileContext, col: Collector) -> None:
    t = ctx.text
    has_django = bool(re.search(r"^\s*(?:from|import)\s+django\.db", t, re.M))
    has_sa = bool(re.search(r"^\s*(?:from|import)\s+(?:sqlalchemy|flask_sqlalchemy)", t, re.M))
    has_sqlmodel = bool(re.search(r"^\s*(?:from|import)\s+sqlmodel", t, re.M))
    has_peewee = bool(re.search(r"^\s*(?:from|import)\s+peewee", t, re.M))
    has_mongo = re.search(r"^\s*(?:from|import)\s+(mongoengine|beanie)", t, re.M)
    if not (has_django or has_sa or has_sqlmodel or has_peewee or has_mongo):
        return
    for i, line in enumerate(ctx.code):
        m = _PY_CLASS.match(line)
        if not m:
            continue
        bases = m.group("bases") or ""
        body = _body(ctx.code, i, len(m.group("ind")))
        name, n = m.group("name"), i + 1
        if has_django and re.search(r"\bmodels\.Model\b|\bModel\b", bases):
            tbl = re.search(r"db_table\s*=\s*['\"](\w+)", body)
            fields = len(re.findall(r"models\.\w*(?:Field|Key)\(", body))
            _add(col, ctx, "Django ORM", name, tbl.group(1) if tbl else None, n, fields,
                 "table name derived by Django unless db_table is set")
        elif has_sqlmodel and "SQLModel" in bases and "table=True" in bases:
            _add(col, ctx, "SQLModel", name, name.lower(), n, len(re.findall(r"\bField\(|:\s*\w", body)))
        elif has_sa and re.search(r"__tablename__|\bColumn\(|\bmapped_column\(|\bMapped\[", body):
            tbl = re.search(r"__tablename__\s*=\s*['\"](\w+)", body)
            _add(col, ctx, "SQLAlchemy", name, tbl.group(1) if tbl else None, n,
                 len(re.findall(r"\bColumn\(|\bmapped_column\(", body)))
        elif has_peewee and "Model" in bases and re.search(r"\w+Field\(", body):
            tbl = re.search(r"table_name\s*=\s*['\"](\w+)", body)
            _add(col, ctx, "Peewee", name, tbl.group(1) if tbl else name.lower(), n, len(re.findall(r"\w+Field\(", body)))
        elif has_mongo and re.search(r"\b(?:Document|EmbeddedDocument|DynamicDocument)\b", bases):
            _add(col, ctx, f"{has_mongo.group(1).capitalize()} (MongoDB ODM)", name, None, n,
                 len(re.findall(r"\w+Field\(", body)))


def _js(ctx: FileContext, col: Collector) -> None:
    t, low = ctx.code_text, ctx.lower_text
    lines = ctx.code
    for i, line in enumerate(lines):
        m = re.search(r"@Entity\s*\(([^)]*)\)", line) or (re.search(r"@Entity\b", line) if "@Entity" in line else None)
        if m:
            args = m.group(1) if m.lastindex else ""
            tbl = re.search(r"['\"](\w+)['\"]", args or "")
            for j in range(i, min(i + 5, len(lines))):
                c = re.search(r"\bclass\s+(\w+)", lines[j])
                if c:
                    orm = "MikroORM" if "mikro-orm" in low else "TypeORM"
                    _add(col, ctx, orm, c.group(1), tbl.group(1) if tbl else None, j + 1, None)
                    break
        if "sequelize" in low:
            d = re.search(r"\.define\(\s*['\"](\w+)['\"]", line)
            if d:
                _add(col, ctx, "Sequelize", d.group(1), None, i + 1, None)
            c = re.search(r"\bclass\s+(\w+)\s+extends\s+Model\b", line)
            if c:
                _add(col, ctx, "Sequelize", c.group(1), None, i + 1, None)
        if "mongoose" in low:
            d = re.search(r"\bmodel\(\s*['\"](\w+)['\"]", line)
            if d:
                _add(col, ctx, "Mongoose (MongoDB ODM)", d.group(1), None, i + 1, None)
        if re.search(r"@Schema\s*\(", line) and "mongoose" in low:
            for j in range(i, min(i + 4, len(lines))):
                c = re.search(r"\bclass\s+(\w+)", lines[j])
                if c:
                    _add(col, ctx, "Mongoose (NestJS)", c.group(1), None, j + 1, None)
                    break


def _prisma(ctx: FileContext, col: Collector) -> None:
    for m in re.finditer(r"(?m)^\s*model\s+(\w+)\s*\{(.*?)^\s*\}", ctx.code_text, re.S):
        body = m.group(2)
        tbl = re.search(r'@@map\(\s*"(\w+)"', body)
        fields = len([l for l in body.splitlines() if l.strip() and not l.strip().startswith("@@")])
        _add(col, ctx, "Prisma", m.group(1), tbl.group(1) if tbl else None,
             ctx.code_text.count("\n", 0, m.start()) + 1, fields)


def _java(ctx: FileContext, col: Collector) -> None:
    lines, t = ctx.code, ctx.text
    jpa = "persistence." in t
    for i, line in enumerate(lines):
        if re.search(r"@Entity\b", line) and jpa or re.search(r"@Document\b", line) and "springframework.data.mongodb" in t:
            orm = "JPA/Hibernate" if "@Entity" in line else "Spring Data MongoDB"
            tbl = None
            for j in range(max(0, i - 3), min(i + 6, len(lines))):
                tm = re.search(r"@(?:Table\(\s*name\s*=|Document\(\s*(?:collection\s*=)?)\s*\"(\w+)\"", lines[j])
                if tm:
                    tbl = tm.group(1)
                c = re.search(r"\bclass\s+(\w+)", lines[j])
                if c and j >= i:
                    _add(col, ctx, orm, c.group(1), tbl, j + 1, None)
                    break


def _csharp(ctx: FileContext, col: Collector) -> None:
    for i, line in enumerate(ctx.code):
        c = re.search(r"\bclass\s+(\w+)\s*:\s*(?:[\w.]+,\s*)*(?:\w+\.)?DbContext\b", line)
        if c:
            col.orm.append(OrmEntity("EF Core DbContext", c.group(1), None, ctx.path, i + 1, None, "context class"))
        d = re.search(r"\bDbSet<(\w+)>\s+(\w+)", line)
        if d:
            _add(col, ctx, "EF Core", d.group(1), None, i + 1, None, f"DbSet property '{d.group(2)}'")


def _php(ctx: FileContext, col: Collector) -> None:
    t = ctx.code_text
    for m in re.finditer(r"\bclass\s+(\w+)\s+extends\s+(?:\\?[\w\\]*\\)?Model\b(.*?)(?=\nclass\s|\Z)", t, re.S):
        if "Illuminate" in ctx.text or "Eloquent" in ctx.text:
            tbl = re.search(r"\$table\s*=\s*['\"](\w+)", m.group(2))
            _add(col, ctx, "Eloquent", m.group(1), tbl.group(1) if tbl else None, t.count("\n", 0, m.start()) + 1, None)
    for m in re.finditer(r"(?:@ORM\\Entity|#\[ORM\\Entity)", ctx.text):
        c = re.search(r"\bclass\s+(\w+)", ctx.text[m.end():m.end() + 600])
        if c:
            _add(col, ctx, "Doctrine ORM", c.group(1), None, ctx.text.count("\n", 0, m.start()) + 1, None)


def _go(ctx: FileContext, col: Collector) -> None:
    for m in re.finditer(r"\btype\s+(\w+)\s+struct\s*\{(.*?)\n\}", ctx.code_text, re.S):
        body = m.group(2)
        if "gorm.Model" in body or 'gorm:"' in body:
            _add(col, ctx, "GORM", m.group(1), None, ctx.code_text.count("\n", 0, m.start()) + 1, body.count('gorm:"'))


def detect_orm(ctx: FileContext, col: Collector) -> None:
    if ctx.category == "prisma":
        _prisma(ctx, col)
        return
    if ctx.category != "source":
        return
    fn = {"python": _python, "javascript": _js, "typescript": _js, "java": _java, "kotlin": _java,
          "csharp": _csharp, "php": _php, "go": _go}.get(ctx.lang or "")
    if fn:
        fn(ctx, col)


# ------------------------------------------------------------------ migrations
_MIGRATION_RULES = [
    ("Django", re.compile(r"(?:^|/)migrations/(\d{4})_\w+\.py$")),
    ("Alembic", re.compile(r"(?:^|/)(?:alembic|migrations)/versions/([\w\-]+)\.py$")),
    ("Prisma Migrate", re.compile(r"(?:^|/)prisma/migrations/([\w\-]+)/migration\.sql$")),
    ("Flyway", re.compile(r"(?:^|/)(?:v(\d+(?:[._]\d+)*)__[^/]+|r__[^/]+)\.sql$")),
    ("Liquibase", re.compile(r"(?:^|/)(?:db\.)?changelog[\w\-.]*\.(?:xml|ya?ml|json|sql)$")),
    ("EF Core migrations", re.compile(r"(?:^|/)migrations/(\d{14})_\w+\.cs$")),
    ("Rails", re.compile(r"(?:^|/)db/migrate/(\d+)_\w+\.rb$")),
    ("Laravel", re.compile(r"(?:^|/)database/migrations/(\d{4}_\d{2}_\d{2}_\d+)_\w+\.php$")),
    ("Node.js migrations (knex/sequelize/typeorm/other)", re.compile(r"(?:^|/)migrations?/([\w\-]+)\.(?:js|ts|cjs|mjs)$")),
    ("SQL migrations", re.compile(r"(?:^|/)(?:db/)?migrations?/([^/]+)\.sql$")),
]
_CREATE_PATTERNS = [
    re.compile(r"CreateModel\(\s*name=['\"](\w+)"), re.compile(r"op\.create_table\(\s*['\"](\w+)"),
    re.compile(r"CreateTable\(\s*name:\s*\"(\w+)\""), re.compile(r"createTable\(\s*['\"](\w+)"),
    re.compile(r"Schema::create\(\s*['\"](\w+)"), re.compile(r"create_table\s+:?['\"]?(\w+)"),
]


def migration_tool(path: str) -> Optional[str]:
    low = path.lower()
    for tool, rx in _MIGRATION_RULES:
        if rx.search(low):
            return tool
    return None


def detect_migration(ctx: FileContext, col: Collector) -> bool:
    low = ctx.path.lower()
    base = low.rsplit("/", 1)[-1]
    if base in ("alembic.ini", "flyway.conf", "liquibase.properties"):
        col.migrations.append(MigrationInfo({"alembic.ini": "Alembic", "flyway.conf": "Flyway",
                                             "liquibase.properties": "Liquibase"}[base], ctx.path, "config"))
        return False
    tool = migration_tool(ctx.path)
    if not tool or low.endswith(".designer.cs") or low.endswith("modelsnapshot.cs"):
        return False
    for rx_tool, rx in _MIGRATION_RULES:
        if rx_tool == tool:
            m = rx.search(low)
            version = (m.group(1) or "") if m and m.lastindex else ""
            break
    creates: List[str] = []
    for rx in _CREATE_PATTERNS:
        creates += rx.findall(ctx.code_text)
    creates = sorted(set(creates))
    col.migrations.append(MigrationInfo(tool, ctx.path, version, creates))
    for t in creates:
        col.schema.append(SchemaObject("table", t, ctx.path, None, "migration"))
    return True
