"""Real Windows OpenCode integration smoke test against a scripted local protocol fixture."""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from engine import Engine, EngineError
from workspace_tools import Workspace


steps = [
    ("list", {}),
    ("read", {"path": "example.py"}),
    ("replace", {"path": "example.py", "old_text": "return a - b", "new_text": "return a * b"}),
    ("create", {"path": "test_example.py", "text": (
        "import unittest\nfrom example import add\n\n\nclass AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )}),
    ("run", {"program": "python", "args": ["-m", "unittest", "discover", "-v"]}),
    ("replace", {"path": "example.py", "old_text": "return a * b", "new_text": "return a + b"}),
    ("run", {"program": "python", "args": ["-m", "unittest", "discover", "-v"]}),
    ("read", {"path": "../outside.txt"}),
    ("delete", {"path": "example.py"}),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer fixture-token":
            self.send_error(401)
            return
        entries = {
            "/health": {"status": "ok"},
            "/v1/models": {"data": [{"id": "lab-general", "name": "Qwen fixture", "meta": {"n_ctx_train": 262144}}]},
            "/props": {"total_slots": 5, "n_ctx_total": 131072, "default_generation_settings": {"n_ctx": 26368}},
            "/slots": [{"id": index, "n_ctx": 26368, "is_processing": False} for index in range(5)],
        }
        if self.path not in entries:
            self.send_error(404)
            return
        raw = json.dumps(entries[self.path]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer fixture-token":
            self.send_error(401)
            return
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        tools = tuple(t["function"]["name"] for t in request.get("tools", []))
        messages = request["messages"]
        is_stop_test = any("SLOW-TURN" in str(m.get("content", "")) for m in messages)
        is_plan_test = str(messages[-1].get("content", "")).find("PLAN-TURN") != -1
        done = [m for m in messages if m["role"] == "tool"]
        self.server.calls.append({"tools": tools, "step": len(done)})
        if time.monotonic() < self.server.down_until:
            data = b'{"error":{"message":"502 model fixture unavailable"}}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response_id = "chatcmpl-test"
        now = int(time.time())

        def send(delta, finish=None):
            record = {"id": response_id, "object": "chat.completion.chunk", "created": now,
                      "model": "lab-general", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(record, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            send({"role": "assistant"})
            if is_plan_test and tools:
                assert "workspace_read" in tools
                assert not set(tools).intersection({"workspace_replace", "workspace_create", "workspace_delete", "workspace_run"}), tools
                send({"content": "## 实施方案\n\n先确认边界，再新增回归测试。"})
                send({}, "stop")
            elif not tools:
                send({"content": "修复加法并运行测试"})
                send({}, "stop")
            elif is_stop_test:
                send({"content": "正在执行停止测试。"})
                self.server.slow.set()
                time.sleep(12)
                send({}, "stop")
            elif len(done) < len(steps):
                name, args = steps[len(done)]
                name = "workspace_" + name
                assert name in tools, (name, tools)
                send({"tool_calls": [{"index": 0, "id": f"call_{len(done)}", "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]})
                send({}, "tool_calls")
            else:
                send({"content": "## 测试通过\n\n**已运行** `unittest`。\n\n- 修复加法\n- 保留源文件\n\n```python\nassert add(2, 3) == 5\n```"})
                send({}, "stop")
            usage = {"id": response_id, "object": "chat.completion.chunk", "created": now,
                     "model": "lab-general", "choices": [],
                     "usage": {"prompt_tokens": 6200, "completion_tokens": 300, "total_tokens": 6500}}
            self.wfile.write(("data: " + json.dumps(usage) + "\n\ndata: [DONE]\n\n").encode("utf-8"))
            self.wfile.flush()
        except (OSError, ConnectionError):
            pass


def test_paths(root, outside):
    workspace = Workspace(root)
    for name in ("../outside.txt", "C:/Windows/win.ini", "C:outside.txt", "/absolute", "example.py:ads",
                 "aux.txt", "NUL", ".git/config", ".GiT/config", ".env", "example.py.", "EXAMPL~1.PY", "outside-hardlink.txt"):
        try:
            workspace.path(name)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError(f"path allowed: {name}")
    junction = root / "outside-junction"
    cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    attempt = subprocess.run([str(cmd), "/d", "/c", "mklink", "/J", str(junction), str(outside.parent)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if attempt.returncode != 0:
        print("SKIP actual junction creation disallowed in current runner", flush=True)
        return
    try:
        workspace.path("outside-junction/outside.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("junction outside workspace allowed")
    junction.rmdir()
    print("PASS actual outside directory junction rejected", flush=True)


def main():
    work = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="engine-smoke-", dir=work) as td:
        temp = Path(td)
        root = temp / "中文路径 项目"
        root.mkdir()
        (root / "example.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        outside = temp / "outside.txt"
        outside.write_text("外部受保护内容", encoding="utf-8")
        try:
            os.link(outside, root / "outside-hardlink.txt")
            print("PASS actual external hardlink created for rejection test", flush=True)
        except PermissionError:
            print("SKIP actual hardlink creation disallowed in current runner", flush=True)
        test_paths(root, outside)
        print("PASS Windows traversal, cross-drive, Win32 device, ADS, .git and secret protections", flush=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.calls = []
        server.slow = threading.Event()
        server.down_until = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/v1"
        engine = Engine(temp / "state", base, "fixture-token")
        approvals, events = [], []

        def approve(request):
            approvals.append(request)
            return request["kind"] != "delete"

        try:
            engine.open_project(str(root))
            session = engine.new_session()
            print("PASS OpenCode", engine.version, "REST startup and MCP registration", flush=True)
            engine.run(session, "修复加法，添加测试并运行", events.append, approve, strict=True)
            print("got", len(events), "events and", len(approvals), "approvals", flush=True)
            assert "return a + b" in (root / "example.py").read_text()
            assert "测试通过" in "".join(e["text"] for e in events if e["type"] == "delta")
            outputs = [e["output"] for e in events if e["type"] == "tool"]
            assert any("FAILED" in o and "退出码：1" in o for o in outputs), outputs
            assert any("Ran 1 test" in o and "退出码：0" in o for o in outputs), outputs
            assert any("路径必须位于已选项目内" in o for o in outputs), outputs
            assert any("用户未批准" in o for o in outputs), outputs
            assert outside.read_text(encoding="utf-8") == "外部受保护内容"
            assert [a["kind"] for a in approvals] == ["write", "write", "command", "write", "command", "delete"]
            assert all(not [name for name in r["tools"] if not name.startswith("workspace_")
                            and name != "invalid"] for r in server.calls), server.calls
            assert engine.history(session)
            engine.close()
            engine.open_project(str(root))
            assert session in [item["id"] for item in engine.sessions()]
            assert any(p.get("tool") == "workspace_run" for m in engine.history(session) for p in m["parts"])
            print("PASS actual reads, writes, failed+successful unittest subprocess, approval denial, stream and restart history", flush=True)

            failure = []
            def slow_task():
                try:
                    engine.run(slow_session, "SLOW-TURN", events.append, approve)
                except Exception as error:
                    failure.append(str(error))
            slow_session = engine.new_session()
            worker = threading.Thread(target=slow_task, daemon=True)
            worker.start()
            assert server.slow.wait(15), failure
            engine.stop(slow_session)
            worker.join(6)
            assert not worker.is_alive() and not failure, failure
            print("PASS cancellation of active streamed task", flush=True)

            server.down_until = time.monotonic() + 60
            try:
                engine.run(engine.new_session(), "测试连接失败", events.append, approve)
            except EngineError as error:
                assert "网关" in str(error), str(error)
            else:
                raise AssertionError("502 was not surfaced")
            print("PASS HTTP 502 feedback with bounded retry", flush=True)
        finally:
            engine.close()
            server.shutdown()
            server.server_close()
        for local_state in (temp / "state").rglob("*"):
            if local_state.is_file() and local_state.stat().st_size < 16_000_000:
                assert b"fixture-token" not in local_state.read_bytes(), local_state
        print("PASS fixture model credential absent from OpenCode session storage and runtime logs", flush=True)


if __name__ == "__main__":
    main()
