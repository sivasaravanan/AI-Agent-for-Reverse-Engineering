"""File classification, scope detection and comment-aware line preparation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from .models import SCOPE_EXAMPLE, SCOPE_PROD, SCOPE_TEST

SOURCE_LANGS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".cs": "csharp", ".vb": "vbnet", ".java": "java",
    ".kt": "kotlin", ".php": "php", ".go": "go", ".rb": "ruby", ".sh": "shell", ".ps1": "powershell",
}
SQL_EXTS = {".sql", ".ddl", ".psql", ".pgsql", ".tsql", ".plsql", ".mysql"}
CONFIG_EXTS = {".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".config", ".properties",
               ".xml", ".env", ".settings", ".props"}
DOC_EXTS = {".md", ".rst", ".txt", ".adoc", ".markdown"}
DATA_FILE_EXTS = {".sqlite", ".sqlite3", ".db", ".db3", ".s3db", ".mdf", ".ldf", ".bson"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "composer.lock", "poetry.lock",
             "pipfile.lock", "npm-shrinkwrap.json"}

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "testing", "testdata", "unittests",
             "integrationtests"}


def manifest_kind(path: str) -> Optional[str]:
    low = path.lower()
    b = low.rsplit("/", 1)[-1]
    parent = low.rsplit("/", 2)[-2] if "/" in low else ""
    if re.fullmatch(r"(?:requirements|constraints)[\w.\-]*\.txt", b) or \
            (parent in ("requirements", "reqs") and b.endswith(".txt")):
        return "requirements"
    simple = {"pyproject.toml": "pyproject", "pipfile": "pipfile", "setup.py": "setup_py",
              "setup.cfg": "setup_cfg", "package.json": "package_json", "pom.xml": "pom",
              "build.gradle": "gradle", "build.gradle.kts": "gradle", "packages.config": "packages_config",
              "directory.packages.props": "csproj", "composer.json": "composer", "go.mod": "gomod"}
    if b in simple:
        return simple[b]
    if b.endswith((".csproj", ".vbproj", ".fsproj")):
        return "csproj"
    return None


def classify(path: str) -> Tuple[str, Optional[str]]:
    b = path.lower().rsplit("/", 1)[-1]
    ext = os.path.splitext(b)[1]
    if manifest_kind(path):
        return "manifest", None
    if b.startswith("dockerfile") or b.endswith(".dockerfile") or re.match(r"(?:docker-)?compose[\w.\-]*\.ya?ml$", b):
        return "docker", None
    if ext == ".prisma":
        return "prisma", None
    if ext in SQL_EXTS:
        return "sql", "sql"
    if ext in SOURCE_LANGS:
        return "source", SOURCE_LANGS[ext]
    if b.startswith(".env") or ext in CONFIG_EXTS or b in ("web.config", "app.config"):
        return "config", None
    if ext in DOC_EXTS or b.startswith("readme"):
        return "docs", None
    return "other", None


def detect_scope(path: str) -> str:
    low = path.lower()
    parts = low.split("/")
    base = parts[-1]
    if any(p in TEST_DIRS for p in parts[:-1]) or re.search(
            r"(^test_|_test\.|\.test\.|\.spec\.|tests?\.(?:cs|vb|java|kt)$)", base):
        return SCOPE_TEST
    if re.search(r"(?:^|[._-])(?:example|sample|template|tmpl|dist)(?:[._-]|$)", base) or \
            any(p in ("examples", "docs", "doc") for p in parts[:-1]):
        return SCOPE_EXAMPLE
    return SCOPE_PROD


def _comment_style(ext: str, base: str, category: str) -> str:
    if category == "docs":
        return "none"
    if ext in (".py", ".rb", ".sh", ".ps1", ".yml", ".yaml", ".toml", ".env") or base.startswith(".env") \
            or category == "docker":
        return "hash"
    if ext in (".ini", ".cfg", ".conf", ".properties"):
        return "ini"
    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".cs", ".go", ".prisma"):
        return "slash"
    if ext == ".php":
        return "php"
    if category == "sql":
        return "sql"
    if ext == ".vb":
        return "vb"
    if ext in (".xml", ".config", ".csproj", ".vbproj", ".fsproj", ".props", ".html", ".htm"):
        return "xml"
    return "none"


_PREFIXES = {"hash": ("#",), "ini": ("#", ";", "!"), "slash": ("//",), "php": ("//", "#"), "sql": ("--",),
             "vb": ("'",), "xml": (), "none": ()}
_BLOCK_C = re.compile(r"/\*.*?\*/", re.S)
_BLOCK_X = re.compile(r"<!--.*?-->", re.S)


def _blank(m: "re.Match[str]") -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


@dataclass
class FileContext:
    path: str
    basename: str
    ext: str
    category: str
    lang: Optional[str]
    scope: str
    text: str
    lines: List[str]
    code: List[str]
    comments: List[bool]
    _lower: Optional[str] = field(default=None, repr=False)

    @property
    def is_example(self) -> bool:
        return self.scope == SCOPE_EXAMPLE

    @property
    def lower_text(self) -> str:
        if self._lower is None:
            self._lower = self.text.lower()
        return self._lower

    @property
    def code_text(self) -> str:
        return "\n".join(self.code)

    def iter_code(self) -> Iterator[Tuple[int, str]]:
        for i, line in enumerate(self.code, 1):
            if line.strip():
                yield i, line

    def iter_comments(self) -> Iterator[Tuple[int, str]]:
        for i, (flag, line) in enumerate(zip(self.comments, self.lines), 1):
            if flag:
                yield i, line


def build_context(path: str, text: str, category: str, lang: Optional[str]) -> FileContext:
    base = path.lower().rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1]
    style = _comment_style(ext, base, category)
    work = text
    if style in ("slash", "php", "sql"):
        work = _BLOCK_C.sub(_blank, work)
    elif style == "xml":
        work = _BLOCK_X.sub(_blank, work)
    lines = text.splitlines()
    blanked = work.splitlines()
    if len(blanked) != len(lines):  # defensive: keep the two aligned
        blanked = lines
    prefixes = _PREFIXES[style]
    code: List[str] = []
    flags: List[bool] = []
    for orig, b in zip(lines, blanked):
        is_c = False
        if orig.strip():
            s = b.strip()
            if not s or (prefixes and s.startswith(prefixes)) or (style == "vb" and s.lower().startswith("rem ")):
                is_c = True
        flags.append(is_c)
        code.append("" if is_c else b)
    return FileContext(path, base, ext, category, lang, detect_scope(path), text, lines, code, flags)


def decode_text(data: bytes) -> Optional[str]:
    if b"\x00" in data[:4096]:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return data.decode("utf-16")
            except UnicodeError:
                return None
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")
