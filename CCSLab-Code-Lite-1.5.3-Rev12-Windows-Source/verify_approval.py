"""Focused checks for one-shot approval and narrow session grants."""

from collections import deque
from threading import Event, Lock, Thread
import tempfile
import time
import sys
from types import SimpleNamespace
from pathlib import Path
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from approval_policy import SessionGrants, session_scope
from app import App
from verify_support import TEST_ROOT


ACCOUNT = {"id": 17, "username": "alice"}
PROJECT = str(Path.cwd())
SESSION = "ses_approval_test"
WRITE = {"kind": "write", "title": "替换文件内容", "path": "src/app.py",
         "detail": "src/app.py\n\n原文本\nold\n\n替换文本\nnew"}


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

    def test_session_write_grant_and_button_mouse_flow(self):
        self.assertEqual(self.app.approval_panel.winfo_toplevel(), self.root)
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
        self.assertIn("当前命令仅支持单次授权", self.app.approval_hint.cget("text"))
        self.app.approval_once_button.invoke()
        self.assertTrue(self.settle(thread, result))

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
