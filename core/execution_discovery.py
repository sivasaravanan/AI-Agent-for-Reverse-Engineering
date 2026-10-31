"""Phase 4 integration (discovery only): report isolation status and candidate build/test commands.

Nothing is executed here. Running a candidate happens only from the Build & Test tab / plugin API after the user
approves the exact plan (command, image, limits, network) and only inside a Docker/Podman container.
"""
from __future__ import annotations

from typing import Dict

from security.secret_scanner import redact

MESSAGE = ("No build, test or project command was executed. Candidate commands are discovered from manifests only; "
           "running one requires explicit approval of the exact plan and a working Docker/Podman runtime.")


def discover_execution(source, options=None) -> Dict:
    from plugins.build_execution import discover_candidates, isolation_status
    from plugins.build_execution.discovery import DEFAULT_IMAGES
    iso = isolation_status(options)
    iso.pop("executable", None)
    cands, notes = discover_candidates(source)
    return {
        "executed": False, "message": MESSAGE, "isolation": iso, "default_images": dict(DEFAULT_IMAGES),
        "candidates": [{"id": c.id, "ecosystem": c.ecosystem, "command": redact(c.display),
                        "description": c.description, "source_file": c.source_file, "source_line": c.source_line,
                        "origin": c.origin, "needs_network": c.needs_network,
                        "risks": [redact(r) for r in c.risks], "notes": list(c.notes)} for c in cands],
        "notes": list(notes),
    }
