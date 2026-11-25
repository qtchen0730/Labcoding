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
from admin_view import AdminPage, CONTEXT_LABELS, CONTEXT_OPTIONS, account_time, build_admin_page
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
    gui.theme.set(THEMES["mint"][0])
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
    assert detail.cget("text").startswith(f"普通用户  ·  注册于 {account_time(CREATED)}")
    assert "执行任务 暂不可用" in detail.cget("text")
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

    page.update_task_counts({"users": {"14": 3, "9": 1}, "available_slots": 1})
    assert any("执行任务 3" in label.cget("text") for label in descendants(page.user_cards[14], tk.Label))
    assert any("执行任务 1" in label.cget("text") for label in descendants(page.user_cards[9], tk.Label))
    assert page.model_metrics[4].get() == "1"
    for identity in (14, 9, 2):
        card = page.user_cards[identity]
        assert card.cget("background") == gui.colors["field"]
        assert all(label.cget("background") == gui.colors["field"]
                   for label in descendants(card, tk.Label) if "执行任务" in label.cget("text"))
    page.update_task_counts({})
    assert any("执行任务 暂不可用" in label.cget("text") for label in descendants(page.user_cards[14], tk.Label))
    assert page.model_metrics[4].get() == "暂不可用"
    assert page.user_cards[14].cget("background") == gui.colors["field"]

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
        self.model["configuration_version"] = "f" * 64
        self.requests = []
        self.requested = None
        self.fail_apply = False
        self.no_effect = False
        self.partial = False

    def request(self, method, path, body=None, **_options):
        self.requests.append((method, path, body))
        if (method, path) == ("GET", "/api/admin/users"):
            return {"users": []}
        if (method, path) == ("GET", "/api/model"):
            return {"model": dict(self.model)}
        if (method, path) == ("POST", "/api/admin/model/preview"):
            self.requested = {key: body[key] for key in ("total_context", "slots")}
            return {"confirmation": "fixture-confirmation", "expires_in": 90,
                    "current": dict(self.model), "requested": dict(body),
                    "estimated_slot_context": body["total_context"] // body["slots"],
                    "gpu_risk": "隔离测试", "restart_required": True, "user_impact": "隔离测试",
                    "opencode": {"managed": True}}
        if (method, path) == ("POST", "/api/admin/model/apply"):
            if self.fail_apply:
                raise AccountError("fixture apply failure", 503)
            if not self.no_effect:
                self.model.update(self.requested)
                self.model["effective_context_limit"] = self.requested["total_context"] // self.requested["slots"]
            self.model["gpu"] = {"index": 0, "used_mib": 21000, "total_mib": 32000}
            self.model["gpu"].update(name="Fixture 5090", free_mib=10000, unaccounted_mib=1000,
                                     model_process_mib=20000, utilization_percent=65.6, sampled_at=1795538763.6)
            self.model["context_sync"] = {
                "saved_configuration": dict(self.requested),
                "backend": {"status": "synced", "effective_context_limit": self.model["effective_context_limit"]},
                "builtin": {"context_limit": self.model["effective_context_limit"]},
                "opencode": {"managed": True, "configured_context_limit": self.model["effective_context_limit"],
                             "status": "error" if self.partial else "pending_reload",
                             "detail": "同步失败，请重试。" if self.partial else "配置已同步；运行值未核实。"},
            }
            return {"status": "partially_applied" if self.partial else "applied", "model": dict(self.model)}
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
    fixture.model["control_available"] = False
    fixture.model["detail"] = "服务器未启用受控模型重配置。当前页面仅提供运行状态。"
    page.refresh_model()
    wait(root, lambda: page.has_model and not gui.busy)
    assert page.total.get() == "131072" and page.slots.get() == "5"
    assert page.preview_button.instate(["disabled"])
    before = len(fixture.requests)
    page.preview_button.invoke()
    page.preview()
    root.update()
    assert len(fixture.requests) == before
    assert "仅提供运行状态" in page.model_note.get()

    fixture.model["control_available"] = True
    fixture.model["detail"] = ""
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert not page.preview_button.instate(["disabled"])

    fixture.model.update(total_context=163840, slots=1, effective_context_limit=163840)
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.total.get() == "163840" and page.slots.get() == "1", "read live 160K rather than initialization default"

    page.total.set("65535")
    assert page.preview_button.instate(["disabled"])
    page.total.set("65536")
    page.slots.set("2")
    page.preview_button.invoke()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    assert fixture.requests[-1] == ("POST", "/api/admin/model/preview",
                                    {"total_context": 65536, "slots": 2, "expected_version": "f" * 64})
    page.apply_preview()
    wait(root, lambda: not gui.busy and page.total.get() == "65536")
    assert fixture.requests[-2] == ("POST", "/api/admin/model/apply",
                                    {"confirmation": "fixture-confirmation"})
    assert "运行参数与目标一致" in page.model_note.get()
    assert page.model_fields["backend_context"].get() == "32,768 Token"
    assert page.model_fields["independent_loaded"].get() == "尚未核验"
    assert page.model_fields["independent_state"].get() == "待重新加载"
    assert page.model_fields["gpu_memory"].get() == "21,000 / 32,000 MiB"
    assert "Fixture 5090" in page.model_fields["gpu_name"].get()
    assert page.model_metrics[5].get() == "65.6%" and abs(page.gpu_usage.get() - 65.625) < .001
    assert page.model_fields["gpu_process"].get() == "20,000 MiB"
    assert page.model_fields["gpu_difference"].get() == "1,000 MiB"
    assert "GPU" not in page.model_meta.get() and "配置版本" in page.advanced_text.get()
    fixture.model["running_tasks"] = 1
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.sync_state.get() == "服务器忙碌"
    fixture.model["running_tasks"] = 0

    fixture.fail_apply = True
    page.total.set("131072")
    page.slots.set("5")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    page.apply_preview()
    wait(root, lambda: not gui.busy and "fixture apply failure" in page.model_note.get())

    fixture.fail_apply = False
    fixture.no_effect = True
    # Failure now refreshes the actual parameters too; choose a new target again.
    page.total.set("131072")
    page.slots.set("5")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    page.apply_preview()
    wait(root, lambda: not gui.busy and "尚未确认生效" in page.model_note.get())

    fixture.no_effect = False
    fixture.partial = True
    assert CONTEXT_OPTIONS == ("32768", "65536", "131072", "147456", "163840", "196608")
    page.total.set("196608")
    page.slots.set("4")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    page.apply_preview()
    wait(root, lambda: not gui.busy and "部分完成" in page.model_note.get())
    assert page.model_fields["independent_state"].get() == "同步失败"
    fixture.partial = False

    saved = dict(fixture.model)
    page.total.set("163840")
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.total.get() == "163840", "read-only refresh must preserve the unsaved draft"
    assert page.sync_state.get() == "有未保存的更改"
    before = len(fixture.requests)
    default = next(button for button in descendants(page.model_viewport, ttk.Button)
                   if button.cget("text") == "恢复默认值")
    default.invoke()
    assert page.total.get() == "131072"
    assert len(fixture.requests) == before and fixture.model == saved
    page.resynchronize()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    assert page.review_requested == {"total_context": saved["total_context"], "slots": saved["slots"]}
    assert page.total.get() == "131072", "resynchronize must not submit or discard the draft"
    page.apply_preview()
    wait(root, lambda: not gui.busy)
    assert fixture.model["total_context"] == saved["total_context"]
    assert page.total.get() == "131072"

    # Older responses without an explicit capability must remain read-only too.
    page.total.set("131072")
    page.slots.set("5")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    fixture.model.pop("control_available")
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.preview_button.instate(["disabled"])
    assert page.review_ticket is None
    before = len(fixture.requests)
    page.preview()
    root.update()
    assert len(fixture.requests) == before
    assert "仅可查看运行状态" in page.model_note.get()

    fixture.model["control_available"] = True
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert not page.preview_button.instate(["disabled"])
    gui.account["role"] = "user"
    before = len(fixture.requests)
    page.preview()
    root.update()
    assert len(fixture.requests) == before
    assert build_admin_page(gui, parent) is None
    parent.destroy()

    gui.account["role"] = "admin"
    gui.show_page("admin")
    page = gui.page_controller
    page.tabs.select(page.model_tab)
    root.geometry("800x600")
    wait(root, lambda: page.has_model and not gui.busy)
    page.total.set("65536")
    page.slots.set("2")
    page.preview()
    wait(root, lambda: bool(page.review_ticket) and not gui.busy)
    root.update()
    viewport = page.model_viewport
    assert viewport.yview()[1] < 1, "small window must expose scrolling"
    confirm = next(button for button in descendants(page.review_card, ttk.Button) if button.cget("text") == "确认应用")
    confirm.focus_force()
    root.update()
    assert confirm.winfo_rooty() >= viewport.winfo_rooty()
    assert confirm.winfo_rooty() + confirm.winfo_height() <= viewport.winfo_rooty() + viewport.winfo_height()
    previous_scroll = viewport.yview()[0]
    page.review_card.event_generate("<MouseWheel>", delta=120)
    root.update()
    assert viewport.yview()[0] < previous_scroll
    # High-resolution wheel deltas accumulate even while a selector owns focus,
    # and scrolling must never change the selected configuration.
    previous_scroll = viewport.yview()[0]
    selector = next(descendants(viewport, ttk.Combobox))
    selected = selector.get()
    for _ in range(3):
        selector.event_generate("<MouseWheel>", delta=40)
    root.update()
    assert viewport.yview()[0] < previous_scroll
    assert selector.get() == selected

    verify_model_layout(root, gui, page, fixture)


