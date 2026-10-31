r"""App Preview (Phase 17): run, debug, trace and test a project's application from a ZIP or folder inside a disposable
Docker / Podman / nerdctl / Finch container, and watch it like a VM viewer: virtual screen (GUI apps), live web page (web apps),
console, the code that is executing and the output each code block produced.

    from app_preview import discover_entries, make_plan, approve, start, PreviewOptions
    entries, notes = discover_entries(zip_or_folder)              # read-only, executes nothing
    plan = make_plan(zip_or_folder, entries[0], "trace")          # exact command / image / ports / limits / risks
    # ... show the plan to the user and get a YES ...
    session = start(plan, approve(plan))                          # runs in a disposable container only
    # events: plugins.build_execution.events.BUS (standard) and app_preview.events.PV (preview-specific)

Everything the Phase 4 sandbox guarantees still holds: approval per exact plan, no host fallback, no automatic image pulls,
scratch copy only, dropped capabilities, non-root, read-only root filesystem.
"""
from plugins.build_execution.models import Approval

from .codeblocks import Block, OutputIndex, block_text, blocks_for, innermost
from .discovery import custom_entry, discover_entries
from .events import PV
from .imagebuilder import BuildPlan, build_gui_image, gui_image_ready, make_build_plan
from .models import BACKENDS, GUI_IMAGE, MODES, Entry, PreviewOptions, PreviewPlan
from .planner import free_port, make_plan
from .runtimes import installed, status
from .session import PreviewSession
from .stream import ProtocolDecoder

PLUGIN = {"name": "app_preview", "title": "App Preview (Phase 17)", "version": "0.2.0",
          "api": ["discover_entries", "make_plan", "approve", "start", "status"]}

__all__ = ["Approval", "BuildPlan", "build_gui_image", "gui_image_ready", "make_build_plan", "BACKENDS", "Block", "Entry", "GUI_IMAGE", "MODES", "OutputIndex", "PLUGIN", "PV", "PreviewOptions",
           "PreviewPlan", "PreviewSession", "ProtocolDecoder", "approve", "block_text", "blocks_for", "custom_entry",
           "discover_entries", "free_port", "innermost", "installed", "make_plan", "start", "status"]


def approve(plan: PreviewPlan) -> Approval:
    """Call ONLY after the user has reviewed ``plan`` (approval dialog / CLI prompt)."""
    return Approval.for_plan(plan)          # binds to plan.plan_hash (duck-typed: PreviewPlan has the same hash field)


def start(plan: PreviewPlan, approval, options=None, on_result=None) -> PreviewSession:
    return PreviewSession(plan, approval, options, on_result).start()
