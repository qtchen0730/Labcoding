"""Verify the approval cards in the native desktop against a local account fixture."""

from verify_support import TEST_ROOT

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from account_client import AccountError
from admin_view import AdminPage, account_time, build_admin_page
from app import App, DARK, LIGHT
from themes import THEMES


CREATED = 1735689600
LONG_NAME = "账号审批界面用户名长度测试用" * 3 + "甲"


class RosterFixture:
    def __init__(self):
        self.users = [
            self.user(14, LONG_NAME, "pending"),
            self.user(9, "Example.Disabled", "disabled"),
            self.user(8, "Example.Active", "active"),
            self.user(7, "Example.Rejected", "rejected"),
            self.user(2, "Current.Admin", "active", role="admin"),
            self.user(3, "Another.Admin", "active", role="admin"),
            *(self.user(identity, f"Member.{identity}", "active") for identity in range(50, 170)),
        ]
        self.changes = []

    @staticmethod
    def user(identity, username, status, role="user"):
        return {"id": identity, "username": username, "role": role, "status": status, "created_at": CREATED}

    def request(self, method, path, body=None, **_options):
        if (method, path) == ("GET", "/api/admin/users"):
            return {"users": [dict(user) for user in self.users]}
        prefix, identity, suffix = path.rsplit("/", 2)
        assert (method, prefix, suffix) == ("POST", "/api/admin/users", "status")
        user = next(user for user in self.users if user["id"] == int(identity))
        assert user["role"] == "user"
        self.changes.append((int(identity), body["status"]))
        user["status"] = body["status"]
        return {"user": dict(user)}

    def clear_open_webui_access(self):
        pass


def descendants(widget, kind):
    for child in widget.winfo_children():
        if isinstance(child, kind):
            yield child
        yield from descendants(child, kind)


def buttons(card):
    return {button.cget("text"): button for button in descendants(card, ttk.Button)}


def wait(root, condition):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        root.update()
        if condition():
            return
        sleep(0.02)
    raise AssertionError("The desktop did not reach the expected state")


def verify(gui, root):
    fixture = RosterFixture()
    gui.account_client = fixture
    gui.account = fixture.users[4]
    gui.account_online = True
    gui.show_page("admin")
    page = gui.page_controller
    wait(root, lambda: len(page.user_cards) == 126 and not gui.busy)
    pending = page.user_cards[14]
    name, badge, detail, quota = list(descendants(pending, tk.Label))
    assert quota.cget("text") == "额度不可用"
    assert name.cget("text") == LONG_NAME
    assert badge.cget("text") == "待审批"
    assert detail.cget("text") == f"普通用户  ·  注册于 {account_time(CREATED)}"
    assert "管理员" in next(label.cget("text") for label in descendants(page.user_cards[3], tk.Label)
                         if "注册于" in label.cget("text"))
    assert [value.get() for value in page.account_overview] == ["1", "123", "1", "1"]
    assert page.user_summary.get() == "显示 126 / 126 个账号"
    assert [set(buttons(page.user_cards[identity])) for identity in (14, 8, 9, 7, 2, 3)] == [
        {"激活", "禁用", "拒绝", "重置登录密码", "设置额度"}, {"禁用", "重置登录密码", "设置额度"},
        {"重新激活", "重置登录密码", "设置额度"}, {"重新激活", "重置登录密码", "设置额度"}, set(), set(),
    ]

    for width in (950, 1280):
        root.geometry(f"{width}x620")
        root.update()
        assert 24 <= int(name.cget("wraplength")) <= name.winfo_width()
        assert name.winfo_x() + name.winfo_width() <= badge.winfo_x() - 12
        assert name.winfo_height() >= name.winfo_reqheight()
        assert pending.winfo_y() + pending.winfo_height() + 12 <= page.user_cards[9].winfo_y()
        action = buttons(pending)["激活"].master
        assert detail.winfo_y() + detail.winfo_height() + 12 <= action.winfo_y()

    assert badge.cget("background") == THEMES["mint"][1]["soft"]
    gui.theme.set("经典深色")
    gui.apply_theme()
    assert badge.cget("background") == DARK["soft"]
    assert page.user_viewport.yview()[0] == 0
    name.event_generate("<MouseWheel>", delta=-120)
    root.update()
    assert page.user_viewport.yview()[0] > 0
    page.search.set("eXaMpLe")
    root.update()
    assert page.user_viewport.yview()[0] == 0
    assert page.user_summary.get() == "显示 3 / 126 个账号"
    page.status_filter.set("已禁用")
    root.update()
    assert page.user_list.pack_slaves() == [page.user_cards[9]]
    page.status_filter.set("待审批")
    root.update()
    assert page.user_list.pack_slaves() == [page.no_users]
    assert page.no_users.cget("text") == "没有符合筛选条件的账号。"
    page.search.set("")
    page.status_filter.set("全部状态")
    root.update()
    assert page.user_cards[14] is pending

    activate = buttons(pending)["激活"]
    with patch("admin_view.messagebox.askyesno", return_value=False) as confirm:
        activate.invoke()
        page.update_status(2, "disabled")
        page.update_status(3, "disabled")
        page.update_status(8, "active")
        assert confirm.call_count == 1 and LONG_NAME in confirm.call_args.args[1]
    assert not fixture.changes
    with patch("admin_view.messagebox.askyesno", return_value=True) as confirm:
        activate.invoke()
        wait(root, lambda: page.users[14]["status"] == "active" and not gui.busy)
        assert fixture.changes == [(14, "active")]
        assert LONG_NAME in confirm.call_args.args[1]
        buttons(page.user_cards[8])["禁用"].invoke()
        wait(root, lambda: page.users[8]["status"] == "disabled" and not gui.busy)
        assert fixture.changes[-1] == (8, "disabled")
        buttons(page.user_cards[9])["重新激活"].invoke()
        wait(root, lambda: page.users[9]["status"] == "active" and not gui.busy)
        assert fixture.changes[-1] == (9, "active")
    assert set(buttons(page.user_cards[14])) == {"禁用", "重置登录密码", "设置额度"}
    assert set(buttons(page.user_cards[8])) == {"重新激活", "重置登录密码", "设置额度"}
    assert [value.get() for value in page.account_overview] == ["0", "124", "1", "1"]

    with patch.object(gui, "show_page", wraps=gui.show_page) as navigate:
        page.reset_password(2)
        page.reset_password(3)
        assert not navigate.called
        buttons(page.user_cards[9])["重置登录密码"].invoke()
        navigate.assert_called_once_with("settings", section="passwords")
    assert gui.current_page == "settings" and gui.password_reset_target is None
    picker = next(descendants(gui.page_panel, ttk.Combobox))
    wait(root, lambda: not gui.busy and "Example.Disabled" in picker.get())
    assert picker.get().endswith("（9）")


