"""Setup doctor: checks the prerequisites of this application and, only after the user confirms, installs what is missing.

Nothing here runs project code. Every install command comes from a fixed catalog (see ``catalog``), never from a
project or archive, and nothing is installed or downloaded without an explicit confirmation by the user.
"""
from setup_doctor.catalog import Action, Item  # noqa: F401
from setup_doctor.checks import SystemEnv, apply_known_paths, run_checks, summarize  # noqa: F401
from setup_doctor.installer import ActionResult, run_actions, validate_action  # noqa: F401
