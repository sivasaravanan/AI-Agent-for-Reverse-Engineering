r"""Phase 4 plugin - controlled build/test execution for the AI Software Engineering Agent.

Drop this folder into <project>/plugins/ and use ONE of:

    from plugins.build_execution import discover_candidates, build_plan, Approval, run_plan, isolation_status
    cands, notes = discover_candidates(zip_path)                 # read-only, executes nothing
    plan = build_plan(zip_path, cands[0])                        # exact command/image/limits/network
    # ... show the plan to the user and get a YES ...
    result = run_plan(plan, Approval.for_plan(plan))             # runs in a disposable container only

    from plugins.build_execution import attach_tab
    attach_tab(notebook, get_source=lambda: current_zip)         # Tkinter tab with the approval dialog
"""
from .models import Approval, Candidate, ExecutionResult, Limits, Options, Plan
from .discovery import custom_candidate, discover_candidates, DEFAULT_IMAGES
from .sandbox import build_plan, container_command, isolation_status, run_plan
from .autotune import apply_autotune, profile_source, retry_limits

PLUGIN = {"name": "build_execution", "title": "Build & Test Execution (Phase 4)", "version": "0.1.0",
          "api": ["discover_candidates", "build_plan", "run_plan", "isolation_status", "attach_tab"]}

__all__ = ["Approval", "Candidate", "ExecutionResult", "Limits", "Options", "Plan", "custom_candidate",
           "discover_candidates", "DEFAULT_IMAGES", "build_plan", "container_command", "isolation_status",
           "run_plan", "attach_tab", "PLUGIN", "apply_autotune", "profile_source", "retry_limits"]


def attach_tab(notebook, get_source=None, title: str = "Build & Test"):
    """Add the tab to an existing ttk.Notebook. `get_source` returns the ZIP/folder currently loaded."""
    from .ui_tab import BuildExecutionTab  # lazy: the core works without Tkinter
    tab = BuildExecutionTab(notebook, get_source=get_source)
    notebook.add(tab, text=title)
    return tab
