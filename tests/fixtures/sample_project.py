"""Builds the realistic multi-language sample project used by tests and the demo."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict

# NOTE: every secret below is a fake placeholder value used to exercise redaction.
SAMPLE_FILES: Dict[str, str] = {
    "shop/requirements.txt": "flask==3.0.0\npsycopg2-binary>=2.9  # PostgreSQL driver (declared only)\npytest\nrequests~=2.31\n",
    "shop/app/main.py": '''"""Flask storefront."""
import sqlite3
from flask import Flask, request

app = Flask(__name__)
DEBUG = True  # TODO: turn off before release


def get_db():
    return sqlite3.connect("shop.db")


@app.route("/search")
def search():
    term = request.args.get("q", "")
    cur = get_db().cursor()
    cur.execute(f"SELECT * FROM products WHERE name LIKE '%{term}%'")
    return str(cur.fetchall())


@app.route("/calc")
def calc():
    return str(eval(request.args.get("expr", "0")))  # FIXME: unsafe
''',
    "shop/app/broken.py": "def oops(:\n    return 1\n",
    "shop/config/settings.py": 'DATABASE_URL = "postgresql://admin:Sup3rS3cret!@db.example.com:5432/shop"\nAPI_KEY = "abcd1234efgh5678"\n',
    "shop/db/schema.sql": "-- schema\nCREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT);\n",
    "shop/README.md": "# Shop\nMay migrate to MySQL someday (not implemented).\n",
    "web/package.json": '{\n  "name": "web",\n  "engines": {"node": ">=18"},\n  "dependencies": {\n    "express": "^4.18.0",\n    "react": "^18.2.0",\n    "mongoose": "^7.0.0"\n  }\n}\n',
    "web/server.js": "const express = require('express');\nconst app = express();\napp.get('/', (req, res) => { document.body.innerHTML = req.query.x; res.send('ok'); });\n",
    "api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n  <ItemGroup>\n    <PackageReference Include="Microsoft.Data.SqlClient" Version="5.1.0" />\n    <PackageReference Include="Microsoft.EntityFrameworkCore" Version="8.0.0" />\n  </ItemGroup>\n</Project>\n',
    "api/appsettings.json": '{\n  "ConnectionStrings": {\n    "Default": "Server=sql01;Database=Shop;User Id=sa;Password=Fake#Pass1;"\n  }\n}\n',
    "api/Program.cs": "var builder = WebApplication.CreateBuilder(args);\nvar app = builder.Build();\napp.Run();\n",
    "svc/pom.xml": "<project>\n  <properties><java.version>17</java.version></properties>\n  <dependencies>\n    <dependency>\n      <groupId>org.springframework.boot</groupId>\n      <artifactId>spring-boot-starter-web</artifactId>\n      <version>3.2.0</version>\n    </dependency>\n  </dependencies>\n</project>\n",
    "svc/src/Main.java": "public class Main { public static void main(String[] a) { /* TODO implement */ } }\n",
    "tools/go.mod": "module example.com/tools\n\ngo 1.21\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n",
    "tools/main.go": "package main\nfunc main() {}\n",
    "site/composer.json": '{"require": {"php": "^8.1", "laravel/framework": "^10.0"}}\n',
    "site/index.php": "<?php echo 'hi';\n",
    "site/style.css": "body { margin: 0; }\n",
    "shop/node_modules/left-pad/index.js": "module.exports = function () { eval('1'); };\n",
}


def build_sample_zip(path: str | Path) -> Path:
    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in SAMPLE_FILES.items():
            zf.writestr(name, content)
    return path
