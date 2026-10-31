"""Source-sharing consent policy (Phase 7).

Rules (fail closed):
* Nothing leaves the machine unless the endpoint is local (localhost) OR the user has granted consent for the EXACT
  payload that will be sent (a hash of the redacted text shown in the preview) to the EXACT host.
* Only user-selected items are ever included. Secrets are redacted before preview and before sending.
* Every transmission is recorded in an audit ledger (what, where, how many bytes, hash) - never the content.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from security.secret_scanner import REDACTED, redact

MAX_ITEM_BYTES = 24_000
MAX_TOTAL_BYTES = 80_000


def may_send_to_external(consent_given: bool = False, selected_files=()) -> bool:
    """Fail closed: consent AND a non-empty explicit selection are both required."""
    return bool(consent_given and selected_files)


@dataclass
class SelectedItem:
    path: str            # file path, finding id, or a label such as "architecture-summary"
    text: str
    kind: str = "file"   # file | finding | metadata


@dataclass
class PayloadPreview:
    items: List[dict] = field(default_factory=list)      # {path, kind, bytes, redactions, truncated}
    text: str = ""                                        # EXACTLY what will be placed in the prompt
    total_bytes: int = 0
    redaction_count: int = 0
    payload_hash: str = ""
    dropped: List[str] = field(default_factory=list)      # items skipped because of the total-size cap

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(i["path"] for i in self.items)


def _fence_safe(text: str) -> str:
    return text.replace("<<<", "< < <").replace(">>>", "> > >")


def build_payload(items: Iterable[SelectedItem], max_item_bytes: int = MAX_ITEM_BYTES,
                  max_total_bytes: int = MAX_TOTAL_BYTES) -> PayloadPreview:
    prev = PayloadPreview()
    parts: List[str] = []
    for it in items:
        if prev.total_bytes >= max_total_bytes:
            prev.dropped.append(it.path)
            continue
        red = redact(it.text)
        n_red = red.count(REDACTED) - it.text.count(REDACTED)
        data = red.encode("utf-8")
        truncated = False
        room = min(max_item_bytes, max_total_bytes - prev.total_bytes)
        if len(data) > room:
            red, truncated = data[:room].decode("utf-8", "ignore"), True
            data = red.encode("utf-8")
        parts.append(f"<<<ITEM kind={it.kind} path={_fence_safe(it.path)}{' (truncated)' if truncated else ''}>>>\n"
                     f"{_fence_safe(red)}\n<<<END ITEM>>>")
        prev.items.append({"path": it.path, "kind": it.kind, "bytes": len(data), "redactions": max(0, n_red),
                           "truncated": truncated})
        prev.total_bytes += len(data)
        prev.redaction_count += max(0, n_red)
    prev.text = "\n\n".join(parts)
    prev.payload_hash = hashlib.sha256(prev.text.encode("utf-8")).hexdigest()
    return prev


@dataclass(frozen=True)
class ConsentGrant:
    """Created ONLY by the UI/CLI after the user has seen the preview and the destination host(s)."""
    hosts: frozenset
    payload_hash: str
    granted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @classmethod
    def for_preview(cls, preview: PayloadPreview, hosts: Iterable[str]) -> "ConsentGrant":
        return cls(hosts=frozenset(h.lower() for h in hosts), payload_hash=preview.payload_hash)


def authorize(is_local: bool, host: str, preview: PayloadPreview, grant: Optional[ConsentGrant]) -> Tuple[bool, str]:
    """May ``preview`` be sent to ``host``? Returns (allowed, reason)."""
    if not preview.items:
        return True, "No project content is included."
    if is_local:
        return True, "Local endpoint: content stays on this machine."
    if grant is None:
        return False, f"External endpoint '{host}': explicit consent for the selected content is required."
    if grant.payload_hash != preview.payload_hash:
        return False, "The content changed after consent was given; consent must be given again."
    if host.lower() not in grant.hosts:
        return False, f"Consent was not given for '{host}'."
    if not may_send_to_external(True, preview.paths):
        return False, "No explicit file/finding selection."
    return True, f"Consent granted for {host}."


class ConsentLedger:
    """Audit trail of transmissions. Stores metadata only, never content or keys."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.entries: List[dict] = []
        self._lock = threading.Lock()

    def record(self, *, provider: str, host: str, model: str, task: str, local: bool, preview: PayloadPreview) -> dict:
        e = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": provider, "host": host,
             "model": model, "task": task, "local": local, "items": [i["path"] for i in preview.items],
             "bytes": preview.total_bytes, "redactions": preview.redaction_count, "sha256": preview.payload_hash}
        with self._lock:
            self.entries.append(e)
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(e) + "\n")
                except OSError:
                    pass
        return e
