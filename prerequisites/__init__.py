"""Prerequisite detection and (optional, approved) installation of container runtimes and analysis tools.

    from prerequisites import load_config, Env, detect_all, build_plan, run_plan, Approval
    cfg, env = load_config(), Env.real()
    statuses = detect_all(env)                       # always detect first
    plan = build_plan("podman", env, cfg)            # exact steps, nothing happens yet
    result = run_plan(plan, Approval.for_plan(plan), env, cfg)   # only after the user approved THIS plan

Nothing is ever installed silently: the GUI shows the plan and asks; the CLI needs --yes. See docs/PREREQUISITES.md.
"""
__version__ = "1.0.0"

from prerequisites.audit import AuditLog                                                                       # noqa: E402,F401
from prerequisites.catalog import BY_ID, CATALOG, CONTAINER_RUNTIMES, SANDBOX_IMAGES, get                     # noqa: E402,F401
from prerequisites.config import PrereqConfig, load_config, save_user_config                                   # noqa: E402,F401
from prerequisites.detect import (INSTALLED, MISSING, READY, Status, container_ready, detect, detect_all)    # noqa: E402,F401
from prerequisites.diagnose import Diagnosis, Hurdle, diagnose                                                 # noqa: E402,F401
from prerequisites.env import Env, RunResult                                                                   # noqa: E402,F401
from prerequisites.install import (Approval, InstallResult, pull_sandbox_images, run_plan, start_runtime)    # noqa: E402,F401
from prerequisites.itrequest import build_it_request, offline_manifest                                         # noqa: E402,F401
from prerequisites.plan import InstallPlan, PlanRefusal, build_plan, expand_dependencies                      # noqa: E402,F401
from prerequisites.startup import Decision, decide                                                             # noqa: E402,F401
from prerequisites.tools_env import activate as activate_tool_paths                                            # noqa: E402,F401
