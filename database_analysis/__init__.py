"""Phase 5 - Database Analysis plug-in for the AI Software Engineering Agent.

Quick start:
    import database_analysis as dba
    result = dba.run("project.zip")          # or a folder, or {path: content}
    print(dba.render_markdown(result))
"""
__version__ = "1.0.0"

from .models import (CONFIRMED, STRONG, WEAK, DatabaseAnalysisResult, DatabaseDetection, DbFinding,  # noqa: E402
                     ConnectionConfig, Evidence)
from .options import AnalysisOptions  # noqa: E402
from .sources import (ArchiveError, ArchiveLimits, DirectoryProvider, FileProvider, MemoryProvider,  # noqa: E402
                      ZipProvider)
from .analyzer import (DatabaseAnalyzer, analyze_directory, analyze_files, analyze_zip)  # noqa: E402
from .plugin import PLUGIN_INFO, DatabaseAnalysisPlugin, register, run, to_findings  # noqa: E402
from .report import render_html, render_html_fragment, render_json, render_markdown  # noqa: E402

__all__ = [n for n in dir() if not n.startswith("_")]
