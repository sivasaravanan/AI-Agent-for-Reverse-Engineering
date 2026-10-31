"""Create sample_project.zip in the project root (deterministic fixture)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.fixtures.sample_project import build_sample_zip  # noqa: E402

if __name__ == "__main__":
    print("Created", build_sample_zip(ROOT / "sample_project.zip"))
