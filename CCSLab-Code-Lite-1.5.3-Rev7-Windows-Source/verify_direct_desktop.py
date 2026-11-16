"""Verify desktop authentication and the native Agent against two local HTTPS origins."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

from verify_direct import TEST_KEY, DirectHandler, direct_fixture
from app import App
from account_client import AccountClient
from direct_connection import DirectConnection


EMAIL = "agent-fixture@example.test"
PASSWORD = "a-fixture-password"
SESSION = "webui.fixture.session"
WEBUI_SIGNIN = "/api/v1/auths/signin"
WEBUI_UPDATE = "/api/v1/auths/update/password"
GATE_PATH = "/api/open-webui/access"
ENTRY_PASSWORD = "fixture-entry-password"
GATE_SESSION = "fixture-ccslab-admin"
GATE_GRANT = "p" * 64
GATE_USER = {"id": 1, "username": "fixture-admin", "role": "admin", "assigned_role": "admin", "status": "active"}
NEW_WEBUI_PASSWORD = "Web#42"


class RoutedHandler(DirectHandler):
    """Record auth at the origin boundary before adapting to the model fixture."""

    def do_GET(self):
        if self.path == GATE_PATH:
            if (self.headers.get("Authorization") == f"Bearer {GATE_SESSION}"
                    and self.headers.get("X-CCSLab-WebUI-Grant") == GATE_GRANT):
                self.send_response(204)
                self.end_headers()
            else:
                self._json(403, {"detail": "无效授权"})
            return
        if self.path == "/api/me":
            self._json(200, {"user": GATE_USER})
            return
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        super().do_GET()

    def do_POST(self):
        path, authorization = self.path, self.headers.get("Authorization")
        if path == GATE_PATH:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.access_checks.append(authorization)
            if authorization == f"Bearer {GATE_SESSION}" and body == {"password": ENTRY_PASSWORD}:
                self._json(200, {"grant": GATE_GRANT, "expires_at": int(time.time()) + 600})
            else:
                self._json(403, {"detail": "最高权限密码错误。"})
            return
        self.server.requests.append((path, authorization))
        if path == WEBUI_UPDATE and self.server.webui:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if authorization != f"Bearer {SESSION}" or body != {
                "password": self.server.webui_password, "new_password": NEW_WEBUI_PASSWORD,
            }:
                self._json(400, {"detail": "Invalid credentials"})
            else:
                self.server.webui_password = NEW_WEBUI_PASSWORD
                self._json(200, True)
            return
        if path == WEBUI_SIGNIN and self.server.webui:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if authorization is not None or body != {"email": EMAIL, "password": self.server.webui_password}:
                self._json(401, {"detail": "Invalid credentials"})
            else:
                self._json(200, {"token": SESSION, "token_type": "Bearer", "expires_at": int(time.time()) + 3600})
            return
        required = f"Bearer {SESSION}" if self.server.webui else None
        if path != self.server.chat_path or authorization != required:
            self.rfile.read(int(self.headers["Content-Length"]))
            self._json(404 if path != self.server.chat_path else 401, {"detail": "Not authenticated"})
            return
        self.path = "/v1/chat/completions"
        if authorization is None:
            self.headers["Authorization"] = f"Bearer {TEST_KEY}"
        else:
            self.headers.replace_header("Authorization", f"Bearer {TEST_KEY}")
        super().do_POST()


def configure_route(server, base_path, *, webui=False):
    server.RequestHandlerClass = RoutedHandler
    server.chat_path = base_path + "/chat/completions"
    server.webui = webui
    server.webui_password = PASSWORD
    server.requests = []
    server.access_checks = []


@contextmanager
def paired_fixtures(directory):
    directory.mkdir()
    with direct_fixture(directory) as first:
        tls = first[2]
        files = directory / "tls.pem", directory / "tls.key", tls
        with patch("verify_direct.tls_files", return_value=files):
            with direct_fixture(directory) as second:
                yield first, second


def wait(root, done, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if done():
            return
        time.sleep(0.02)
    raise AssertionError("Desktop verification did not finish.")


def widgets(widget, kind):
    for child in widget.winfo_children():
        if isinstance(child, kind):
            yield child
        yield from widgets(child, kind)


def button(app, label):
    return next(child for child in widgets(app.page_panel, ttk.Button) if child["text"] == label)


def fill(app, address, email="", password=""):
    entries = list(widgets(app.page_panel, ttk.Entry))
    for entry, value in zip(entries, (address, email, password), strict=True):
        entry.delete(0, "end")
        entry.insert(0, value)


def open_app(data, tls, *, smoke=False):
    root = tk.Tk()
    app = App(root, data, smoke=smoke)
    app.direct_connection = DirectConnection(data, base_url=app.direct_address.get(), tls=tls)
    app.gateway_transport = app.account_connection, AccountClient(app.account_connection[0], tls=tls, mode="direct")
    errors = []
    app.show_error = lambda title, error: errors.append((title, str(error)))
    return root, app, errors


def sign_in_gateway_admin(app):
    assert app.current_page == "access"
    account = app.get_account_client()
    account.token = GATE_SESSION
    app.signed_in(account, GATE_USER)
    app.select_connection("direct")
    assert app.current_page == "direct" and app.account_client.token == GATE_SESSION


def unlock(root, app, password):
    field = next(widgets(app.page_panel, ttk.Entry))
    assert field["show"]
    field.insert(0, password)
    button(app, "验证并继续").invoke()
    wait(root, lambda: not app.busy)


def plan(root, app, errors):
    app.show_page("workspace")
    app.mode.set("方案")
    app.input.insert("1.0", "PLAN-TURN 分析项目")
    app.send_button.invoke()
    wait(root, lambda: app.session and not app.running and not app.busy or errors)
    assert not errors, errors
    assert "实施方案" in app.chat.get("1.0", "end"), app.chat.get("1.0", "end")


def main():
    with tempfile.TemporaryDirectory(prefix="direct-desktop-", dir=Path(__file__).resolve().parent,
                                     ignore_cleanup_errors=True) as temporary:
        root_dir = Path(temporary)
        data = root_dir / "desktop"
        data.mkdir()
        project = root_dir / "已有项目"
        project.mkdir()
        with paired_fixtures(root_dir / "tls") as ((server_a, base_a, tls), (server_b, base_b, _)):
            base_b = base_b.removesuffix("/v1") + "/api"
            configure_route(server_a, "/v1")
            configure_route(server_b, "/api", webui=True)
            (data / "preferences.json").write_text(json.dumps({
                "connection_mode": "direct", "direct_address": base_a,
                "account_server": base_a.removesuffix("/v1"), "account_server_manual": True,
                "account_proxy_mode": "direct",
            }), encoding="utf-8")

            root, app, errors = open_app(data, tls, smoke=True)
            try:
                sign_in_gateway_admin(app)
                assert app.current_page == "direct"
                assert not app.check_direct_connection(address=base_b, email=EMAIL, password=PASSWORD)
                assert not server_a.requests and not server_b.requests
                unlock(root, app, "wrong-entry-password")
                assert "最高权限密码错误" in app.direct_feedback.get()
                assert not server_a.requests and not server_b.requests
                unlock(root, app, ENTRY_PASSWORD)
                assert server_a.access_checks == [f"Bearer {GATE_SESSION}"] * 2
                server_a.requests.clear()
                assert all("API Key" not in str(widget["text"]) for widget in widgets(app.page_panel, tk.Label))
                fill(app, base_a.removesuffix("/v1"))
                password_entry = next(entry for entry in widgets(app.page_panel, ttk.Entry) if entry["show"])
                assert not password_entry.master.winfo_manager()
                button(app, "连接").invoke()
                wait(root, lambda: not app.busy)
                assert app.direct_ready and app.can_use_agent and app.token == "" and not errors
                assert app.api_base == app.direct_address.get() == base_a
                assert server_a.requests == [("/v1/chat/completions", None)]
                app.open_project(str(project))
                wait(root, lambda: app.engine and not app.busy or errors)
                assert not errors, errors
                engine_a = app.engine
                plan(root, app, errors)
                assert all(header is None for _, header in server_a.requests), server_a.requests

                app.connection_button.invoke()
                fill(app, base_b)
                password_entry = next(entry for entry in widgets(app.page_panel, ttk.Entry) if entry["show"])
                assert password_entry.master.winfo_manager() == "grid"
                button(app, "连接").invoke()
                wait(root, lambda: not app.busy)
                assert app.direct_ready and app.engine is engine_a and app.api_base == base_a
                assert server_b.requests == [("/api/chat/completions", None)]
                assert "401" in app.direct_feedback.get(), app.direct_feedback.get()

                fill(app, base_b, EMAIL, "wrong-password")
                button(app, "登录并连接").invoke()
                wait(root, lambda: not app.busy)
                assert "登录失败" in app.direct_feedback.get(), app.direct_feedback.get()
                assert app.api_base == base_a and app.engine is engine_a
                assert server_b.requests[-1] == (WEBUI_SIGNIN, None)

                fill(app, base_b, EMAIL, PASSWORD)
                button(app, "登录并连接").invoke()
                wait(root, lambda: app.api_base == base_b and app.engine and not app.busy or errors)
                assert not errors, errors
                assert app.engine is not engine_a and app.engine.api_base == base_b
                assert app.project == str(project) and app.token == SESSION
                assert app.direct_address.get() == base_b
                assert server_b.requests[-2:] == [(WEBUI_SIGNIN, None), ("/api/chat/completions", f"Bearer {SESSION}")]
                plan(root, app, errors)
                assert server_b.calls

                app.connection_button.invoke()
                fill(app, base_a)
                button(app, "连接").invoke()
                wait(root, lambda: app.api_base == base_a and app.engine and not app.busy or errors)
                assert app.can_use_agent and app.token == ""
                assert all(header is None for _, header in server_a.requests), server_a.requests

                app.connection_button.invoke()
                fill(app, base_b, EMAIL, PASSWORD)
                button(app, "登录并连接").invoke()
                wait(root, lambda: app.api_base == base_b and app.engine and not app.busy or errors)
                assert app.token == SESSION
            finally:
                app.close()

            prefs = (data / "preferences.json").read_text(encoding="utf-8")
            assert json.loads(prefs)["direct_address"] == base_b
            assert all(secret not in prefs for secret in (EMAIL, PASSWORD, SESSION, ENTRY_PASSWORD))
            assert not (data / "direct-model.dpapi").exists()
            for path in data.rglob("*"):
                if path.is_file() and path.stat().st_size < 16_000_000:
                    raw = path.read_bytes()
                    assert all(secret.encode() not in raw for secret in (PASSWORD, SESSION, ENTRY_PASSWORD, GATE_GRANT)), str(path)

            before_restore = len(server_b.requests)
            root, app, errors = open_app(data, tls)
            try:
                assert app.current_page == "access"
                sign_in_gateway_admin(app)
                assert app.current_page == "direct" and not app.can_use_agent
                assert not app.check_direct_connection(restore=True)
                assert len(server_b.requests) == before_restore
                unlock(root, app, ENTRY_PASSWORD)
                wait(root, lambda: len(server_b.requests) > before_restore and not app.busy)
                assert not errors, errors
                assert app.current_page == "direct"
                assert not app.direct_ready and not app.can_use_agent and app.token == ""
                assert app.direct_address.get() == base_b
                assert server_b.requests[before_restore:] == [("/api/chat/completions", None)]
                assert "401" in app.direct_feedback.get(), app.direct_feedback.get()
                fill(app, base_b, EMAIL, PASSWORD)
                button(app, "登录并连接").invoke()
                wait(root, lambda: app.direct_ready and not app.busy)
                app.connection_button.invoke()
                button(app, "验证工具调用").invoke()
                wait(root, lambda: not app.busy and "工具调用通过" in app.direct_feedback.get())
                assert server_b.diagnostic_read and not server_a.diagnostic_read
                button(app, "修改当前 Open WebUI 登录密码").invoke()
                submit = button(app, "修改登录密码")
                for field, value in zip(widgets(submit.master, ttk.Entry),
                                        (PASSWORD, NEW_WEBUI_PASSWORD, NEW_WEBUI_PASSWORD), strict=True):
                    field.insert(0, value)
                submit.invoke()
                wait(root, lambda: not app.busy and not app.token)
                assert app.account_client.token == GATE_SESSION and app.direct_unlocked
                assert server_b.requests[-1] == (WEBUI_UPDATE, f"Bearer {SESSION}")
                fill(app, base_b, EMAIL, PASSWORD)
                button(app, "登录并连接").invoke()
                wait(root, lambda: not app.busy)
                assert not app.direct_ready and "登录失败" in app.direct_feedback.get()
                fill(app, base_b, EMAIL, NEW_WEBUI_PASSWORD)
                button(app, "登录并连接").invoke()
                wait(root, lambda: app.direct_ready and not app.busy)
                print("PASS desktop without API Key, strict unauthenticated HTTPS, and native anonymous OpenCode")
                print("PASS WebUI login, failed sign-in rollback, isolated origins, and session-only restart")
                print("PASS highest access password guards desktop model requests and must be re-entered after restart")
                print("PASS Open WebUI account password change and login with the new upstream password")
            finally:
                app.close()


if __name__ == "__main__":
    main()
