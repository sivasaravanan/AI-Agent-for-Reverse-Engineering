"""Per-file detectors: connections, providers, config keys, containers, env refs, SQL-injection
heuristics, documentation mentions. All are pure text analysis."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import signatures as sig
from .collector import Collector
from .context import FileContext
from .models import CONFIRMED, SCOPE_EXAMPLE, STRONG, WEAK
from .redaction import looks_templated, redact_text

# ===================================================================== connection strings
URL_RE = re.compile(
    r"(?i)\b(?P<scheme>postgres(?:ql)?|pgsql|mysql|mariadb|mongodb(?:\+srv)?|sqlite|mssql|sqlserver|oracle)"
    r"(?:\+[a-z0-9_]+)?://(?P<rest>[^\s'\"<>`]*)")
JDBC_RE = re.compile(
    r"(?i)\bjdbc:(?P<sub>postgresql|mysql|mariadb|sqlserver|microsoft:sqlserver|jtds:sqlserver|"
    r"oracle(?::thin|:oci8?)?|sqlite|mongodb|h2|derby|db2|hsqldb)(?P<rest>[:/@;][^\s'\"<>`]*)?")
_ADO_KEYS = (r"(?:Driver|Provider|Server|Data Source|DataSource|Host|Address|Addr|Network Address|Database|"
             r"Initial Catalog|User Id|User ID|Uid|Username|User|Password|Pwd|Port|Trusted_Connection|"
             r"Integrated Security|Encrypt|TrustServerCertificate|Version|Persist Security Info|"
             r"MultipleActiveResultSets|SslMode|Ssl Mode|Connect Timeout|Connection Timeout|Pooling|"
             r"Application Name|Dbq|Service Name|Max Pool Size|Filename|Mode|Cache)")
ADO_RE = re.compile(r"(?i)(?:(?<![\w.])" + _ADO_KEYS + r"\s*=\s*[^;'\"\n]*(?:;|(?=['\"]|$))\s*){2,}")
SQLITE_FILE_RE = re.compile(r"""(?i)\b(?:Data Source|Filename)\s*=\s*([^;'"\n]+\.(?:db|sqlite3?|db3|s3db))""")
PDO_RE = re.compile(
    r"""PDO\s*\(\s*['"](?P<drv>mysql|pgsql|sqlite|sqlsrv|oci|dblib|mssql)\s*:(?P<rest>[^'"]*)['"]"""
    r"""(?:\s*,\s*['"](?P<u>[^'"]*)['"]\s*,\s*['"](?P<p>[^'"]*)['"])?""")
PY_SQLITE_RE = re.compile(r"""\bsqlite3\.connect\(\s*[rbfu]*['"]([^'"]+)['"]""")
JS_SQLITE_RE = re.compile(r"""\bnew\s+(?:sqlite3\.)?Database\(\s*['"]([^'"]+\.(?:db|sqlite3?|db3))['"]""")
TLS_OFF_RE = re.compile(
    r"(?i)(?:sslmode\s*=\s*(?:disable|allow)|\bencrypt\s*=\s*(?:false|no)|\bssl\s*=\s*(?:false|0)|"
    r"usessl\s*=\s*false|\btls\s*=\s*false|trustservercertificate\s*=\s*(?:true|yes)|"
    r"tlsallowinvalidcertificates\s*=\s*true|sslverify\s*=\s*false)")
_PARAM_USER = re.compile(r"(?i)(?<![\w])(?:user|username|uid)=([^;&]*)")
_PARAM_PW = re.compile(r"(?i)(?<![\w])(?:password|pwd)=([^;&]*)")
_PARAM_DB = re.compile(r"(?i)(?<![\w])(?:databaseName|database|dbname)=([^;&]*)")

_SCHEME_DB = {"postgres": "postgresql", "postgresql": "postgresql", "pgsql": "postgresql", "mysql": "mysql",
              "mariadb": "mariadb", "mongodb": "mongodb", "mongodb+srv": "mongodb", "sqlite": "sqlite",
              "mssql": "sqlserver", "sqlserver": "sqlserver", "oracle": "oracle"}
_JDBC_DB = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mariadb", "sqlserver": "sqlserver",
            "microsoft:sqlserver": "sqlserver", "jtds:sqlserver": "sqlserver", "oracle": "oracle",
            "oracle:thin": "oracle", "oracle:oci": "oracle", "oracle:oci8": "oracle", "sqlite": "sqlite",
            "mongodb": "mongodb", "h2": "h2", "derby": "derby", "db2": "db2", "hsqldb": "hsqldb"}


def _split_host_port(hostport: str) -> Tuple[str, Optional[str]]:
    hostport = hostport.strip()
    if hostport.startswith("["):
        end = hostport.find("]")
        host = hostport[1:end] if end > 0 else hostport
        rest = hostport[end + 1:] if end > 0 else ""
        return host, rest[1:] if rest.startswith(":") else None
    if hostport.count(":") == 1:
        h, p = hostport.split(":")
        return h, p
    return hostport, None


def parse_url(rest: str) -> Dict[str, Optional[str]]:
    authority, _, path = rest.partition("/")
    path_part, _, query = path.partition("?")
    userinfo, hostport = ("", authority)
    if "@" in authority:
        userinfo, hostport = authority.rsplit("@", 1)
    user, _, pw = userinfo.partition(":")
    host, port = _split_host_port(hostport.split(",")[0])
    return {"host": host or None, "port": port, "user": user or None, "password": pw or None,
            "database": (path_part.split("/")[0] if path_part else None) or None, "query": query,
            "path": path_part or authority}


def _clean_tail(s: str) -> str:
    s = s.rstrip(".,;)]")
    while s.endswith("}") and s.count("}") > s.count("{"):
        s = s[:-1]
    return s


def _scan_urls(ctx: FileContext, col: Collector, n: int, line: str) -> None:
    for m in URL_RE.finditer(line):
        if re.search(r"(?i)jdbc:[\w:]*$", line[:m.start()]):
            continue  # handled by the JDBC scanner
        rest = _clean_tail(m.group("rest"))
        scheme = m.group("scheme").lower()
        db = _SCHEME_DB[scheme]
        p = parse_url(rest)
        raw = _clean_tail(line[m.start():m.start() + len(m.group(0))])
        if db == "sqlite":
            p["database"] = p["path"] or p["host"]
            p["host"] = None
        if p["query"]:
            if not p["user"]:
                u = _PARAM_USER.search(p["query"])
                p["user"] = u.group(1) if u else None
            if not p["password"]:
                w = _PARAM_PW.search(p["query"])
                p["password"] = w.group(1) if w else None
        col.connection(ctx, n, db, "url", raw, host=p["host"], port=p["port"], database=p["database"],
                       user=p["user"], password=p["password"], tls_disabled=bool(TLS_OFF_RE.search(rest)))


def _scan_jdbc(ctx: FileContext, col: Collector, n: int, line: str) -> None:
    for m in JDBC_RE.finditer(line):
        sub = m.group("sub").lower()
        db = _JDBC_DB[sub]
        rest = _clean_tail(m.group("rest") or "")
        raw = _clean_tail(m.group(0))
        host = port = database = None
        body = rest.lstrip(":/@")
        if db == "sqlite":
            database = body or None
        elif db == "oracle":
            body = body.lstrip("/@").lstrip("/")
            hp, _, tail = re.split(r"[?;]", body)[0].partition("/")
            hostport = hp.split(":")
            host = hostport[0] or None
            port = hostport[1] if len(hostport) > 1 else None
            database = tail or (hostport[2] if len(hostport) > 2 else None)
        elif db in ("h2", "derby", "hsqldb"):
            database = body or None
        else:
            body = body.lstrip("/")
            auth = re.split(r"[/;?]", body)[0]
            host, port = _split_host_port(auth.split(",")[0])
            path = body[len(auth):]
            if path.startswith("/"):
                database = re.split(r"[?;]", path[1:])[0] or None
        d = _PARAM_DB.search(rest)
        u, w = _PARAM_USER.search(rest), _PARAM_PW.search(rest)
        col.connection(ctx, n, db, "jdbc", raw, host=host or None, port=port, database=database or (d.group(1) if d else None),
                       user=u.group(1) if u else None, password=w.group(1) if w else None,
                       tls_disabled=bool(TLS_OFF_RE.search(rest)))


def classify_ado(kv: Dict[str, str]) -> Tuple[str, bool]:
    drv = (kv.get("driver", "") + " " + kv.get("provider", "")).lower()
    ds = (kv.get("data source") or kv.get("datasource") or kv.get("server") or kv.get("host")
          or kv.get("address") or kv.get("addr") or kv.get("network address") or "").lower()
    if "sql server" in drv or any(t in drv for t in ("sqloledb", "sqlncli", "msoledbsql", "sqlserver")):
        return "sqlserver", True
    if "postgres" in drv:
        return "postgresql", True
    if "mariadb" in drv:
        return "mariadb", True
    if "mysql" in drv:
        return "mysql", True
    if "oracle" in drv or "oraoledb" in drv or "msdaora" in drv:
        return "oracle", True
    if "sqlite" in drv:
        return "sqlite", True
    if ".database.windows.net" in ds or "trustservercertificate" in kv or "multipleactiveresultsets" in kv:
        return "sqlserver", True
    if "(description" in ds or ":1521" in ds or "service name" in kv:
        return "oracle", True
    if re.search(r"\.(?:db|sqlite3?|db3)$", ds) or ds == ":memory:" or "filename" in kv:
        return "sqlite", True
    if "initial catalog" in kv or "trusted_connection" in kv or "integrated security" in kv:
        return "sqlserver", False
    port = kv.get("port", "")
    if port in sig.PORT_HINTS or re.search(r",(\d+)$|:(\d+)$", ds):
        m = re.search(r"[,:](\d+)$", ds)
        hint = sig.PORT_HINTS.get(port) or (sig.PORT_HINTS.get(m.group(1)) if m else None)
        if hint:
            return hint, False
    if "host" in kv and ("username" in kv):
        return "postgresql", False
    return "unknown-sql", False


def _scan_ado(ctx: FileContext, col: Collector, n: int, line: str) -> None:
    for m in ADO_RE.finditer(line):
        raw = m.group(0).strip()
        kv: Dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                kv.setdefault(k.strip().lower(), v.strip())
        if not (kv.keys() & {"server", "data source", "datasource", "host", "address", "addr",
                             "network address", "dbq", "filename"}):
            continue
        if not (kv.keys() & {"database", "initial catalog", "user id", "uid", "username", "user", "pwd",
                             "password", "trusted_connection", "integrated security", "version", "port",
                             "filename"}):
            continue
        db, explicit = classify_ado(kv)
        hostval = (kv.get("server") or kv.get("data source") or kv.get("datasource") or kv.get("host")
                   or kv.get("address") or kv.get("addr") or kv.get("network address") or "")
        host = re.sub(r"(?i)^(?:tcp|np):", "", hostval)
        port = kv.get("port")
        if not port:
            hm = re.search(r"[,:](\d+)$", host)
            if hm and db != "sqlite":
                port, host = hm.group(1), host[:hm.start()]
        col.connection(ctx, n, db, "ado", raw, host=host or None, port=port,
                       database=kv.get("database") or kv.get("initial catalog") or kv.get("dbname"),
                       user=kv.get("user id") or kv.get("uid") or kv.get("username") or kv.get("user"),
                       password=kv.get("password") or kv.get("pwd"), explicit=explicit,
                       tls_disabled=bool(TLS_OFF_RE.search(raw)))


def detect_connections(ctx: FileContext, col: Collector) -> None:
    lowered_seen = False
    for n, line in ctx.iter_code():
        if len(line) > 4000:
            continue
        low = line.lower()
        if "://" in line:
            _scan_urls(ctx, col, n, line)
        if "jdbc:" in low:
            _scan_jdbc(ctx, col, n, line)
        if "=" in line and ";" in line:
            _scan_ado(ctx, col, n, line)
        if "=" in line:
            m = SQLITE_FILE_RE.search(line)
            if m and not any(c.line == n and c.file == ctx.path for c in col.connections):
                col.connection(ctx, n, "sqlite", "ado", m.group(0), database=m.group(1))
        if "PDO" in line:
            m = PDO_RE.search(line)
            if m:
                drv = {"pgsql": "postgresql", "sqlite": "sqlite", "mysql": "mysql", "sqlsrv": "sqlserver",
                       "mssql": "sqlserver", "dblib": "sqlserver", "oci": "oracle"}[m.group("drv")]
                rest = m.group("rest")
                h = re.search(r"host=([^;]*)", rest)
                d = re.search(r"(?:dbname|Database)=([^;]*)", rest)
                port = re.search(r"port=(\d+)", rest)
                col.connection(ctx, n, drv, "pdo", m.group(0), host=h.group(1) if h else None,
                               port=port.group(1) if port else None,
                               database=d.group(1) if d else (rest or None if drv == "sqlite" else None),
                               user=m.group("u"), password=m.group("p"))
        if "sqlite3.connect" in line:
            m = PY_SQLITE_RE.search(line)
            if m:
                col.connection(ctx, n, "sqlite", "sqlite-file", m.group(0), database=m.group(1))
        if "Database(" in line and lowered_seen is False:
            m = JS_SQLITE_RE.search(line)
            if m:
                col.connection(ctx, n, "sqlite", "sqlite-file", m.group(0), database=m.group(1))


# ===================================================================== provider / engine settings
def _django(m):
    key = m.group(1) or ""
    if m.group(2):
        return "sqlserver", "Django ENGINE sql_server/mssql"
    db = {"sqlite3": "sqlite", "spatialite": "sqlite", "postgresql": "postgresql",
          "postgresql_psycopg2": "postgresql", "postgis": "postgresql", "mysql": "mysql", "oracle": "oracle"}.get(key)
    return (db, f"Django DATABASES ENGINE ({key})") if db else None


def _alias(label: str):
    return lambda m: (sig.db_from_value(m.group(1)), label) if sig.db_from_value(m.group(1)) else None


_EF = {"sqlserver": "sqlserver", "npgsql": "postgresql", "sqlite": "sqlite", "mysql": "mysql",
       "oracle": "oracle", "mongodb": "mongodb"}
_PROVIDER_RULES = [
    (re.compile(r"""(?i)\bENGINE['"]?\s*[:=]\s*['"](?:django\.(?:contrib\.gis\.)?db\.backends\.(\w+)|(sql_server\.pyodbc|mssql))"""),
     _django, None),
    (re.compile(r"\.Use(SqlServer|Npgsql|Sqlite|MySql|MySQL|Oracle|MongoDB)\s*\("),
     lambda m: (_EF[m.group(1).lower()], f"EF Core provider Use{m.group(1)}()"), None),
    (re.compile(r"org\.hibernate\.(?:community\.)?dialect\.(\w+?)(?:\d+\w*)?Dialect"),
     _alias("Hibernate dialect"), None),
    (re.compile(r"""\bdialect['"]?\s*[:=]\s*['"]([\w\-]+)['"]"""), _alias("ORM dialect setting"), None),
    (re.compile(r"""\bclient['"]?\s*[:=]\s*['"](pg|pg-native|mysql2?|sqlite3|better-sqlite3|mssql|oracledb|oracle|postgres)['"]"""),
     _alias("Knex client setting"), None),
    (re.compile(r"""['"]driver['"]\s*=>\s*['"](mysql|pgsql|sqlite|sqlsrv|mariadb)['"]"""), _alias("Laravel driver setting"), None),
    (re.compile(r"""\badapter:\s*['"]?(postgresql|mysql2|sqlite3|sqlserver|oracle_enhanced|mongodb)\b"""), _alias("Rails adapter setting"), None),
    (re.compile(r"""\bspring\.jpa\.database(?:-platform)?\s*[=:]\s*['"]?([\w.]+)"""), _alias("Spring JPA database setting"), None),
    (re.compile(r"""\bdriver-?class-?name\s*[=:]\s*['"]?([\w.]+)"""), _alias("JDBC driver class setting"), None),
    (re.compile(r"""\bhibernate\.dialect\s*[=:]\s*['"]?([\w.]+)"""), _alias("hibernate.dialect setting"), None),
    (re.compile(r"""\bClass\.forName\(\s*"([\w.]+)"\s*\)"""), _alias("JDBC Class.forName"), None),
    (re.compile(r"""\bsqlx?\.(?:Open|Connect|MustConnect)\(\s*"(\w+)\""""), _alias("Go database/sql driver name"), None),
    (re.compile(r"""\bprovider\s*=\s*"(\w+)\""""), _alias("Prisma datasource provider"), "prisma"),
    (re.compile(r"""\btype['"]?\s*[:=]\s*['"](postgres|mysql|mariadb|mssql|sqlite|better-sqlite3|oracle|mongodb)['"]"""),
     _alias("TypeORM connection type"), "typeorm"),
]
_CONNECT_CALL = re.compile(
    r"\b(psycopg2|psycopg|pymysql|MySQLdb|pymssql|oracledb|cx_Oracle|asyncpg|mariadb|mysql\.connector)"
    r"\.(?:connect|create_pool|connect_async)\s*\(")
_CONNECT_DB = {"psycopg2": "postgresql", "psycopg": "postgresql", "asyncpg": "postgresql", "pymysql": "mysql",
               "MySQLdb": "mysql", "mysql.connector": "mysql", "mariadb": "mariadb", "pymssql": "sqlserver",
               "oracledb": "oracle", "cx_Oracle": "oracle"}
_MONGO_CLIENT = re.compile(r"\b(?:MongoClient|AsyncIOMotorClient)\s*\(")
_MONGO_SHELL = re.compile(r"\bdb\.(?:getSiblingDB|createCollection)\s*\(")


def detect_providers(ctx: FileContext, col: Collector) -> None:
    typeorm_file = "typeorm" in ctx.lower_text or ctx.basename.startswith("ormconfig")
    for n, line in ctx.iter_code():
        if len(line) > 4000:
            continue
        for rx, fn, only in _PROVIDER_RULES:
            if only == "prisma" and ctx.category != "prisma":
                continue
            if only == "typeorm" and not typeorm_file:
                continue
            m = rx.search(line)
            if not m:
                continue
            res = fn(m)
            if res and res[0]:
                col.add(res[0], CONFIRMED, "provider", ctx, n, f"{res[1]} selects {sig.DB_DISPLAY.get(res[0], res[0])}", line)
        if ctx.category == "source":
            m = _CONNECT_CALL.search(line)
            if m:
                db = _CONNECT_DB[m.group(1)]
                col.add(db, STRONG, "connect_call", ctx, n, f"{m.group(1)}.connect(...) call", line)
            if _MONGO_CLIENT.search(line):
                col.add("mongodb", STRONG, "connect_call", ctx, n, "MongoClient(...) call", line)
            if ctx.lang in ("javascript", "typescript") and _MONGO_SHELL.search(line):
                col.add("mongodb", STRONG, "connect_call", ctx, n, "MongoDB shell/script API (db.getSiblingDB/createCollection)", line)


# ===================================================================== config keys
_KEY_LINE = re.compile(r"""^\s*(?:export\s+)?["']?(?P<key>[A-Za-z_][\w.\-]*)["']?\s*(?:=>|[=:])\s*(?P<val>.*?)\s*,?\s*$""")
_SECTION_INI = re.compile(r"^\[([^\]\"']+)\]$")
_SECTION_MAP = re.compile(r"""^["']?([\w.\-]+)["']?\s*:\s*\{?\s*$""")
_DBISH = re.compile(r"(?i)(?:^|[._\-])(?:db|database|datasource|datasources|sql|mongo\w*|postgres\w*|pg|mysql\w*|"
                    r"oracle|mssql|sqlserver|sqlite|jdbc|maria\w*|connection)(?:$|[._\-])|^sa$")
_ENGINE_KEY = re.compile(r"(?:db_?(?:connection|driver|engine|type|dialect|adapter)|"
                         r"database_?(?:type|engine|driver|dialect|provider|adapter|vendor)|sqlalchemy_database_uri?|"
                         r"(?:[\w.\-]+\.)?(?:engine|dialect|adapter|provider))")
_FIELD = re.compile(r"^(?P<prefix>.*?)[._]?(?P<field>host|hostname|server|address|port|name|db|dbname|database|"
                    r"user|username|password|passwd|pwd)$")
_SHORT_KEYS = {"host", "hostname", "port", "name", "dbname", "database", "user", "username", "password",
               "passwd", "pwd", "server", "address", "engine", "dialect", "driver", "db"}


def _unquote(v: str) -> str:
    v = v.strip().rstrip(",")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v.strip("'\"")


def detect_config_keys(ctx: FileContext, col: Collector) -> None:
    if ctx.category not in ("config", "docker"):
        return
    section = ""
    groups: Dict[str, Dict[str, Tuple[int, str, str]]] = defaultdict(dict)
    engine_db: Optional[str] = None
    for n, line in ctx.iter_code():
        s = line.strip()
        sm = _SECTION_INI.match(s)
        if sm:
            section = sm.group(1).lower()
            continue
        m = _KEY_LINE.match(line)
        if not m:
            mm = _SECTION_MAP.match(s)
            if mm:
                section = mm.group(1).lower()
            continue
        key = m.group("key").lower()
        val = _unquote(m.group("val"))
        if val in ("", "{", "[", "|", ">") and ctx.ext in (".yml", ".yaml", ".json", ".toml"):
            section = key
            continue
        full = f"{section}.{key}" if section and key in _SHORT_KEYS and _DBISH.search(section) else key
        upper = m.group("key").upper()
        if sig.DB_ENV_NAME_RE.match(upper) and (ctx.basename.startswith(".env") or ctx.category == "docker"
                                                or ctx.ext in (".env", ".properties", ".ini", ".cfg", ".conf")):
            col.env(upper, "definition", ctx, n)
        if _ENGINE_KEY.fullmatch(key) or _ENGINE_KEY.fullmatch(full):
            db = sig.db_from_value(val)
            if db:
                engine_db = db
                col.add(db, CONFIRMED, "config_key", ctx, n, f"'{m.group('key')}' selects {sig.DB_DISPLAY.get(db, db)}", line)
        fm = _FIELD.match(full)
        if fm and _DBISH.search("_" + fm.group("prefix") + "_" if fm.group("prefix") else "_db_"):
            prefix = re.sub(r"[._\-]+", "_", fm.group("prefix")).strip("_") or "db"
            groups[prefix][fm.group("field")] = (n, val, line)
        elif fm and not fm.group("prefix") and section and _DBISH.search(section):
            groups[re.sub(r"[._\-]+", "_", section)][fm.group("field")] = (n, val, line)
    for prefix, f in groups.items():
        _build_group_connection(ctx, col, prefix, f, engine_db)


def _build_group_connection(ctx: FileContext, col: Collector, prefix: str,
                            f: Dict[str, Tuple[int, str, str]], engine_db: Optional[str]) -> None:
    def get(*names: str) -> Optional[Tuple[int, str, str]]:
        return next((f[x] for x in names if x in f), None)

    host, port = get("host", "hostname", "server", "address"), get("port")
    name, user = get("dbname", "database", "name", "db"), get("user", "username")
    pw = get("password", "passwd", "pwd")
    if not (host or (port and name) or pw or (user and name)):
        return
    prefix_db = "sqlserver" if prefix == "sa" else sig.db_from_value(prefix)
    port_db = sig.PORT_HINTS.get(port[1]) if port else None
    db = prefix_db or engine_db or port_db or "unknown-sql"
    explicit = bool(prefix_db or engine_db)
    if db == "unknown-sql":  # e.g. spring.datasource.username/password next to a jdbc: URL in the same file
        sibling = next((c.db for c in col.connections if c.file == ctx.path and c.db != "unknown-sql"), None)
        db = sibling or db
    first = min(v[0] for v in f.values())
    raw = "; ".join(f"{k}={v[1]}" for k, v in f.items())
    col.connection(ctx, first, db, "config-keys", raw, host=host[1] if host else None,
                   port=port[1] if port else None, database=name[1] if name else None,
                   user=user[1] if user else None, password=pw[1] if pw else None, explicit=explicit)
    if pw and not looks_templated(pw[1]) and ctx.scope != SCOPE_EXAMPLE:
        col.flagged_lines.add((ctx.path, pw[0]))


# ===================================================================== container services
_IMAGE = re.compile(r"""(?i)^\s*(?:-\s*)?(?:image\s*:|FROM)\s*['"]?(?P<img>[\w./\-]+)""")


def detect_services(ctx: FileContext, col: Collector) -> None:
    if ctx.category != "docker":
        return
    for n, line in ctx.iter_code():
        m = _IMAGE.match(line)
        if not m:
            continue
        img = m.group("img").lower()
        base = img.rsplit("/", 1)[-1]
        if "exporter" in img:
            continue
        db = None
        if "mssql" in img:
            db = "sqlserver"
        elif base.startswith(("postgres", "postgis")):
            db = "postgresql"
        elif base.startswith("mariadb"):
            db = "mariadb"
        elif base.startswith("mysql"):
            db = "mysql"
        elif base.startswith("mongo"):
            db = "mongodb"
        elif base.startswith("oracle") or "oracle-xe" in img or "oracle/database" in img:
            db = "oracle"
        if db:
            col.add(db, STRONG, "service", ctx, n, f"container image '{m.group('img')}' defines a {sig.DB_DISPLAY[db]} service", line)


# ===================================================================== environment variable references
_ENV_REF = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|process\.env\.|process\.env\[\s*|\bgetenv\s*\(\s*|"""
    r"""\benv\s*\(\s*|System\.getenv\s*\(\s*|Environment\.GetEnvironmentVariable\s*\(\s*|\$_(?:ENV|SERVER)\[\s*|"""
    r"""\$\{|\$|ENV\[\s*)['"]?(?P<n>[A-Z][A-Z0-9_]+)""")


def detect_env_refs(ctx: FileContext, col: Collector) -> None:
    if ctx.category not in ("source", "config", "docker", "manifest"):
        return
    for n, line in ctx.iter_code():
        if "$" not in line and "env" not in line.lower():
            continue
        for m in _ENV_REF.finditer(line):
            col.env(m.group("n"), "reference", ctx, n)


# ===================================================================== SQL-injection heuristics (low confidence)
_SQL_WORD = re.compile(r"(?i)\b(?:select|insert|update|delete|where|from)\b")
_INJ = {
    "python": re.compile(
        r"""\.(?:execute|executemany|executescript|raw|read_sql(?:_query)?|text)\(\s*(?:f(?:"[^"]*\{[^"]*"|'[^']*\{[^']*')|(?:"[^"]*"|'[^']*')\s*(?:%\s*[\w(]|\+\s*\w|\.format\())"""),
    "js": re.compile(
        r"""\.(?:query|execute|raw|\$queryRawUnsafe|\$executeRawUnsafe)\(\s*(?:`[^`]*\$\{|(?:"[^"]*"|'[^']*')\s*\+\s*\w)"""),
    "php": re.compile(r"""(?:->query|mysqli_query|pg_query)\(\s*(?:[^,()]+,\s*)?"[^"]*\$\w+|->query\(\s*['"][^'"]*['"]\s*\.\s*\$"""),
    "java": re.compile(r"""(?:executeQuery|executeUpdate|createStatement\(\)\.execute)\(\s*"[^"]*"\s*\+"""),
    "dotnet": re.compile(r"""(?:new\s+\w*Command\(\s*|CommandText\s*=\s*)(?:\$"[^"]*\{|"[^"]*"\s*\+)"""),
    "go": re.compile(r"""\.(?:Query|Exec|QueryRow)(?:Context)?\(\s*(?:[\w.]+,\s*)?(?:fmt\.Sprintf\(|"[^"]*"\s*\+)"""),
}
_INJ_LANG = {"python": "python", "javascript": "js", "typescript": "js", "php": "php", "java": "java",
             "kotlin": "java", "csharp": "dotnet", "vbnet": "dotnet", "go": "go"}


def detect_injection(ctx: FileContext, col: Collector) -> None:
    key = _INJ_LANG.get(ctx.lang or "")
    if ctx.category != "source" or not key:
        return
    rx = _INJ[key]
    for n, line in ctx.iter_code():
        if len(line) > 2000 or not _SQL_WORD.search(line):
            continue
        if rx.search(line):
            col.finding(
                "DB-SEC-003", "low" if ctx.scope != "production-or-unknown" else "medium",
                "SQL built by string concatenation/interpolation (heuristic)", ctx.path, n, line.strip(),
                "A SQL statement appears to be assembled from variables instead of bound parameters. If any part "
                "is user-controlled this allows SQL injection. This is a pattern match only - it has NOT been "
                "confirmed as exploitable.",
                "Use parameterised queries / prepared statements or the ORM's query builder.", "low", None,
                "database-security")


# ===================================================================== docs / comment mentions (weak)
_MENTION = {
    "sqlite": re.compile(r"(?i)\bsqlite\b"),
    "postgresql": re.compile(r"(?i)\bpostgres(?:ql)?\b"),
    "mysql": re.compile(r"(?i)\bmysql\b"),
    "mariadb": re.compile(r"(?i)\bmariadb\b"),
    "sqlserver": re.compile(r"(?i)\b(?:sql\s*server|mssql)\b"),
    "oracle": re.compile(r"(?i)\boracle\s+(?:db|database|xe|19c|12c|11g)\b|\bpl/sql\b"),
    "mongodb": re.compile(r"(?i)\bmongo(?:db)?\b"),
}


def detect_mentions(ctx: FileContext, col: Collector) -> None:
    if ctx.category == "docs":
        source = ((i, l) for i, l in enumerate(ctx.lines, 1))
    else:
        source = ctx.iter_comments()
    seen = set()
    for n, line in source:
        if len(line) > 2000:
            continue
        for db, rx in _MENTION.items():
            if db not in seen and rx.search(line):
                seen.add(db)
                col.add(db, WEAK, "documentation", ctx, n,
                        "mentioned in documentation" if ctx.category == "docs" else "mentioned in a code comment", line)
        if ctx.category == "docs":
            for m in URL_RE.finditer(line):
                db = _SCHEME_DB[m.group("scheme").lower()]
                col.add(db, WEAK, "documentation", ctx, n, "connection string example in documentation", line)
            for m in JDBC_RE.finditer(line):
                col.add(_JDBC_DB[m.group("sub").lower()], WEAK, "documentation", ctx, n,
                        "JDBC URL example in documentation", line)


# ===================================================================== credential literals (second pass)
_PW_LITERAL = re.compile(
    r"""(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.\-])*(?:password|passwd|pwd)['"]?\s*(?:=>|[=:])\s*(['"])(?P<v>[^'"]+)\1""")


def detect_credential_literals(ctx: FileContext, col: Collector) -> None:
    if ctx.category not in ("source", "config") or ctx.path not in col.files_with_db_access:
        return
    if ctx.scope == SCOPE_EXAMPLE:
        return
    for n, line in ctx.iter_code():
        if (ctx.path, n) in col.flagged_lines or len(line) > 2000:
            continue
        m = _PW_LITERAL.search(line)
        if m and not looks_templated(m.group("v")):
            col.flagged_lines.add((ctx.path, n))
            col.finding(
                "DB-SEC-001", "low" if ctx.scope == "test" else "high", "Hard-coded database credential",
                ctx.path, n, line.strip(),
                "A password literal appears in a file that also contains database access code. The value is "
                "masked in this report.",
                "Load the credential from an environment variable or secret store and rotate the exposed value.",
                "medium", None, "database-security")


# ===================================================================== driver imports
def detect_imports(ctx: FileContext, col: Collector) -> None:
    rules = sig.IMPORT_RULES_BY_LANG.get(ctx.lang or "")
    if ctx.category != "source" or not rules:
        return
    for n, line in ctx.iter_code():
        if len(line) > 2000:
            continue
        for rx, db, label in rules:
            if rx.search(line):
                col.add(db, STRONG, "import", ctx, n, f"{label} imported/used", line)
