"""Print which phase files are implemented, placeholders, or missing.

Usage: python scripts/phase_status.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.phase_registry import PHASES, layout_report  # noqa: E402


def main() -> int:
    rows = layout_report(ROOT)
    for phase, info in PHASES.items():
        mine = [r for r in rows if r["phase"] == phase]
        counts = {s: sum(1 for r in mine if r["state"] == s) for s in ("implemented", "placeholder", "missing")}
        print(f"Phase {phase:>2}  {info['title']:<40} implemented={counts['implemented']} "
              f"placeholder={counts['placeholder']} missing={counts['missing']}")
    missing = [r["path"] for r in rows if r["state"] == "missing"]
    if missing:
        print("\nMISSING:", ", ".join(missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
