"""Exercise real Tk, OpenCode, and HTTPS gateway with a scripted upstream model."""

from verify_support import TEST_ROOT
from http.server import ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Event, Thread
import time
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

import uvicorn

from verify_engine import Handler
from verify_tls import tls_files
from app import App
from account_client import AccountClient, AccountError
from server.accounts import write_admin_code
from server.gateway import Settings, create_app
from server.model_control import ModelControl

TEST_CODE = "654319"
NEW_CODE = "503682"
ADMIN_PASSWORD = "A4min!"
NEW_PASSWORD = "N3w!42"
NEW_ENTRY_PASSWORD = "WebEntry-42!"
SELF_NEW_PASSWORD = "Own#42"


class DialogControlFixture(ModelControl):
    def __init__(self, settings):
        super().__init__(settings.database, settings.llama_url, settings.upstream_token)
        self.applied = []
        self.previewed = []
        self.applied_profile = None

    def preview(self, actor, total_context, slots):
        self.previewed.append((actor, total_context, slots))
        return {"confirmation": "fixture-confirmation",
                "current": self.status(), "requested": {"total_context": total_context, "slots": slots},
                "estimated_slot_context": total_context // slots, "gpu_risk": "隔离测试数据",
                "restart_required": True, "user_impact": "隔离测试操作", "expires_in": 90}

    def apply(self, actor, confirmation):
        assert confirmation == "fixture-confirmation"
        self.applied.append((actor, self.previewed[-1][1:]))
        self.applied_profile = self.previewed[-1][1:]
        return {"status": "applied", "model": self.status()}

    def status(self):
        model = super().status()
        if self.applied_profile:
            model.update(total_context=self.applied_profile[0], slots=self.applied_profile[1])
        return model


def start_gateway(directory, model):
    cert, key, tls = tls_files(directory)
    db = directory / "accounts.sqlite3"
    code_path = directory / "registration.hash"
    write_admin_code(code_path, TEST_CODE)
    settings = Settings(db, f"http://127.0.0.1:{model.server_port}", "lab-general", "fixture-token",
                        admin_registration_code_file=code_path)
    control = DialogControlFixture(settings)
    web_app = create_app(settings, model_control=control)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"https://127.0.0.1:{listener.getsockname()[1]}"
    configuration = uvicorn.Config(web_app, ssl_certfile=str(cert), ssl_keyfile=str(key),
                                   access_log=False, proxy_headers=False, log_level="error")
    gateway = uvicorn.Server(configuration)
    runner = Thread(target=lambda: gateway.run(sockets=[listener]), daemon=True)
    runner.start()
    deadline = time.monotonic() + 15
    while not gateway.started and runner.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert gateway.started, "HTTPS gateway did not start"
    return gateway, runner, url, tls, control


def expect_status(status, request):
    try:
        request()
    except AccountError as error:
        assert error.status == status, (error.status, str(error))
    else:
        raise AssertionError(f"Expected HTTP {status}")


