"""Plain-text request an IT desk can act on: what, why, from where, how to verify, what it needs, how to remove it."""
from __future__ import annotations

import time
from typing import Iterable, List, Optional

from prerequisites.catalog import BY_ID, DIRECT
from prerequisites.env import Env
from prerequisites.plan import InstallPlan, PlanRefusal, build_plan

UNINSTALL = {"podman": "winget uninstall RedHat.Podman  /  Settings > Apps", "docker": "Settings > Apps > Docker Desktop > Uninstall",
             "wsl2": "wsl --uninstall (or Turn Windows features on or off)", "ollama": "Settings > Apps > Ollama"}


def build_it_request(ids: Iterable[str], env: Env, cfg, contact: str = "", justification: str = "") -> str:
    ids = list(ids)
    out: List[str] = [
        "# Software installation request - AI Software Engineering Agent prerequisites", "",
        f"Date: {time.strftime('%Y-%m-%d')}   Machine platform: {env.platform}/{env.arch}   Requester: {contact or '(your name)'}", "",
        "## Why", justification or ("The AI Software Engineering Agent analyses uploaded project archives. It runs builds and tests only inside "
                                   "disposable, network-less containers; a container engine is therefore required. Analysis tools (linters/compilers) are optional."), "",
        "## Software requested", ""]
    hosts: set = set()
    needs_admin = False
    for pid in ids:
        p = BY_ID.get(pid)
        if not p:
            continue
        plan = build_plan(pid, env, cfg)
        out.append(f"### {p.name}")
        out.append(f"- Purpose: {p.why}")
        if isinstance(plan, InstallPlan):
            needs_admin = needs_admin or plan.needs_admin
            hosts.update(h for h in plan.hosts if h and not h.startswith("("))
            out.append(f"- Suggested method: {plan.method} ({plan.scope} scope) - administrator rights: {'yes' if plan.needs_admin else 'no'}")
            for s in plan.steps:
                if s.url:
                    out.append(f"- Download: {s.url}")
                if s.kind == "run" and s.command:
                    out.append(f"- Silent command: `{' '.join(s.command)}`")
            if plan.checksum_source:
                out.append(f"- Integrity: {plan.checksum_source}")
        else:
            out.append(f"- Automated install not possible here: {plan.reason}")
            if plan.manual_url:
                out.append(f"- Vendor page: {plan.manual_url}")
        if p.license_ack:
            out.append(f"- Licence: {p.license_note}")
        out.append(f"- Removal: {UNINSTALL.get(pid, 'standard uninstall')}")
        out.append("")
    if hosts:
        out += ["## Network access the installers need (HTTPS, port 443)", ""] + [f"- {h}" for h in sorted(hosts)] + [""]
    out += ["## Notes", "",
            "- The application never installs software silently: each install shows the exact steps and needs the user's approval.",
            "- Every download and install is written to an audit log (logs/prerequisites_audit.jsonl) with URLs and SHA-256 values.",
            "- Alternatively, place approved installers plus a SHA256SUMS file in a folder and point the application's 'offline folder' setting to it."]
    if needs_admin:
        out.append("- Administrator rights are needed for the machine-wide installs listed above.")
    return "\n".join(out) + "\n"


def offline_manifest(ids: Iterable[str], env: Env, cfg) -> dict:
    """What to download elsewhere and carry in (air-gapped machines)."""
    items = []
    for pid in ids:
        spec = DIRECT.get(pid)
        p = BY_ID.get(pid)
        if not p:
            continue
        item = {"id": pid, "name": p.name, "manual_url": p.manual_url}
        if spec:
            item.update({"file": spec.filename.format(arch=env.arch), "url": spec.urls.get(env.arch) or spec.urls.get("amd64"),
                         "silent_args": list(spec.machine_args), "expected_publishers": list(spec.publishers),
                         "checksum": spec.checksum or "verify the Authenticode signature"})
        items.append(item)
    return {"copy_to": cfg.offline_dir or "(set 'offline_dir' in the Prerequisites settings)",
            "note": "Add a SHA256SUMS file (sha256sum format) next to the installers; the app refuses unverifiable installers.", "items": items}
