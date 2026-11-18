"""Focused checks for one-shot approval and narrow session grants."""

from collections import deque
from threading import Event, Lock, Thread
import tempfile
import time
import sys
import os
import subprocess
from http.client import HTTPConnection
import json
from types import SimpleNamespace
from pathlib import Path
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from approval_policy import SessionGrants, session_scope, signature
from workspace_tools import Workspace, ToolBridge
from engine import _Job
from app import App
from verify_support import TEST_ROOT


ACCOUNT = {"id": 17, "username": "alice"}
PROJECT = str(Path.cwd())
SESSION = "ses_approval_test"
def replacement_request(old="old", new="new", path="src/app.py"):
    return {"kind": "write", "title": "替换文件内容", "path": path,
            "old_text": old, "new_text": new,
            "detail": f"{path}\n\n原文本\n{old}\n\n替换文本\n{new}"}


WRITE = replacement_request()


def safe_query_request(project=PROJECT, cwd=None, arguments=None, executable=None):
    cwd = cwd or project
    executable = executable or str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                                   / "System32" / "where.exe")
    arguments = arguments or ["python.exe"]
    return {
        "kind": "command", "title": "运行 Windows 命令",
        "command": subprocess.list2cmdline([executable, *arguments]),
        "path": cwd, "project_root": project,
        "detail": f"工作目录：{cwd}\\n\\nwhere.exe {arguments[0]}",
        "execution_context": {
            "executable": os.path.realpath(executable),
            "arguments": list(arguments),
            "working_directory": os.path.normcase(os.path.realpath(cwd)),
            "timeout_seconds": 10, "file_size": 1000, "mtime_ns": 123456, "file_device": 3,
            "file_index": 42,
        },
    }


class _Panel:
    def __init__(self):
        self.hidden = False

    def pack_forget(self):
        self.hidden = True