def verify_model_layout(root, gui, page, fixture):
    """Native rendering checks against isolated telemetry; no model engine is started."""
    from types import SimpleNamespace

    page.cancel_preview()
    fixture.model.update(total_context=163840, slots=1, effective_context_limit=163840, available_slots=1)
    fixture.model["context_sync"].update(
        saved_configuration={"total_context": 163840, "slots": 1},
        backend={"status": "synced", "effective_context_limit": 163840, "ready_checked_at": CREATED},
        builtin={"context_limit": 163840},
        opencode={"status": "pending_reload", "managed": True, "configured_context_limit": 163840,
                  "effective_context_limit": None, "detail": "文件已同步，运行时尚未核验。"})
    page.total.set("163840")
    page.slots.set("1")
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert tuple(page.context_selector.cget("values")) == tuple(CONTEXT_LABELS.values())
    assert page.context_selector.get() == "160K（163840）"
    page.context_selector.current(2)
    page.context_selector.event_generate("<<ComboboxSelected>>")
    assert page.total.get() == "131072" and page.estimated_context.get() == "131,072 Token"
    page.refresh_model_button.invoke()
    wait(root, lambda: not gui.busy)
    assert page.context_selector.get() == "128K（131072）" and page.sync_state.get() == "有未保存的更改"

    assert page.model_fields["builtin_loaded"].get() == "未启动"
    with patch.object(gui, "engine", SimpleNamespace(process=SimpleNamespace(poll=lambda: None),
                      limits=(163840, 4096), loaded_limits={"context": 163840, "output": 4096})):
        for theme in THEMES:
            gui.theme.set(THEMES[theme][0])
            gui.apply_theme()
            page.refresh_model()
            wait(root, lambda: not gui.busy)
            assert page.model_fields["builtin_state"].get() == "已同步"
            assert page.model_field_labels["builtin_state"].cget("foreground") == gui.colors["success"]
            assert page.model_fields["independent_loaded"].get() == "尚未核验"
            assert page.model_field_labels["independent_state"].cget("foreground") == gui.colors["warning"]
            assert gui.style.lookup("Context.Horizontal.TProgressbar", "background") == gui.colors["accent"]
        gui.theme.set("Dark")
        gui.apply_theme()
        for width in (1440, 800):
            root.geometry(f"{width}x900")
            root.update()
            left, right = (page.model_field_labels[key] for key in ("builtin_target", "independent_target"))
            if width == 1440:
                assert abs(left.winfo_rooty() - right.winfo_rooty()) <= 1
                assert right.winfo_rootx() > left.winfo_rootx() + left.winfo_width()
            else:
                assert right.winfo_rooty() > left.winfo_rooty()
            for widget in (*descendants(page.model_viewport, ttk.Button),
                           *descendants(page.model_viewport, ttk.Combobox),
                           *page.model_field_labels.values()):
                x = widget.winfo_rootx() - page.model_viewport.winfo_rootx()
                if widget.winfo_ismapped():
                    assert x >= 0 and x + widget.winfo_width() <= page.model_viewport.winfo_width(), (width, widget)
                    if isinstance(widget, ttk.Button):
                        assert widget.winfo_width() >= widget.winfo_reqwidth()

    for status, text, tone in (("error", "同步失败", "error"), ("out_of_sync", "配置不一致", "error"),
                               ("unverified", "尚未核验", "muted"), ("unmanaged", "未纳管", "muted"),
                               ("synced", "尚未核验", "muted")):
        fixture.model["context_sync"]["opencode"]["status"] = status
        page.refresh_model()
        wait(root, lambda: not gui.busy)
        assert page.model_fields["independent_state"].get() == text
        assert page.model_field_labels["independent_state"].cget("foreground") == gui.colors[tone]
    fixture.model["context_sync"]["opencode"]["effective_context_limit"] = 163840
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.model_fields["independent_state"].get() == "已同步"
    fixture.model["context_sync"]["opencode"].update(status='ready',instance_count=0,effective_context_limit=None)
    page.refresh_model()
    wait(root,lambda:not gui.busy)
    assert page.model_fields['builtin_state'].get()=='配置已就绪，待引擎启动'
    assert page.model_fields['independent_state'].get()=='配置已就绪，待启动'
    assert page.model_fields['independent_loaded'].get()=='未启动'
    assert page.server_sync_state=='配置已就绪'
    with patch.object(gui,'reload_idle_model_engine') as synchronize:
        before=len(fixture.requests)
        page.refresh_model()
        wait(root,lambda:not gui.busy)
        synchronize.assert_not_called()
        assert all(method=='GET' for method,path,body in fixture.requests[before:])
    for text, tone in (("正在应用", "warning"), ("部分同步", "warning"), ("恢复失败", "error"),
                       ("状态未知", "muted"), ("已同步", "success")):
        page.sync_state.set(text)
        assert page.sync_state_label.cget("foreground") == gui.colors[tone]

    assert not page.advanced_card.winfo_manager()
    page.advanced_toggle.invoke()
    root.update()
    assert page.advanced_card.winfo_manager() == "pack" and "f" * 64 in page.advanced_text.get()
    page.copy_model_button.invoke()
    assert root.clipboard_get() == page.advanced_text.get()
    page.advanced_toggle.invoke()
    root.update()
    assert not page.advanced_card.winfo_manager()
    fixture.model["gpu"] = None
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.model_fields["gpu_memory"].get() == page.model_metrics[5].get() == "未知"
    assert not page.gpu_bar.winfo_manager()
    fixture.model["gpu"] = {"index": 0, "name": "Fixture 5090", "used_mib": 12000, "total_mib": 32000,
                            "free_mib": 19000, "unaccounted_mib": 1000, "sampled_at": CREATED + 60}
    page.refresh_model()
    wait(root, lambda: not gui.busy)
    assert page.gpu_bar.winfo_manager() == "pack" and page.gpu_usage.get() == 37.5
    assert page.model_fields["gpu_sampled"].get().endswith(":00")


if __name__ == "__main__":
    # Match the application's native Windows entry point for geometry checks.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
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
    print("PASS model layout: six presets, responsive fields, seven themes, sync semantics, GPU unknown/refresh, details/copy")
