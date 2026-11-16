"""Rev7 coexistence: compact a real tool session, then delete it safely."""

from verify_support import TEST_ROOT
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

sys.path.insert(0, str(Path(__file__).parent / "app"))
from engine import Engine, EngineError
from verify_context_budget import ModelFixture


class MergedReleaseTests(unittest.TestCase):
    def test_delete_compacted_session_preserves_work_and_other_sessions(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="merged-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            (project / "work.py").write_text("def work():\n    return 0\n")
            (project / "long.py").write_text("\n".join(f"value_{i} = {i}" for i in range(1000)))
            git = ["git", "-C", str(project)]
            subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
            subprocess.run(git + ["add", "."], check=True, capture_output=True)
            model = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
            model.mode, model.failed, model.round, model.calls = "tools", False, 0, []
            Thread(target=model.serve_forever, daemon=True).start()
            engine = Engine(root / "state", f"http://127.0.0.1:{model.server_port}/v1",
                            "fixture-secret", limits=(26368, 4096))
            try:
                engine.open_project(str(project))
                target, keep = engine.new_session(), engine.new_session()
                events = []
                engine.run(target, "Complete the work and preserve progress", events.append, lambda _: True)
                self.assertEqual(model.round, 13)
                self.assertTrue(any(e.get("state") == "compacted" for e in events))
                self.assertTrue(engine.history(target))
                self.assertIn("return 1", (project / "work.py").read_text())
                files = {p.name: p.read_bytes() for p in project.glob("*.py")}
                diff = subprocess.run(git + ["diff", "--binary"], check=True, capture_output=True).stdout
                self.assertIn(b"+    return 1", diff)
                engine.delete_session(target)
                self.assertNotIn(target, {s["id"] for s in engine.sessions()})
                with self.assertRaises(EngineError):
                    engine.history(target)
                self.assertEqual({p.name: p.read_bytes() for p in project.glob("*.py")}, files)
                self.assertEqual(subprocess.run(git + ["diff", "--binary"], check=True,
                                                capture_output=True).stdout, diff)
                engine.close()
                engine.open_project(str(project))
                self.assertNotIn(target, {s["id"] for s in engine.sessions()})
                self.assertIn(keep, {s["id"] for s in engine.sessions()})
                # A retained conversation can still run after the compacted one is deleted.
                resumed = []
                engine.run(keep, "Continue retained session", resumed.append, lambda _: True)
                self.assertIn("RECOVERED-DONE", "".join(e.get("text", "") for e in resumed))
            finally:
                engine.close()
                model.shutdown()
                model.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
