"""Local-only checks for output-limit persistence and engine injection."""

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from app import App
from engine import Engine
from output_settings import load_output_limit, set_output_limit


class OutputLimitTests(unittest.TestCase):
    def test_default_and_persistent_round_trip(self):
        prefs = {}
        self.assertEqual(load_output_limit(prefs), 4096)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "preferences.json"
            for value in (8192, 4096):
                set_output_limit(prefs, value)
                path.write_text(json.dumps(prefs), encoding="utf-8")
                self.assertEqual(load_output_limit(json.loads(path.read_text(encoding="utf-8"))), value)

    def test_gateway_and_context_caps_and_engine_configuration(self):
        meta = {"effective_context_limit": 131072, "output_token_limit": 8192, "output_limit": 8192}
        limits = App.model_limits(meta, 8192)
        self.assertEqual(limits, (131072, 8192))
        engine = Engine(Path("."), "https://gateway.invalid/v1", "fixture", limits=limits)
        engine.bridge = SimpleNamespace(url="http://127.0.0.1:1/workspace")
        self.assertEqual(engine._config()["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"],
                         8192)
        self.assertEqual(App.model_limits(meta | {"output_token_limit": 4096}, 8192), (131072, 4096))
        self.assertEqual(App.model_limits(meta | {"effective_context_limit": 4096}, 8192), (4096, 2048))

    def test_rejects_invalid_persisted_and_requested_values(self):
        for value in (True, 127, 8193, "8192"):
            self.assertEqual(load_output_limit({"output_limit": value}), 4096)
            with self.assertRaises(ValueError):
                set_output_limit({}, value)

    def test_output_command_reports_partial_failure_and_retry(self):
        class Client:
            remote = 4096
            failures = 1

            def model(self):
                return {"effective_context_limit": 131072, "output_limit": 4096,
                        "output_token_limit": 4096}

            def opencode_output_limit(self):
                return self.remote

            def set_opencode_output_limit(self, value):
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("fixture offline")
                self.remote = value
                return {"output_limit": value}

        app = App.__new__(App)
        app.busy = False
        app.closing = False
        app.output_limit = 4096
        app.prefs = {}
        app.model_info = {}
        app.history = {}
        app.session = None
        app.context_meter = SimpleNamespace(set_model=lambda *_args, **_kwargs: None,
                                            set_output_limit=lambda value, label:
                                            setattr(app, "display_budget", (value, label)))
        app.connection_mode = "account"
        app.account_client = Client()
        app.chat = object()
        app.messages = []
        app.append = lambda _box, message, *_args, **_kwargs: app.messages.append(message)
        app.save_prefs = lambda: None

        def worker(_caption, operation, done, on_error=None, **_kwargs):
            try:
                done(operation())
            except Exception as error:
                if on_error:
                    on_error(error)
                else:
                    raise

        app.worker = worker
        app.output_command("/output 8192")
        self.assertEqual(app.prefs["output_limit"], 8192)
        self.assertEqual(app.display_budget, (4096, "新任务有效输出预算"))
        self.assertIn("Labcoding 已设置 8192，当前有效 4096", "".join(app.messages))
        self.assertIn("同步失败", "".join(app.messages))
        self.assertEqual(app.account_client.remote, 4096)
        app.output_command("/output 8192")
        self.assertEqual(app.account_client.remote, 8192)
        self.assertEqual(app.display_budget, (4096, "新任务有效输出预算"))
        app.output_command("/output 4096")
        self.assertEqual((app.prefs["output_limit"], app.account_client.remote), (4096, 4096))


if __name__ == "__main__":
    unittest.main(verbosity=2)
