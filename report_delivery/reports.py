"""Writes the full report set to a folder."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from .env_info import collect_environment
from .render import render_html, render_json, render_markdown, render_section_markdown
from .sections import normalize_input


def build_reports(report_input: Mapping[str, Any], out_dir: Union[str, Path], *, collect_env: bool = True,
                  probe_tools: bool = True, title: Optional[str] = None) -> Dict[str, str]:
    """Creates analysis_report.html/.json/.md plus dependency, static-analysis, security, build-test reports.
    Returns {name: path}. Missing input sections are reported as 'not provided', never fabricated."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = normalize_input(report_input)
    env = n["environment"] or (collect_environment(probe=probe_tools) if collect_env else {})
    files = {
        "analysis_report.html": render_html(n, env, title),
        "analysis_report.json": render_json(n, env),
        "analysis_summary.md": render_markdown(n, env),
        "dependency_report.md": render_section_markdown(n, "dependencies"),
        "static_analysis_report.md": render_section_markdown(n, "static_analysis"),
        "security_findings.md": render_section_markdown(n, "security"),
        "build_test_results.md": render_section_markdown(n, "build_test"),
        "project_inventory.md": render_section_markdown(n, "inventory"),
    }
    paths: Dict[str, str] = {}
    for name, content in files.items():
        p = out / name
        p.write_text(content, encoding="utf-8")
        paths[name] = str(p)
    return paths