def wait(root, condition, timeout=30, *, approval_gui=None, approvals=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        ticket = approval_gui.active_approval if approval_gui is not None else None
        if ticket is not None and ticket["id"] not in {item["id"] for item in approvals}:
            request = ticket["request"]
            approvals.append({"id": ticket["id"], **request})
            if request.get("kind") in {"write", "command"}:
                approval_gui.approval_once_button.invoke()
            else:
                approval_gui.approval_reject_button.invoke()
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("desktop event did not complete")


def git(project, *args):
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def widgets(node, kind):
    for child in node.winfo_children():
        if isinstance(child, kind):
            yield child
        yield from widgets(child, kind)


def button(parent, text):
    return next(item for item in widgets(parent, ttk.Button) if item["text"] == text)


def switch_role(root, gui, target, old_token):
    gui.role_button.invoke()
    if target == "user":
        wait(root, lambda: not gui.busy and gui.account["role"] == target)
        client = gui.account_client
        assert gui.token == client.token and client.token != old_token
        old_client = AccountClient(client.base, tls=client.tls)
        old_client.token = old_token
        expect_status(401, old_client.me)
        return
    window = next(widgets(root, tk.Toplevel))
    field = next(widgets(window, ttk.Entry))
    assert field["show"]
    field.insert(0, TEST_CODE)
    button(window, "确认切换").invoke()
    wait(root, lambda: not gui.busy and gui.account["role"] == target)
    client = gui.account_client
    assert gui.token == client.token and client.token != old_token
    old_client = AccountClient(client.base, tls=client.tls)
    old_client.token = old_token
    expect_status(401, old_client.me)


def manage_credentials(root, gui, user, old_password):
    gui.show_page("settings", section="passwords")
    wait(root, lambda: not gui.busy)
    picker = next(widgets(gui.page_panel, ttk.Combobox))
    picker.set(next(value for value in picker["values"] if "fixture.user" in value))
    picker.event_generate("<<ComboboxSelected>>")
    root.update()
    reset = button(gui.page_panel, "重置登录密码")
    fields = (entry for entry in widgets(reset.master, ttk.Entry) if entry["show"])
    for element, value in zip(fields, (NEW_PASSWORD, NEW_PASSWORD, TEST_CODE), strict=True):
        element.insert(0, value)
    reset.invoke()
    wait(root, lambda: not gui.busy)
    expect_status(401, user.me)
    expect_status(401, lambda: user.login("fixture.user", old_password))
    assert user.login("fixture.user", NEW_PASSWORD)["status"] == "active"

    admin = gui.account_client
    desktop_entry = AccountClient(admin.base, tls=admin.tls, mode="direct")
    expect_status(401, lambda: desktop_entry.verify_open_webui_access("000425"))
    verify_entry = button(gui.page_panel, "验证最高权限")
    next(widgets(verify_entry.master, ttk.Entry)).insert(0, "000425")
    verify_entry.invoke()
    wait(root, lambda: not gui.busy and admin.open_webui_expires_at)
    entry = button(gui.page_panel, "修改 Open WebUI 访问密码")
    for element in list(widgets(entry.master, ttk.Entry))[1:]:
        assert element["show"]
        element.insert(0, NEW_ENTRY_PASSWORD)
    entry.invoke()
    wait(root, lambda: not gui.busy)
    expect_status(403, lambda: admin.verify_open_webui_access("000425"))
    admin.verify_open_webui_access(NEW_ENTRY_PASSWORD)
    assert admin.me()["role"] == "admin"

    rotate = button(gui.page_panel, "修改授权密码")
    for element, value in zip(widgets(rotate.master, ttk.Entry), (TEST_CODE, NEW_CODE, NEW_CODE), strict=True):
        assert element["show"]
        element.insert(0, value)
    rotate.invoke()
    wait(root, lambda: not gui.busy and gui.current_page == "access")
    expect_status(401, admin.me)
    expect_status(403, lambda: admin.login("fixture.admin", ADMIN_PASSWORD, authorization_code=TEST_CODE))
    assert admin.login("fixture.admin", ADMIN_PASSWORD, authorization_code=NEW_CODE)["role"] == "admin"
    assert user.me()["role"] == "user"
    admin.verify_open_webui_access(NEW_ENTRY_PASSWORD)
    print("PASS six-character account passwords and independent WebUI entry password rotation in administrator settings")


def dialogue_confirmation(root, gui, control):
    gui.admin_button.invoke()
    assert gui.current_page == "admin"
    page = gui.page_controller
    assert not list(widgets(root, tk.Toplevel))
    wait(root, lambda: not gui.busy)
    assert page.user_cards
    page.tabs.select(page.model_tab)
    root.update()
    wait(root, lambda: page.has_model and not gui.busy)
    page.total.set("65536")
    page.slots.set("2")
    page.total.set("65535")
    assert page.preview_button.instate(["disabled"])
    assert not control.previewed
    page.total.set("65536")
    page.preview_button.invoke()
    wait(root, lambda: page.review_ticket and not gui.busy)
    assert control.previewed[-1][1:] == (65536, 2)
    cancel = next(b for b in widgets(page.review_card, ttk.Button) if b["text"] == "取消预览")
    apply = next(b for b in widgets(page.review_card, ttk.Button) if b["text"] == "确认应用")
    cancel.invoke()
    assert not page.review_ticket and not control.applied
    page.preview_button.invoke()
    wait(root, lambda: page.review_ticket and not gui.busy)
    apply.invoke()
    wait(root, lambda: len(control.applied) == 1 and not gui.busy)
    assert control.applied[-1][1] == (65536, 2)
    assert "配置已保存；Gateway 重新读取的运行参数与目标一致" in page.model_note.get()
    assert page.total.get() == "65536" and page.slots.get() == "2"
    gui.workspace_button.invoke()
    print("PASS Tk administrator console and explicit confirm/cancel before model apply")


def register_accounts(root, gui, server, tls, login_password):
    gui.select_connection("account")
    with patch("app.AccountClient", side_effect=lambda base, **options: AccountClient(base, tls=tls, **options)):
        gui.show_page("settings", section="connection")
        assert gui.current_page == "settings" and not list(widgets(root, tk.Toplevel))
        address = next(widgets(gui.page_panel, ttk.Entry))
        address.delete(0, "end")
        address.insert(0, server)
        proxy = next(w for w in widgets(gui.page_panel, ttk.Combobox) if "直连" in w["values"])
        proxy.set("直连")
        save = next(w for w in widgets(gui.page_panel, ttk.Button) if w["text"] == "保存并测试")
        save.invoke()
        wait(root, lambda: gui.gateway_report and gui.gateway_report.healthy)
        assert gui.account_connection == (server, "direct")
        assert gui.gateway_last_success.get() != "—"
        transport = gui.get_account_client()
        diagnose = next(w for w in widgets(gui.page_panel, ttk.Button) if w["text"] == "运行完整诊断")
        diagnose.invoke()
        wait(root, lambda: not gui.gateway_inflight and bool(list(widgets(root, tk.Toplevel))))
        with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as copied:
            gui.copy_account_diagnostics()
            assert "未登录" in copied.call_args.args[0]
        for dialog in list(widgets(root, tk.Toplevel)):
            dialog.destroy()

        gui.workspace_button.invoke()
        assert gui.current_page == "access"
        page = gui.page_controller

        def fill(role, user, password):
            page.select(role)
            page.username.set(user)
            page.password.set(password)
            page.confirmation.set(password)

        fill("user", "fixture.user", login_password)
        assert not page.admin_entry[1].winfo_manager()
        page.action.invoke()
        wait(root, lambda: page.mode == "login" and not gui.busy)
        fill("admin", "fixture.admin", ADMIN_PASSWORD)
        assert page.admin_entry[1].winfo_manager() and page.admin_entry[1]["show"]
        page.action.invoke()
        assert "授权密码" in page.feedback.get() and not gui.busy
        page.authorization.set(TEST_CODE)
        page.action.invoke()
        wait(root, lambda: page.mode == "login" and not gui.busy)
        assert not page.authorization.get()
        assert not page.admin_login.get()
        page.username.set("fixture.user")
        page.password.set(login_password)
        page.action.invoke()
        wait(root, lambda: gui.account_pending and not gui.busy)
        assert gui.account_client is transport
    print("PASS settings save-and-test, full diagnostics, safe report copy, and registration/login on one HTTPS client")


def main():
    with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="desktop-",
                                     ignore_cleanup_errors=True) as scratch:
        temp = Path(scratch)
        project = temp / "中文 Git 项目"
        project.mkdir()
        (project / "example.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        git(project, "init")
        git(project, "config", "core.autocrlf", "false")
        git(project, "add", "example.py")
        git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=NUL", "commit", "-m", "fixture")
        model = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        model.calls, model.slow, model.down_until = [], Event(), 0
        Thread(target=model.serve_forever, daemon=True).start()
        gateway, runner, server, tls, control = start_gateway(temp, model)
        login_password = "U5er#1"
        root = tk.Tk()
        gui = App(root, temp / "state", smoke=True)
        register_accounts(root, gui, server, tls, login_password)
        admin, user = AccountClient(server, tls=tls), gui.account_client
        assert admin.login("fixture.admin", ADMIN_PASSWORD, authorization_code=TEST_CODE)["role"] == "admin"
        expect_status(0, lambda: AccountClient(server).request("GET", "/health"))
        expect_status(401, lambda: AccountClient(server, tls=tls).request("GET", "/v1/models"))
        pending = profile = user.me()
        assert profile["status"] == "pending"
        expect_status(403, lambda: user.request("GET", "/v1/models"))
        expect_status(403, lambda: user.request("GET", "/api/admin/users"))
        path = f"/api/admin/users/{pending['id']}/status"
        assert gui.theme.get() == "薄荷绿"
        approvals = []
        errors = []
        gui.show_error = lambda title, error: errors.append((title, str(error)))
        assert gui.current_page == "workspace"
        assert gui.pending_banner.winfo_manager()
        assert not gui.can_use_agent and gui.send_button.instate(["disabled"])
        assert gui.open_button.instate(["disabled"]) and gui.input["state"] == "disabled"
        assert not gui.admin_button.winfo_manager()

        try:
            assert any(u["id"] == pending["id"] for u in admin.request("GET", "/api/admin/users")["users"])
            admin.request("POST", path, {"status": "active"})
            wait(root, lambda: not gui.account_polling)
            gui.poll_account()
            wait(root, lambda: gui.can_use_agent and not gui.account_polling)
            assert gui.current_page == "workspace"
            assert not gui.pending_banner.winfo_manager()
            metadata = gui.model_info
            assert metadata["effective_context_limit"] == 26368 and metadata["total_context"] == 131072
            assert metadata["slots"] == 5
            gui.open_project(str(project))
            wait(root, lambda: gui.engine and not gui.busy or errors)
            assert not errors, errors
            gui.input.insert("1.0", "修复加法并运行测试")
            gui.send_button.invoke()
            try:
                wait(root, lambda: gui.session and not gui.running and not gui.busy or errors,
                     approval_gui=gui, approvals=approvals)
            except AssertionError as error:
                calls = [(item.get("step"), item.get("tools")) for item in model.calls]
                raise AssertionError(
                    f"desktop task stalled: busy={gui.busy!r}, running={gui.running}, "
                    f"task_state={gui.task_state.get()!r}, errors={errors!r}, "
                    f"model_calls={calls!r}, approvals={[item.get('kind') for item in approvals]!r}"
                ) from error
            assert not errors, errors
            response = gui.chat.get("1.0", "end")
            assert all(part in response for part in ("测试通过", "已运行", "unittest", "assert add")), response
            assert not any(delimiter in response for delimiter in ("## 测试通过", "**已运行**", "```", "`unittest`")), response
            assert [item["kind"] for item in approvals] == ["write", "command", "write", "command", "delete"], approvals
            tools = gui.activity.transcript()
            assert "退出码：1" in tools and "退出码：0" in tools, tools
            files = [item.path for item in gui.change_rows]
            assert {"example.py", "test_example.py"}.issubset(files), files
            gui.change_filter.set("EXAMPLE.PY")
            assert len(gui.visible_changes) == 2
            gui.change_filter.set("test_")
            assert [change.path for change in gui.visible_changes] == ["test_example.py"]
            gui.change_filter.set("")
            gui.change_list.selection_set(files.index("example.py"))
            gui.select_change(None)
            wait(root, lambda: not gui.busy)
            diff = gui.diff.get("1.0", "end")
            assert "-    return a - b" in diff and "+    return a + b" in diff, diff
            assert "+1 / −1" in gui.result_status.get(), gui.result_status.get()
            assert "24.7%" in gui.context_meter.title.get(), gui.context_meter.title.get()
            session_id = gui.session
            gui.results_button.invoke()
            assert str(gui.results_pane) not in map(str, gui.workspace_view.panes())
            gui.settings_button.invoke()
            assert gui.current_page == "settings" and not list(widgets(root, tk.Toplevel))
            size_picker = next(w for w in widgets(gui.page_panel, ttk.Combobox) if "大" in w["values"])
            size_picker.set("大")
            size_picker.event_generate("<<ComboboxSelected>>")
            root.update()
            gui.workspace_button.invoke()
            assert gui.session == session_id and gui.current_page == "workspace"
            assert "测试通过" in gui.chat.get("1.0", "end")
            assert gui.chat_size.get() == "大" and gui.prefs["chat_size"] == "大"
            gui.results_button.invoke()
            assert str(gui.results_pane) in map(str, gui.workspace_view.panes())
            gui.mode.set("方案")
            gui.input.insert("1.0", "PLAN-TURN 只读分析测试方案")
            gui.send_button.invoke()
            wait(root, lambda: not gui.running and not gui.busy or errors)
            response = gui.chat.get("1.0", "end")
            assert "实施方案" in response and "## 实施方案" not in response, response
            assert len(approvals) == 5
            assert "+ b" in (project / "example.py").read_text(encoding="utf-8")
            assert gui.task_state.get() == "方案已生成", gui.task_state.get()
            admin.request("POST", path, {"status": "disabled"})
            expect_status(403, lambda: user.request("GET", "/v1/models"))
            wait(root, lambda: not gui.account_polling)
            gui.poll_account()
            wait(root, lambda: not gui.account_polling and gui.account["status"] == "disabled")
            assert gui.send_button.instate(["disabled"])
            user.logout()
            assert user.login("fixture.user", login_password)["status"] == "disabled"
            print("PASS certified HTTPS, role/approval gates, revocation, login after disable and real context usage")
            print("PASS new theme default, rendered heading, inline formatting and code; real engine read-only tool set")
            print("PASS installed app source flow: Windows Tk project open, submit, stream, tool/test output, real Git diff")
        finally:
            with patch("app.messagebox.askyesno", return_value=True):
                gui.close()

        root = tk.Tk()
        gui = App(root, temp / "state", smoke=True)
        try:
            assert gui.account_connection == (server, "direct")
            admin.logout()
            ordinary = admin.login("fixture.admin", ADMIN_PASSWORD)
            assert ordinary["role"] == "user" and ordinary["assigned_role"] == "admin"
            expect_status(403, lambda: admin.request("GET", "/api/admin/users"))
            expect_status(403, lambda: user.request(
                "POST", "/api/admin/model/preview", {"total_context": 65536, "slots": 2}))
            expect_status(403, lambda: user.request(
                "POST", "/api/admin/model/apply", {"confirmation": "not-a-ticket"}))
            gui.signed_in(admin, ordinary)
            assert gui.can_switch_role and not gui.is_admin and not gui.admin_button.winfo_manager()
            switch_role(root, gui, "admin", admin.token)
            dialogue_confirmation(root, gui, control)
            switch_role(root, gui, "user", admin.token)
            expect_status(403, lambda: admin.request("GET", "/api/admin/users"))
            switch_role(root, gui, "admin", admin.token)
            admin.request("POST", path, {"status": "active"})
            manage_credentials(root, gui, user, login_password)
            gui.signed_in(user, user.me())
            gui.open_project(str(project))
            wait(root, lambda: not gui.busy)
            gui.open_session(session_id)
            assert "测试通过" in gui.chat.get("1.0", "end")
            assert "## 测试通过" not in gui.chat.get("1.0", "end")
            assert "退出码：0" in gui.activity.transcript()
            assert "test_example.py" in gui.activity.transcript()
            print("PASS Tk application restart: same account restored transcript, tools and file status")
            previous = AccountClient(server, tls=tls, mode="direct")
            previous.login("fixture.user", NEW_PASSWORD)
            gui.show_page("settings", section="passwords")
            submit = button(gui.page_panel, "修改登录密码")
            for element, value in zip(widgets(submit.master, ttk.Entry),
                                      (NEW_PASSWORD, SELF_NEW_PASSWORD, SELF_NEW_PASSWORD), strict=True):
                assert element["show"]
                element.insert(0, value)
            submit.invoke()
            wait(root, lambda: not gui.busy and gui.current_page == "access")
            expect_status(401, previous.me)
            expect_status(401, lambda: previous.login("fixture.user", NEW_PASSWORD))
            assert previous.login("fixture.user", SELF_NEW_PASSWORD)["status"] == "active"
            assert admin.me()["role"] == "admin"
            print("PASS ordinary user changed own password and all old sessions were revoked without affecting the administrator")
        finally:
            with patch("app.messagebox.askyesno", return_value=True):
                gui.close()
            gateway.should_exit = True
            runner.join(10)
            model.shutdown()
            model.server_close()


if __name__ == "__main__":
    main()
