"""Pinned OpenCode processes, native sessions and MCP tools against loopback SSE only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import select
import ssl
from threading import Event, Thread
import time
import unittest

from engine import Engine
from context_guard import ContextGuard
from verify_tls import tls_files
import urllib3
from verify_support import TEST_ROOT


class ConcurrentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer fixture-token":
            self.send_error(401)
            return
        tools = {tool["function"]["name"] for tool in request.get("tools", [])}
        messages = request["messages"]
        prompt = " ".join(str(item.get("content", "")) for item in messages if item.get("role") == "user")
        label = "A" if "TASK-A" in prompt else "B"
        write = "WRITE-TURN" in prompt
        done = [item for item in messages if item.get("role") == "tool"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(delta, finish=None):
            chunk = {"id": "fixture-" + label, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": "lab-general", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()

        try:
            send({"role": "assistant"})
            if not tools:
                send({"content": "Fixture " + label}, "stop")
            elif not done:
                name = "workspace_create" if write else "workspace_read"
                if name not in tools:
                    self.server.errors.append((name, tools))
                arguments = {"path": "output.txt", "text": label + "-written"} if write else {"path": "marker.txt"}
                if not write and tools.intersection({"workspace_create", "workspace_run", "workspace_replace", "workspace_delete"}):
                    self.server.errors.append("readonly tools exposed")
                send({"tool_calls": [{"index": 0, "id": "call_" + label, "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")
            else:
                self.server.tool_results.append((label, str(done[-1].get("content"))))
                send({"content": label + "-first|"})
                self.server.started[label].set()
                deadline = time.monotonic() + 25
                while not self.server.release[label].wait(.03) and time.monotonic() < deadline:
                    if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1):
                        self.server.disconnected[label].set()
                        return
                send({"content": label + "-last|"}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (OSError, ConnectionError):
            pass


class NativeConcurrentTests(unittest.TestCase):
    def test_native_engine_reuses_its_global_slot_through_https_guard(self):
        from uuid import uuid4
        from verify_desktop import start_gateway, TEST_CODE
        from server.accounts import Accounts
        from account_client import AccountClient

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.calls += 1
                body = b'data: {"choices":[{"delta":{"content":"single-slot-native-ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="single-slot-native-", ignore_cleanup_errors=True) as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            upstream.calls = 0
            Thread(target=upstream.serve_forever, daemon=True).start()
            gateway, runner, url, tls, control = start_gateway(base, upstream)
            control.applied_profile = (32768, 1)
            accounts = Accounts(base / "accounts.sqlite3", admin_code_file=base / "registration.hash")
            accounts.create("single-slot-admin", "Fixture#42", bootstrap=True)
            client = AccountClient(url, tls=tls)
            client.login("single-slot-admin", "Fixture#42", authorization_code=TEST_CODE)
            task_id = uuid4().hex
            engine = Engine(base / "state", client.model_base, client.token,
                            ca_bundle=base / "ca.pem", task_id=task_id)
            try:
                self.assertTrue(client.acquire_task(task_id))
                engine.open_project(str(project))
                session, events = engine.new_session(), []
                engine.run(session, "Reply briefly", events.append, lambda _request: False, read_only=True)
                self.assertIn("single-slot-native-ok", "".join(event.get("text", "") for event in events))
                stats = client.request("GET", "/api/tasks")
                self.assertEqual((stats["running_tasks"], stats["available_slots"]), (1, 0))
                self.assertGreaterEqual(upstream.calls, 1)  # Native title generation can make another call.
                self.assertEqual(control._in_flight, 1)
            finally:
                engine.close()
                client.release_task(task_id)
                gateway.should_exit = True
                runner.join(10)
                upstream.shutdown()
                upstream.server_close()

    def test_https_upstream_cancel_drains_pending_windows_read(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[]}\n\n')
                self.wfile.flush()
                self.server.started.set()
                self.server.release.wait(10)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="cancel-tls-") as directory:
            base = Path(directory)
            cert, key, _tls = tls_files(base)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.started, server.release = Event(), Event()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            Thread(target=server.serve_forever, daemon=True).start()
            guard = ContextGuard(f"https://127.0.0.1:{server.server_port}/v1", "fixture-token", (32768, 4096),
                                 lambda _event: None, base / "context.jsonl", ca_bundle=base / "ca.pem")
            done, errors = Event(), []
            def consume():
                pool = urllib3.PoolManager()
                try:
                    response = pool.request("POST", guard.url + "/chat/completions",
                                            headers={"Authorization": "Bearer " + guard.secret, "Content-Type": "application/json"},
                                            body=json.dumps({"messages": [], "stream": True}).encode(), preload_content=False)
                    while response.read1(4096):
                        pass
                    response.close()
                except Exception as error:
                    errors.append(error)
                finally:
                    pool.clear()
                    done.set()
            worker = Thread(target=consume, daemon=True)
            worker.start()
            try:
                self.assertTrue(server.started.wait(5))
                deadline = time.monotonic() + 3
                while not any(guard.requests.values()) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(any(guard.requests.values()))
                started = time.monotonic()
                guard.close()
                self.assertLess(time.monotonic() - started, 4)
                self.assertFalse(guard.requests)
                self.assertTrue(done.wait(2))
                self.assertFalse(errors)
                self.assertIn('"state": "request_cancelled"', guard.log_path.read_text(encoding="utf-8"))
            finally:
                server.release.set()
                if not guard.closed.is_set():
                    guard.close()
                server.shutdown()
                server.server_close()
                worker.join(3)

    def test_native_sessions_interleaved_tools_abort_and_project_isolation(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="native-concurrent-", ignore_cleanup_errors=True) as directory:
            base = Path(directory)
            projects = [base / "project-a", base / "project-b"]
            for project in projects:
                project.mkdir()
                (project / "marker.txt").write_text(project.name, encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), ConcurrentHandler)
            server.started = {label: Event() for label in ("A", "B")}
            server.release = {label: Event() for label in ("A", "B")}
            server.disconnected = {label: Event() for label in ("A", "B")}
            server.tool_results, server.errors = [], []
            Thread(target=server.serve_forever, daemon=True).start()
            engines = [Engine(base / "state", f"http://127.0.0.1:{server.server_port}/v1", "fixture-token") for _ in range(2)]
            errors, streams, approvals, threads = [], {"A": [], "B": []}, [], []

            def start(index, session, *, write=False):
                label = "AB"[index]
                def execute():
                    try:
                        engines[index].run(session, "TASK-" + label + (" WRITE-TURN" if write else " READ-TURN"),
                                           streams[label].append,
                                           lambda request: approvals.append((label, request)) or True,
                                           read_only=not write, strict=write)
                    except Exception as error:
                        errors.append(error)
                thread = Thread(target=execute, daemon=True)
                thread.start()
                threads.append(thread)
                return thread

            def wait(predicate, seconds=25):
                deadline = time.monotonic() + seconds
                while not predicate() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(predicate(), f"native timeout; errors={errors!r}")

            try:
                # Independent bridges and callbacks, with the SAME native project database.
                for engine in engines:
                    engine.open_project(str(projects[0]))
                sessions = [engine.new_session() for engine in engines]
                self.assertTrue(set(sessions).issubset({item["id"] for item in engines[1].sessions()}))
                self.assertNotEqual(engines[0].port, engines[1].port)
                self.assertIsNot(engines[0].bridge, engines[1].bridge)
                a, b = (start(index, session) for index, session in enumerate(sessions))
                wait(lambda: all(event.is_set() for event in server.started.values()))
                self.assertTrue(a.is_alive() and b.is_alive())
                wait(lambda: all(any(event.get("type") == "delta" and label + "-first|" in event.get("text", "")
                                    for event in streams[label]) for label in "AB"))
                server.release["A"].set()
                a.join(8)
                self.assertFalse(a.is_alive())
                self.assertTrue(b.is_alive())
                engines[1].stop(sessions[1])
                b.join(8)
                self.assertFalse(b.is_alive())
                stopped_guard = engines[1].context_guard
                started = time.monotonic()
                engines[1].close()
                self.assertLess(time.monotonic() - started, 4)
                self.assertFalse(stopped_guard.requests)
                wait(server.disconnected["B"].is_set, 4)
                server.release["B"].set()
                self.assertFalse(errors)
                self.assertFalse(approvals)
                for label in "AB":
                    text = "".join(event.get("text", "") for event in streams[label] if event["type"] == "delta")
                    self.assertNotIn(("B" if label == "A" else "A") + "-first|", text)
                self.assertTrue(all("project-a" in output for _label, output in server.tool_results))

                # Two different workspaces, concurrent native mutation tools, separate approval jobs.
                engines[1].open_project(str(projects[1]))
                server.started = {label: Event() for label in "AB"}
                server.release = {label: Event() for label in "AB"}
                sessions = [engine.new_session() for engine in engines]
                a, b = (start(index, session, write=True) for index, session in enumerate(sessions))
                wait(lambda: all(event.is_set() for event in server.started.values()))
                self.assertTrue(a.is_alive() and b.is_alive())
                self.assertEqual((projects[0] / "output.txt").read_text(), "A-written")
                self.assertEqual((projects[1] / "output.txt").read_text(), "B-written")
                self.assertEqual({label for label, _request in approvals}, {"A", "B"})
                for release in server.release.values():
                    release.set()
                for thread in (a, b):
                    thread.join(8)
                    self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertFalse(server.errors)
                self.assertTrue(engines[0].history(sessions[0]))
                self.assertTrue(engines[1].history(sessions[1]))
            finally:
                for release in server.release.values():
                    release.set()
                for engine in engines:
                    engine.close()
                for thread in threads:
                    thread.join(5)
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
