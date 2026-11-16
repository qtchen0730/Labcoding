"""Regression for the recovered overflow seen in the actual local session."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "app"))
from engine import Engine, _Job


class RecoveryTests(unittest.TestCase):
    def test_mcp_waits_for_registry_but_does_not_ignore_failure(self):
        engine = Engine(Path("unused"), "http://127.0.0.1/v1", "secret")
        with patch.object(engine, "_json", side_effect=[{}, {"workspace": {"status": "connected"}}]), patch("engine.time.sleep"):
            engine._wait_for_mcp()
        with patch.object(engine, "_json", return_value={"workspace": {"status": "failed"}}):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                engine._wait_for_mcp()
        with patch.object(engine, "_json", side_effect=[
            {"workspace": {"status": "failed", "error": "Failed to get tools"}}, None,
            {"workspace": {"status": "connected"}},
        ]) as request:
            engine._wait_for_mcp()
            self.assertEqual(request.call_args_list[1].args[:2], ("POST", "/mcp/workspace/connect"))

    def test_recovered_overflow_is_not_final_failure(self):
        engine = Engine(Path("unused"), "http://127.0.0.1/v1", "secret")
        sid = "ses_fixture"
        def event(kind, **props):
            return {"type": kind, "properties": {"sessionID": sid, **props}}
        events = [
            event("session.error", error={"name": "ContextOverflowError", "data": {"message": "context length exceeded"}}),
            event("session.compacted"),
            event("message.updated", info={"id": "a", "sessionID": sid, "role": "assistant", "finish": "stop",
                                           "time": {"completed": 1}, "tokens": {"total": 3587}}),
            event("session.idle"),
        ]
        self.assertFalse(engine._track(iter(events), _Job(sid, lambda _: False, False), lambda _: None))

    def test_terminal_overflow_stays_failed(self):
        engine = Engine(Path("unused"), "http://127.0.0.1/v1", "secret")
        sid = "ses_fixture"
        events = [
            {"type": "session.error", "properties": {"sessionID": sid, "error": {
                "name": "ContextOverflowError", "data": {"message": "context length exceeded"}}}},
            {"type": "session.idle", "properties": {"sessionID": sid}},
        ]
        self.assertTrue(engine._track(iter(events), _Job(sid, lambda _: False, False), lambda _: None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