class _Value:
    def set(self, _value):
        pass


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.scope = session_scope(ACCOUNT, PROJECT, SESSION)
        self.grants = SessionGrants()

    def test_exact_single_file_write_is_session_scoped(self):
        self.assertTrue(self.grants.allow(self.scope, WRITE))
        self.assertFalse(self.grants.allowable(WRITE))
        self.assertTrue(self.grants.allows(self.scope, dict(WRITE)))
        self.assertFalse(self.grants.allows(session_scope(ACCOUNT, PROJECT, "ses_other"), WRITE))
        self.assertFalse(self.grants.allows(session_scope({"id": 18}, PROJECT, SESSION), WRITE))
        self.assertFalse(self.grants.allows(session_scope(ACCOUNT, PROJECT + "-other", SESSION), WRITE))

    def test_near_match_and_high_risk_operations_never_inherit_grant(self):
        self.grants.allow(self.scope, WRITE)
        changed = dict(WRITE, detail=WRITE["detail"].replace("new", "other"))
        self.assertFalse(self.grants.allows(self.scope, changed))
        for request in (
            dict(WRITE, kind="delete", title="删除文件"),
            dict(WRITE, kind="command", title="运行 Windows 命令", command="python -c pass"),
            dict(WRITE, kind="unknown", title="其他操作"),
            {"kind": "write", "title": "批量覆盖文件", "path": "src/app.py", "detail": "many"},
        ):
            self.assertFalse(self.grants.allowable(request))
            self.assertFalse(self.grants.allow(self.scope, request))

    def test_clear_revokes_in_memory_grants(self):
        self.grants.allow(self.scope, WRITE)
        self.grants.clear()
        self.assertFalse(self.grants.allows(self.scope, WRITE))
        self.assertFalse(self.grants.has_scope(self.scope))

    def test_replacement_grants_distinguish_display_delimiters_and_missing_fields(self):
        first = replacement_request("a", "b\n\n替换文本\nc")
        second = replacement_request("a\n\n替换文本\nb", "c")
        self.assertEqual(first["detail"], second["detail"])
        self.assertTrue(self.grants.allow(self.scope, first))
        self.assertNotEqual(signature(first), signature(second))
        self.assertFalse(self.grants.allows(self.scope, second))
        for field in ("old_text", "new_text", "detail", "path"):
            incomplete = dict(first)
            incomplete.pop(field)
            self.assertFalse(self.grants.allow(self.scope, incomplete), field)
        self.assertFalse(self.grants.allow(self.scope, dict(first, detail="partial")))

    def test_only_exact_project_scoped_where_query_is_command_grantable(self):
        request = safe_query_request()
        self.assertTrue(self.grants.allowable(request, self.scope))
        self.assertTrue(self.grants.allow(self.scope, request))
        self.assertTrue(self.grants.allows(self.scope, dict(request)))
        self.assertFalse(self.grants.allowable(request))
        self.assertFalse(self.grants.allowable(
            safe_query_request(arguments=["python.exe", "/r"]), self.scope))
        self.assertFalse(self.grants.allowable(
            safe_query_request(cwd=str(Path(PROJECT).parent)), self.scope))
        self.assertFalse(self.grants.allowable(
            safe_query_request(executable=str(Path(os.environ.get("SystemRoot", r"C:\\Windows"))
                                               / "System32" / "cmd.exe"),
                               arguments=["/c", "echo hi"]), self.scope))
        self.assertFalse(self.grants.allowable(
            safe_query_request(executable=str(Path(os.environ.get("SystemRoot", r"C:\\Windows"))
                                               / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                               arguments=["-File", "script.ps1"]), self.scope))
        self.assertFalse(self.grants.allowable(
            dict(request, execution_context={**request["execution_context"], "script": "unknown.ps1"}),
            self.scope))

    def test_toolbridge_supplies_real_query_execution_context(self):
        captured = []
        job = SimpleNamespace(strict=True, stop=Event(), authorize=None,
                              approve=lambda request: captured.append(request) or True)
        Workspace(Path(PROJECT)).run(job, "where.exe", ["python.exe"], ".", 10)
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertTrue(self.grants.allowable(request, self.scope), request)
        self.assertEqual(request["execution_context"]["arguments"], ["python.exe"])
        self.assertEqual(request["execution_context"]["working_directory"],
                         os.path.normcase(os.path.realpath(PROJECT)))

    def test_stale_request_id_cannot_finish_next_ticket(self):
        app = App.__new__(App)
        current = {"id": "current", "request": WRITE, "scope": self.scope,
                   "result": {"allow": False}, "completed": Event(), "guard": Lock()}
        app.active_approval = current
        app.session_grants = self.grants
        app.approval_lock = Lock()
        app.approval_tickets = {"current": current}
        app.approval_panel = _Panel()
        app.approval_queue = deque()
        app.running = False
        app.approval_action_locked = False
        app.stop_requested = Event()
        app.closing = False
        app.task_state = _Value()
        app.update_session_grant_ui = lambda: None
        app.show_next_approval = lambda: None
        app.restore_approval_focus = lambda: None
        app.schedule = lambda *_args: None
        app.finish_approval(True, ticket_id="stale")
        self.assertFalse(current["completed"].is_set())
        app.finish_approval(True, ticket_id="current")
        app.finish_approval(False, ticket_id="current")
        self.assertTrue(current["completed"].is_set())
        self.assertTrue(current["result"]["allow"])

    def test_enter_is_latched_and_escape_rejects_only_active_item(self):
        app = App.__new__(App)
        app.active_approval = object()
        app.approval_key_latched = False
        decisions = []
        app.finish_approval = lambda allow=False, **_kwargs: decisions.append(allow)
        self.assertIsNone(app.approval_enter(SimpleNamespace(state=0x4)))
        self.assertEqual(decisions, [])
        self.assertEqual(app.approval_enter(), "break")
        self.assertEqual(app.approval_enter(), "break")
        self.assertEqual(decisions, [True])
        app.approval_enter_released()
        self.assertEqual(app.approval_escape(), "break")
        self.assertEqual(decisions, [True, False])

    def test_approval_has_no_standalone_window_implementation(self):
        self.assertFalse(hasattr(App, "approval_window"))


class ApprovalWidgetTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="approval-ui-")
        self.addCleanup(self.scratch.cleanup)
        self.root = tk.Tk()
        self.app = App(self.root, Path(self.scratch.name), smoke=True)
        self.addCleanup(self.close_app)
        self.root.update()
        self.app.page_panel.pack_forget()
        self.app.workspace_view.pack(fill="both", expand=True)
        self.app.account = ACCOUNT
        self.app.project = PROJECT
        self.app.session = SESSION
        self.app.active_approval_scope = self.app.current_approval_scope()
        self.app.running = True
        self.root.update()

    def close_app(self):
        self.app.running = False
        if self.root.winfo_exists():
            self.app.close()

    def ask(self, request):
        result = {}
        thread = Thread(target=lambda: result.update(allow=self.app.approve(request)))
        thread.start()
        deadline = time.monotonic() + 2
        while self.app.active_approval is None and thread.is_alive() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNotNone(self.app.active_approval)
        return thread, result

    def settle(self, thread, result):
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.root.update()
        return result["allow"]

    def wait_unlock(self):
        deadline = time.monotonic() + 1
        while self.app.approval_action_locked and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()

    def replace_bridge(self):
        project = Path(self.scratch.name) / "project"
        project.mkdir()
        self.app.project = str(project)
        self.app.active_approval_scope = self.app.current_approval_scope()
        job = _Job(SESSION, self.app.approve, False)
        bridge = ToolBridge(Workspace(project), lambda: job)
        self.addCleanup(bridge.close)
        return project / "approval_test.txt", job, bridge

    @staticmethod
    def mcp_call(bridge, name, arguments):
        connection = HTTPConnection("127.0.0.1", bridge.server_port, timeout=5)
        try:
            connection.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}}),
                {"Authorization": f"Bearer {bridge.secret}", "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError(response.status)
            return json.loads(response.read())["result"]
        finally:
            connection.close()

    def ask_replace(self, bridge, old, new):
        result = {}
        def call():
            try:
                result["reply"] = self.mcp_call(bridge, "replace", {
                    "path": "approval_test.txt", "old_text": old, "new_text": new})
            except Exception as error:
                result["error"] = repr(error)
        thread = Thread(target=call)
        thread.start()
        deadline = time.monotonic() + 3
        while self.app.active_approval is None and thread.is_alive() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNotNone(self.app.active_approval, result)
        self.assertTrue(thread.is_alive())
        return thread, result

    def settle_replace(self, thread, result, *, allowed):
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(bool(result["reply"].get("isError")), not allowed, result)
        self.root.update()

    def test_mcp_replace_default_mode_blocks_until_each_distinct_request_is_approved(self):
        filename, job, bridge = self.replace_bridge()
        changed, year = "CCSLab approval test changed", "CCSLab approval test 2026"
        filename.write_text(changed, encoding="utf-8")
        self.assertFalse(job.strict)
        signatures = []
        for old, new in ((changed, year), (year, year), (year, changed)):
            before = filename.read_bytes(), filename.stat().st_mtime_ns
            thread, result = self.ask_replace(bridge, old, new)
            self.assertEqual((filename.read_bytes(), filename.stat().st_mtime_ns), before)
            request = self.app.active_approval["request"]
            self.assertEqual(request, replacement_request(old, new, filename.name))
            signatures.append(signature(request))
            self.app.approval_session_button.invoke()
            self.settle_replace(thread, result, allowed=True)
            self.assertEqual(filename.read_text(encoding="utf-8"), new)
        self.assertEqual(len(set(signatures)), 3)
        repeated = self.mcp_call(bridge, "replace", {
            "path": filename.name, "old_text": changed, "new_text": year})
        self.assertFalse(repeated.get("isError"), repeated)
        self.assertIsNone(self.app.active_approval)
        self.assertEqual(filename.read_text(encoding="utf-8"), year)

        self.app.revoke_approval_button.invoke()
        thread, result = self.ask_replace(bridge, year, year)
        self.app.approval_reject_button.invoke()
        self.settle_replace(thread, result, allowed=False)
        self.assertEqual(filename.read_text(encoding="utf-8"), year)

        thread, result = self.ask_replace(bridge, year, year)
        self.app.approval_session_button.invoke()
        self.settle_replace(thread, result, allowed=True)
        self.app.open_session("ses_replacement_new")
        self.app.active_approval_scope = self.app.current_approval_scope()
        job.session_id = self.app.session
        thread, result = self.ask_replace(bridge, year, year)
        self.app.approval_reject_button.invoke()
        self.settle_replace(thread, result, allowed=False)
        self.assertEqual(filename.read_text(encoding="utf-8"), year)

    def test_mcp_replace_missing_callback_and_permission_denial_preserve_file(self):
        filename, job, bridge = self.replace_bridge()
        filename.write_text("original", encoding="utf-8")
        arguments = {"path": filename.name, "old_text": "original", "new_text": "modified"}
        for callback in (None, lambda _request: False):
            job.approve = callback
            result = self.mcp_call(bridge, "replace", arguments)
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(filename.read_text(encoding="utf-8"), "original")
        del job.approve
        result = self.mcp_call(bridge, "replace", arguments)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(filename.read_text(encoding="utf-8"), "original")
        job.approve = lambda _request: self.fail("Account/read-only/stop check was bypassed")
        def denied():
            raise RuntimeError("fixture account denied")
        job.authorize = denied
        self.assertTrue(self.mcp_call(bridge, "replace", arguments).get("isError"))
        job.authorize = None
        job.read_only = True
        self.assertTrue(self.mcp_call(bridge, "replace", arguments).get("isError"))
        job.read_only = False
        permission_checks = []
        def expires_after_approval():
            permission_checks.append(True)
            if len(permission_checks) == 2:
                raise RuntimeError("fixture account expired after approval")
        job.authorize = expires_after_approval
        job.approve = lambda _request: True
        self.assertTrue(self.mcp_call(bridge, "replace", arguments).get("isError"))
        self.assertEqual(len(permission_checks), 2)
        job.authorize = None
        def stopped_after_approval(_request):
            job.stop.set()
            return True
        job.approve = stopped_after_approval
        self.assertTrue(self.mcp_call(bridge, "replace", arguments).get("isError"))
        job.stop.set()
        self.assertTrue(self.mcp_call(bridge, "replace", arguments).get("isError"))
        self.assertEqual(filename.read_text(encoding="utf-8"), "original")

    def test_replace_fix_preserves_other_tool_approval_policies(self):
        filename, job, bridge = self.replace_bridge()
        calls = []
        job.approve = lambda request: calls.append(request) or False
        self.assertFalse(self.mcp_call(bridge, "create", {
            "path": filename.name, "text": "original"}).get("isError"))
        self.assertFalse(self.mcp_call(bridge, "read", {"path": filename.name}).get("isError"))
        self.assertEqual(calls, [])
        self.assertTrue(self.mcp_call(bridge, "delete", {"path": filename.name}).get("isError"))
        self.assertEqual(calls[-1]["kind"], "delete")
        self.assertTrue(self.mcp_call(bridge, "run", {
            "program": "where.exe", "args": ["python.exe"]}).get("isError"))
        self.assertEqual(calls[-1]["kind"], "command")
        self.assertEqual(filename.read_text(encoding="utf-8"), "original")

    def test_replacement_with_incomplete_details_cannot_be_approved(self):
        for field in ("old_text", "new_text", "detail"):
            request = dict(WRITE)
            request.pop(field)
            thread, result = self.ask(request)
            self.assertEqual(str(self.app.approval_once_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.approval_session_button.cget("state")), "disabled")
            self.app.approval_enter()
            self.app.approval_enter_released()
            self.assertTrue(thread.is_alive())
            self.app.approval_reject_button.invoke()
            self.assertFalse(self.settle(thread, result))
            self.wait_unlock()

    def test_session_write_grant_and_button_mouse_flow(self):
        self.assertEqual(self.app.approval_panel.winfo_toplevel(), self.root)
        self.assertEqual(self.app.approval_session_button.cget("text"), "允许本次会话")
        self.assertEqual(self.app.approval_once_button.cget("text"), "仅允许本次")
        self.assertEqual(self.app.approval_reject_button.cget("text"), "拒绝")
        thread, result = self.ask(WRITE)
        self.assertTrue(self.app.approval_panel.winfo_ismapped())
        self.app.approval_session_button.invoke()
        self.assertTrue(self.settle(thread, result))
        self.assertTrue(self.app.revoke_approval_button.winfo_manager())

        result = {}
        thread = Thread(target=lambda: result.update(allow=self.app.approve(dict(WRITE))))
        thread.start()
        self.assertTrue(self.settle(thread, result))
        self.assertIsNone(self.app.active_approval)

        self.app.revoke_approval_button.invoke()
        self.assertFalse(self.app.revoke_approval_button.winfo_manager())
        thread, result = self.ask(WRITE)
        self.wait_unlock()
        self.app.approval_once_button.invoke()
        self.assertTrue(self.settle(thread, result))

        request = {"kind": "command", "title": "运行 Windows 命令", "path": PROJECT,
                   "command": "python.exe -c \"print('ok')\"", "detail": "请核对完整命令。"}
        thread, result = self.ask(request)
        self.wait_unlock()
        self.assertEqual(str(self.app.approval_session_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.approval_once_button.cget("state")), "normal")
        self.assertIn("仅 where.exe", self.app.approval_hint.cget("text"))
        self.app.approval_once_button.invoke()
        self.assertTrue(self.settle(thread, result))

    def test_revoke_entry_is_fully_visible_with_narrow_window_and_long_project_name(self):
        self.root.geometry("1240x800")
        self.app.project_label.set("长" * 28)
        self.app.task_state.set("任务已完成")
        self.app.task_elapsed.set("00:06")
        thread, result = self.ask(WRITE)
        self.app.approval_session_button.invoke()
        self.assertTrue(self.settle(thread, result))
        button = self.app.revoke_approval_button
        self.assertTrue(button.winfo_ismapped())
        self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
        self.assertGreaterEqual(button.winfo_rootx(), button.master.winfo_rootx())
        self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                             button.master.winfo_rootx() + button.master.winfo_width())

    def test_revoke_entry_and_grant_survive_task_completion_then_revoke_reprompts(self):
        self.app.account = {**ACCOUNT, "status": "active", "role": "user", "assigned_role": "user"}
        self.app.running = False
        decisions = []
        def run(_session, _prompt, _on_event, approve, **_kwargs):
            decisions.append(approve(WRITE))
        self.app.engine = SimpleNamespace(run=run, close=lambda: None)
        self.app.run_task("isolated task lifecycle fixture")
        deadline = time.monotonic() + 3
        while self.app.active_approval is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNotNone(self.app.active_approval)
        self.app.approval_session_button.invoke()
        deadline = time.monotonic() + 3
        while (self.app.running or self.app.busy) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.app.running)
        self.assertFalse(self.app.busy)
        self.assertEqual(decisions, [True])
        self.assertEqual(self.app.task_state.get(), "任务已完成")
        self.assertTrue(self.app.session_grants.allows(self.app.current_approval_scope(), WRITE))
        self.assertTrue(self.app.revoke_approval_button.winfo_ismapped())
        self.app.revoke_approval_button.invoke()
        self.root.update()
        self.assertFalse(self.app.revoke_approval_button.winfo_ismapped())
        self.assertFalse(self.app.session_grants.allows(self.app.current_approval_scope(), WRITE))
        thread, result = self.ask(WRITE)
        self.app.approval_reject_button.invoke()
        self.assertFalse(self.settle(thread, result))

    def test_clear_workspace_refreshes_revoke_entry(self):
        self.app.account = {**ACCOUNT, "status": "active", "role": "user", "assigned_role": "user"}
        thread, result = self.ask(WRITE)
        self.app.approval_session_button.invoke()
        self.assertTrue(self.settle(thread, result))
        self.assertTrue(self.app.revoke_approval_button.winfo_manager())
        self.app.running = False
        self.app.clear_workspace()
        self.root.update()
        self.assertFalse(self.app.session_grants.has_scope(self.app.current_approval_scope()))
        self.assertFalse(self.app.revoke_approval_button.winfo_manager())

    def test_safe_query_session_grant_and_exact_repeat(self):
        def start_query(arguments):
            result = {}
            job = SimpleNamespace(strict=True, stop=Event(), authorize=None,
                                  approve=self.app.approve)
            thread = Thread(target=lambda: result.update(output=Workspace(Path(PROJECT)).run(
                job, "where.exe", arguments, ".", 10)))
            thread.start()
            deadline = time.monotonic() + 2
            while self.app.active_approval is None and thread.is_alive() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            return thread, result

        self.app.approval_action_locked = True
        thread, result = start_query(["python.exe"])
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertIsNone(self.app.active_approval)
        self.assertFalse(self.app.approval_panel.winfo_ismapped())
        self.app.unlock_approval_controls()
        deadline = time.monotonic() + 1
        while self.app.active_approval is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNotNone(self.app.active_approval)
        self.assertEqual(str(self.app.approval_session_button.cget("state")), "normal")
        self.app.approval_session_button.invoke()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("python", result["output"].casefold())

        repeated, repeated_result = start_query(["python.exe"])
        repeated.join(2)
        self.assertFalse(repeated.is_alive())
        self.assertIn("python", repeated_result["output"].casefold())
        self.assertIsNone(self.app.active_approval)

        changed_thread, changed_result = start_query(["where.exe"])
        self.assertIsNotNone(self.app.active_approval)
        self.assertEqual(str(self.app.approval_session_button.cget("state")), "normal")
        self.assertIn("只读 where.exe", self.app.approval_hint.cget("text"))
        self.app.approval_once_button.invoke()
        changed_thread.join(2)
        self.assertFalse(changed_thread.is_alive())
        self.assertIn("where.exe", changed_result["output"].casefold())

    def test_missing_session_scope_disables_button_with_reason(self):
        self.app.active_approval_scope = None
        thread, result = self.ask(WRITE)
        self.assertEqual(str(self.app.approval_session_button.cget("state")), "disabled")
        self.assertIn("会话范围不可用", self.app.approval_hint.cget("text"))
        self.assertTrue(thread.is_alive())
        self.app.approval_once_button.invoke()
        self.assertTrue(self.settle(thread, result))

    def test_incomplete_details_cannot_be_approved_with_enter(self):
        thread, result = self.ask({"kind": "command", "title": "运行 Windows 命令"})
        self.assertEqual(str(self.app.approval_once_button.cget("state")), "disabled")
        self.app.approval_enter()
        self.app.approval_enter_released()
        self.assertIsNotNone(self.app.active_approval)
        self.assertTrue(thread.is_alive())
        self.app.approval_action_locked = True
        self.app.unlock_approval_controls()
        self.assertEqual(str(self.app.approval_once_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.approval_reject_button.cget("state")), "normal")
        self.app.approval_escape()
        self.app.approval_enter_released()
        self.assertFalse(self.settle(thread, result))

    def test_enter_approves_and_escape_rejects_active_request(self):
        request = {"kind": "command", "title": "运行 Windows 命令", "path": PROJECT,
                   "command": "python.exe -c \"print('key')\"", "detail": "完整命令"}
        thread, result = self.ask(request)
        self.assertTrue(self.app.approval_panel.winfo_ismapped())
        self.assertTrue(self.app.input.bind("<Control-Return>"))
        self.assertIn(request["command"], self.app.approval_detail.get("1.0", "end-1c"))
        self.assertNotIn("请求 ID", self.app.approval_detail.get("1.0", "end-1c"))
        self.assertNotIn("操作：", self.app.approval_detail.get("1.0", "end-1c"))
        self.root.geometry("1240x760")
        self.root.update()
        self.assertLessEqual(self.app.approval_panel.winfo_width(),
                             self.app.approval_panel.master.winfo_width())
        for widget in (self.app.approval_once_button, self.app.approval_session_button,
                       self.app.approval_reject_button, self.app.input, self.app.send_button):
            self.assertTrue(widget.winfo_ismapped(), str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        self.app.approval_once_button.focus_force()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.app.approval_once_button.focus_force()
        self.root.update()
        ticket = self.app.active_approval
        self.app.approval_once_button.event_generate("<KeyPress-Return>", when="now")
        self.assertTrue(ticket["completed"].is_set(),
                        f"Tk Return did not reach approval binding; all={self.root.tk.call('bind', 'all', '<KeyPress-Return>')!r}; key={self.app.approval_key_bindings!r}")
        self.app.approval_once_button.event_generate("<KeyRelease-Return>", when="now")
        self.assertTrue(self.settle(thread, result))

        request = dict(request, command="python.exe -c \"print('reject')\"")
        thread, result = self.ask(request)
        self.wait_unlock()
        self.app.approval_once_button.focus_force()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.app.approval_once_button.focus_force()
        self.root.update()
        ticket = self.app.active_approval
        self.app.approval_once_button.event_generate("<KeyPress-Escape>", when="now")
        self.app.approval_once_button.event_generate("<KeyRelease-Escape>", when="now")
        self.assertTrue(ticket["completed"].is_set())
        self.assertFalse(self.settle(thread, result))

    def test_queued_requests_are_completed_one_at_a_time(self):
        first = {"kind": "command", "title": "运行 Windows 命令", "path": PROJECT,
                 "command": "python.exe -c first", "detail": "first"}
        second = dict(first, command="python.exe -c second", detail="second")
        first_thread, first_result = self.ask(first)
        second_result = {}
        second_thread = Thread(target=lambda: second_result.update(allow=self.app.approve(second)))
        second_thread.start()
        deadline = time.monotonic() + 2
        while not self.app.approval_queue and second_thread.is_alive() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(len(self.app.approval_queue), 1)
        self.assertEqual(self.app.active_approval["request"], first)
        self.app.approval_once_button.invoke()
        self.assertTrue(self.settle(first_thread, first_result))
        deadline = time.monotonic() + 1
        while self.app.active_approval is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertEqual(self.app.active_approval["request"], second)
        self.assertTrue(second_thread.is_alive())
        self.wait_unlock()
        self.app.approval_once_button.focus_force()
        self.root.update()
        self.app.approval_enter()
        self.assertTrue(self.settle(second_thread, second_result))


if __name__ == "__main__":
    unittest.main()
