"""Check token accounting against the bundled OpenCode event stream and Tk."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
import tkinter as tk
from tkinter import ttk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from context_usage import ContextMeter, ContextReading, historical_reading, step_tokens
from engine import Engine


class QAApp:
    frame = staticmethod(tk.Frame)
    label = staticmethod(lambda parent, variable, **_kwargs: tk.Label(parent, textvariable=variable))

    def __init__(self, root):
        self.style = ttk.Style(root)
        self.style.theme_use("clam")


class StopAtFirstOrdinaryResponse(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        is_tool_turn = bool(request.get("tools"))
        self.server.calls.append(request)
        is_summary = any(
            message.get("role") == "system" and "compaction" in str(message.get("content", "")).lower()
            for message in request.get("messages", [])
        )
        if is_tool_turn and not self.server.large_sent:
            # Above the configured usable window, forcing real OpenCode compaction.
            count = 22_800
            self.server.large_sent = True
        else:
            count = 6500 if is_tool_turn and not is_summary else 500
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {"id": "context-test", "object": "chat.completion.chunk",
                    "created": 1, "model": "lab-general"}
        frames = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "实测上下文"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"total_tokens": count, "prompt_tokens": count - 300,
                                      "completion_tokens": 300, "prompt_tokens_details": {"cached_tokens": 100}}},
        ]
        try:
            for frame in frames:
                self.wfile.write(("data: " + json.dumps(response | frame, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class ContextVerification(unittest.TestCase):
    def test_usage(self):
        self.assertEqual(step_tokens({
            "total": 6500, "input": 5050, "output": 350, "reasoning": 100,
            "cache": {"read": 1000, "write": 0},
        }), 6500)
        self.assertEqual(step_tokens({
            "input": 5050, "output": 300, "reasoning": 100,
            "cache": {"read": 1000, "write": 50},
        }), 6500)
        self.assertIsNone(step_tokens({"input": 0, "output": 0}))
        self.assertIsNone(step_tokens({"output": 1}))

    def test_tk(self):
        root = tk.Tk()
        try:
            meter = ContextMeter(QAApp(root), root)
            root.update_idletasks()
            self.assertIn("暂不可用", meter.title.get())
            meter.set_model({"effective_context_limit": 26368, "total_context": 131072,
                             "output_limit": 8192, "output_token_limit": 4096}, output_limit=4096)
            meter.record({"state": "measured", "used_tokens": 6500})
            self.assertIn("24.7%", meter.title.get())
            self.assertIn("26,368", meter.title.get())
            self.assertIn("4,096", meter.detail.get())
            meter.record({"state": "measured", "used_tokens": 24_000})
            self.assertIn("接近上限", meter.detail.get())
            meter.record({"state": "compacting"})
            self.assertIn("压缩中", meter.detail.get())
            self.assertIn("暂不可用", meter.title.get())
            meter.record({"state": "compacted"})
            meter.record({"state": "measured", "used_tokens": 6500})
            self.assertIn("已压缩", meter.detail.get())
            self.assertIn("24.7%", meter.title.get())
        finally:
            root.destroy()

    def test_native_sse_accounts_one_step_and_compaction(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="context-") as folder:
            parent = Path(folder)
            project = parent / "project"
            project.mkdir()
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), StopAtFirstOrdinaryResponse)
            fixture.calls = []
            fixture.large_sent = False
            Thread(target=fixture.serve_forever, daemon=True).start()
            model = f"http://127.0.0.1:{fixture.server_port}/v1"
            engine = Engine(parent / "state", model, "fixture-token")
            events = []
            try:
                engine.open_project(str(project))
                session = engine.new_session()
                engine.run(session, "验证上下文事件", events.append, lambda _what: False)
                history = historical_reading(engine.history(session))
            finally:
                engine.close()
                fixture.shutdown()
                fixture.server_close()
            contexts = [event for event in events if event.get("type") == "context"]
            self.assertIn({"type": "context", "state": "measured", "used_tokens": 22_800}, contexts)
            self.assertIn({"type": "context", "state": "compacting"}, contexts)
            self.assertIn({"type": "context", "state": "compacted"}, contexts)
            latest = ContextReading()
            for usage in contexts:
                latest.record(usage)
            self.assertEqual(latest, ContextReading(6500, "compacted"))
            self.assertEqual(latest, history)


if __name__ == "__main__":
    unittest.main(verbosity=2)
