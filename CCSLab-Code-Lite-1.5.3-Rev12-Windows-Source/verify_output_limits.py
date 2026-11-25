"""Local-only checks for output-limit persistence and engine injection."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import select
import socket
import tempfile
from threading import Event, Thread
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import App
import engine as engine_module
from engine import Engine
from output_settings import (load_account_output_limit, load_output_limit, set_account_output_limit,
                             set_output_limit)


class _StreamFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _write_event(self, record, payload):
        data = ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()
        self.wfile.write(data)
        self.wfile.flush()
        record["frames"] += 1

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        fixture = self.server
        record = {"started": time.monotonic(), "status": 200, "max_tokens": body.get("max_tokens"),
                  "stream": body.get("stream"), "model": body.get("model"),
                  "tool_count": len(body.get("tools", [])), "reason": "running", "frames": 0,
                  "primary": bool(body.get("tools")), "finished": Event()}
        fixture.records.append(record)
        if not record["primary"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                                       "model": "lab-general", "choices": [{"index": 0,
                                       "delta": {"role": "assistant", "content": "fixture"},
                                       "finish_reason": None}]})
            self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                                       "model": "lab-general", "choices": [{"index": 0, "delta": {},
                                       "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            record["reason"] = "fixture_completed"
            record["elapsed"] = time.monotonic() - record["started"]
            record["finished"].set()
            return
        if fixture.primary_started:
            record["status"] = 503
            record["reason"] = "retry_rejected"
            data = b'{"error":{"message":"fixture rejects retry"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            record["elapsed"] = time.monotonic() - record["started"]
            record["finished"].set()
            return
        fixture.primary_started = True

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                                       "model": "lab-general", "choices": [{"index": 0,
                               "delta": {"role": "assistant", "content": "x"}, "finish_reason": None}]})
            deadline = record["started"] + fixture.duration
            while time.monotonic() < deadline:
                if fixture.idle:
                    readable, _, _ = select.select([self.connection], [], [], 0.25)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        raise ConnectionResetError("client closed during idle SSE read")
                else:
                    time.sleep(min(fixture.interval, max(0, deadline - time.monotonic())))
                    self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                                       "model": "lab-general", "choices": [{"index": 0,
                                       "delta": {"content": "x"}, "finish_reason": None}]})
            self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                               "model": "lab-general", "choices": [{"index": 0, "delta": {},
                               "finish_reason": "stop"}]})
            self._write_event(record, {"id": "stream-fixture", "object": "chat.completion.chunk", "created": 1,
                               "model": "lab-general", "choices": [], "usage": {"prompt_tokens": 1,
                               "completion_tokens": 1, "total_tokens": 2}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            record["reason"] = "fixture_completed"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as error:
            record["reason"] = "client_disconnected"
            record["disconnect_error"] = type(error).__name__
        finally:
            record["elapsed"] = time.monotonic() - record["started"]
            record["finished"].set()


class _DirectFixtureGuard:
    """Bypass only ContextGuard for isolating OpenCode's own idle chunk timeout."""

    def __init__(self, base, _token, limits, callback, _log_path, **_kwargs):
        self.url = base.rstrip("/")
        self.secret = "local-stream-fixture"
        self.limits = limits
        self.callback = callback
        self.closed = Event()
        self.budget_rejections = 0

    def close(self):
        self.closed.set()


