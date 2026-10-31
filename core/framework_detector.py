"""Framework / runtime identification from manifests, marker files and imports.

Confidence: strong = declared in a manifest or explicit config,
            moderate = marker file or import in source,
            weak = not used in Phase 1.
"""
from __future__ import annotations

import posixpath
import re
from typing import Dict, List, Mapping, Sequence

from models.analysis_result import CONFIDENCE_RANK, Detection, Evidence, Manifest
from security.secret_scanner import redact

# ecosystem -> exact lowercase dependency name -> (display name, kind)
DEP_RULES: Dict[str, Dict[str, tuple]] = {
    "python": {
        "django": ("Django", "web framework"), "flask": ("Flask", "web framework"),
        "fastapi": ("FastAPI", "web framework"), "tornado": ("Tornado", "web framework"),
        "streamlit": ("Streamlit", "web framework"), "pytest": ("pytest", "test framework"),
        "sqlalchemy": ("SQLAlchemy", "ORM"), "pyside6": ("PySide6", "GUI toolkit"),
        "pyqt5": ("PyQt5", "GUI toolkit"), "pyqt6": ("PyQt6", "GUI toolkit"),
        "customtkinter": ("CustomTkinter", "GUI toolkit"), "flet": ("Flet", "GUI toolkit"),
        "nicegui": ("NiceGUI", "web framework"),
    },
    "javascript": {
        "react": ("React", "frontend framework"), "vue": ("Vue", "frontend framework"),
        "@angular/core": ("Angular", "frontend framework"), "svelte": ("Svelte", "frontend framework"),
        "next": ("Next.js", "web framework"), "nuxt": ("Nuxt", "web framework"),
        "express": ("Express", "web framework"), "@nestjs/core": ("NestJS", "web framework"),
        "jest": ("Jest", "test framework"), "mocha": ("Mocha", "test framework"),
        "vite": ("Vite", "build tool"), "electron": ("Electron", "desktop framework"),
        "typescript": ("TypeScript compiler", "build tool"),
    },
    "php": {
        "laravel/framework": ("Laravel", "web framework"), "phpunit/phpunit": ("PHPUnit", "test framework"),
    },
}
# prefix rules for ecosystems whose package ids are namespaced
PREFIX_RULES: Dict[str, List[tuple]] = {
    "dotnet": [
        ("microsoft.entityframeworkcore", "Entity Framework Core", "ORM"),
        ("xunit", "xUnit", "test framework"), ("nunit", "NUnit", "test framework"),
        ("dapper", "Dapper", "micro-ORM"), ("microsoft.aspnetcore", "ASP.NET Core", "web framework"),
    ],
    "java": [
        ("org.springframework.boot:", "Spring Boot", "web framework"),
        ("org.hibernate", "Hibernate", "ORM"), ("junit:", "JUnit", "test framework"),
        ("org.junit", "JUnit", "test framework"),
    ],
    "php": [("symfony/", "Symfony components", "web framework")],
    "go": [
        ("github.com/gin-gonic/gin", "Gin", "web framework"),
        ("github.com/labstack/echo", "Echo", "web framework"),
        ("github.com/gofiber/fiber", "Fiber", "web framework"),
    ],
}

PY_IMPORTS = {
    "flask": "Flask", "django": "Django", "fastapi": "FastAPI", "tkinter": "Tkinter",
    "PySide6": "PySide6", "customtkinter": "CustomTkinter", "flet": "Flet",
}
JS_IMPORTS = {"react": "React", "vue": "Vue", "express": "Express"}
_PY_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", re.M)
_JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*)['"]([\w@/.\-]+)['"]""")

MAX_EVIDENCE = 6


class _Collector:
    def __init__(self) -> None:
        self.items: Dict[str, Detection] = {}

    def add(self, name: str, kind: str, confidence: str, file: str, line, text: str) -> None:
        det = self.items.get(name)
        if det is None:
            det = self.items[name] = Detection(name=name, kind=kind, confidence=confidence)
        det.evidence_count += 1
        if CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[det.confidence]:
            det.confidence = confidence
        if len(det.evidence) < MAX_EVIDENCE:
            det.evidence.append(Evidence(file, line, redact(text)[:200]))

    def result(self) -> List[Detection]:
        out = list(self.items.values())
        out.sort(key=lambda d: (-CONFIDENCE_RANK[d.confidence], d.kind, d.name))
        return out


def _first_line(text: str, pattern: str):
    m = re.search(pattern, text)
    return (text.count("\n", 0, m.start()) + 1, m.group(0)) if m else (None, "")


