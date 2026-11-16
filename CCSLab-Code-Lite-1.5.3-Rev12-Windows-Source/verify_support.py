"""One ignored workspace for temporary verification artifacts."""
from pathlib import Path

WORK_ROOT = Path(__file__).resolve().parent.parent / ".work"
TEST_ROOT = WORK_ROOT / "tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