class OutputLimitTests(unittest.TestCase):
    def test_default_and_persistent_round_trip(self):
        prefs = {}
        self.assertEqual(load_output_limit(prefs), 4096)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "preferences.json"
            for value in (1024, 2048, 4096, 6144, 8192, 128, 3000, 5000, 7500, 8192):
                set_output_limit(prefs, value)
                path.write_text(json.dumps(prefs), encoding="utf-8")
                self.assertEqual(load_output_limit(json.loads(path.read_text(encoding="utf-8"))), value)

    def test_gateway_and_context_caps_and_engine_configuration(self):
        meta = {"effective_context_limit": 131072, "output_token_limit": 8192, "output_limit": 8192}
        limits = App.model_limits(meta, 8192)
        self.assertEqual(limits, (131072, 8192))
        engine = Engine(Path("."), "https://gateway.invalid/v1", "fixture", limits=limits)
        engine.bridge = SimpleNamespace(url="http://127.0.0.1:1/workspace")
        provider = engine._config()["provider"]["ccslab"]
        self.assertEqual(provider["models"]["lab-general"]["limit"]["output"], 8192)
        self.assertEqual(provider["options"]["timeout"], 300000)
        self.assertEqual(provider["options"]["chunkTimeout"], 60000)
        self.assertEqual(App.model_limits(meta | {"output_token_limit": 4096}, 8192), (131072, 4096))
        self.assertEqual(App.model_limits(meta | {"effective_context_limit": 4096}, 8192), (4096, 2048))

    def test_task_timeout_uses_its_budget_snapshot(self):
        meta = {"effective_context_limit": 131072, "output_token_limit": 8192, "output_limit": 8192}
        for task_budget, later_global_budget, expected_timeout in (
                (8192, 4096, 300000), (4096, 8192, 120000)):
            app = App.__new__(App)
            app.output_limit = task_budget
            task = SimpleNamespace(output_limit=App.model_limits(meta, app.output_limit)[1])
            engine = Engine(Path("."), "https://gateway.invalid/v1", "fixture",
                            limits=(131072, task.output_limit))
            engine.bridge = SimpleNamespace(url="http://127.0.0.1:1/workspace")
            app.output_limit = later_global_budget
            self.assertNotEqual(task.output_limit, app.output_limit)
            options = engine._config()["provider"]["ccslab"]["options"]
            self.assertEqual(options["timeout"], expected_timeout)
            self.assertEqual(options["chunkTimeout"], 60000)

    def test_persisted_budget_reaches_running_opencode_after_restart(self):
        meta = {"effective_context_limit": 131072, "output_token_limit": 8192, "output_limit": 8192}
        with tempfile.TemporaryDirectory(prefix="ccslab-output-timeout-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
            for context, budget, expected_timeout in ((26368, 4096, 120000), (65536, 8192, 300000)):
                meta["effective_context_limit"] = context
                preferences = {}
                set_output_limit(preferences, budget)
                preference_file = root / "preferences.json"
                preference_file.write_text(json.dumps(preferences), encoding="utf-8")

                # Simulate a fresh client process loading its persisted preference.
                restarted_preferences = json.loads(preference_file.read_text(encoding="utf-8"))
                loaded_budget = load_output_limit(restarted_preferences)
                limits = App.model_limits(meta, loaded_budget)
                engine = Engine(root / f"engine-{budget}", "https://gateway.invalid/v1", "fixture",
                                limits=limits)
                try:
                    engine.open_project(str(project))
                    runtime = engine._json("GET", "/config")
                    provider = runtime["provider"]["ccslab"]
                    self.assertEqual(provider["options"]["timeout"], expected_timeout)
                    self.assertEqual(provider["options"]["chunkTimeout"], 60000)
                    self.assertEqual(provider["models"]["lab-general"]["limit"]["output"], budget)
                    self.assertEqual(provider["models"]["lab-general"]["limit"]["context"], context)
                    self.assertEqual(provider["models"]["lab-general"]["limit"]["input"], context - 2048)
                    self.assertEqual(engine.loaded_limits["context"], context)
                finally:
                    engine.close()
                self.assertIsNone(engine.loaded_limits)

    @unittest.skipUnless(os.environ.get("CCSLAB_LONG_STREAM_TESTS") == "1",
                         "set CCSLAB_LONG_STREAM_TESTS=1 to run real 60/120/150/300 second streams")
    def test_8192_stream_completes_after_150_seconds(self):
        result, record = self._run_stream_case(8192, duration=150, interval=3)
        self.assertIsNone(result["error"], result)
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["max_tokens"], 8192)
        self.assertEqual(record["reason"], "fixture_completed")
        self.assertGreaterEqual(record["elapsed"], 150)
        self.assertLess(record["elapsed"], 165)
        self.assertGreaterEqual(result["delta_count"], 45)

    @unittest.skipUnless(os.environ.get("CCSLAB_LONG_STREAM_TESTS") == "1",
                         "set CCSLAB_LONG_STREAM_TESTS=1 to run real 60/120/150/300 second streams")
    def test_8192_stream_is_cancelled_by_300_second_total_timeout(self):
        result, record = self._run_stream_case(8192, duration=330, interval=3)
        self.assertIsNotNone(result["error"], "OpenCode unexpectedly completed a stream past its 300s deadline")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["max_tokens"], 8192)
        self.assertEqual(record["reason"], "client_disconnected")
        self.assertGreaterEqual(record["elapsed"], 295)
        self.assertLess(record["elapsed"], 315)

    @unittest.skipUnless(os.environ.get("CCSLAB_LONG_STREAM_TESTS") == "1",
                         "set CCSLAB_LONG_STREAM_TESTS=1 to run real 60/120/150/300 second streams")
    def test_4096_stream_is_cancelled_by_120_second_total_timeout(self):
        result, record = self._run_stream_case(4096, duration=150, interval=3)
        self.assertIsNotNone(result["error"], "OpenCode unexpectedly completed a stream past its 120s deadline")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["max_tokens"], 4096)
        self.assertEqual(record["reason"], "client_disconnected")
        self.assertGreaterEqual(record["elapsed"], 115)
        self.assertLess(record["elapsed"], 135)

    @unittest.skipUnless(os.environ.get("CCSLAB_LONG_STREAM_TESTS") == "1",
                         "set CCSLAB_LONG_STREAM_TESTS=1 to run real 60/120/150/300 second streams")
    def test_8192_stream_idle_is_cancelled_by_60_second_chunk_timeout(self):
        result, record = self._run_stream_case(8192, duration=90, interval=0, idle=True)
        self.assertIsNotNone(result["error"], "OpenCode unexpectedly survived an SSE chunk idle gap over 60s")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["max_tokens"], 8192)
        self.assertEqual(record["reason"], "client_disconnected")
        self.assertGreaterEqual(record["elapsed"], 55)
        self.assertLess(record["elapsed"], 75)

    def _run_stream_case(self, budget, *, duration, interval, idle=False):
        with tempfile.TemporaryDirectory(prefix=f"ccslab-stream-{budget}-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), _StreamFixture)
            fixture.daemon_threads = True
            fixture.block_on_close = False
            fixture.duration, fixture.interval, fixture.idle, fixture.records = duration, interval, idle, []
            fixture.primary_started = False
            server_thread = Thread(target=fixture.serve_forever, daemon=True)
            server_thread.start()
            base = f"http://127.0.0.1:{fixture.server_port}/v1"
            engine = Engine(root / "state", base, "", limits=(32768, budget))
            events, outcome = [], {}
            try:
                if idle:
                    with patch.object(engine_module, "ContextGuard", _DirectFixtureGuard):
                        engine.open_project(str(project))
                else:
                    engine.open_project(str(project))
                session = engine.new_session()

                def run():
                    try:
                        engine.run(session, "Return the local streaming fixture response.", events.append,
                                   lambda _request: True)
                    except Exception as error:
                        outcome["error"] = str(error)
                        try:
                            history = engine.history(session)
                            outcome["session_errors"] = [part.get("state", {}).get("error")
                                                         for message in history
                                                         for part in message.get("parts", [])
                                                         if part.get("state", {}).get("error")]
                        except Exception as history_error:
                            outcome["history_error"] = type(history_error).__name__
                    else:
                        outcome["error"] = None
                    outcome["delta_count"] = sum(event.get("type") == "delta" for event in events)

                started = time.monotonic()
                worker = Thread(target=run, daemon=True)
                worker.start()
                wait_limit = max(duration, 300) + 40
                worker.join(wait_limit)
                outcome["elapsed"] = time.monotonic() - started
                if worker.is_alive():
                    engine.stop(session)
                    worker.join(10)
                self.assertFalse(worker.is_alive(), "OpenCode request did not terminate within the fixture deadline")
                self.assertTrue(fixture.records, f"OpenCode did not send a local streaming request: {outcome}")
                record = next((item for item in fixture.records if item["primary"]), None)
                self.assertIsNotNone(record, f"fixture did not receive a tool-enabled primary request: {fixture.records}")
                if outcome.get("error") is not None:
                    engine.close()
                    diagnostics = list((root / "state").rglob("context-diagnostics.jsonl"))
                    if diagnostics:
                        outcome["context_diagnostics"] = [line for path in diagnostics
                                                          for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(record["finished"].wait(10),
                                f"fixture did not observe request completion or cancellation: {outcome}; "
                                f"reason={record['reason']} frames={record['frames']} "
                                f"elapsed={time.monotonic() - record['started']:.1f}s")
                return outcome, record
            finally:
                engine.close()
                fixture.shutdown()
                fixture.server_close()
                server_thread.join(2)

    def test_rejects_invalid_persisted_and_requested_values(self):
        for value in (True, -1, 127, 8193, "8192", 3000.0, None):
            self.assertEqual(load_output_limit({"output_limit": value}), 4096)
            with self.assertRaises(ValueError):
                set_output_limit({}, value)

    def test_output_command_changes_personal_budget_only(self):
        class Client:
            remote = 4096
            writes = 0
            reads = 0

            def model(self):
                return {"effective_context_limit": 131072, "output_limit": 4096,
                        "output_token_limit": 4096}

            def opencode_output_limit(self):
                self.reads += 1
                return self.remote

            def set_opencode_output_limit(self, value):
                self.writes += 1
                self.remote = value
                return {"output_limit": value}

        app = App.__new__(App)
        app.busy = False
        app.closing = False
        app.output_limit = 4096
        app.output_gateway_limit = None
        app.output_remote_limit = None
        app.output_sync_status = "尚未同步"
        app.output_sync_error = ""
        app.account = {"status": "active", "role": "admin"}
        app.prefs = {}
        app.model_info = {}
        app.history = {}
        app.session = None
        app.context_meter = SimpleNamespace(set_model=lambda *_args, **_kwargs: None,
                                            set_output_limit=lambda value, label:
                                            setattr(app, "display_budget", (value, label)))
        app.connection_mode = "account"
        app.account_client = Client()
        app.account_online = True
        app.chat = object()
        app.messages = []
        app.append = lambda _box, message, *_args, **_kwargs: app.messages.append(message)
        app.save_prefs = lambda: None
        app.refresh_context_output = lambda: setattr(
            app, "display_budget", (App.model_limits(app.model_info, app.output_limit)[1],
                                    "新任务有效输出预算"))
        app.update_model = lambda info: setattr(
            app, "display_budget", (App.model_limits(info, app.output_limit)[1], "新任务有效输出预算"))

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
        self.assertIn("个人预算：8192；新任务有效预算：4096", "".join(app.messages))
        self.assertEqual(app.account_client.remote, 4096)
        self.assertEqual(app.account_client.writes, 0)
        app.output_command("/output 8192")
        self.assertEqual(app.account_client.remote, 4096)
        self.assertEqual(app.account_client.reads, 0)
        self.assertEqual(app.account_client.writes, 0)
        self.assertEqual(app.display_budget, (4096, "新任务有效输出预算"))
        app.output_command("/output 4096")
        self.assertEqual((app.prefs["output_limit"], app.account_client.remote), (4096, 4096))

    def test_server_output_write_requires_authenticated_admin(self):
        class Client:
            remote = 4096
            writes = 0

            def opencode_output_limit(self):
                return self.remote

            def set_opencode_output_limit(self, value):
                self.writes += 1
                self.remote = value
                return {"output_limit": value}

            def model(self):
                return {"effective_context_limit": 131072, "output_token_limit": 8192}

        app = App.__new__(App)
        app.connection_mode = "account"
        app.account = {"status": "active", "role": "user"}
        app.account_online = True
        app.account_client = Client()
        with self.assertRaises(PermissionError):
            app.configure_output_limit(8192)
        self.assertEqual(app.account_client.writes, 0)

    def test_personal_budgets_are_scoped_per_account(self):
        preferences = {"output_limit": 2048}
        self.assertEqual(load_account_output_limit(preferences, "qtchen"), 2048)
        set_account_output_limit(preferences, "qtchen", 2048)
        self.assertEqual(load_account_output_limit(preferences, "bl"), 4096)
        set_account_output_limit(preferences, "bl", 4096)
        set_account_output_limit(preferences, "another-user", 6144)
        self.assertEqual(load_account_output_limit(preferences, "qtchen"), 2048)
        self.assertEqual(load_account_output_limit(preferences, "bl"), 4096)
        self.assertEqual(load_account_output_limit(preferences, "another-user"), 6144)
        self.assertEqual(preferences["output_limit"], 6144)

    def test_output_budget_error_states_are_distinguished(self):
        class Error(Exception):
            def __init__(self, status=None, category=None):
                self.status = status
                self.category = category

        self.assertEqual(App.output_sync_error_status(Error(401)), "账号未激活或登录已过期")
        self.assertEqual(App.output_sync_error_status(Error(403), {"status": "pending"}), "账号未激活")
        self.assertEqual(App.output_sync_error_status(Error(403), {"status": "active"}),
                         "权限不足（需要管理员账号）")
        self.assertEqual(App.output_sync_error_status(Error(404)), "Gateway 不支持该接口")
        self.assertEqual(App.output_sync_error_status(Error(503)), "Gateway 服务错误（HTTP 503）")
        self.assertEqual(App.output_sync_error_status(TimeoutError()), "网络连接失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
