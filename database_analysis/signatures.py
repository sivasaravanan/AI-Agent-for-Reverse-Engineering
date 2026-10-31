"""Signature tables. To add a database: extend DB_DISPLAY, DEPENDENCY_RULES, IMPORT_RULES,
CONNECTION schemes and SQL_CLUES here (or register a custom detector - see plugin docs)."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern, Tuple

DB_DISPLAY: Dict[str, str] = {
    "sqlite": "SQLite",
    "sqlserver": "Microsoft SQL Server",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "oracle": "Oracle Database",
    "mongodb": "MongoDB",
    "h2": "H2 Database",
    "derby": "Apache Derby",
    "db2": "IBM Db2",
    "hsqldb": "HSQLDB",
    "unknown-sql": "Unspecified SQL database (generic ODBC/JDBC/DB-API)",
}
TARGET_DATABASES = ("sqlite", "sqlserver", "postgresql", "mysql", "mariadb", "oracle", "mongodb")

DB_ALIASES: Dict[str, str] = {
    "postgres": "postgresql", "postgresql": "postgresql", "pgsql": "postgresql", "psql": "postgresql",
    "pg": "postgresql", "pgx": "postgresql", "postgis": "postgresql", "pg-native": "postgresql",
    "mysql": "mysql", "mysql2": "mysql", "mysqli": "mysql", "pdo_mysql": "mysql",
    "mariadb": "mariadb",
    "mssql": "sqlserver", "sqlsrv": "sqlserver", "sqlserver": "sqlserver", "sql_server": "sqlserver",
    "tedious": "sqlserver", "dblib": "sqlserver",
    "sqlite": "sqlite", "sqlite3": "sqlite", "better-sqlite3": "sqlite", "spatialite": "sqlite",
    "oracle": "oracle", "oci": "oracle", "oci8": "oracle", "oracledb": "oracle", "godror": "oracle",
    "oracle_enhanced": "oracle",
    "mongo": "mongodb", "mongodb": "mongodb",
    "h2": "h2", "derby": "derby", "db2": "db2", "hsqldb": "hsqldb",
}

_SUBSTR_ORDER = [
    ("postgres", "postgresql"), ("mariadb", "mariadb"), ("mysql", "mysql"),
    ("sqlserver", "sqlserver"), ("mssql", "sqlserver"), ("sqljdbc", "sqlserver"),
    ("oracle", "oracle"), ("sqlite", "sqlite"), ("mongo", "mongodb"),
    ("derby", "derby"), ("hsqldb", "hsqldb"), ("db2", "db2"),
]


def db_from_value(value: str) -> Optional[str]:
    """Map an engine/driver/dialect value or class name to a database key."""
    v = (value or "").strip().strip("'\"").lower()
    if not v or len(v) > 120:
        return None
    if v in DB_ALIASES:
        return DB_ALIASES[v]
    for token, db in _SUBSTR_ORDER:
        if token in v:
            return db
    if re.fullmatch(r"(?:org\.h2\.driver|h2dialect|h2)", v):
        return "h2"
    return None


# ---------------------------------------------------------------- dependencies
# (fullmatch regex on the lower-cased dependency name, db, role). First match wins.
_RAW_DEP_RULES: List[Tuple[str, Optional[str], str]] = [
    (r"psycopg2(-binary)?|psycopg(-binary|-pool)?|asyncpg|pg8000|pg|pg-promise|pg-native|postgres|"
     r"org\.postgresql:.*|npgsql(\..*)?|github\.com/lib/pq|github\.com/jackc/pgx.*|"
     r"gorm\.io/driver/postgres|ext-pgsql|ext-pdo_pgsql", "postgresql", "driver"),
    (r"pymysql|mysqlclient|mysql-connector(-python)?|aiomysql|asyncmy|mysql|mysql2|mysql\.data(\..*)?|"
     r"mysqlconnector|pomelo\.entityframeworkcore\.mysql|mysql:mysql-connector-java|com\.mysql:.*|"
     r"github\.com/go-sql-driver/mysql|gorm\.io/driver/mysql|ext-mysqli|ext-pdo_mysql|ext-mysqlnd|django-mysql",
     "mysql", "driver"),
    (r"mariadb|org\.mariadb\.jdbc:.*", "mariadb", "driver"),
    (r"pymssql|mssql|tedious|mssql-django|django-mssql-backend|com\.microsoft\.sqlserver:.*|"
     r"system\.data\.sqlclient|microsoft\.data\.sqlclient|microsoft\.entityframeworkcore\.sqlserver|"
     r"github\.com/(denisenkom|microsoft)/go-mssqldb|gorm\.io/driver/sqlserver|ext-sqlsrv|ext-pdo_sqlsrv|ext-pdo_dblib",
     "sqlserver", "driver"),
    (r"cx[-_]oracle|oracledb|oracle\.manageddataaccess(\..*)?|oracle\.entityframeworkcore|"
     r"com\.oracle\.database\.jdbc:.*|com\.oracle\.ojdbc:.*|com\.oracle\.jdbc:.*|ojdbc\d+|"
     r"github\.com/sijms/go-ora.*|github\.com/godror/godror|ext-oci8|ext-pdo_oci", "oracle", "driver"),
    (r"aiosqlite|sqlite3|better-sqlite3|sqlite|microsoft\.data\.sqlite(\..*)?|system\.data\.sqlite(\..*)?|"
     r"org\.xerial:sqlite-jdbc|github\.com/mattn/go-sqlite3|modernc\.org/sqlite|gorm\.io/driver/sqlite|"
     r"ext-sqlite3|ext-pdo_sqlite", "sqlite", "driver"),
    (r"pymongo|motor|mongoengine|beanie|mongodb|mongoose|mongodb\.driver(\..*)?|org\.mongodb:.*|"
     r"org\.springframework\.boot:spring-boot-starter-data-mongodb.*|go\.mongodb\.org/mongo-driver.*|"
     r"mongodb/mongodb|ext-mongodb|@nestjs/mongoose", "mongodb", "driver"),
    (r"com\.h2database:h2", "h2", "driver"),
    (r"org\.apache\.derby:.*", "derby", "driver"),
    (r"com\.ibm\.db2:.*|ibm[-_]db", "db2", "driver"),
    (r"org\.hsqldb:.*", "hsqldb", "driver"),
    (r"sqlalchemy|sqlmodel|django|peewee|tortoise-orm|pony|sequelize|sequelize-typescript|typeorm|prisma|"
     r"@prisma/client|knex|@mikro-orm/.*|mikro-orm|drizzle-orm|objection|bookshelf|"
     r"microsoft\.entityframeworkcore(\.(tools|design|relational|proxies|abstractions))?|dapper|"
     r"org\.hibernate(\.orm)?:.*|org\.springframework\.boot:spring-boot-starter-data-jpa|"
     r"org\.springframework\.boot:spring-boot-starter-jdbc|org\.mybatis.*:.*|gorm\.io/gorm|"
     r"github\.com/jmoiron/sqlx|doctrine/(dbal|orm)|illuminate/database|laravel/framework", None, "orm"),
    (r"alembic|flask-migrate|org\.flywaydb:.*|org\.liquibase:.*|db-migrate|umzug|"
     r"github\.com/golang-migrate/.*|github\.com/pressly/goose.*", None, "migration"),
]
DEPENDENCY_RULES: List[Tuple[Pattern[str], Optional[str], str]] = [
    (re.compile(p), db, role) for p, db, role in _RAW_DEP_RULES
]


def match_dependency(name: str) -> Optional[Tuple[Optional[str], str]]:
    n = re.sub(r"\[[^\]]*\]$", "", (name or "").strip().lower())
    for rx, db, role in DEPENDENCY_RULES:
        if rx.fullmatch(n):
            return db, role
    return None


# ---------------------------------------------------------------- imports
PY = frozenset({"python"})
JS = frozenset({"javascript", "typescript"})
JV = frozenset({"java", "kotlin"})
NET = frozenset({"csharp", "vbnet"})
PHP = frozenset({"php"})
GO = frozenset({"go"})


def _js(names: str) -> str:
    return r"""(?:require\(\s*|\bfrom\s+|\bimport\s+|\bimport\(\s*)['"](?:%s)(?:/[^'"]*)?['"]""" % names


_RAW_IMPORTS = [
    (PY, r"^\s*(?:import|from)\s+(?:sqlite3|aiosqlite|apsw)\b", "sqlite", "sqlite3 module"),
    (PY, r"^\s*(?:import|from)\s+(?:psycopg2|psycopg|asyncpg|pg8000)\b", "postgresql", "PostgreSQL driver"),
    (PY, r"^\s*(?:import|from)\s+(?:pymysql|MySQLdb|aiomysql|asyncmy|mysql\.connector)\b", "mysql", "MySQL driver"),
    (PY, r"^\s*(?:import|from)\s+mariadb\b", "mariadb", "MariaDB connector"),
    (PY, r"^\s*(?:import|from)\s+pymssql\b", "sqlserver", "pymssql driver"),
    (PY, r"^\s*(?:import|from)\s+(?:cx_Oracle|oracledb)\b", "oracle", "Oracle driver"),
    (PY, r"^\s*(?:import|from)\s+(?:pymongo|motor|mongoengine|beanie)\b", "mongodb", "MongoDB driver"),
    (PY, r"^\s*(?:import|from)\s+pyodbc\b", "unknown-sql", "pyodbc (database type set by connection string)"),
    (JS, _js(r"pg|pg-promise|pg-native|postgres"), "postgresql", "PostgreSQL client"),
    (JS, _js(r"mysql|mysql2"), "mysql", "MySQL client"),
    (JS, _js(r"mariadb"), "mariadb", "MariaDB client"),
    (JS, _js(r"mssql|tedious"), "sqlserver", "SQL Server client"),
    (JS, _js(r"oracledb"), "oracle", "Oracle client"),
    (JS, _js(r"mongodb|mongoose|@nestjs/mongoose"), "mongodb", "MongoDB client"),
    (JS, _js(r"sqlite3|better-sqlite3|sqlite|node:sqlite"), "sqlite", "SQLite client"),
    (JV, r"^\s*import\s+(?:static\s+)?org\.postgresql\.", "postgresql", "PostgreSQL JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?com\.mysql\.", "mysql", "MySQL JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?org\.mariadb\.jdbc", "mariadb", "MariaDB JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?com\.microsoft\.sqlserver\.", "sqlserver", "SQL Server JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?oracle\.jdbc", "oracle", "Oracle JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?org\.sqlite\.", "sqlite", "SQLite JDBC"),
    (JV, r"^\s*import\s+(?:static\s+)?(?:com\.mongodb|org\.bson)\.", "mongodb", "MongoDB driver"),
    (JV, r"^\s*import\s+(?:static\s+)?(?:java|javax)\.sql\.", "unknown-sql", "JDBC API (database set by URL/driver)"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?Npgsql\b", "postgresql", "Npgsql"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?(?:MySql\.Data|MySqlConnector)\b", "mysql", "MySQL connector"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?(?:System\.Data\.SqlClient|Microsoft\.Data\.SqlClient)\b",
     "sqlserver", "SqlClient"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?(?:Microsoft\.Data\.Sqlite|System\.Data\.SQLite)\b",
     "sqlite", "SQLite provider"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?Oracle\.(?:ManagedDataAccess|DataAccess)\b", "oracle", "ODP.NET"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?MongoDB\.", "mongodb", "MongoDB.Driver"),
    (NET, r"^\s*(?:using|Imports)\s+(?:static\s+)?System\.Data\.(?:OleDb|Odbc)\b", "unknown-sql",
     "OleDb/ODBC (database set by connection string)"),
    (PHP, r"\bmysqli_connect\s*\(|\bnew\s+mysqli\s*\(", "mysql", "mysqli"),
    (PHP, r"\bpg_connect\s*\(", "postgresql", "pg_connect"),
    (PHP, r"\bsqlsrv_connect\s*\(", "sqlserver", "sqlsrv_connect"),
    (PHP, r"\boci_connect\s*\(", "oracle", "oci_connect"),
    (PHP, r"\bnew\s+\\?MongoDB\\(?:Client|Driver)", "mongodb", "MongoDB PHP library"),
    (GO, r"\"(?:github\.com/lib/pq|github\.com/jackc/pgx[^\"]*)\"", "postgresql", "pq/pgx driver"),
    (GO, r"\"github\.com/go-sql-driver/mysql\"", "mysql", "go-sql-driver/mysql"),
    (GO, r"\"github\.com/(?:denisenkom|microsoft)/go-mssqldb[^\"]*\"", "sqlserver", "go-mssqldb"),
    (GO, r"\"(?:github\.com/mattn/go-sqlite3|modernc\.org/sqlite)\"", "sqlite", "SQLite driver"),
    (GO, r"\"go\.mongodb\.org/mongo-driver[^\"]*\"", "mongodb", "mongo-driver"),
    (GO, r"\"github\.com/(?:sijms/go-ora|godror/godror)[^\"]*\"", "oracle", "Oracle driver"),
]
IMPORT_RULES_BY_LANG: Dict[str, List[Tuple[Pattern[str], str, str]]] = {}
for _langs, _rx, _db, _label in _RAW_IMPORTS:
    _flags = re.I if _langs is NET else 0
    for _l in _langs:
        IMPORT_RULES_BY_LANG.setdefault(_l, []).append((re.compile(_rx, _flags), _db, _label))

# ---------------------------------------------------------------- SQL dialect clues
# (label, regex, sql_only). sql_only clues are skipped in non-SQL source files (too ambiguous).
SQL_CLUES: Dict[str, List[Tuple[str, str, bool]]] = {
    "sqlserver": [
        ("IDENTITY(seed,inc)", r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", False),
        ("NVARCHAR", r"\bNVARCHAR\b", False),
        ("[dbo] schema", r"\[dbo\]\.|\bdbo\.\w+", True),
        ("GO batch separator", r"^\s*GO\s*;?\s*$", True),
        ("SET NOCOUNT", r"\bSET\s+NOCOUNT\b", False),
        ("UNIQUEIDENTIFIER", r"\bUNIQUEIDENTIFIER\b", False),
        ("GETDATE()", r"\bGETDATE\s*\(\s*\)", False),
        ("SELECT TOP n", r"\bSELECT\s+TOP\s*\(?\s*\d+", False),
        ("SCOPE_IDENTITY", r"\bSCOPE_IDENTITY\s*\(|@@IDENTITY", False),
        ("BEGIN TRY", r"\bBEGIN\s+TRY\b", False),
        ("CLUSTERED index", r"\bNONCLUSTERED\b|\bCLUSTERED\b", False),
        ("sp_executesql / EXEC sp_", r"\bsp_executesql\b|\bEXEC(?:UTE)?\s+sp_", False),
        ("DECLARE @variable", r"\bDECLARE\s+@\w+", True),
    ],
    "postgresql": [
        ("SERIAL column", r"\b(?:BIG|SMALL)?SERIAL\b", False),
        ("::type cast", r"::\s*(?:int|integer|text|varchar|numeric|timestamp|date|bool|boolean|jsonb?|uuid|bigint|float|regclass)\b", True),
        ("CREATE EXTENSION", r"\bCREATE\s+EXTENSION\b", False),
        ("JSONB", r"\bJSONB\b", False),
        ("LANGUAGE plpgsql", r"\bLANGUAGE\s+plpgsql\b", False),
        ("ILIKE", r"\bILIKE\b", False),
        ("TIMESTAMPTZ / WITH TIME ZONE", r"\bTIMESTAMPTZ\b|\bTIMESTAMP\s+WITH(?:OUT)?\s+TIME\s+ZONE\b", False),
        ("$$ quoted body", r"\$\$", True),
        ("BYTEA", r"\bBYTEA\b", False),
    ],
    "mysql": [
        ("AUTO_INCREMENT", r"\bAUTO_INCREMENT\b", False),
        ("ENGINE=InnoDB/MyISAM", r"\bENGINE\s*=\s*(?:InnoDB|MyISAM|MEMORY|ARCHIVE)\b", False),
        ("backtick identifiers", r"`[A-Za-z_][\w$]*`", True),
        ("DELIMITER", r"^\s*DELIMITER\b", True),
        ("UNSIGNED", r"\bUNSIGNED\b", True),
        ("CHARSET/CHARACTER SET =", r"\b(?:DEFAULT\s+)?(?:CHARSET|CHARACTER\s+SET)\s*=\s*\w+", False),
        ("ON DUPLICATE KEY UPDATE", r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", False),
        ("SHOW TABLES/DATABASES", r"\bSHOW\s+(?:TABLES|DATABASES)\b", False),
        ("TINYINT(1)", r"\bTINYINT\s*\(\s*1\s*\)", False),
    ],
    "sqlite": [
        ("AUTOINCREMENT", r"\bAUTOINCREMENT\b", False),
        ("PRAGMA", r"\bPRAGMA\s+\w+", False),
        ("sqlite_master", r"\bsqlite_(?:master|sequence|schema)\b", False),
        ("WITHOUT ROWID", r"\bWITHOUT\s+ROWID\b", False),
        ("INSERT OR REPLACE/IGNORE", r"\bINSERT\s+OR\s+(?:REPLACE|IGNORE)\b", False),
    ],
    "oracle": [
        ("VARCHAR2", r"\bN?VARCHAR2\b", False),
        ("NUMBER(p)", r"\bNUMBER\s*\(\s*\d", True),
        ("FROM DUAL", r"\bFROM\s+DUAL\b", False),
        ("SYSDATE", r"\bSYSDATE\b", False),
        ("ROWNUM", r"\bROWNUM\b", False),
        ("NVL()", r"\bNVL2?\s*\(", False),
        ("sequence.NEXTVAL", r"\b\w+\.NEXTVAL\b", False),
        ("PL/SQL", r"\bPL/SQL\b", False),
        ("'/' statement terminator", r"^/\s*$", True),
    ],
}

SQL_CLUES_COMPILED: Dict[str, List[Tuple[str, Pattern[str], Pattern[str], bool]]] = {}
for _db, _items in SQL_CLUES.items():
    SQL_CLUES_COMPILED[_db] = [
        (lbl, re.compile(rx, re.I), re.compile(rx), only) for lbl, rx, only in _items
    ]

# ---------------------------------------------------------------- misc
DB_ENV_NAME_RE = re.compile(
    r"^(?:DATABASE_URL|DATABASE_URI|DB_[A-Z0-9_]+|[A-Z0-9_]*_DB(?:_[A-Z0-9_]+)?|[A-Z0-9_]*DATABASE[A-Z0-9_]*|"
    r"MONGO[A-Z0-9_]*|MYSQL_[A-Z0-9_]+|POSTGRES[A-Z0-9_]*|PG(?:HOST|PORT|USER|PASSWORD|DATABASE|SSLMODE)|"
    r"MSSQL[A-Z0-9_]*|SQLSERVER[A-Z0-9_]*|ORACLE[A-Z0-9_]*|SQLITE[A-Z0-9_]*|[A-Z0-9_]*CONNECTION_?STRING|"
    r"SQLALCHEMY_[A-Z0-9_]+)$"
)

PORT_HINTS = {"5432": "postgresql", "3306": "mysql", "1433": "sqlserver", "1521": "oracle", "27017": "mongodb"}
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "(local)", ".", "0.0.0.0", "host.docker.internal"}
ADMIN_USERS = {"root", "sa", "sys", "system", "sysdba", "postgres", "admin", "administrator"}
