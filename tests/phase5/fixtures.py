"""Programmatic sample projects (no secrets other than clearly fake test values)."""
import io
import zipfile

SECRET = "SuperSecret123"


def multi_db_project():
    return {
        "app/main.py": (
            "import psycopg2\nfrom sqlalchemy import Column, Integer, String\n"
            "from sqlalchemy.orm import declarative_base\nBase = declarative_base()\n\n"
            "class User(Base):\n    __tablename__ = 'users'\n    id = Column(Integer, primary_key=True)\n"
            "    name = Column(String)\n\n"
            f"DSN = 'postgresql://appuser:{SECRET}@db.internal.corp:5432/shop'\n"
            "def get(uid, cur):\n    cur.execute(f\"SELECT * FROM users WHERE id={uid}\")\n"),
        "requirements.txt": "psycopg2-binary==2.9.9\nsqlalchemy>=2.0\nalembic\nrequests\n",
        "alembic/versions/0001_init.py": "def upgrade():\n    op.create_table('users', sa.Column('id'))\n",
        "web/package.json": '{"name":"w","dependencies":{"mysql2":"^3.0","mongoose":"^7","express":"4"}}',
        "web/db.js": ("const mysql = require('mysql2');\n"
                      "const m = require('mongoose');\n"
                      "const Cat = mongoose.model('Cat', new Schema({n:String}));\n"
                      "mysql.createConnection('mysql://shop:pw1@mysql.internal.corp:3306/catalog');\n"),
        "dotnet/App.csproj": '<Project><ItemGroup><PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" Version="8.0.0" />'
                             '<PackageReference Include="Microsoft.EntityFrameworkCore" Version="8.0.0" /></ItemGroup></Project>',
        "dotnet/Ctx.cs": ("using Microsoft.EntityFrameworkCore;\npublic class ShopCtx : DbContext {\n"
                          "  public DbSet<Order> Orders { get; set; }\n"
                          "  protected override void OnConfiguring(DbContextOptionsBuilder b) => b.UseSqlServer(cs);\n}\n"),
        "dotnet/appsettings.json": ('{"ConnectionStrings":{"Default":"Server=sql01.corp.local,1433;Database=Shop;'
                                    f'User Id=svc_shop;Password={SECRET};TrustServerCertificate=True;"}}}}'),
        "java/pom.xml": ("<project><dependencies><dependency><groupId>com.oracle.database.jdbc</groupId>"
                         "<artifactId>ojdbc8</artifactId><version>21.1</version></dependency></dependencies></project>"),
        "java/application.properties": ("spring.datasource.url=jdbc:oracle:thin:@//ora01.corp.local:1521/ORCLPDB1\n"
                                        "spring.datasource.username=app\nspring.datasource.password=" + SECRET + "\n"),
        "java/Customer.java": ('import javax.persistence.*;\n@Entity\n@Table(name = "customers")\npublic class Customer {}\n'),
        "db/schema.sql": ("-- comment mentioning mysql only\nCREATE TABLE orders (\n  id INT IDENTITY(1,1) PRIMARY KEY,\n"
                          "  customer_id INT REFERENCES customers(id),\n  title NVARCHAR(100)\n);\nGO\n"),
        "db/seed_mysql.sql": "CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;\nINSERT INTO t VALUES (1);\n",
        "docker-compose.yml": ("services:\n  db:\n    image: postgres:16\n    environment:\n"
                               f"      POSTGRES_PASSWORD: {SECRET}\n      POSTGRES_USER: app\n"),
        "README.md": "We might move to MongoDB later.\n",
        "php/conn.php": "<?php $p = new PDO('mysql:host=h1.corp.local;dbname=blog', 'bloguser', 'pw2');\n",
        "go/main.go": 'package main\nimport _ "github.com/lib/pq"\nfunc main(){ sql.Open("postgres", "x") }\n',
    }


def negative_project():
    return {
        "requirements.txt": "psycopg2-binary==2.9\nflask\n",
        "app.py": "# TODO: maybe use mysql://user:pw@host/db later\nprint('hello')\n",
        "docs/notes.md": "Considering PostgreSQL or Oracle Database.\n",
        "src/other.py": "x = 1\n",
    }


def build_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    return path
