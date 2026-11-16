"""Local protocol fixtures, not measurements of real Qwen tokenization."""

from verify_support import TEST_ROOT
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

sys.path.insert(0, str(Path(__file__).parent / "app"))
from context_errors import context_error
from context_guard import ContextGuard, estimate_input
from context_usage import ContextReading, historical_reading
from engine import Engine, EngineError, _Job, _model_failure
from workspace_tools import Workspace


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        p = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(p)
        ordinary = bool(p.get("tools"))
        if ordinary and self.server.mode in {"overflow", "terminal"} and not self.server.failed:
            self.server.failed = True
            data = json.dumps({"error": {"type": "exceed_context_size_error", "code": "context_length_exceeded",
                                          "message": "request (28000 tokens) exceeds the available context size (26368 tokens), try increasing it"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not ordinary and self.server.mode == "terminal":
            data = b'{"error":{"code":"context_length_exceeded","message":"context length exceeded"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        def emit(delta, finish=None, usage=None):
            event = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "lab-general",
                     "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                event["usage"] = usage
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()
        if ordinary and self.server.mode == "tools" and self.server.round < 13:
            n = self.server.round
            self.server.round += 1
            if n == 0:
                name, args = "workspace_replace", {"path": "work.py", "old_text": "return 0", "new_text": "return 1"}
            else:
                name, args = "workspace_read", {"path": "long.py", "start_line": 1 + (n - 1) * 40, "line_count": 40}
            emit({"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{n}", "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls")
            total = 21000 if n == 6 else 6000 + n * 300
        else:
            emit({"role": "assistant", "content": "Summary retains goal, work.py change, tests and next steps." if not ordinary else "RECOVERED-DONE"}, "stop")
            total = 600 if not ordinary else 3587
        # These counts are scripted protocol values, intentionally labelled fixture.
        emit({}, usage={"prompt_tokens": total - 100, "completion_tokens": 100, "total_tokens": total})
        self.wfile.write(b"data: [DONE]\n\n")


class BudgetTests(unittest.TestCase):
    def test_guard_preflight_is_bounded_and_keeps_payload(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            events = []
            guard = ContextGuard("http://127.0.0.1:1/v1", "upstream-secret", (26368, 4096), events.append,
                                 Path(folder) / "diagnostics.jsonl")
            payload = {"messages": [{"role": "system", "content": "用户限制" * 6000}],
                       "tools": [{"type": "function", "function": {"name": "workspace_read"}}], "max_tokens": 9000}
            original = json.dumps(payload)
            try:
                for attempt in range(3):
                    conn = HTTPConnection("127.0.0.1", guard.server.server_port, timeout=5)
                    conn.request("POST", "/v1/chat/completions", original.encode(),
                                 {"Authorization": "Bearer " + guard.secret, "Content-Type": "application/json"})
                    response = conn.getresponse()
                    data = json.loads(response.read())
                    self.assertEqual(response.status, 400)
                    self.assertEqual(data["error"]["code"], "context_length_exceeded" if attempt < 2 else "local_budget_guard")
                    self.assertIsNone(data["error"]["context_diagnostics"]["input_tokens"])
                    self.assertEqual(data["error"]["context_diagnostics"]["output_reserve"], 4096)
                    conn.close()
                self.assertEqual(json.dumps(payload), original)
                self.assertEqual(len({e["request_id"] for e in events}), 3)
                logs = (Path(folder) / "diagnostics.jsonl").read_text()
                self.assertNotIn("upstream-secret", logs)
                self.assertNotIn("用户限制", logs)
                self.assertIn('"stage": "preflight"', logs)
            finally:
                guard.close()

    def test_every_input_component_counts(self):
        base = {"messages": [{"role": "user", "content": "start"}]}
        for role in ("system", "assistant", "tool", "user"):
            other = {"messages": base["messages"] + [{"role": role, "content": "中文文件内容" * 500}]}
            self.assertGreater(estimate_input(other), estimate_input(base) + 1000)
        self.assertGreater(estimate_input({**base, "tools": [{"schema": "x" * 6000}]}), estimate_input(base) + 1000)

    def test_strict_error_classification_and_numbers(self):
        for message in ("invalid schema context length field", "context window property too large",
                        "unauthorized: supplied text says context length exceeded"):
            self.assertIsNone(context_error(json.dumps({"error": {"message": message}})))
        # User text anywhere else in the response cannot change the classification.
        self.assertIsNone(context_error(json.dumps({"detail": "context length exceeded", "error": {"message": "bad schema"}})))
        d = context_error(json.dumps({"error": {"message": "request (28000 tokens) exceeds the available context size (26368 tokens), try increasing it"}}))
        self.assertEqual((d["input_tokens"], d["context_limit"]), (28000, 26368))
        self.assertIsNone(context_error("not json"))
        self.assertNotEqual(_model_failure({"data": {"statusCode": 502, "message": "context length exceeded"}})[2], "context")

    def test_current_and_previous_readings(self):
        r = ContextReading()
        r.record({"state": "measured", "used_tokens": 3587})
        r.record({"state": "request", "estimated_input_tokens": 28000, "request_id": "test"})
        self.assertEqual((r.used_tokens, r.last_confirmed, r.estimated_tokens), (None, 3587, 28000))
        r.record({"state": "request_failed"})
        self.assertIsNone(r.used_tokens)
        r.record({"state": "measured", "used_tokens": None})
        self.assertIsNone(r.used_tokens)

    def test_complete_line_pages_and_explicit_fragments(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            (root / "long.py").write_text("\n".join(f"line_{n} = '" + "中" * 80 + "'" for n in range(80)), encoding="utf-8")
            workspace = Workspace(root)
            job = _Job("ses_fixture", lambda _: True, False)
            page = workspace.read(job, "long.py", line_count=80)
            self.assertIn("next_line=", page)
            self.assertNotIn("截断", page)
            self.assertLess(len(page.encode("utf-8")), 6500)
            (root / "one.py").write_text("long=" + "a" * 19000)
            self.assertIn("未返回部分代码", workspace.read(job, "one.py"))
            fragment = workspace.read(job, "one.py", start_column=1001, char_count=1000)
            self.assertIn("next_column=2001", fragment)
            self.assertEqual(len((root / "one.py").read_text()), 19005)

    def native(self, mode):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="budget-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            (project / "work.py").write_text("def work():\n    return 0\n")
            (project / "long.py").write_text("\n".join(f"value_{i} = {i}" for i in range(1000)))
            git = ["git", "-C", str(project)]
            subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
            subprocess.run(git + ["add", "."], check=True, capture_output=True)
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
            fixture.mode, fixture.failed, fixture.round, fixture.calls = mode, False, 0, []
            Thread(target=fixture.serve_forever, daemon=True).start()
            engine = Engine(root / "state", f"http://127.0.0.1:{fixture.server_port}/v1", "fixture-secret", limits=(26368, 4096))
            events = []
            try:
                engine.open_project(str(project))
                session = engine.new_session()
                if mode == "terminal":
                    with self.assertRaises(EngineError) as caught:
                        engine.run(session, "Keep user restrictions and work progress", events.append, lambda _: True)
                    self.assertEqual(caught.exception.category, "context")
                    self.assertIn("请求", str(caught.exception))
                    self.assertIn("4", str(caught.exception))
                else:
                    engine.run(session, "Keep user restrictions and work progress", events.append, lambda _: True)
                    self.assertIn("RECOVERED-DONE", "".join(e.get("text", "") for e in events))
                history = engine.history(session)
                self.assertFalse(any(p.get("state", {}).get("status") in {"pending", "running"}
                                     for m in history for p in m.get("parts", [])))
                if mode == "tools":
                    self.assertEqual(fixture.round, 13)
                    self.assertEqual(sum(p.get("type") == "tool" for m in history for p in m.get("parts", [])), 13)
                    before = subprocess.run(git + ["diff", "--binary"], check=True, capture_output=True).stdout
                    self.assertIn(b"+    return 1", before)
                    self.assertTrue(any(e.get("state") == "compacted" for e in events))
                    self.assertEqual(historical_reading(history).used_tokens, 3587)
                    engine.close()
                    engine.open_project(str(project))
                    self.assertEqual(len(engine.history(session)), len(history))
                    after = subprocess.run(git + ["diff", "--binary"], check=True, capture_output=True).stdout
                    self.assertEqual(after, before)
                for p in fixture.calls:
                    self.assertLessEqual(p["max_tokens"], 4096)
                    if p.get("stream"):
                        self.assertEqual(p["stream_options"], {"include_usage": True})
                    ids = [m.get("tool_call_id") for m in p["messages"] if m.get("role") == "tool"]
                    self.assertEqual(len(ids), len(set(ids)), "duplicate tool result in model request")
                diagnostics = list((root / "state").rglob("context-diagnostics.jsonl"))
                self.assertTrue(diagnostics)
                contents = diagnostics[0].read_text()
                for secret in ("fixture-secret", "Keep user restrictions", "long.py", "return 1"):
                    self.assertNotIn(secret, contents)
                if mode == "overflow":
                    self.assertIn('"input_tokens": 28000', contents)
                    self.assertTrue(any(e.get("text") == "上下文已压缩并恢复执行。" for e in events))
            finally:
                engine.close()
                fixture.shutdown()
                fixture.server_close()

    def test_native_overflow_recovery(self):
        self.native("overflow")

    def test_native_terminal_compaction_failure(self):
        self.native("terminal")

    def test_native_13_tools_compaction_restart_and_git_diff(self):
        self.native("tools")


if __name__ == "__main__":
    unittest.main(verbosity=2)