class ModelConfigFixture:
    def __init__(self):
        self.model = {"status": "running", "model_id": "fixture", "model_name": "Fixture",
                      "total_context": 131072, "slots": 5, "effective_context_limit": 26368,
                      "output_token_limit": 4096, "gpu": None, "control_available": True, "detail": ""}
        self.requests = []
        self.requested = None
        self.fail_apply = False
        self.no_effect = False

    def request(self, method, path, body=None, **_options):
        self.requests.append((method, path, body))
        if (method, path) == ("GET", "/api/model"):
            return {"model": dict(self.model)}
        if (method, path) == ("POST", "/api/admin/model/preview"):
            self.requested = dict(body)
            return {"confirmation": "fixture-confirmation", "expires_in": 90,
                    "current": dict(self.model), "requested": dict(body),
                    "estimated_slot_context": body["total_context"] // body["slots"],
                    "gpu_risk": "隔离测试", "restart_required": True, "user_impact": "隔离测试"}
        if (method, path) == ("POST", "/api/admin/model/apply"):
            if self.fail_apply:
                raise AccountError("fixture apply failure", 503)
            if not self.no_effect:
                self.model.update(self.requested)
                self.model["effective_context_limit"] = self.requested["total_context"] // self.requested["slots"]
            return {"status": "applied", "model": dict(self.model)}
        raise AssertionError((method, path, body))

    def clear_open_webui_access(self):
        pass


def verify_model_configuration(root, gui):
    fixture = ModelConfigFixture()
    gui.account_client = fixture
    gui.account = {"id": 2, "username": "Current.Admin", "role": "admin", "status": "active"}
    gui.account_online = True
    parent = gui.frame(root)
    page = AdminPage(gui, parent)
    page.refresh_model()
    wait(root, lambda: page.has_model and not gui.busy)
    assert page.total.get() == "131072" and page.slots.get() == "5"

    page.total.set("65535")
    assert page.preview_button.instate(["disabled"])
    page.total.set("65536")
    page.slots.set("2")
    page.preview_button.invoke()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    assert fixture.requests[-1] == ("POST", "/api/admin/model/preview",
                                    {"total_context": 65536, "slots": 2})
    page.apply_preview()
    wait(root, lambda: not gui.busy and page.total.get() == "65536")
    assert fixture.requests[-2] == ("POST", "/api/admin/model/apply",
                                    {"confirmation": "fixture-confirmation"})
    assert "运行参数与目标一致" in page.model_note.get()

    fixture.fail_apply = True
    page.total.set("131072")
    page.slots.set("5")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    page.apply_preview()
    wait(root, lambda: not gui.busy and "fixture apply failure" in page.model_note.get())

    fixture.fail_apply = False
    fixture.no_effect = True
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    page.apply_preview()
    wait(root, lambda: not gui.busy and "尚未确认生效" in page.model_note.get())

    gui.account["role"] = "user"
    before = len(fixture.requests)
    page.preview()
    root.update()
    assert len(fixture.requests) == before
    assert build_admin_page(gui, parent) is None
    parent.destroy()


if __name__ == "__main__":
    with TemporaryDirectory(dir=TEST_ROOT, prefix="approval-cards-",
                            ignore_cleanup_errors=True) as data:
        window = tk.Tk()
        app = App(window, Path(data), smoke=True)
        try:
            verify(app, window)
        finally:
            app.close()
    with TemporaryDirectory(dir=TEST_ROOT, prefix="admin-model-") as data:
        window = tk.Tk()
        app = App(window, Path(data), smoke=True)
        try:
            verify_model_configuration(window, app)
        finally:
            app.close()
    print("PASS native approval cards: data, sizes, themes, scrolling, filters, counts, protected status actions and reset targeting")
    print("PASS admin model settings: typed payload, validation, apply/error/mismatch status, admin access gate")
