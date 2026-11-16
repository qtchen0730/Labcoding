"""Windows HTTPS authentication, generation, and OpenCode tool tests."""

from contextlib import contextmanager, suppress
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
from threading import Event, Thread
import time
from unittest import TestCase, main

from verify_engine import Handler
from verify_tls import tls_files

from changes import Changes
from direct_connection import BASE_URL, MODEL_ID, DirectConnection, DirectError, supports_webui_sign_in
from direct_diagnostics import check_read_tool
from engine import Engine


TEST_KEY = "fixture-token"
EMAIL = "fixture@example.test"
PASSWORD = " fixture password "
DIAGNOSTIC_MARKER = "CCSLAB_TOOL_CHECK_271"


class DirectHandler(Handler):
    def _json(self, status, data, headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with suppress(OSError):
            self.wfile.write(payload)

    def do_GET(self):
        self.server.paths.append(("GET", self.path))
        super().do_GET()

    def do_POST(self):
        self.server.paths.append(("POST", self.path))
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/api/v1/auths/signin"):
            valid = (self.headers.get("Authorization") is None
                     and json.loads(raw) == {"email": EMAIL, "password": PASSWORD})
            if valid:
                self._json(self.server.login_status, self.server.login_response,
                           {"Location": "/elsewhere"})
            else:
                self._json(400, {"detail": PASSWORD})
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {})
            return
        authorization = self.headers.get("Authorization")
        if authorization != f"Bearer {TEST_KEY}" and not (self.server.allow_anonymous and authorization is None):
            self._json(401, {"error": {"message": "invalid credentials"}})
            return
        request = json.loads(raw)
        assert request.get("model") == MODEL_ID
        if not request.get("stream"):
            if self.server.probe_delay:
                time.sleep(self.server.probe_delay)
            if self.server.response_status != 200:
                self._json(self.server.response_status,
                           {"error": {"message": TEST_KEY}},
                           {"Retry-After": "9", "Location": "/v1/models"})
                return
            self.server.generated.append(request["messages"][0]["content"])
            if self.server.invalid_reply:
                self._json(200, {"data": [{"id": MODEL_ID}]})
                return
            self._json(200, {
                "id": "chatcomp-direct-test", "object": "chat.completion", "model": MODEL_ID,
                "choices": [{"message": {"role": "assistant", "content": "Ready"}, "finish_reason": "stop"}],
            })
            return

        if any("connection-check.txt" in str(item.get("content", "")) for item in request["messages"]):
            self._diagnostic(request)
            return
        original = self.rfile
        self.rfile = BytesIO(raw)
        try:
            super().do_POST()
        finally:
            self.rfile = original

    def _diagnostic(self, request):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        tools = {item["function"]["name"] for item in request.get("tools", [])}
        tool_results = [item["content"] for item in request["messages"] if item["role"] == "tool"]
        finish = "stop"
        delta = {"role": "assistant", "content": "连接测试"}
        if tools and not tool_results:
            assert "workspace_read" in tools
            assert not {"workspace_replace", "workspace_create", "workspace_run"} & tools
            finish = "tool_calls"
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "check-file", "type": "function",
                "function": {"name": "workspace_read", "arguments": '{"path":"connection-check.txt"}'},
            }]}
        if tool_results:
            assert any(DIAGNOSTIC_MARKER in str(item) for item in tool_results)
            self.server.diagnostic_read = True
            delta["content"] = DIAGNOSTIC_MARKER
        chunk = {"id": "diag-test", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": MODEL_ID}
        with suppress(OSError):
            for item, end in ((delta, None), ({}, finish)):
                response = chunk | {"choices": [{"index": 0, "delta": item, "finish_reason": end}]}
                self.wfile.write(("data: " + json.dumps(response, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")


@contextmanager
def direct_fixture(scratch):
    cert, key, client_context = tls_files(Path(scratch))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    with ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler) as server:
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        server.response_status = 200
        server.login_status = 200
        server.login_response = {"token": TEST_KEY, "token_type": "Bearer"}
        server.allow_anonymous = False
        server.invalid_reply = False
        server.probe_delay = 0
        server.calls = []
        server.generated = []
        server.paths = []
        server.diagnostic_read = False
        server.slow = Event()
        server.down_until = 0
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server, f"https://127.0.0.1:{server.server_port}/v1", client_context
        finally:
            server.shutdown()


def git(project, *args):
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


class Verification(TestCase):
    def test_https_generation_and_error_handling(self):
        self.assertEqual(BASE_URL, "https://ccslab-llm.qtchen.cn/api")
        scratch = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="direct-api-", dir=scratch) as temporary:
            root = Path(temporary)
            with direct_fixture(root) as (server, base, tls):
                client = DirectConnection(root / "state", base_url=base, tls=tls)
                self.assertEqual(client.model_base, base)
                self.assertEqual(client.model_id, MODEL_ID)
                self.assertEqual(client.probe(TEST_KEY)["model_id"], MODEL_ID)
                self.assertEqual(server.paths, [("POST", "/v1/chat/completions")])
                self.assertEqual(len(server.generated), 1)
                with self.assertRaises(DirectError) as rejected:
                    client.probe("invalid-test-key")
                self.assertEqual(rejected.exception.status, 401)
                with self.assertRaises(DirectError) as requires_login:
                    client.probe()
                self.assertIn("要求登录", str(requires_login.exception))
                server.allow_anonymous = True
                self.assertEqual(client.probe()["model_id"], MODEL_ID)
                server.allow_anonymous = False
                for status, category in ((403, "forbidden"), (429, "rate_limit"), (502, "unavailable"),
                                         (503, "unavailable"), (307, "redirect")):
                    with self.subTest(status=status):
                        server.response_status = status
                        with self.assertRaises(DirectError) as error:
                            client.probe(TEST_KEY)
                        self.assertEqual((error.exception.status, error.exception.category), (status, category))
                        self.assertNotIn(TEST_KEY, str(error.exception))
                        if status == 429:
                            self.assertIn("9", str(error.exception))
                self.assertTrue(all(method == "POST" for method, _ in server.paths))
                server.response_status = 200
                server.invalid_reply = True
                with self.assertRaises(DirectError) as bad_payload:
                    client.probe(TEST_KEY)
                self.assertEqual(bad_payload.exception.category, "response")
                server.invalid_reply = False
                untrusted = DirectConnection(root / "state", base_url=base)
                with self.assertRaises(DirectError) as certificate:
                    untrusted.probe(TEST_KEY)
                self.assertEqual(certificate.exception.category, "certificate")
                server.probe_delay = 1.5
                timeout = DirectConnection(root / "state", base_url=base, tls=tls, timeout=0.5)
                with self.assertRaises(DirectError) as timed_out:
                    timeout.probe(TEST_KEY)
                self.assertEqual(timed_out.exception.category, "timeout")
            with self.assertRaises(DirectError) as offline:
                client.probe(TEST_KEY)
            self.assertEqual(offline.exception.category, "network")

    def test_https_sign_in_without_storing_credentials(self):
        scratch = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="direct-login-", dir=scratch) as temporary:
            root = Path(temporary)
            with direct_fixture(root) as (server, base, tls):
                client = DirectConnection(root / "state", base_url=base, tls=tls)
                webui = client.for_address(base.removesuffix("/v1") + "/prefixed/api/v1")
                self.assertFalse(supports_webui_sign_in(base))
                self.assertTrue(supports_webui_sign_in(webui.model_base))
                token = webui.sign_in(" " + EMAIL + " ", PASSWORD)
                self.assertEqual(token, TEST_KEY)
                self.assertEqual(server.paths, [("POST", "/prefixed/api/v1/auths/signin")])
                self.assertEqual(client.probe(token)["model_id"], MODEL_ID)
                self.assertFalse((root / "state").exists())

                with self.assertRaises(DirectError) as bad_password:
                    webui.sign_in(EMAIL, "wrong")
                self.assertEqual(bad_password.exception.category, "unauthorized")
                self.assertNotIn(PASSWORD, str(bad_password.exception))
                for step, category in (("verify", "mfa"), ("enroll", "mfa"),
                                       ("recover", "mfa"), ("pending", "pending")):
                    server.login_response = {"next_step": step, "challenge_token": TEST_KEY}
                    with self.subTest(step=step), self.assertRaises(DirectError) as blocked:
                        webui.sign_in(EMAIL, PASSWORD)
                    self.assertEqual(blocked.exception.category, category)
                    self.assertNotIn(TEST_KEY, str(blocked.exception))

                server.login_response = {"token": "fake\r\nInjected: value", "token_type": "Bearer"}
                with self.assertRaises(DirectError) as unsafe:
                    webui.sign_in(EMAIL, PASSWORD)
                self.assertEqual(unsafe.exception.category, "response")
                server.paths.clear()
                server.login_status = 307
                with self.assertRaises(DirectError) as redirect:
                    webui.sign_in(EMAIL, PASSWORD)
                self.assertEqual(redirect.exception.category, "redirect")
                self.assertEqual(server.paths, [("POST", "/prefixed/api/v1/auths/signin")])

    def test_windows_native_agent_and_git(self):
        scratch = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="direct-agent-", dir=scratch, ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            project = root / "中文 Git 项目"
            project.mkdir()
            (project / "example.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            git(project, "init", "--quiet")
            git(project, "config", "core.autocrlf", "false")
            git(project, "add", "example.py")
            git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=NUL", "commit", "--quiet", "-m", "fixture")
            state = root / "state"
            state.mkdir()
            with direct_fixture(root) as (server, base, tls):
                client = DirectConnection(state, base_url=base, tls=tls)
                webui = client.for_address(base.removesuffix("/v1") + "/api")
                token = webui.sign_in(EMAIL, PASSWORD)
                client.probe(token)
                ca = client.engine_ca_bundle()
                check_read_tool(base, token, ca)
                self.assertTrue(server.diagnostic_read)
                engine = Engine(state, client.model_base, token, ca_bundle=ca)
                events, approvals = [], []

                def approve(item):
                    approvals.append(item["kind"])
                    return item["kind"] != "delete"

                try:
                    engine.open_project(str(project))
                    session = engine.new_session()
                    engine.run(session, "修复加法，添加测试并运行", events.append, approve, strict=True)
                    self.assertIn("return a + b", (project / "example.py").read_text(encoding="utf-8"))
                    self.assertIn("测试通过", "".join(e.get("text", "") for e in events if e["type"] == "delta"))
                    self.assertIn("command", approvals)
                    self.assertTrue(any("Ran 1 test" in e.get("output", "") and "退出码：0" in e["output"]
                                        for e in events if e["type"] == "tool"))
                    changes = Changes(project)
                    _, changed = changes.status()
                    file = next(item for item in changed if item.path == "example.py")
                    diff = changes.diff(file)
                    self.assertIn("-    return a - b", diff)
                    self.assertIn("+    return a + b", diff)
                    engine.close()
                    engine.open_project(str(project))
                    self.assertIn(session, {item["id"] for item in engine.sessions()})
                    self.assertTrue(engine.history(session))
                finally:
                    engine.close()
                for path in state.rglob("*"):
                    if path.is_file() and path.stat().st_size < 16_000_000:
                        self.assertNotIn(TEST_KEY.encode(), path.read_bytes(), str(path))
                        self.assertNotIn(PASSWORD.encode(), path.read_bytes(), str(path))


if __name__ == "__main__":
    main(verbosity=2)
