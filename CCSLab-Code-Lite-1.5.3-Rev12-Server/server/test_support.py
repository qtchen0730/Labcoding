"""Keep local test artifacts out of source directories."""
from pathlib import Path
TEST_ROOT = Path(__file__).resolve().parents[2] / ".work" / "tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