def detect_frameworks(manifests: Sequence[Manifest], texts: Mapping[str, str],
                      file_names: Sequence[str]) -> List[Detection]:
    col = _Collector()
    # 1. declared dependencies
    for mf in manifests:
        for dep in mf.dependencies:
            key = dep.name.lower().replace("_", "-")
            hit = DEP_RULES.get(dep.ecosystem, {}).get(key)
            if hit:
                col.add(hit[0], hit[1], "strong", dep.file, dep.line, f"{dep.name} {dep.spec}".strip())
                continue
            for prefix, name, kind in PREFIX_RULES.get(dep.ecosystem, []):
                if dep.name.lower().startswith(prefix):
                    col.add(name, kind, "strong", dep.file, dep.line, f"{dep.name} {dep.spec}".strip())
                    break
    # 2. manifest-level runtime / SDK information
    for mf in manifests:
        text = texts.get(mf.path)
        if not text:
            continue
        base = posixpath.basename(mf.path).lower()
        if base == "package.json":
            line, snippet = _first_line(text, r'"node"\s*:\s*"[^"]+"')
            if line:
                col.add("Node.js " + snippet.split(":")[1].strip(' "'), "runtime", "strong", mf.path, line, snippet)
            else:
                col.add("Node.js", "runtime", "moderate", mf.path, 1, "package.json present")
        elif base.endswith((".csproj", ".vbproj")):
            line, snippet = _first_line(text, r"<TargetFrameworks?>[^<]+</TargetFrameworks?>")
            if line:
                tfm = re.sub(r"<[^>]+>", "", snippet)
                col.add(f".NET {tfm}", "runtime", "strong", mf.path, line, snippet)
            if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"', text):
                line, snippet = _first_line(text, r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"')
                col.add("ASP.NET Core", "web framework", "strong", mf.path, line, snippet)
            for flag, name in (("UseWindowsForms", "Windows Forms"), ("UseWPF", "WPF")):
                line, snippet = _first_line(text, rf"<{flag}>\s*true\s*</{flag}>")
                if line:
                    col.add(name, "desktop framework", "strong", mf.path, line, snippet)
        elif base == "pom.xml":
            line, snippet = _first_line(text, r"<(?:java\.version|maven\.compiler\.(?:source|release))>[^<]+<")
            if line:
                ver = re.sub(r"<[^>]*>?", "", snippet)
                col.add(f"Java {ver}", "runtime", "strong", mf.path, line, snippet)
            else:
                col.add("Java", "runtime", "moderate", mf.path, 1, "pom.xml present")
            if "spring-boot" in text:
                line, snippet = _first_line(text, r"[\w.\-:]*spring-boot[\w.\-]*")
                col.add("Spring Boot", "web framework", "strong", mf.path, line, snippet)
        elif base == "go.mod":
            line, snippet = _first_line(text, r"(?m)^go\s+\d[\d.]*")
            if line:
                col.add(snippet.replace("go ", "Go ", 1), "runtime", "strong", mf.path, line, snippet)
        elif base == "composer.json":
            line, snippet = _first_line(text, r'"php"\s*:\s*"[^"]+"')
            if line:
                col.add("PHP " + snippet.split(":")[1].strip(' "'), "runtime", "strong", mf.path, line, snippet)
        elif base == "pyproject.toml":
            line, snippet = _first_line(text, r'requires-python\s*=\s*"[^"]+"')
            if line:
                col.add("Python " + snippet.split("=")[1].strip(' "'), "runtime", "strong", mf.path, line, snippet)
    # 3. marker files
    for name in file_names:
        base = posixpath.basename(name).lower()
        text = texts.get(name, "")
        if base == "manage.py" and "DJANGO_SETTINGS_MODULE" in text:
            line, snip = _first_line(text, r"DJANGO_SETTINGS_MODULE")
            col.add("Django", "web framework", "strong", name, line, snip)
        elif base == "artisan" and "laravel" in text.lower():
            col.add("Laravel", "web framework", "moderate", name, 1, "artisan script")
        elif base in ("angular.json",):
            col.add("Angular", "frontend framework", "moderate", name, 1, "angular.json present")
        elif base.startswith("next.config."):
            col.add("Next.js", "web framework", "moderate", name, 1, base)
        elif base in ("program.cs", "startup.cs") and re.search(r"WebApplication\.CreateBuilder|UseStartup", text):
            line, snip = _first_line(text, r"WebApplication\.CreateBuilder|UseStartup")
            col.add("ASP.NET Core", "web framework", "strong", name, line, snip)
    # 4. imports in source (moderate: shows use, not necessarily a declared dependency)
    for name, text in texts.items():
        ext = posixpath.splitext(name)[1].lower()
        if ext in (".py", ".pyw"):
            for m in _PY_IMPORT.finditer(text[:60_000]):
                label = PY_IMPORTS.get(m.group(1))
                if label:
                    kind = "GUI toolkit" if label in ("Tkinter", "PySide6", "CustomTkinter", "Flet") else "web framework"
                    col.add(label, kind, "moderate", name, text.count("\n", 0, m.start()) + 1, m.group(0).strip())
        elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
            for m in _JS_IMPORT.finditer(text[:60_000]):
                label = JS_IMPORTS.get(m.group(1))
                if label:
                    col.add(label, "frontend framework" if label != "Express" else "web framework",
                            "moderate", name, text.count("\n", 0, m.start()) + 1, m.group(0).strip())
    return col.result()
