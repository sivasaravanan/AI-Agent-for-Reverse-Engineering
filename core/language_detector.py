"""Programming-language detection from extensions and project markers."""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Dict, Iterable, List

from core.archive_manager import ArchiveEntry
from models.analysis_result import LanguageStat

EXTENSION_LANGUAGE: Dict[str, str] = {
    ".py": "Python", ".pyw": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".cs": "C#", ".vb": "VB.NET", ".java": "Java", ".kt": "Kotlin",
    ".sql": "SQL", ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "CSS",
    ".php": "PHP", ".go": "Go", ".rb": "Ruby", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++",
    ".sh": "Shell", ".ps1": "PowerShell",
}

# language -> marker file patterns (matched against the basename, lowercase)
LANGUAGE_MARKERS: Dict[str, List[str]] = {
    "Python": ["requirements*.txt", "pyproject.toml", "setup.py", "pipfile", "setup.cfg"],
    "JavaScript": ["package.json"],
    "TypeScript": ["tsconfig.json"],
    "C#": ["*.csproj", "*.sln"],
    "VB.NET": ["*.vbproj"],
    "Java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "PHP": ["composer.json"],
    "Go": ["go.mod"],
    "Ruby": ["gemfile"],
    "Rust": ["cargo.toml"],
}


def detect_languages(entries: Iterable[ArchiveEntry]) -> List[LanguageStat]:
    """Aggregate languages over non-vendored, readable file entries."""
    files: Dict[str, int] = defaultdict(int)
    sizes: Dict[str, int] = defaultdict(int)
    markers: Dict[str, List[str]] = defaultdict(list)
    for e in entries:
        if e.is_dir or e.skip_reason or e.vendored:
            continue
        lang = EXTENSION_LANGUAGE.get(e.extension)
        if lang:
            files[lang] += 1
            sizes[lang] += e.size
        base = e.basename.lower()
        for mlang, patterns in LANGUAGE_MARKERS.items():
            if any(fnmatch.fnmatchcase(base, p) for p in patterns):
                markers[mlang].append(e.name)
    languages = set(files) | {l for l in markers if l in files}
    total = sum(sizes.values()) or 1
    stats: List[LanguageStat] = []
    for lang in languages:
        n = files[lang]
        mk = sorted(markers.get(lang, []))
        if mk and n >= 1:
            confidence = "strong"
        elif n >= 3:
            confidence = "moderate"
        else:
            confidence = "weak"
        evidence = [f"{n} file(s) with matching extensions"]
        evidence += [f"marker file: {m}" for m in mk[:5]]
        stats.append(LanguageStat(
            language=lang, files=n, bytes=sizes[lang],
            percent_of_bytes=round(100.0 * sizes[lang] / total, 1),
            confidence=confidence, evidence=evidence,
        ))
    stats.sort(key=lambda s: (-s.bytes, s.language))
    return stats
