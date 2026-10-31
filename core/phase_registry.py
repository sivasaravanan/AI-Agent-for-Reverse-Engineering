"""Phase ownership registry: which file belongs to which phase, and whether it is still a placeholder.

One owner per file keeps merges between sessions disjoint. See docs/MERGE_GUIDE.md.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

PHASES: Dict[int, Dict] = {
    1: {"title": "Working ZIP analyzer", "files": [
        "app.py", "core/archive_manager.py", "core/language_detector.py", "core/framework_detector.py",
        "core/database_detector.py", "core/dependency_analyzer.py", "core/static_analysis.py",
        "core/report_generator.py", "core/analysis_orchestrator.py", "models/finding.py",
        "models/analysis_result.py", "security/archive_validation.py", "security/secret_scanner.py",
        "services/log_service.py", "ui/main_window.py"]},
    2: {"title": "Project structure and dependencies", "files": [
        "core/project_inventory.py", "core/architecture_analyzer.py", "core/config_discovery.py",
        "core/structure_analysis.py", "models/project_info.py", "core/phase_registry.py"]},
    3: {"title": "Language-specific static analysis", "files": [
        "analyzers/__init__.py", "analyzers/base.py", "analyzers/python_analyzer.py", "analyzers/javascript_analyzer.py",
        "analyzers/dotnet_analyzer.py", "analyzers/java_analyzer.py", "analyzers/sql_analyzer.py",
        "plugins/static_analysis/__init__.py"]},
    4: {"title": "Controlled build and test execution", "files": [
        "services/environment_service.py", "services/build_runner.py", "services/test_runner.py",
        "security/execution_policy.py", "services/isolated_test_runner.py", "plugins/build_execution/__init__.py", "ui/execution_dialog.py"]},
    5: {"title": "Database analysis", "files": ["core/database_analyzer.py", "database_analysis/__init__.py"]},
    6: {"title": "Error explanation and debugging", "files": ["core/finding_store.py", "core/log_analyzer.py",
                                                         "findings_plugin/__init__.py"]},
    7: {"title": "AI provider integration", "files": [
        "providers/base_provider.py", "providers/model_router.py", "providers/ollama_provider.py",
        "providers/openai_compatible_provider.py", "providers/openrouter_provider.py",
        "security/consent_policy.py", "core/ai_assistant.py", "core/ai_settings.py", "core/source_access.py",
        "ui/ai_panel.py", "config/settings.json"]},
    8: {"title": "Human-approved code correction", "files": [
        "core/patch_manager.py", "core/patch_generators.py", "models/patch.py", "ui/patch_dialog.py",
        "scripts/fix_cli.py"]},
    9: {"title": "Desktop UI and history", "files": [
        "database/db_manager.py", "database/schema.sql", "ui/project_view.py", "ui/findings_view.py",
        "ui/report_view.py", "ui/settings_view.py", "ui/history_view.py", "core/settings.py", "core/report_preview.py"]},
    10: {"title": "Reports and delivery", "files": [
        "core/delivery_packager.py", "report_delivery/__init__.py", "scripts/build.bat", "docs/USER_GUIDE.md", "docs/ARCHITECTURE.md",
        "docs/KNOWN_ISSUES.md"]},
    11: {"title": "Stage A: inputs, code map, documentation", "files": [
        "core/input_adapter.py", "core/code_map.py", "core/doc_generator.py", "analyzers/builtin_analyzers.py"]},
    12: {"title": "Stage B: education (batch grading, similarity, rubrics)", "files": [
        "education/__init__.py", "education/grader.py", "education/reports.py", "education/rubric.py", "education/similarity.py",
        "ui/grading_panel.py"]},
    13: {"title": "Stage C: enterprise, audit and compliance", "files": [
        "enterprise/__init__.py", "enterprise/audit.py", "enterprise/licenses.py", "enterprise/sbom.py", "enterprise/policy.py",
        "enterprise/sarif.py", "enterprise/trends.py", "enterprise/portfolio.py", "enterprise/ci.py", "ui/compliance_panel.py"]},
    14: {"title": "Stage D: shared use (roles, approvals, signing)", "files": [
        "collab/__init__.py", "collab/users.py", "collab/approvals.py", "collab/signing.py", "collab/workspace.py",
        "scripts/team_cli.py", "ui/team_panel.py"]},
    15: {"title": "Compact icon UI", "files": ["ui/theme.py", "ui/sidebar.py", "ui/widgets.py"]},
    16: {"title": "Runtime auto-start and live run preview", "files": ["plugins/build_execution/events.py", "plugins/build_execution/runtime_start.py", "ui/live_run_view.py"]},
    17: {"title": "App Preview: run, debug, trace and test in containers with a viewer panel", "files": [
        "app_preview/__init__.py", "app_preview/__main__.py", "app_preview/models.py", "app_preview/events.py", "app_preview/stream.py",
        "app_preview/runtimes.py", "app_preview/discovery.py", "app_preview/planner.py", "app_preview/container.py",
        "app_preview/session.py", "app_preview/codeblocks.py", "app_preview/tracemodel.py", "app_preview/runtime/ase_tracer.py",
        "app_preview/runtime/ase_gui_entry.sh", "app_preview/runtime/ase_diag.py", "app_preview/runtime/ase_screen.py", "app_preview/images/Dockerfile.gui", "ui/code_view.py", "ui/preview_card.py", "ui/app_preview_view.py",
        "scripts/build_preview_image.bat", "scripts/build_preview_image.sh", "app_preview/imagebuilder.py", "plugins/build_execution/storage.py", "ui/storage_dialog.py", "app_preview/quick.py", "ui/quick_run.py", "ui/lean_copy.py", "docs/APP_PREVIEW.md"]},
    18: {"title": "Local AI auto-setup and live cloud model catalogue", "files": ["providers/discovery.py"]},
    19: {"title": "Virtual machine windows: Manager, machine window, snapshot tree, settings", "files": [
        "app_preview/vmstore.py", "ui/vm_integration.py", "ui/vm_manager.py", "ui/vm_window.py", "ui/vm_snapshots.py", "ui/vm_settings.py", "ui/compact.py", "ui/guest_input.py", "docs/VM_MANAGER.md"]},
}

_PY_MARK = re.compile(r"^PLACEHOLDER\s*=\s*True\b", re.M)
_TXT_MARK = re.compile(r"PLACEHOLDER \(Phase \d+\)|\"_placeholder\"\s*:\s*true", re.I)


def is_placeholder_text(path: str, text: str) -> bool:
    if path.endswith(".py"):
        return bool(_PY_MARK.search(text))
    return bool(_TXT_MARK.search(text[:600]))


def is_placeholder_file(path: Path) -> bool:
    try:
        return is_placeholder_text(str(path), path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False


def owner_of(rel_path: str) -> Optional[int]:
    rel = rel_path.replace("\\", "/")
    for phase, info in PHASES.items():
        if rel in info["files"]:
            return phase
    return None


def layout_report(root: Path) -> List[Dict]:
    """One row per registered file: state is implemented | placeholder | missing."""
    rows = []
    for phase, info in PHASES.items():
        for rel in info["files"]:
            p = Path(root) / rel
            state = "missing" if not p.is_file() else ("placeholder" if is_placeholder_file(p) else "implemented")
            rows.append({"phase": phase, "path": rel, "state": state})
    return rows
