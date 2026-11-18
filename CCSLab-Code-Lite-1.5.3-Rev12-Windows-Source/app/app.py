"""CCSLab Code Lite desktop application."""

import argparse
from collections import deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic, time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from uuid import uuid4

from changes import Changes
from account_client import (
    ACCOUNT_STATUS, DEFAULT_SERVER, LEGACY_DEFAULT_SERVER, PASSWORD_RANGE,
    AccountClient, AccountError, normalize_server, preferred_account_server, valid_account_password,
)
from account_diagnostics import run_diagnostics, unavailable_report
from account_transport import CONNECTION_MODES
from access_view import build_access_page, prompt_role_switch
from admin_view import build_admin_page
from activity import ActivityPanel
from context_usage import ContextMeter
from direct_connection import BASE_URL, DirectConnection, DirectError, MODEL_ID, supports_webui_sign_in
from direct_diagnostics import check_read_tool
from direct_view import build_direct_page
from markdown_view import configure_markdown, insert_markdown
from new_project import show_new_project
from settings_view import build_settings_page
from themes import DARK, LIGHT, THEMES, theme_key, migrate_default_theme
from ui_icons import ActionIcon, gear_image, apply_window_icon
from saved_login import SavedLogin
from quota_view import balance_text, blocked_message
from approval_policy import SessionGrants, session_scope, replacement_details_complete


FONT = "Microsoft YaHei UI"


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def valid_session_title(title):
    return (isinstance(title, str) and 1 <= len(title.strip()) <= 100
            and not any(ord(char) < 32 or ord(char) == 127 for char in title))


class App:
    def __init__(self, root, data_dir=None, smoke=False):
        self.root = root
        self.data_root = data_dir or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CCSLabCodeLite" / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.state = self.data_root
        self.prefs_path = self.data_root / "preferences.json"
        self.prefs = read_json(self.prefs_path, {})
        theme_migrated = migrate_default_theme(self.prefs)
        self.saved_login = SavedLogin(self.data_root)
        self.saved_login_suppressed = False
        self.direct_requested = self.prefs.get("connection_mode") == "direct"
        self.connection_mode = "account"
        server = preferred_account_server(self.prefs)
        try:
            server = normalize_server(server)
        except AccountError:
            pass
        proxy = self.prefs.get("account_proxy_mode")
        self.account_connection = (server, proxy if proxy in CONNECTION_MODES else "auto")
        self.gateway_transport = None
        self.gateway_client_lock = Lock()
        self.gateway_check_number = 0
        self.gateway_inflight = None
        self.gateway_report = None
        try:
            self.direct_connection = DirectConnection(self.data_root, base_url=self.prefs.get("direct_address", BASE_URL))
        except DirectError:
            self.direct_connection = DirectConnection(self.data_root)
        self.direct_restore_after_unlock = not smoke
        self.direct_ready = False
        self.direct_identity = ""
        self.account_client = None
        self.account = None
        self.account_online = False
        self.account_polling = False
        self.pending_locked = False
        self.quota_locked = False
        self.account_generation = 0
        self.account_ca_path = None
        self.model_info = {}
        self.model_checked = 0
        self.ca_path = None
        self.engine_limits = None
        self.api_base = ""
        self.token = ""
        self.engine = None
        self.project = ""
        self.changes = None
        self.change_rows = []
        self.visible_changes = []
        self.sessions = []
        self.visible_sessions = []
        self.session = None
        self.history_path = None
        self.history = {}
        self.session_titles = {}
        self.drafts = {}
        self.events = Queue()
        self.approval_queue = deque()
        self.active_approval = None
        self.approval_tickets = {}
        self.approval_lock = Lock()
        self.session_grants = SessionGrants()
        self.active_approval_scope = None
        self.timers = {}
        self.busy = ""
        self.running = False
        self.closing = False
        self.stop_requested = Event()
        self.live_text = False
        self.live_markdown = ""
        self.render_later = None
        self.status = tk.StringVar(value="请登录")
        self.quota_text = tk.StringVar()
        self.direct_status = tk.StringVar(value="待权限验证")
        self.direct_feedback = tk.StringVar()
        self.direct_address = tk.StringVar(value=self.direct_connection.model_base)
        self.gateway_status = tk.StringVar(value="尚未检查")
        self.gateway_last_success = tk.StringVar(value="—")
        self.gateway_error = tk.StringVar()
        self.gateway_recent_error = tk.StringVar(value="—")
        self.gateway_report_status = tk.StringVar()
        self.account_title = tk.StringVar()
        self.account_detail = tk.StringVar()
        self.project_label = tk.StringVar(value="未打开项目")
        self.branch = tk.StringVar(value="—")
        self.result_status = tk.StringVar(value="选择文件查看差异")
        self.change_filter = tk.StringVar()
        self.session_search = tk.StringVar()
        saved_mode = self.prefs.get("default_mode")
        self.default_mode = tk.StringVar(value=saved_mode if saved_mode in {"执行", "方案"} else "执行")
        self.mode = tk.StringVar(value=self.default_mode.get())
        self.chat_size = tk.StringVar(value="大" if self.prefs.get("chat_size") == "大" else "标准")
        self.results_preference = tk.BooleanVar(value=self.prefs.get("show_results", True))
        self.results_visible = self.results_preference.get()
        self.task_state = tk.StringVar()
        self.task_elapsed = tk.StringVar()
        self.task_started = None
        self.diff_text = ""
        self.strict = tk.BooleanVar(value=self.prefs.get("strict", False))
        self.theme = tk.StringVar(value=THEMES[theme_key(self.prefs.get("theme"))][0])
        self.themed = []
        self.style = ttk.Style()
        self.style.theme_use("clam")
        root.title("CCSLab Labcoding")
        apply_window_icon(root)
        root.geometry(f"{min(1748, root.winfo_screenwidth() - 70)}x{min(1228, root.winfo_screenheight() - 130)}")
        root.minsize(950, 580)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.apply_theme()
        self.show_page("access", force=True)
        if theme_migrated:
            try:
                self.save_prefs()
            except OSError:
                self.page_controller.feedback.set("无法保存主题设置，请检查本机数据目录权限。")
        if self.direct_requested:
            self.show_direct_login_hint()
        self.update_actions()
        self.schedule("pump", 30, self.pump)
        self.schedule("timer", 200, self.update_timer)
        if not smoke:
            self.schedule("legacy", 100, self.remove_legacy_key)
            self.schedule("connection", 3500, self.monitor_account)
            self.schedule("gateway_check", 100, self.check_account_connection)
            self.schedule("saved_login", 250, self.restore_saved_login)

    def schedule(self, name, delay, callback):
        previous = self.timers.pop(name, None)
        if previous:
            self.root.after_cancel(previous)

        def run():
            self.timers.pop(name, None)
            callback()

        self.timers[name] = self.root.after(delay, run)

    def skin(self, widget, bg="panel", fg="text", **extra):
        self.themed.append((widget, bg, fg, extra))
        return widget

    def label(self, parent, text="", *, variable=None, muted=False, bg="panel", size=10, bold=False,
              color=None):
        color = color or ("muted" if muted else "text")
        return self.skin(tk.Label(
            parent, text=text, textvariable=variable, anchor="w", justify="left",
            font=(FONT, size, "bold" if bold else "normal")), bg, color)

    def frame(self, parent, bg="panel", **kwargs):
        return self.skin(tk.Frame(parent, **kwargs), bg, None)

    def button(self, parent, text, command, style="TButton"):
        return ttk.Button(parent, text=text, command=command, style=style)

    def text_panel(self, parent, *, wrap="word", mono=False, editable=False, height=6,
                   background="field", text_size=10):
        holder = self.frame(parent, background)
        box = tk.Text(holder, wrap=wrap, undo=editable, height=height, borderwidth=0,
                      highlightthickness=0, padx=12, pady=9,
                      font=("Consolas" if mono else FONT, text_size), spacing1=2, spacing3=3)
        self.skin(box, background, "text", insertbackground="text", selectbackground="border")
        vertical = ttk.Scrollbar(holder, command=box.yview)
        vertical.pack(side="right", fill="y")
        box.configure(yscrollcommand=vertical.set)
        if wrap == "none":
            horizontal = ttk.Scrollbar(holder, orient="horizontal", command=box.xview)
            horizontal.pack(side="bottom", fill="x")
            box.configure(xscrollcommand=horizontal.set)
        box.pack(side="left", fill="both", expand=True)
        if not editable:
            box.configure(state="disabled")
        return holder, box

    def build_approval_panel(self, parent):
        self.approval_panel = self.frame(parent, "soft")
        self.approval_heading = self.label(self.approval_panel, "", bg="soft", size=12,
                                           bold=True, color="heading")
        self.approval_heading.pack(anchor="w", padx=12, pady=(8, 2))
        self.approval_hint = self.label(self.approval_panel, "", bg="soft", muted=True, size=9)
        self.approval_hint.pack(fill="x", padx=12, pady=(0, 5))
        self.approval_detail_holder, self.approval_detail = self.text_panel(
            self.approval_panel, height=4, mono=True, background="field", text_size=9)
        self.approval_detail_holder.pack(fill="x", padx=10)
        actions = self.frame(self.approval_panel, "soft")
        actions.pack(fill="x", padx=10, pady=(6, 8))
        self.approval_reject_button = self.button(
            actions, "拒绝", lambda: self.finish_approval(False))
        self.approval_reject_button.pack(side="right")
        self.approval_session_button = self.button(
            actions, "允许本次会话", lambda: self.finish_approval(True, session=True))
        self.approval_session_button.pack(side="right", padx=(0, 7))
        self.approval_once_button = self.button(
            actions, "仅允许本次", lambda: self.finish_approval(True), "Accent.TButton")
        self.approval_once_button.pack(side="right", padx=(0, 7))
        self.approval_restore_focus = None
        self.approval_restore_after = None
        self.approval_action_locked = False
        self.approval_panel.pack_forget()
        self.approval_key_latched = False
        self.approval_key_bindings = None

    def current_approval_scope(self):
        account = self.account or ({"id": self.direct_identity} if self.connection_mode == "direct" else None)
        return session_scope(account, self.project, self.session)

    def update_session_grant_ui(self):
        if not hasattr(self, "revoke_approval_button"):
            return
        if self.session_grants.has_scope(self.current_approval_scope()):
            if not self.revoke_approval_button.winfo_manager():
                self.revoke_approval_button.pack(anchor="e", padx=18, pady=(0, 4),
                                                  before=self.context_meter.panel)
        else:
            self.revoke_approval_button.pack_forget()

    def revoke_session_approvals(self):
        scope = self.current_approval_scope()
        if scope:
            self.session_grants.clear()
            self.update_session_grant_ui()
            self.status.set("已撤销当前会话授权")

    @staticmethod
    def approval_title(request):
        return {"command": "运行 Windows 命令", "read": "读取文件",
                "write": "修改文件", "delete": "删除文件"}.get(
                    request.get("kind"), f"其他操作：{request.get('kind') or '未知类型'}")

    def approval_enter(self, _event=None):
        if _event is not None and _event.state & 0x4:
            return None
        if not self.active_approval:
            return None
        if self.approval_key_latched:
            return "break"
        self.approval_key_latched = True
        self.finish_approval(True)
        return "break"

    def approval_enter_released(self, _event=None):
        self.approval_key_latched = False
        self.clear_approval_key_bindings_if_idle()
        return "break"

    def approval_escape(self, _event=None):
        if not self.active_approval:
            return None
        if self.approval_key_latched:
            return "break"
        self.approval_key_latched = True
        self.finish_approval(False)
        return "break"

    def clear_approval_key_bindings_if_idle(self):
        if (self.active_approval or self.approval_queue or getattr(self, "approval_key_latched", False)
                or getattr(self, "approval_key_bindings", None) is None):
            return
        for sequence, binding_id in zip(("<KeyPress-Return>", "<KeyRelease-Return>",
                                         "<KeyPress-Escape>", "<KeyRelease-Escape>"),
                                        self.approval_key_bindings):
            if binding_id:
                self.root._unbind(("bind", "all", sequence), binding_id)
        self.approval_key_bindings = None

    def enqueue_approval(self, ticket):
        if (ticket["completed"].is_set() or self.closing or self.stop_requested.is_set()
                or ticket["scope"] != self.active_approval_scope):
            self.cancel_approval_ticket(ticket)
            return
        self.approval_queue.append(ticket)
        self.show_next_approval()

    @staticmethod
    def approval_has_operation_detail(request):
        if request.get("kind") == "write" and request.get("title") == "替换文件内容":
            return replacement_details_complete(request)
        return bool(
            (request.get("kind") == "command" and request.get("command") and request.get("path"))
            or (request.get("kind") in {"read", "write", "delete"} and request.get("path"))
            or request.get("display")
        )

    def show_next_approval(self):
        if self.active_approval or self.closing:
            return
        if self.approval_action_locked:
            self.schedule("approval_next", 250, self.show_next_approval)
            return
        while self.approval_queue:
            ticket = self.approval_queue.popleft()
            if ticket["completed"].is_set():
                continue
            if ticket["scope"] != self.active_approval_scope:
                self.cancel_approval_ticket(ticket)
                continue
            self.active_approval = ticket
            break
        if not self.active_approval:
            self.approval_panel.pack_forget()
            return
        ticket = self.active_approval
        request = ticket["request"]
        if self.approval_restore_focus is None:
            self.approval_restore_focus = self.root.focus_get()
        self.approval_heading.configure(text=self.approval_title(request))
        if request.get("kind") == "command":
            hint = ("命令及其子进程拥有当前 Windows 用户权限。请核对工作目录、完整命令和参数。"
                    "命令不会自动获得会话授权。")
        else:
            hint = "请检查操作类型、目标路径和完整详情。未知类型与删除操作始终需要逐次审批。"
        self.approval_hint.configure(text=hint)
        details = []
        if request.get("path"):
            if request.get("kind") == "command":
                details.append(f"工作目录：{request['path']}")
            elif ticket["scope"]:
                target = Path(ticket["scope"][1]) / request["path"]
                details.append(f"目标路径：{target}")
            else:
                details.append(f"目标路径：{request['path']}")
        if request.get("command"):
            details.append(f"完整命令：\n{request['command']}")
        if request.get("detail"):
            details.append(str(request["detail"]))
        elif request.get("display"):
            details.append(str(request["display"]))
        has_operation_detail = self.approval_has_operation_detail(request)
        if not has_operation_detail:
            details = ["无法获取完整操作详情"]
        preview = self.redact("\n\n".join(details))
        self.approval_detail.configure(state="normal")
        self.approval_detail.delete("1.0", "end")
        self.approval_detail.insert("1.0", preview)
        self.approval_detail.configure(state="disabled")
        grantable = self.session_grants.allowable(request, ticket["scope"])
        if not ticket["scope"]:
            unsupported_reason = "当前会话范围不可用，无法授权本次会话"
        elif request.get("kind") == "command":
            unsupported_reason = ("只读 where.exe 查询可在本会话授权；其他命令需逐次审批" if grantable
                                  else "仅 where.exe <name>.exe 查询可会话授权；其他命令需逐次审批")
        else:
            unsupported_reason = "当前操作仅支持单次授权"
        self.approval_hint.configure(text=unsupported_reason if request.get("kind") == "command" or not ticket["scope"]
                                      else ("" if grantable else unsupported_reason))
        if not has_operation_detail:
            self.approval_hint.configure(text="无法获取完整操作详情，已禁用批准")
        control_state = "disabled" if self.approval_action_locked or not has_operation_detail else "normal"
        self.approval_once_button.configure(state=control_state, command=lambda ticket_id=ticket["id"]:
                                            self.finish_approval(True, ticket_id=ticket_id))
        self.approval_session_button.configure(
            state="normal" if grantable and not self.approval_action_locked and has_operation_detail else "disabled",
            command=lambda ticket_id=ticket["id"]:
                                                self.finish_approval(True, session=True, ticket_id=ticket_id))
        self.approval_reject_button.configure(state="disabled" if self.approval_action_locked else "normal",
                                               command=lambda ticket_id=ticket["id"]:
                                               self.finish_approval(False, ticket_id=ticket_id))
        self.task_state.set("等待审批")
        self.approval_panel.pack(fill="x", padx=12, pady=(4, 0), before=self.composer_separator)
        if self.approval_key_bindings is None:
            self.approval_key_bindings = (
                self.root.bind_all("<KeyPress-Return>", self.approval_enter, add="+"),
                self.root.bind_all("<KeyRelease-Return>", self.approval_enter_released, add="+"),
                self.root.bind_all("<KeyPress-Escape>", self.approval_escape, add="+"),
                self.root.bind_all("<KeyRelease-Escape>", self.approval_enter_released, add="+"),
            )
        self.root.after_idle(lambda ticket_id=ticket["id"]: self.focus_approval(ticket_id))

    def focus_approval(self, ticket_id):
        ticket = self.active_approval
        if (ticket and ticket["id"] == ticket_id and self.approval_panel.winfo_exists()
                and self.approval_panel.winfo_ismapped()):
            self.root.focus_force()
            self.approval_once_button.focus_set()

    def finish_approval(self, allow=False, *, session=False, ticket_id=None):
        ticket = self.active_approval
        if (not ticket or self.approval_action_locked
                or (ticket_id is not None and ticket["id"] != ticket_id)
                or (allow and not self.approval_has_operation_detail(ticket["request"]))):
            return
        with ticket["guard"]:
            if ticket["completed"].is_set():
                return
            if session and allow and not self.session_grants.allow(ticket["scope"], ticket["request"]):
                return
            ticket["result"]["allow"] = bool(allow)
            ticket["completed"].set()
        with self.approval_lock:
            self.approval_tickets.pop(ticket["id"], None)
        self.approval_action_locked = True
        self.schedule("approval_unlock", 250, self.unlock_approval_controls)
        self.active_approval = None
        self.approval_panel.pack_forget()
        self.clear_approval_key_bindings_if_idle()
        self.update_session_grant_ui()
        if self.running and not self.stop_requested.is_set() and not self.closing:
            self.task_state.set("任务执行中")
        if self.approval_queue:
            self.schedule("approval_next", 250, self.show_next_approval)
        if not self.active_approval:
            self.restore_approval_focus()

    def unlock_approval_controls(self):
        self.approval_action_locked = False
        if not self.active_approval:
            self.show_next_approval()
            return
        grantable = self.session_grants.allowable(
            self.active_approval["request"], self.active_approval["scope"])
        self.approval_once_button.configure(state="normal" if self.approval_has_operation_detail(
            self.active_approval["request"]) else "disabled")
        self.approval_reject_button.configure(state="normal")
        self.approval_session_button.configure(state="normal" if grantable else "disabled")

    def cancel_approval_ticket(self, ticket):
        with ticket["guard"]:
            if ticket["completed"].is_set():
                return
            ticket["result"]["allow"] = False
            ticket["completed"].set()
        with self.approval_lock:
            self.approval_tickets.pop(ticket["id"], None)
        if self.active_approval is ticket:
            self.active_approval = None
            self.approval_panel.pack_forget()
        self.show_next_approval()

    def cancel_all_approvals(self):
        had_active = self.active_approval is not None
        with self.approval_lock:
            tickets = list(self.approval_tickets.values())
            self.approval_tickets.clear()
        for ticket in tickets:
            with ticket["guard"]:
                ticket["result"]["allow"] = False
                ticket["completed"].set()
        self.approval_queue.clear()
        self.active_approval = None
        if hasattr(self, "approval_panel"):
            self.approval_panel.pack_forget()
        if had_active and not self.closing:
            self.restore_approval_focus()

    def restore_approval_focus(self):
        target = self.approval_restore_focus
        self.approval_restore_focus = None
        def restore():
            self.approval_restore_after = None
            try:
                if target and target.winfo_exists() and str(target.cget("state")) != "disabled":
                    target.focus_set()
                elif self.chat.winfo_exists():
                    self.chat.focus_set()
            except (tk.TclError, AttributeError):
                pass
        if not self.closing:
            if self.approval_restore_after:
                self.root.after_cancel(self.approval_restore_after)
            self.approval_restore_after = self.root.after_idle(restore)

    def list_widget(self, parent, height, background="field"):
        result = tk.Listbox(parent, height=height, activestyle="none", borderwidth=0,
                            font=(FONT, 10), highlightthickness=0, exportselection=False)
        return self.skin(result, background, "text", selectbackground="soft", selectforeground="heading")

    def scroll_list(self, parent, height, background="field"):
        holder = self.frame(parent, background)
        listing = self.list_widget(holder, height, background)
        scroll = ttk.Scrollbar(holder, command=listing.yview)
        scroll.pack(side="right", fill="y")
        listing.configure(yscrollcommand=scroll.set)
        listing.pack(side="left", fill="both", expand=True)
        return holder, listing

    def build(self):
        self.skin(self.root, "bg", None)
        bar = self.frame(self.root)
        bar.pack(fill="x")
        self.label(bar, "CCSLab Labcoding", size=15, bold=True).pack(side="left", padx=(17, 19), pady=13)
        self.workspace_button = self.button(bar, "工作台", lambda: self.show_page("workspace"), "Nav.TButton")
        self.workspace_button.pack(side="left", padx=(0, 4))
        footer = self.frame(self.root)
        footer.pack(side="bottom", fill="x")
        self.settings_button = self.button(footer, "设置", lambda: self.show_page("settings"), "Nav.TButton")
        self.settings_button.configure(compound="right", width=0)
        self.settings_button.pack(side="left", padx=(13, 12), pady=8)
        self.settings_icon_palette = None
        self.quota_label = self.label(footer, variable=self.quota_text, color="heading", size=9)
        self.admin_button = self.button(bar, "管理控制台", lambda: self.show_page("admin"), "Nav.TButton")
        self.connection_button = self.button(bar, "登录 / 注册", self.account_action, "Nav.TButton")
        self.connection_button.pack(side="right", padx=(7, 15))
        self.role_button = self.button(bar, "切换角色", self.change_role, "Nav.TButton")
        identity = self.frame(bar)
        identity.pack(side="right", padx=(6, 4))
        self.theme_icon = ActionIcon(self, identity, "sun", self.cycle_theme)
        self.theme_icon.pack(side="left")
        self.status_label = self.label(identity, variable=self.status, muted=True)
        self.status_label.pack(side="left", padx=(4, 0))
        ttk.Separator(self.root).pack(fill="x")

        self.page_host = self.frame(self.root, "bg")
        self.page_host.pack(fill="both", expand=True)
        panes = self.workspace_view = tk.PanedWindow(self.page_host, orient="horizontal", bd=0, sashwidth=6, sashpad=0)
        self.current_page = None
        self.page_panel = None
        self.page_controller = None
        self.settings_admin = False
        self.skin(panes, "bg", None)
        side = self.frame(panes, "side")
        center = self.frame(panes)
        right = self.results_pane = self.frame(panes)
        panes.add(side, width=421, minsize=182, stretch="never")
        panes.add(center, minsize=365, stretch="always")
        if self.results_visible:
            panes.add(right, width=372, minsize=278, stretch="never")
        project_head = self.frame(side, "side")
        project_head.pack(fill="x", padx=14, pady=(15, 1))
        self.label(project_head, "项目", bg="side", size=13, bold=True).pack(side="left")
        project_actions = self.frame(side, "side")
        project_actions.pack(fill="x", padx=12, pady=(10, 9))
        self.new_project_button = self.button(project_actions, "＋ 新建项目", self.new_project)
        self.new_project_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.open_button = self.button(project_actions, "打开", self.choose_project)
        self.open_button.pack(side="right")
        recent_head = self.frame(side, "side")
        recent_head.pack(fill="x", padx=13, pady=(6, 5))
        self.label(recent_head, "最近项目", bold=True, bg="side", muted=True).pack(side="left")
        self.remove_recent_button = self.button(recent_head, "删除", self.remove_recent)
        self.remove_recent_button.pack(side="right")
        recent_panel, self.recent_list = self.scroll_list(side, 5, "side")
        recent_panel.pack(fill="x", padx=12)
        self.recent_list.bind("<Double-Button-1>", self.open_recent)
        self.recent_list.bind("<Return>", self.open_recent)
        self.recent_list.bind("<<ListboxSelect>>", lambda _event: self.update_delete_actions())
        self.new_button = self.button(side, "新建会话", self.new_session)
        self.new_button.pack(fill="x", padx=13, pady=(16, 8))
        self.label(side, "搜索会话", bold=True, bg="side", muted=True).pack(fill="x", padx=13, pady=(4, 5))
        ttk.Entry(side, textvariable=self.session_search).pack(fill="x", padx=13, pady=(0, 6))
        session_head = self.frame(side, "side")
        session_head.pack(fill="x", padx=13, pady=(4, 5))
        self.label(session_head, "历史会话", bold=True, bg="side", muted=True).pack(side="left")
        self.delete_session_button = self.button(session_head, "删除", self.delete_session)
        self.delete_session_button.pack(side="right")
        session_panel, self.session_list = self.scroll_list(side, 7, "side")
        session_panel.pack(fill="both", expand=True, padx=12, pady=(0, 13))
        self.session_list.bind("<<ListboxSelect>>", self.select_session)
        self.session_list.bind("<Button-3>", self.show_session_menu)
        self.session_list.bind("<F2>", lambda _event: self.rename_session())
        self.session_menu = self.skin(tk.Menu(self.root, tearoff=False, font=(FONT, 10)),
                                      "panel", "text", activebackground="soft", activeforeground="heading",
                                      disabledforeground="muted")
        self.session_menu.add_command(label="重命名")
        self.session_search.trace_add("write", lambda *_args: self.render_session_list())
        self.refresh_recents()

        conversation_head = self.frame(center)
        conversation_head.pack(fill="x", padx=18, pady=(15, 8))
        self.label(conversation_head, variable=self.project_label, size=14, bold=True).pack(side="left")
        self.results_button = self.button(conversation_head, "", self.toggle_results, "Nav.TButton")
        self.results_button.pack(side="right", padx=(8, 0))
        self.results_button.configure(text="隐藏结果" if self.results_visible else "显示结果")
        self.revoke_approval_button = self.button(
            center, "撤销会话授权", self.revoke_session_approvals, "Nav.TButton")
        self.revoke_approval_button.pack_forget()
        self.label(conversation_head, variable=self.task_elapsed, muted=True).pack(side="right")
        self.label(conversation_head, variable=self.task_state, color="heading").pack(side="right", padx=(0, 7))
        self.pending_banner = self.frame(center, "soft")
        self.label(self.pending_banner, "账号待激活", bg="soft", bold=True, color="heading").pack(
            anchor="w", padx=13, pady=(8, 1))
        guidance = self.label(self.pending_banner, variable=self.account_detail, bg="soft", muted=True, size=9)
        guidance.pack(anchor="w", padx=13, pady=(0, 7))
        guidance.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(event.width, 90)))
        self.context_meter = ContextMeter(self, center)
        ttk.Separator(center).pack(fill="x", padx=16)
        conversation, self.chat = self.text_panel(center, height=1, background="panel", text_size=11)
        conversation.pack(fill="both", expand=True, padx=10)
        self.show_welcome()
        self.build_approval_panel(center)
        self.composer_separator = ttk.Separator(center)
        self.composer_separator.pack(fill="x", padx=16, pady=(6, 0))
        self.label(center, "输入开发任务", bold=True).pack(fill="x", padx=16, pady=(10, 5))
        composer, self.input = self.text_panel(center, editable=True, height=4)
        self.quota_banner = self.label(self.input, "", color="remove", bg="field")
        self.quota_banner.configure(wraplength=400)
        composer.pack(fill="x", padx=14)
        self.input.bind("<Control-Return>", self.submit_shortcut)
        actions = self.frame(center)
        actions.pack(fill="x", padx=14, pady=(7, 12))
        self.send_button = self.button(actions, "发送任务", self.submit, "Accent.TButton")
        self.send_button.pack(side="right")
        self.stop_button = self.button(actions, "停止", self.stop)
        self.stop_button.pack(side="right", padx=8)
        self.mode_picker = ttk.Combobox(actions, textvariable=self.mode, values=("执行", "方案"),
                                        state="readonly", width=6)
        self.mode_picker.pack(side="left")
        self.mode_hint = tk.StringVar(value="允许编辑项目 · Ctrl+Enter")
        self.label(actions, variable=self.mode_hint, muted=True).pack(side="left", padx=(8, 0))
        self.mode.trace_add("write", self.mode_changed)

        rs = self.frame(right)
        rs.pack(fill="x", padx=13, pady=(12, 7))
        self.label(rs, "项目结果", size=13, bold=True).pack(side="left")
        self.refresh_button = self.button(rs, "刷新", self.refresh_changes)
        self.refresh_button.pack(side="right")
        self.label(right, variable=self.branch, muted=True).pack(fill="x", padx=13, pady=(0, 8))
        tabs = self.result_tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True, padx=13, pady=(0, 13))
        changes_tab = self.frame(tabs)
        tools_tab = self.frame(tabs)
        tabs.add(changes_tab, text="文件变更")
        tabs.add(tools_tab, text="执行记录")
        tabs.select(tools_tab)
        search = self.frame(changes_tab)
        search.pack(fill="x", pady=(7, 0))
        self.label(search, "筛选", muted=True, size=9).pack(side="left", padx=(0, 6))
        ttk.Entry(search, textvariable=self.change_filter).pack(side="left", fill="x", expand=True)
        self.change_filter.trace_add("write", lambda *_args: self.render_change_list())
        changes_panel, self.change_list = self.scroll_list(changes_tab, 7)
        changes_panel.pack(fill="x", pady=(7, 5))
        self.change_list.bind("<<ListboxSelect>>", self.select_change)
        self.label(changes_tab, variable=self.result_status, muted=True).pack(fill="x", pady=(2, 5))
        diff_panel, self.diff = self.text_panel(changes_tab, wrap="none", mono=True)
        diff_panel.pack(fill="both", expand=True)
        diff_actions = self.frame(changes_tab)
        diff_actions.pack(fill="x", pady=(7, 0))
        self.diff_expand = self.button(diff_actions, "展开", self.expand_diff)
        self.diff_expand.pack(side="left")
        self.diff_copy = self.button(diff_actions, "复制 Diff", self.copy_diff)
        self.diff_copy.pack(side="left", padx=5)
        self.activity = ActivityPanel(self, tools_tab)
        self.root.bind("<Control-o>", lambda _event: self.choose_project())
        self.root.bind("<Control-Shift-N>", lambda _event: self.new_project())
        self.root.bind("<Control-n>", lambda _event: self.new_session())
        self.root.bind("<Control-comma>", lambda _event: self.show_page("settings"))

    def show_page(self, page, *, force=False, section="general"):
        if self.closing or not force and self.busy:
            return
        if page == "workspace" and not (self.can_open_project or self.account_pending):
            page = "direct" if self.connection_mode == "direct" else ("status" if self.account else "access")
        if page == "admin" and not self.is_admin:
            return
        if page == self.current_page and not (force and page == "direct"):
            return
        if self.page_panel is self.workspace_view:
            self.workspace_view.pack_forget()
        elif self.page_panel is not None:
            self.page_panel.destroy()
        self.current_page = page
        self.page_controller = None
        if page == "workspace":
            self.page_panel = self.workspace_view
            self.workspace_view.pack(fill="both", expand=True)
        else:
            self.page_panel = self.frame(self.page_host)
            self.page_panel.pack(fill="both", expand=True)
            if page == "settings":
                self.settings_admin = self.is_admin
                build_settings_page(self, self.page_panel, section)
            elif page == "direct":
                build_direct_page(self, self.page_panel)
            elif page == "access":
                self.page_controller = build_access_page(self, self.page_panel)
            elif page == "admin":
                self.page_controller = build_admin_page(self, self.page_panel)
            else:
                self.build_account_status(self.page_panel)
        self.apply_theme()
        self.update_actions()

    def build_account_status(self, parent):
        body = self.frame(parent, "field")
        body.place(relx=0.5, rely=0.36, anchor="center", width=510)
        self.label(body, "账号状态", bg="field", size=10, color="heading").pack(
            fill="x", padx=23, pady=(20, 6))
        self.label(body, variable=self.account_title, bg="field", size=21, bold=True).pack(
            fill="x", padx=23)
        self.label(body, variable=self.account_detail, bg="field", muted=True).pack(
            fill="x", padx=23, pady=(8, 12))
        self.button(body, "刷新状态", self.poll_account).pack(anchor="w", padx=23, pady=(0, 21))

    def toggle_results(self):
        self.set_results_visible(not self.results_visible)

    def set_results_visible(self, visible):
        if visible != self.results_visible:
            if visible:
                self.workspace_view.add(self.results_pane, width=372, minsize=278, stretch="never")
            else:
                self.workspace_view.forget(self.results_pane)
            self.results_visible = visible
        self.results_button.configure(text="隐藏结果" if visible else "显示结果")

    def apply_theme(self):
        c = self.colors = THEMES[theme_key(self.theme.get())][1]
        self.style.configure(".", background=c["panel"], foreground=c["text"], font=(FONT, 10))
        self.style.configure("TButton", padding=(10, 6), background=c["field"],
                             bordercolor=c["border"], focusthickness=1)
        self.style.map("TButton", background=[("active", c["border"])],
                       foreground=[("disabled", c["muted"])])
        self.style.configure("Accent.TButton", background=c["accent"], foreground=c["ink"])
        self.style.map("Accent.TButton",
                       background=[("disabled", c["field"]), ("active", c["accent"])],
                       foreground=[("disabled", c["muted"]), ("active", c["ink"])])
        self.style.configure("Nav.TButton", anchor="w", relief="flat", background=c["panel"],
                             foreground=c["muted"], borderwidth=0, padding=(11, 7))
        self.style.map("Nav.TButton", background=[("active", c["field"])],
                       foreground=[("active", c["text"]), ("disabled", c["muted"])])
        self.style.configure("Selected.Nav.TButton", background=c["soft"], foreground=c["heading"],
                             font=(FONT, 10, "bold"))
        self.style.map("Selected.Nav.TButton", background=[("active", c["soft"])],
                       foreground=[("active", c["heading"])])
        self.style.configure("TNotebook", borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(8, 6))
        self.style.map("TNotebook.Tab", background=[("selected", c["field"])])
        self.style.configure("TEntry", fieldbackground=c["field"], foreground=c["text"],
                             insertcolor=c["text"], padding=6)
        self.style.configure("TCombobox", foreground=c["text"], padding=(6, 4),
                             arrowcolor=c["heading"], bordercolor=c["border"])
        self.style.map("TCombobox", fieldbackground=[("readonly", c["soft"])],
                       selectbackground=[("readonly", c["soft"])],
                       selectforeground=[("readonly", c["heading"])])
        for option, color in (("background", "field"), ("foreground", "text"),
                              ("selectBackground", "accent"), ("selectForeground", "ink")):
            self.root.option_add(f"*TCombobox*Listbox.{option}", c[color])
        active_widgets = []
        for widget, bg, fg, extra in self.themed:
            if widget.winfo_exists():
                colors = {"background": c[bg], **{key: c[value] for key, value in extra.items()}}
                if fg:
                    colors["foreground"] = c[fg]
                widget.configure(**colors)
                active_widgets.append((widget, bg, fg, extra))
        self.themed = active_widgets
        for tag in ("muted", "accent", "heading", "add", "remove"):
            self.chat.tag_configure(tag, foreground=c[tag])
        self.chat.tag_configure("speaker", foreground=c["heading"], font=(FONT, 10, "bold"),
                                spacing1=13, spacing3=4)
        self.chat.tag_configure("user", background=c["soft"], lmargin1=12, lmargin2=12,
                                rmargin=12, spacing1=4, spacing3=5)
        self.chat.tag_configure("welcome", foreground=c["text"], font=(FONT, 17, "bold"), spacing3=10)
        zoom = 2 if self.chat_size.get() == "大" else 0
        self.chat.configure(font=(FONT, 11 + zoom))
        configure_markdown(self.chat, c, font_family=FONT, zoom=zoom)
        self.diff_theme(self.diff, c)
        self.activity.theme(c)
        self.context_meter.theme(c)
        self.update_settings_icon()
        self.theme_icon.theme(c)

    def update_settings_icon(self):
        if not hasattr(self, "colors"):
            return
        selected = self.current_page == "settings"
        palette = (self.colors["heading"], self.colors["soft" if selected else "panel"])
        if palette != self.settings_icon_palette:
            self.settings_image = gear_image(self.root, *palette)
            self.settings_button.configure(image=self.settings_image)
            self.settings_icon_palette = palette

    def cycle_theme(self):
        keys = list(THEMES)
        key = keys[(keys.index(theme_key(self.theme.get())) + 1) % len(keys)]
        self.theme.set(THEMES[key][0])
        self.apply_preferences()

    @staticmethod
    def diff_theme(box, colors):
        box.tag_configure("heading", foreground=colors["heading"])
        for tag in ("add", "remove"):
            box.tag_configure(tag, foreground=colors[tag], background=colors[f"{tag}_bg"])

    def show_welcome(self):
        self.append(self.chat, "\n选择一个项目\n", "welcome", follow=False)
        self.append(self.chat, "打开本地文件夹或新建项目后，可以分析代码、修改文件并检查结果。\n",
                    "muted", follow=False)

    def mode_changed(self, *_args):
        planning = self.mode.get() == "方案"
        self.mode_hint.set("只读分析 · Ctrl+Enter" if planning else "允许编辑项目 · Ctrl+Enter")
        self.send_button.configure(text="生成方案" if planning else "发送任务")

    def update_timer(self):
        if self.task_started is not None:
            seconds = int(monotonic() - self.task_started)
            self.task_elapsed.set(f"{seconds // 60:02d}:{seconds % 60:02d}")
        if not self.closing:
            self.schedule("timer", 250, self.update_timer)

    def append(self, box, value, tag="", follow=True):
        box.configure(state="normal")
        box.insert("end", str(value), tag)
        box.configure(state="disabled")
        if follow:
            box.see("end")

    def clear_text(self, box):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")

    def pump(self):
        for _ in range(200):
            try:
                action = self.events.get_nowait()
            except Empty:
                break
            action()
        if not self.closing:
            self.schedule("pump", 28, self.pump)

    def worker(self, caption, fn, on_done, on_error=None):
        if self.busy or self.closing:
            return
        self.busy = caption
        self.update_actions()

        def finish(result, failed=False):
            self.busy = ""
            self.update_actions()
            if failed:
                if on_error:
                    on_error(result)
                else:
                    self.show_error(caption, result)
            else:
                on_done(result)
            self.update_actions()

        def run():
            try:
                result = fn()
            except Exception as error:
                self.events.put(lambda error=error: finish(error, True))
            else:
                self.events.put(lambda: finish(result))

        Thread(target=run, daemon=True).start()

    def show_error(self, title, error):
        message = str(error).replace(self.token, "••••") if self.token else str(error)
        messagebox.showerror(title, message[:6500], parent=self.root)

    def update_actions(self):
        free = not self.busy and not self.running and not self.closing
        local = free and self.can_open_project
        for button, active in (
            (self.open_button, local), (self.new_project_button, local), (self.connection_button, free),
            (self.admin_button, free and self.is_admin),
            (self.role_button, free and self.can_switch_role),
            (self.settings_button, free), (self.workspace_button, free),
            (self.new_button, free and self.engine and self.can_use_agent),
            (self.send_button, free and self.engine and self.can_use_agent),
            (self.stop_button, self.running and not self.stop_requested.is_set()),
            (self.refresh_button, free and self.changes),
            (self.diff_copy, bool(self.diff_text)), (self.diff_expand, bool(self.diff_text)),
        ):
            button.configure(state="normal" if active else "disabled")
        self.update_delete_actions()
        self.mode_picker.configure(state="readonly" if free and not self.account_pending else "disabled")
        if self.account_pending and not self.pending_locked:
            self.pending_banner.pack(fill="x", padx=18, pady=(0, 8), before=self.context_meter.panel)
            self.input.configure(state="disabled")
        elif self.pending_locked and not self.account_pending:
            self.pending_banner.pack_forget()
            self.input.configure(state="normal")
        self.pending_locked = self.account_pending
        message = blocked_message(self.account) if self.connection_mode == "account" else ""
        if message:
            self.input.configure(state="disabled")
        elif self.quota_locked and not self.account_pending:
            self.input.configure(state="normal")
        self.quota_locked = bool(message)
        if message:
            self.quota_banner.configure(text=message)
            self.quota_banner.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.quota_banner.place_forget()
        if self.account and self.connection_mode == "account" and not self.is_admin:
            self.quota_text.set(balance_text(self.account, online=self.account_online))
            if not self.quota_label.winfo_manager():
                self.quota_label.pack(side="left")
        else:
            self.quota_label.pack_forget()
        if self.connection_mode == "account" and self.account and not self.role_button.winfo_manager():
            self.role_button.pack(side="right", padx=(3, 0), after=self.connection_button)
        elif (self.connection_mode != "account" or not self.account) and self.role_button.winfo_manager():
            self.role_button.pack_forget()
        self.role_button.configure(text=(
            "切换为普通用户" if self.is_admin
            else ("切换为管理员" if self.can_switch_role else "切换角色")
        ))
        if self.is_admin and not self.admin_button.winfo_manager():
            self.admin_button.pack(side="left", padx=(0, 4), after=self.workspace_button)
        elif not self.is_admin and self.admin_button.winfo_manager():
            self.admin_button.pack_forget()
        self.connection_button.configure(text=(
            "模型连接" if self.connection_mode == "direct"
            else ("退出登录" if self.account_client else "登录 / 注册")
        ))
        for page, button in (("workspace", self.workspace_button), ("settings", self.settings_button),
                             ("admin", self.admin_button), ("access", self.connection_button)):
            selected = (page == self.current_page
                        or (page == "workspace" and self.current_page == "status")
                        or (page == "access" and self.current_page == "direct"))
            button.configure(style="Selected.Nav.TButton" if selected else "Nav.TButton")
        self.update_settings_icon()

    @property
    def can_use_agent(self):
        if self.connection_mode == "direct":
            return self.direct_unlocked and self.direct_ready
        return bool(self.account_client and self.account_online and self.account and self.account["status"] == "active"
                    and not blocked_message(self.account))

    @property
    def can_open_project(self):
        if self.connection_mode == "direct":
            return bool(self.direct_unlocked and self.api_base)
        return bool(self.account and self.account["status"] == "active")

    @property
    def account_pending(self):
        return bool(self.connection_mode == "account" and self.account and self.account["status"] == "pending")

    @property
    def can_switch_role(self):
        return bool(self.connection_mode == "account" and self.can_use_agent
                    and self.account.get("assigned_role") == "admin")

    @property
    def is_admin(self):
        return self.connection_mode == "account" and self.has_admin_session

    @property
    def has_admin_session(self):
        return bool(self.account_client and self.account_online and self.account
                    and self.account["status"] == "active" and self.account["role"] == "admin")

    @property
    def direct_unlocked(self):
        return bool(self.connection_mode == "direct" and self.has_admin_session
                    and self.account_client.open_webui_expires_at > time())

    def change_role(self):
        if not self.can_switch_role or self.busy or self.running:
            return
        target = "user" if self.is_admin else "admin"
        if target == "user":
            self.switch_role(target, "")
        else:
            prompt_role_switch(self, target, lambda code: self.switch_role(target, code))

    def switch_role(self, target, authorization_code):
        if not self.can_switch_role or self.busy or self.running or self.closing:
            return
        if target != ("user" if self.is_admin else "admin"):
            return
        client = self.account_client
        self.account_generation += 1
        self.account_polling = False

        def done(user):
            self.token = client.token
            self.account, self.account_online = user, True
            try:
                self.saved_login.update_session(client.base, client.token)
            except (OSError, ValueError):
                self.status.set("角色已切换，保存的登录未能更新")
            if self.engine:
                self.engine.close()
                self.engine = None
            self.engine_limits = None
            self.account_state_changed()
            if self.current_page in {"settings", "admin"}:
                self.show_page("workspace", force=True)
            self.poll_account()

        def failed(error):
            self.show_error("切换角色失败", error)
            self.poll_account()

        self.worker("切换登录角色", lambda: client.switch_role(target, authorization_code), done, failed)

    def require_account_login(self, message):
        self.cancel_saved_login()
        self.saved_login.forget()
        self.clear_account()
        self.connection_mode = "account"
        self.show_page("access", force=True)
        self.status.set(message)
        self.check_account_connection()

    def save_prefs(self):
        self.prefs.update(strict=self.strict.get(), theme=theme_key(self.theme.get()),
                          chat_size=self.chat_size.get(), default_mode=self.default_mode.get(),
                          show_results=self.results_preference.get())
        write_json(self.prefs_path, self.prefs)

    def apply_preferences(self, *, mode_changed=False, results_changed=False):
        try:
            self.save_prefs()
        except OSError as error:
            self.show_error("设置保存失败", error)
            return False
        if mode_changed and not self.running:
            self.mode.set(self.default_mode.get())
        if results_changed:
            self.set_results_visible(self.results_preference.get())
        self.apply_theme()
        return True

    def get_account_client(self, connection=None):
        """Use the same transport for account requests and connection checks."""
        scope = self.account_connection if connection is None else connection
        with self.gateway_client_lock:
            cached = self.gateway_transport
            if cached and cached[0] == scope:
                return cached[1]
            server, proxy_mode = scope
            client = AccountClient(server, mode=proxy_mode)
            if scope == self.account_connection:
                self.gateway_transport = scope, client
            return client

    def save_account_connection(self, server, proxy_mode, *, manual=True):
        if self.account_client or self.busy or self.closing:
            return False
        server = normalize_server(server)
        if proxy_mode not in CONNECTION_MODES:
            raise AccountError("请选择自动、系统代理或直连。")
        update = {"account_server": server, "account_server_manual": manual, "account_proxy_mode": proxy_mode}
        previous = {key: self.prefs[key] for key in update if key in self.prefs}
        self.prefs.update(update)
        if not self.apply_preferences():
            for key in update:
                self.prefs.pop(key, None)
            self.prefs.update(previous)
            return False
        scope = (server, proxy_mode)
        if self.account_connection != scope:
            self.account_connection = scope
            self.gateway_report = None
            self.gateway_last_success.set("—")
            self.gateway_error.set("")
            self.gateway_recent_error.set("—")
        self.check_account_connection()
        return True

    def check_account_connection(self, *, full=False, details=False):
        if self.closing or self.connection_mode != "account":
            return
        scope = self.account_connection
        if self.gateway_inflight and self.gateway_inflight[0] == scope:
            if self.gateway_inflight[1] or not full:
                return
        self.gateway_inflight = scope, full
        self.gateway_check_number += 1
        attempt = self.gateway_check_number
        session = self.account_generation
        page = self.page_panel
        self.gateway_status.set("正在运行完整诊断…" if full else "正在检查账号网关…")
        self.gateway_report_status.set("")

        def done(report):
            if self.closing or attempt != self.gateway_check_number:
                return
            self.gateway_inflight = None
            if scope != self.account_connection or self.connection_mode != "account":
                return
            if full and session != self.account_generation:
                self.check_account_connection()
                return
            self.gateway_report = report
            self.gateway_status.set(report.summary if report.healthy else "连接失败")
            error = report.connection_error or next(
                (row.detail for row in report.rows if row.state == "failure"), "")
            self.gateway_error.set(error)
            if error:
                self.gateway_recent_error.set(error)
            if report.healthy:
                self.gateway_last_success.set(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
            if details and page is self.page_panel:
                self.show_account_diagnostics()

        def check():
            server, mode = scope
            try:
                try:
                    client = self.get_account_client(scope)
                except OSError:
                    report = unavailable_report(server, mode=mode, phase="tls")
                else:
                    report = run_diagnostics(client, full=full)
            except Exception:
                report = unavailable_report(server, mode=mode)
            self.events.put(lambda: done(report))

        Thread(target=check, daemon=True).start()

    def copy_account_diagnostics(self, report=None):
        report = self.gateway_report if report is None else report
        if report is None:
            self.gateway_report_status.set("完成连接检查后可复制报告。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(report.to_text())
        self.gateway_report_status.set("已复制诊断报告。")

    def show_account_diagnostics(self):
        report = self.gateway_report
        if report is None:
            self.gateway_report_status.set("完成连接检查后可查看详情。")
            return
        window = tk.Toplevel(self.root)
        window.title("服务器连接诊断")
        window.transient(self.root)
        window.geometry("690x480")
        self.skin(window, "panel", None)
        holder, body = self.text_panel(window, height=14)
        holder.pack(fill="both", expand=True, padx=16, pady=16)
        self.append(body, report.to_text(), follow=False)
        self.button(window, "复制诊断报告", lambda: self.copy_account_diagnostics(report)).pack(
            anchor="e", padx=16, pady=(0, 12))
        window.bind("<Escape>", lambda _event: window.destroy())
        self.apply_theme()

    def refresh_recents(self):
        recent_path = self.state / "recents.json"
        self.recents = read_json(recent_path, []) if self.can_open_project else []
        self.recent_list.delete(0, "end")
        for path in self.recents:
            self.recent_list.insert("end", Path(path).name)
        self.update_delete_actions()

    def update_delete_actions(self):
        free = not self.busy and not self.running and not self.closing and self.can_open_project
        self.remove_recent_button.configure(
            state="normal" if free and self.recent_list.curselection() else "disabled")
        self.delete_session_button.configure(
            state="normal" if free and self.engine and self.session_list.curselection() else "disabled")

    def remove_recent(self):
        selected = self.recent_list.curselection()
        if self.busy or self.running or self.closing or not self.can_open_project or not selected:
            return
        path = self.recents[selected[0]]
        state = self.state
        if not messagebox.askyesno(
                "删除最近项目记录", f"从最近项目中移除以下记录？\n\n{path}\n\n项目文件和历史会话将保留。",
                parent=self.root, default=messagebox.NO):
            return
        if state != self.state or self.busy or self.running or self.closing or not self.can_open_project:
            return
        try:
            write_json(state / "recents.json", [item for item in self.recents if item != path])
        except OSError as error:
            self.show_error("无法删除最近项目记录", error)
            return
        self.refresh_recents()

    def remove_legacy_key(self):
        try:
            (self.data_root / "connection.dpapi").unlink(missing_ok=True)
        except OSError:
            self.status.set("旧版本地凭据未能移除")

    def account_action(self):
        if self.busy or self.running or self.closing:
            return
        if self.connection_mode == "direct":
            self.show_page("direct")
            return
        client = self.account_client
        if client is None:
            self.show_page("access")
            return
        self.cancel_saved_login()
        try:
            self.saved_login.update_session(client.base, "")
        except (OSError, ValueError):
            pass  # The server session is still revoked; the remembered password is kept.

        def done(_result):
            self.clear_account()
            self.show_page("access", force=True)
            self.page_controller.reset()
            self.check_account_connection()

        def failed(error):
            self.clear_account()
            self.show_page("access", force=True)
            self.page_controller.reset()
            self.status.set("已在本机退出 · 服务器会话按设定过期")
            self.check_account_connection()

        self.worker("退出登录", client.logout, done, failed)

    def select_connection(self, mode):
        if mode not in {"direct", "account"} or self.busy or self.running or self.closing:
            return
        if mode == self.connection_mode:
            self.show_page("direct" if mode == "direct" else ("workspace" if self.account_client else "access"))
            return
        if mode == "direct" and not self.has_admin_session:
            if not self.account_client:
                self.direct_requested = True
                self.show_direct_login_hint()
            elif self.can_switch_role:
                self.status.set("使用 Open WebUI 前，请切换为管理员。")
                self.change_role()
            else:
                self.status.set("Open WebUI 需要管理员会话。请退出并以管理员身份登录。")
            return
        self.account_generation += 1
        self.account_polling = False
        for name in ("direct_expiry", "direct_lock"):
            timer = self.timers.pop(name, None)
            if timer:
                self.root.after_cancel(timer)
        if self.account_client:
            self.account_client.clear_open_webui_access()
        self.connection_mode = mode
        self.clear_workspace()
        if mode == "direct":
            self.show_page("direct", force=True)
            self.direct_state("待权限验证")
        elif self.account_client and self.account:
            self.state = self.account_directory(self.account_client.base, self.account)
            self.token, self.api_base = self.account_client.token, self.account_client.model_base
            self.ca_path = self.account_ca_path
            self.refresh_recents()
            self.show_page("workspace", force=True)
            self.account_state_changed()
            self.poll_account()
        else:
            self.show_page("access", force=True)

    def show_direct_login_hint(self, message="请以 CCSLab 管理员身份登录，再验证 Open WebUI 访问密码。"):
        self.show_page("access", force=True)
        self.page_controller.select("login", message=message)
        self.page_controller.admin_login.set(True)

    def direct_state(self, message):
        self.direct_status.set(message)
        if self.connection_mode == "direct":
            self.status.set("公网模型 · " + message)

    def unlock_direct_access(self, password):
        if (self.connection_mode != "direct" or self.direct_unlocked
                or not self.has_admin_session or self.busy or self.running or self.closing
                or "direct_lock" in self.timers):
            return False
        if not isinstance(password, str) or not password:
            self.direct_feedback.set("请输入最高权限密码。")
            return False
        self.direct_state("正在验证权限")
        self.direct_feedback.set("")
        client = self.account_client

        def verify():
            return client.verify_open_webui_access(password)

        def done(result):
            if self.closing or self.connection_mode != "direct" or client is not self.account_client:
                return
            if not self.direct_unlocked:
                self.lock_direct_access("Open WebUI 访问授权已过期。请重新验证。")
                return
            expires_at = result["expires_at"]
            delay = max(1, int((expires_at - time()) * 1000) + 1)
            self.schedule("direct_expiry", delay, lambda: self.expire_direct_access(expires_at))
            self.direct_state("待连接")
            self.show_page("direct", force=True)
            if self.direct_restore_after_unlock:
                self.restore_direct()

        def failed(error):
            if isinstance(error, AccountError) and error.status == 401:
                self.lock_direct_access("管理员会话已失效。请重新登录。", reauthenticate=True)
                return
            self.direct_state("待权限验证")
            status = getattr(error, "status", 0)
            if status in {404, 405}:
                feedback = "当前网关缺少 Open WebUI 授权接口。请联系管理员部署配套的 1.5.3 修订版网关。"
            elif isinstance(error, AccountError):
                feedback = str(error).replace(password, "••••")
            else:
                feedback = "无法验证最高权限，请重试或联系管理员。"
            self.direct_feedback.set(feedback[:420])

        self.worker("验证最高权限", verify, done, failed)
        return True

    def expire_direct_access(self, expires_at):
        if self.connection_mode == "direct" and not self.direct_unlocked and time() >= expires_at:
            self.lock_direct_access("Open WebUI 访问授权已过期。请重新验证。")

    def lock_direct_access(self, message, *, reauthenticate=False):
        if self.closing or self.connection_mode != "direct":
            return
        if self.account_client:
            self.account_client.clear_open_webui_access()
        if self.running:
            self.stop()
        if self.busy:
            self.update_actions()
            self.schedule("direct_lock", 100, lambda: self.lock_direct_access(
                message, reauthenticate=reauthenticate))
            return
        pending = self.timers.pop("direct_lock", None)
        if pending:
            self.root.after_cancel(pending)
        if reauthenticate:
            self.direct_requested = True
            self.require_account_login(message)
            self.show_direct_login_hint(message)
            return
        self.reset_direct_model()
        self.direct_state("待权限验证")
        self.direct_feedback.set(message)
        self.show_page("direct", force=True)

    def reset_direct_model(self):
        if self.engine:
            self.engine.close()
        self.engine = self.engine_limits = None
        self.token = self.api_base = ""
        self.direct_identity = ""
        self.direct_ready = False

    def direct_grant_failed(self, error):
        if error.status == 401:
            self.lock_direct_access("管理员会话已失效。请重新登录。", reauthenticate=True)
        else:
            self.lock_direct_access("无法确认 Open WebUI 访问授权。请重新验证。")

    @property
    def can_change_webui_password(self):
        return bool(self.direct_unlocked and self.direct_ready and self.token
                    and supports_webui_sign_in(self.api_base))

    def change_webui_password(self, current, new, confirmation):
        if not self.can_change_webui_password or self.running or self.busy or self.closing:
            return False
        if not isinstance(current, str) or not current or not valid_account_password(new):
            self.direct_feedback.set(f"请输入当前密码。新密码须为 {PASSWORD_RANGE}。")
            return False
        if new != confirmation or new == current:
            self.direct_feedback.set("请确认两次新密码一致，并与当前密码不同。")
            return False
        client, target, token = self.account_client, self.direct_connection, self.token
        self.direct_feedback.set("正在修改当前 Open WebUI 账号的登录密码…")

        def work():
            client.check_open_webui_access()
            return target.change_webui_account_password(token, current, new)

        def done(_result):
            self.reset_direct_model()
            if self.direct_unlocked:
                self.direct_state("请重新登录")
                self.direct_feedback.set("Open WebUI 登录密码已修改。请使用新密码重新登录。")
                self.show_page("direct", force=True)

        def failed(error):
            if isinstance(error, AccountError):
                self.direct_grant_failed(error)
                return
            detail = self.redact(str(error))
            for secret in (current, new):
                detail = detail.replace(secret, "••••")
            self.direct_feedback.set(detail[:420])
            if isinstance(error, DirectError) and error.status == 401:
                self.reset_direct_model()
                self.direct_state("请重新登录")
                self.show_page("direct", force=True)

        self.worker("修改 Open WebUI 登录密码", work, done, failed)
        return True

    def restore_direct(self):
        self.check_direct_connection(restore=True)

    def direct_state_dir(self, base, identity=""):
        if not identity and base == "https://ccslab-llm.qtchen.cn/v1":
            return self.data_root / "public-model"
        scope = f"{base}\0{identity}" if identity else base
        return self.data_root / "public-models" / hashlib.sha256(scope.encode("utf-8")).hexdigest()

    def check_direct_connection(self, *, address=None, save=False, restore=False, email=None, password=None):
        if self.connection_mode != "direct" or self.busy or self.running or self.closing:
            return False
        if not self.direct_unlocked:
            self.direct_feedback.set("请先验证最高权限密码。")
            return False
        try:
            target = self.direct_connection.for_address(address) if address is not None else self.direct_connection
        except DirectError as error:
            self.direct_feedback.set(str(error))
            return False
        authenticating = email is not None or password is not None
        if authenticating and (not isinstance(email, str) or not email.strip()
                               or not isinstance(password, str) or not password):
            self.direct_feedback.set("请输入 Open WebUI 的邮箱和密码。")
            return False
        current = self.token
        client = self.account_client
        same_address = target.model_base == self.api_base
        token = current if same_address and not restore else ""
        identity = self.direct_identity if token else ""
        self.direct_feedback.set("正在登录 Open WebUI…" if authenticating else "正在调用模型生成接口…")
        previous = self.direct_status.get()
        self.direct_state("连接测试中")

        def work():
            client.check_open_webui_access()
            candidate = target.sign_in(email, password) if authenticating else token
            scope = email.strip().casefold() if authenticating else identity
            model = target.probe(candidate or None)
            directory = self.direct_state_dir(target.model_base, scope)
            ca = target.engine_ca_bundle(directory) if save or restore else None
            return candidate, scope, model, directory, ca

        def done(result):
            if not self.direct_unlocked:
                return
            candidate, scope, model, directory, ca = result
            if not (save or restore):
                self.direct_state(previous)
                self.direct_feedback.set(f"{MODEL_ID} 已生成测试回复。")
                return
            if save:
                old_base = self.prefs.get("direct_address")
                self.prefs["direct_address"] = target.model_base
                try:
                    self.save_prefs()
                except OSError as error:
                    if old_base is None:
                        self.prefs.pop("direct_address", None)
                    else:
                        self.prefs["direct_address"] = old_base
                    failed(error)
                    return

            reopen = self.project if self.state != directory else ""
            if reopen:
                self.clear_workspace()
            elif candidate != current and self.engine:
                self.engine.close()
                self.engine = None
            self.direct_connection = target
            self.direct_address.set(target.model_base)
            self.direct_identity = scope
            self.state = directory
            self.token, self.api_base, self.ca_path = candidate or "", target.model_base, ca
            self.direct_ready = True
            self.update_model(model)
            self.direct_state("已连接")
            self.direct_feedback.set(f"{MODEL_ID} 已生成测试回复。")
            self.refresh_recents()
            if self.current_page == "direct":
                self.show_page("workspace", force=True)
            if reopen:
                self.open_project(reopen)
            elif self.project and self.engine is None:
                self.start_engine(self.project)

        def failed(error):
            if isinstance(error, AccountError):
                self.direct_grant_failed(error)
                return
            status = getattr(error, "status", 0)
            if not authenticating and same_address and status in {401, 403}:
                self.direct_ready = False
                self.token = ""
                if self.engine:
                    self.engine.close()
                    self.engine = None
            if self.direct_ready:
                self.direct_state(previous)
            else:
                self.direct_state("请登录" if status == 401 else "连接未通过")
            detail = self.redact(str(error))
            if password:
                detail = detail.replace(password, "••••")
            self.direct_feedback.set(detail)
            self.update_actions()

        self.worker("测试公网模型", work, done, failed)
        return True

    def check_direct_tools(self):
        if not self.can_use_agent or self.connection_mode != "direct":
            self.direct_feedback.set("请先连接模型，再验证工具调用。")
            return
        if self.busy or self.running or self.closing:
            return
        base, key, ca = self.api_base, self.token, self.ca_path
        client = self.account_client
        self.direct_feedback.set("正在用临时项目验证只读工具…")

        def work():
            client.check_open_webui_access()
            return check_read_tool(base, key, ca)

        def failed(error):
            if isinstance(error, AccountError):
                self.direct_grant_failed(error)
                return
            self.direct_feedback.set(self.redact(str(error)))

        self.worker("验证模型工具调用", work,
                    lambda _result: self.direct_feedback.set("工具调用通过：模型读取了测试文件并生成回复。"),
                    failed)

    def cancel_saved_login(self):
        self.saved_login_suppressed = True
        timer = self.timers.pop("saved_login", None)
        if timer:
            self.root.after_cancel(timer)

    def restore_saved_login(self):
        if self.account_client or self.closing or self.saved_login_suppressed:
            return
        if not self.saved_login.path.is_file():
            return
        if self.busy:
            self.schedule("saved_login", 1000, self.restore_saved_login)
            return
        if self.current_page != "access" or self.page_controller.mode != "login":
            self.schedule("saved_login", 15000, self.restore_saved_login)
            return
        page = self.page_controller
        if not page.remember.get():
            return
        connection = self.account_connection
        username = page.username.get().strip()
        page.set_busy(True)
        page.feedback.set("正在恢复保存的登录…")

        def request():
            record = self.saved_login.load(connection[0])
            if record is None:
                return None
            if username and username != record["username"]:
                return None
            client = self.get_account_client(connection)
            client.token = record.get("token", "")
            try:
                user = client.me() if client.token else None
            except AccountError as error:
                if error.status != 401:
                    raise
                user = None
                client.token = ""
            if user is None:
                # Expired administrator sessions resume as ordinary users; authorization
                # codes are never stored or replayed for privilege elevation.
                user = client.login(record["username"], record["password"])
            self.saved_login.save(client.base, record["username"], record["password"], client.token)
            return client, user

        def done(result):
            if page.container.winfo_exists():
                page.set_busy(False)
                page.feedback.set("")
            if result and connection == self.account_connection and not self.account_client:
                self.signed_in(*result)

        def failed(error):
            if page.container.winfo_exists():
                page.set_busy(False)
            invalid = isinstance(error, AccountError) and error.status in {401, 403}
            if invalid:
                self.cancel_saved_login()
            elif not self.closing:
                self.schedule("saved_login", 15000, self.restore_saved_login)
            if page.container.winfo_exists():
                page.feedback.set("保存的登录未通过验证，请检查密码后重试。" if invalid else
                                  "暂时无法自动登录，已保留账号密码，连接恢复后重试。")

        self.worker("恢复保存的登录", request, done, failed)

    def signed_in(self, client, user):
        self.clear_account(forget_session=False)
        self.connection_mode = "account"
        self.account_connection = (client.base, self.account_connection[1])
        self.gateway_transport = self.account_connection, client
        self.account_client, self.account = client, user
        self.state = self.account_directory(client.base, user)
        self.state.mkdir(parents=True, exist_ok=True)
        self.ca_path = self.account_ca_path = client.engine_ca_bundle(self.state)
        self.account_online = True
        self.token, self.api_base = client.token, client.model_base
        self.prefs["account_server"] = client.base
        self.prefs["connection_mode"] = "account"
        self.save_prefs()
        self.refresh_recents()
        self.account_state_changed()
        open_direct = self.direct_requested and self.has_admin_session
        self.direct_requested = False
        if open_direct:
            self.select_connection("direct")
            return
        self.show_page("workspace", force=True)
        self.poll_account()

    def account_directory(self, base, user):
        def scoped_directory(endpoint):
            identity = hashlib.sha256(f"{endpoint}\0{user['id']}\0{user['username']}".encode()).hexdigest()
            return self.data_root / "accounts" / identity

        current = scoped_directory(base)
        if base in {DEFAULT_SERVER, f"{DEFAULT_SERVER}:443"} and not current.exists():
            legacy = scoped_directory(LEGACY_DEFAULT_SERVER)
            if legacy.is_dir():
                return legacy
        return current

    def clear_account(self, *, forget_session=True):
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.account_generation += 1
        self.account_polling = False
        if self.account_client:
            self.account_client.clear_open_webui_access()
            if forget_session:
                self.account_client.token = ""
            self.gateway_report = None
        self.account_client = None
        self.account = None
        self.account_online = False
        self.account_ca_path = None
        self.clear_workspace()

    def clear_workspace(self):
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.update_session_grant_ui()
        self.direct_ready = False
        self.direct_identity = ""
        self.direct_status.set("待连接" if self.direct_unlocked else "待权限验证")
        self.direct_feedback.set("")
        self.model_checked = 0
        self.model_info = {}
        self.context_meter.set_model({})
        self.ca_path = None
        self.save_history()
        if self.engine:
            self.engine.close()
        self.engine = None
        self.engine_limits = None
        self.token = self.api_base = ""
        self.project = ""
        self.changes = None
        self.session = None
        self.history_path = None
        self.history = {}
        self.session_titles = {}
        self.state = self.data_root
        self.drafts.clear()
        self.input.configure(state="normal")
        self.input.delete("1.0", "end")
        self.session_search.set("")
        self.update_sessions([])
        self.refresh_recents()
        self.project_label.set("未打开项目")
        self.branch.set("—")
        self.status.set("请登录")
        self.change_rows = []
        self.change_filter.set("")
        self.render_change_list()
        self.diff_text = ""
        self.clear_text(self.diff)
        self.reset_conversation()
        self.show_welcome()
        self.update_actions()

    def account_state_changed(self):
        user = self.account
        if not user:
            self.status.set("登录已过期")
            return
        state = user["status"]
        name = user["username"]
        label = ("网络断开" if not self.account_online else ACCOUNT_STATUS[state])
        title = name if len(name) < 23 else name[:22] + "…"
        mode = "管理员" if self.is_admin else "普通用户"
        self.status.set(f"{title} · {mode} · {label}")
        if self.current_page == "admin" and not self.is_admin:
            self.show_page("workspace", force=True)
        elif self.current_page == "settings" and getattr(self, "settings_admin", False) and not self.is_admin:
            self.show_page("workspace", force=True)
        if state == "active" and self.account_online and self.current_page == "status":
            self.show_page("workspace", force=True)
        elif state != "active":
            self.account_title.set(ACCOUNT_STATUS[state])
            detail = {
                "pending": "可浏览工作台。管理员激活账号后，项目和任务功能将自动开放。",
                "rejected": "请联系实验室管理员确认审核结果。",
                "disabled": "如需重新启用，请联系实验室管理员。",
            }[state]
            self.account_detail.set(
                detail if self.account_online else "连接恢复后可查询最新账号状态。"
            )
            if state == "pending" and self.current_page == "status":
                self.show_page("workspace", force=True)
            elif state != "pending" and self.current_page in {"admin", "workspace"}:
                self.show_page("status", force=True)
        self.update_actions()

    def monitor_account(self):
        self.poll_account()
        if not self.closing:
            self.schedule("connection", 4500, self.monitor_account)

    def poll_account(self):
        client = self.account_client
        if not client or self.account_polling or self.closing or self.busy == "切换登录角色":
            return
        direct = self.connection_mode == "direct"
        if direct and self.busy and not self.running:
            return
        self.account_polling = True
        generation = self.account_generation
        check_grant = direct and self.direct_unlocked
        refresh_model = monotonic() - self.model_checked > 12 or not self.model_checked

        def done(user, model, error):
            if generation != self.account_generation or self.closing:
                return
            self.account_polling = False
            if direct:
                if error:
                    if user is None and error.status in {401, 403}:
                        self.lock_direct_access("管理员会话已失效。请重新登录。", reauthenticate=True)
                    elif check_grant:
                        self.direct_grant_failed(error)
                elif user["status"] != "active" or user["role"] != "admin":
                    self.lock_direct_access("账号已失去管理员权限，请重新登录。", reauthenticate=True)
                else:
                    self.account = user
                return
            if error and error.status in {401, 403}:
                self.account_online = False
                if self.running:
                    self.stop()
                self.status.set("登录已失效，请退出后重新登录")
                self.account_title.set("会话已失效")
                self.account_detail.set("退出后重新登录，即可继续使用。")
                if self.current_page in {"admin", "workspace"} or (
                    self.current_page == "settings" and getattr(self, "settings_admin", False)
                ):
                    self.show_page("status", force=True)
                self.update_actions()
                return
            if error:
                if self.account_online:
                    self.account_online = False
                    self.account_state_changed()
                return
            changed = user != self.account or not self.account_online
            self.account, self.account_online = user, True
            if changed and user["status"] != "active" and self.running:
                self.stop()
            if changed:
                self.account_state_changed()
            if model is not None:
                self.update_model(model)
            if self.can_use_agent and self.engine is None and self.project and not self.busy and not self.running:
                self.start_engine(self.project)

        def request():
            try:
                user = client.me()
            except AccountError as error:
                self.events.put(lambda error=error: done(None, None, error))
                return
            if direct:
                if check_grant and user["status"] == "active" and user["role"] == "admin":
                    try:
                        client.check_open_webui_access()
                    except AccountError as error:
                        self.events.put(lambda error=error: done(user, None, error))
                        return
                self.events.put(lambda: done(user, None, None))
                return
            model = None
            if user["status"] == "active" and (refresh_model or not self.model_info):
                try:
                    model = client.model()
                except AccountError:
                    pass
            self.events.put(lambda: done(user, model, None))

        Thread(target=request, daemon=True).start()

    def update_model(self, model):
        self.model_info = model
        self.model_checked = monotonic()
        self.context_meter.set_model(model, output_limit=self.model_limits(model)[1])

    def choose_project(self):
        if self.busy or self.running or self.closing or not self.can_open_project:
            return
        path = filedialog.askdirectory(parent=self.root, title="选择项目文件夹", mustexist=True)
        if path:
            self.open_project(path)

    def new_project(self):
        if not self.busy and not self.running and not self.closing and self.can_open_project:
            show_new_project(self)

    def open_recent(self, _event):
        selected = self.recent_list.curselection()
        if selected:
            self.open_project(self.recents[selected[0]])

    def open_project(self, path):
        if self.busy or self.running or self.closing or not self.can_open_project:
            return
        candidate = Path(path)
        try:
            path = str(candidate.resolve(strict=True))
            if not Path(path).is_dir():
                raise ValueError("请选择现有的项目文件夹。")
        except (OSError, ValueError) as error:
            self.show_error("打开项目", error)
            return
        if Path(path) == Path(path).parent:
            self.show_error("打开项目", "请选择项目子文件夹。")
            return
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.update_session_grant_ui()
        self.show_page("workspace")
        self.save_history()
        self.save_draft()
        self.session = None
        self.engine, old_engine = None, self.engine
        self.project = path
        name = Path(path).name
        self.project_label.set(name if len(name) <= 28 else name[:27] + "…")
        self.branch.set("正在读取项目…")
        write_json(self.state / "recents.json", [path, *[p for p in self.recents if p != path]][:15])
        self.refresh_recents()
        self.history_path = self.state / (hashlib.sha256(os.path.normcase(path).encode()).hexdigest() + ".json")
        self.history = read_json(self.history_path, {})
        self.load_session_titles()
        self.update_sessions([])
        self.session_search.set("")
        self.changes = None
        self.change_rows = []
        self.change_filter.set("")
        self.render_change_list()
        self.clear_text(self.diff)
        self.diff_text = ""
        self.reset_conversation()
        self.restore_draft()
        self.append(self.chat, path + "\n", "heading")

        def work():
            if old_engine:
                old_engine.close()
            changes = Changes(path)
            return changes, changes.status()

        def done(result):
            self.changes, status = result
            self.show_changes(status)
            if self.can_use_agent:
                self.start_engine(path)
            else:
                self.append(self.chat, "模型连接恢复后可发送任务。\n", "muted")

        self.worker("读取项目", work, done)

    def start_engine(self, path):
        limits = self.model_limits(self.model_info)
        client = self.account_client if self.connection_mode == "account" else None

        def work():
            proxy_url = client.engine_proxy_url() if client else None
            return self.create_engine(path, limits, proxy_url=proxy_url)

        def done(result):
            self.engine, sessions = result
            self.engine_limits = limits
            self.update_sessions(sessions)

        self.worker("启动本地 OpenCode", work, done)

    @staticmethod
    def model_limits(meta):
        limit = meta.get("effective_context_limit")
        caps = [v for key in ("output_token_limit", "output_limit")
                if type(v := meta.get(key)) is int and v > 0]
        output = min(caps) if caps else None
        context = limit if type(limit) is int and 1024 <= limit <= 2_000_000 else 24000
        max_output = output if type(output) is int and output > 0 else 4096
        return context, min(max_output, 4096, context // 2)

    def create_engine(self, path, limits, *, proxy_url=None):
        from engine import Engine
        engine = Engine(self.state, self.api_base, self.token,
                        limits=limits, ca_bundle=self.ca_path, proxy_url=proxy_url)
        try:
            engine.open_project(path)
            return engine, engine.sessions()
        except Exception:
            engine.close()
            raise

    def update_sessions(self, sessions):
        known = {row["id"]: dict(row) for row in sessions}
        for key, record in self.history.items():
            known.setdefault(key, {"id": key, "title": record.get("title", "历史会话")})
        for key, row in known.items():
            if key in self.session_titles:
                row["title"] = self.session_titles[key]
        self.sessions = list(known.values())
        self.render_session_list()

    def load_session_titles(self):
        saved = read_json(self.history_path.with_suffix(".titles.json"), {})
        self.session_titles = ({key: value.strip() for key, value in saved.items()
                                if isinstance(key, str) and valid_session_title(value)}
                               if isinstance(saved, dict) else {})

    def show_session_menu(self, event):
        index = self.session_list.nearest(event.y)
        bounds = self.session_list.bbox(index) if self.visible_sessions else None
        if not bounds or not bounds[1] <= event.y < bounds[1] + bounds[3]:
            return "break"
        session_id = self.visible_sessions[index]["id"]
        enabled = not (self.busy or self.running or self.closing) and self.can_open_project and self.history_path
        self.session_menu.entryconfigure(0, state="normal" if enabled else "disabled",
                                         command=lambda: self.rename_session(session_id))
        try:
            self.session_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.session_menu.grab_release()
        return "break"

    def rename_session(self, session_id=None):
        if self.busy or self.running or self.closing or not self.can_open_project or not self.history_path:
            return
        if session_id is None:
            selection = self.session_list.curselection()
            if not selection:
                return
            session_id = self.visible_sessions[selection[0]]["id"]
        row = next((row for row in self.sessions if row["id"] == session_id), None)
        if row is None:
            return
        project, history_path, generation = self.project, self.history_path, self.account_generation
        title = simpledialog.askstring("重命名会话", "会话名称（1–100 个字符）：",
                                       initialvalue=row.get("title") or "新会话", parent=self.root)
        if title is None:
            return
        if (self.busy or self.running or self.closing or not self.can_open_project
                or project != self.project or history_path != self.history_path
                or generation != self.account_generation):
            return
        if not valid_session_title(title):
            self.show_error("无法重命名", "会话名称须为 1–100 个字符，不能包含换行或控制字符。")
            return
        title = title.strip()
        updated = {**self.session_titles, session_id: title}
        try:
            write_json(history_path.with_suffix(".titles.json"), updated)
        except OSError as error:
            self.show_error("重命名保存失败，请重试", error)
            return
        self.session_titles = updated
        if self.session_search.get().strip().casefold() not in title.casefold():
            self.session_search.set("")
        self.update_sessions(self.sessions)
        for index, item in enumerate(self.visible_sessions):
            if item["id"] == session_id:
                self.session_list.see(index)
                break

    def render_session_list(self):
        query = self.session_search.get().strip().casefold()
        self.visible_sessions = [row for row in self.sessions if query in (row.get("title") or "").casefold()]
        self.session_list.delete(0, "end")
        for index, row in enumerate(self.visible_sessions):
            self.session_list.insert("end", row.get("title") or "新会话")
            if row["id"] == self.session:
                self.session_list.selection_set(index)
        self.update_delete_actions()

    def delete_session(self):
        selected = self.session_list.curselection()
        if self.busy or self.running or self.closing or not self.can_open_project or not self.engine or not selected:
            return
        row = self.visible_sessions[selected[0]]
        session_id = row["id"]
        engine, history, history_path, titles = self.engine, self.history, self.history_path, self.session_titles
        if not messagebox.askyesno(
                "删除历史会话", f"删除会话“{row.get('title') or '新会话'}”？\n\n"
                "此会话的聊天记录、执行记录和草稿将被删除，无法恢复。项目文件将保留。",
                parent=self.root, default=messagebox.NO):
            return
        if (engine is not self.engine or history is not self.history or self.busy
                or self.running or self.closing or not self.can_open_project):
            return

        def deleted(_result):
            remaining = {key: value for key, value in history.items() if key != session_id}
            try:
                write_json(history_path, remaining)
                if session_id in titles:
                    remaining_titles = {key: value for key, value in titles.items() if key != session_id}
                    write_json(history_path.with_suffix(".titles.json"), remaining_titles)
            except OSError as error:
                self.show_error("会话已删除，但本地显示记录保存失败，请重试删除", error)
                return
            history.pop(session_id, None)
            titles.pop(session_id, None)
            if history is not self.history:
                return
            self.drafts.pop((self.project, session_id), None)
            if self.session == session_id:
                self.session_grants.clear()
                self.update_session_grant_ui()
                self.session = None
                self.reset_conversation()
                self.restore_draft()
                self.append(self.chat, "\n会话已删除，可以新建会话或直接发送新任务。\n", "muted")
            self.update_sessions([item for item in self.sessions if item["id"] != session_id])

        self.worker("删除历史会话", lambda: engine.delete_session(session_id), deleted)

    def new_session(self):
        if not self.engine or not self.can_use_agent or self.busy or self.running or self.closing:
            return
        self.show_page("workspace")

        def created(session_id):
            self.history[session_id] = {"title": "新会话", "events": []}
            self.session_search.set("")
            self.open_session(session_id)
            self.update_sessions(self.sessions)

        self.worker("新建会话", self.engine.new_session, created)

    def select_session(self, _event):
        selection = self.session_list.curselection()
        if not self.busy and not self.running and selection:
            key = self.visible_sessions[selection[0]]["id"]
            if key != self.session:
                self.open_session(key)
        self.update_delete_actions()

    def open_session(self, session_id):
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.update_session_grant_ui()
        self.save_draft()
        self.session = session_id
        self.reset_conversation()
        self.restore_draft()
        local = self.history.get(session_id)
        if local:
            for event in local.get("events", []):
                self.render_event(event)
            self.end_reply()
        if not local and self.engine:
            self.worker("读取会话", lambda: self.engine.history(session_id), self.render_history)

    def render_history(self, messages):
        for message in messages:
            info = message.get("info", message)
            self.end_reply()
            for part in message.get("parts", []):
                if part.get("type") == "text":
                    kind = "user" if info.get("role") == "user" else "delta"
                    self.render_event({"type": kind, "text": part.get("text", "")})
                if part.get("type") == "tool":
                    state = part.get("state", {})
                    self.render_event({"type": "tool", "key": part.get("id"), "name": part.get("tool"), **state})
        self.end_reply()
        self.context_meter.restore(messages)

    def submit_shortcut(self, _event):
        self.submit()
        return "break"

    def save_draft(self):
        key = (self.project, self.session)
        draft = self.input.get("1.0", "end-1c")
        if draft.strip():
            self.drafts[key] = draft
        else:
            self.drafts.pop(key, None)

    def restore_draft(self):
        self.input.delete("1.0", "end")
        draft = self.drafts.get((self.project, self.session))
        if draft:
            self.input.insert("1.0", draft)

    def submit(self):
        prompt = self.input.get("1.0", "end-1c").strip()
        if not prompt or self.busy or self.running or not self.engine or not self.can_use_agent:
            return
        client, engine, session = self.account_client, self.engine, self.session
        direct = self.connection_mode == "direct"
        known_model = self.model_info
        self.input.configure(state="disabled")

        def work():
            model = known_model
            if direct:
                client.check_open_webui_access()
            elif client:
                self.require_active(client, check_quota=True)
                try:
                    model = client.model()
                except AccountError as error:
                    if error.status in {401, 403}:
                        raise
            limits = self.model_limits(model)
            proxy_url = client.engine_proxy_url() if client and not direct else None
            current = engine
            sessions = None
            try:
                if limits != self.engine_limits or proxy_url != engine.proxy_url:
                    engine.close()
                    current, sessions = self.create_engine(self.project, limits, proxy_url=proxy_url)
                return model, limits, current, sessions, session or current.new_session()
            except Exception:
                if current is not engine:
                    current.close()
                raise

        def prepared(result):
            self.input.configure(state="normal")
            model, limits, self.engine, sessions, session_id = result
            self.engine_limits = limits
            if sessions is not None:
                self.update_sessions(sessions)
            self.update_model(model)
            if not self.can_use_agent:
                self.update_actions()
                return
            if not self.session:
                self.drafts.pop((self.project, None), None)
                self.session = session_id
                self.reset_conversation()
                self.session_search.set("")
            self.run_task(prompt)

        def failed(error):
            self.input.configure(state="normal")
            if not engine.process:
                self.engine = None
            if isinstance(error, AccountError):
                if direct:
                    self.direct_grant_failed(error)
                    return
                self.poll_account()
            self.show_error("无法开始任务", error)

        self.worker("校验身份并准备任务", work, prepared, failed)

    @staticmethod
    def require_active(client, *, check_quota=False):
        user = client.me()
        if user["status"] != "active":
            raise AccountError("账号未激活或已禁用。", 403)
        if check_quota and blocked_message(user):
            raise AccountError(blocked_message(user), 402)

    def run_task(self, prompt):
        session = self.session
        self.active_approval_scope = self.current_approval_scope()
        engine = self.engine
        client = self.account_client
        direct = self.connection_mode == "direct"
        self.input.delete("1.0", "end")
        self.drafts.pop((self.project, session), None)
        self.running = True
        self.stop_requested.clear()
        self.end_reply()
        planning = self.mode.get() == "方案"
        self.task_started = monotonic()
        self.task_elapsed.set("00:00")
        self.task_state.set("方案分析中" if planning else "任务执行中")
        self.update_actions()
        record = self.history.setdefault(session, {"title": "新会话", "events": []})
        if not record["events"] and record.get("title") == "新会话":
            record["title"] = " ".join(prompt.split())[:35]
        self.update_sessions([row for row in self.sessions if row["id"] != session])
        self.track(record, {"type": "user", "text": prompt, "mode": self.mode.get()})
        strict = self.strict.get()

        def on_event(event):
            clean = self.redact(event)
            self.events.put(lambda: self.track(record, clean))
            if event.get("type") == "context" and event.get("state") in {"request_complete", "request_failed"}:
                self.events.put(self.poll_account)

        def work():
            authorize = None
            if direct:
                client.check_open_webui_access()
                authorize = client.check_open_webui_access
            elif client:
                authorize = lambda: self.require_active(client)
            engine.run(session, prompt, on_event, self.approve, strict=strict, read_only=planning,
                       authorize=authorize)

        def done(_result, error=False):
            self.running = False
            self.poll_account()
            stopped = self.stop_requested.is_set()
            status = "已停止" if stopped else ("任务失败" if error else ("方案已生成" if planning else "任务已完成"))
            self.task_state.set(status)
            self.task_started = None
            self.track(record, {"type": "info", "text": status})
            if self.connection_mode == "direct" and not error and not stopped:
                self.direct_state("已连接")

            def show_result(result):
                self.show_changes(result)
                branch, files = result
                self.track(record, {"type": "tool", "name": "任务结束时的项目状态", "status": "completed",
                                    "output": "\n".join([branch, *(f"{item.status}  {item.path}"
                                                    for item in files[:200])])})

            if self.changes and not planning:
                self.worker("刷新文件变更", self.changes.status, show_result)

        def failed(error):
            if direct and isinstance(error, AccountError):
                done(None, True)
                self.direct_grant_failed(error)
                return
            self.track(record, {"type": "error", "text": self.redact(str(error))})
            if self.connection_mode == "direct":
                status = getattr(error, "status", 0)
                if status in {401, 403}:
                    self.direct_ready = False
                    self.direct_state("需要重新登录" if status == 401 else "无模型调用权限")
                elif status == 429:
                    self.direct_state("请求限流")
                elif status in {500, 502, 503, 504}:
                    self.direct_state("服务暂不可用")
                elif getattr(error, "category", "") == "network":
                    self.direct_state("连接超时或断开")
                self.direct_feedback.set(self.redact(str(error)))
            done(None, True)

        self.worker("执行任务", work, done, failed)

    def redact(self, value):
        if not self.token:
            return value
        if isinstance(value, str):
            return value.replace(self.token, "••••")
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def track(self, record, event):
        dedup = event.get("type") == "tool" and event.get("key")
        if dedup:
            previous = next((row for row in reversed(record["events"]) if row.get("key") == dedup), None)
            if previous == event:
                return
        kind = event.get("type")
        items = record["events"]
        if kind == "delta" and items and items[-1].get("type") == "delta":
            items[-1]["text"] += event.get("text", "")
        else:
            items.append(dict(event))
        self.render_event(event)
        if not getattr(self, "pending_save", False):
            self.pending_save = True
            self.schedule("history", 400, self.save_history)

    def save_history(self):
        self.pending_save = False
        if self.history_path:
            try:
                write_json(self.history_path, self.history)
            except OSError as error:
                self.status.set(f"历史保存失败 · {error.strerror}")

    def render_event(self, event):
        kind = event.get("type")
        value = event.get("text", "")
        if kind == "delta":
            if not value:
                return
            if not self.live_text:
                self.append(self.chat, "\nCCSLab\n", "speaker")
                self.chat.mark_set("reply_start", "end-1c")
                self.chat.mark_gravity("reply_start", "left")
                self.live_text = True
            self.live_markdown += value
            if self.render_later is None:
                self.render_later = self.root.after(55, self.draw_reply)
        elif kind == "user":
            self.end_reply()
            self.context_meter.record({"state": "task_started"})
            mode = "  ·  方案" if event.get("mode") == "方案" else ""
            self.append(self.chat, "\n你" + mode + "\n", "speaker")
            self.append(self.chat, value + "\n", "user")
        elif kind == "tool":
            if event.get("status") in {"pending", "running"}:
                self.end_reply()
            self.activity.record(event)
        elif kind == "context":
            self.context_meter.record(event)
        elif kind in ("info", "error"):
            self.end_reply()
            if kind == "error":
                self.context_meter.record({"state": "request_failed"})
            if value in {"已停止", "任务失败", "任务已结束", "任务已完成", "方案已生成"}:
                self.activity.finish(interrupted=value == "已停止")
                self.context_meter.finish()
            self.append(self.chat, "\n" + value + "\n", "remove" if kind == "error" else "muted")

    def draw_reply(self):
        self.render_later = None
        if not self.live_text:
            return
        top, bottom = self.chat.yview()
        self.chat.configure(state="normal")
        try:
            self.chat.delete("reply_start", "end-1c")
            insert_markdown(self.chat, self.live_markdown)
        finally:
            self.chat.configure(state="disabled")
        if bottom >= 0.98:
            self.chat.see("end")
        else:
            self.chat.yview_moveto(top)

    def end_reply(self):
        if self.render_later is not None:
            self.root.after_cancel(self.render_later)
            self.draw_reply()
        if self.live_text:
            self.chat.mark_unset("reply_start")
        self.live_text = False
        self.live_markdown = ""

    def reset_conversation(self):
        self.end_reply()
        self.clear_text(self.chat)
        self.activity.clear()
        self.context_meter.reset()
        self.task_elapsed.set("")
        self.task_state.set("")

    def approve(self, request):
        scope = self.active_approval_scope
        if self.session_grants.allows(scope, request):
            return True
        ticket = {"id": uuid4().hex, "request": dict(request), "scope": scope,
                  "result": {"allow": False}, "completed": Event(), "guard": Lock()}
        with self.approval_lock:
            self.approval_tickets[ticket["id"]] = ticket
        self.events.put(lambda ticket=ticket: self.enqueue_approval(ticket))
        while not ticket["completed"].wait(0.1):
            if self.stop_requested.is_set() or self.closing:
                self.events.put(lambda ticket=ticket: self.cancel_approval_ticket(ticket))
                return False
        return ticket["result"]["allow"]

    def stop(self):
        if not self.running or self.stop_requested.is_set():
            return
        self.stop_requested.set()
        self.cancel_all_approvals()
        self.task_state.set("正在停止")
        self.update_actions()

        def abort():
            try:
                self.engine.stop(self.session)
            except Exception as error:
                self.events.put(lambda error=error: self.show_error("停止任务", error))

        Thread(target=abort, daemon=True).start()

    def refresh_changes(self):
        if self.changes:
            self.worker("刷新文件变更", self.changes.status, self.show_changes)

    def show_changes(self, result):
        branch, self.change_rows = result
        self.branch.set(f"{branch}  ·  {len(self.change_rows)} 项变更")
        self.render_change_list()

    def render_change_list(self):
        query = self.change_filter.get().strip().casefold()
        self.visible_changes = [
            item for item in self.change_rows if query in item.path.casefold() or query in item.previous.casefold()
        ]
        self.change_list.delete(0, "end")
        for change in self.visible_changes:
            label = f"{change.status.strip()}  {change.path}"
            self.change_list.insert("end", label)
        self.diff_text = ""
        self.clear_text(self.diff)
        warning = self.changes.warning if self.changes else ""
        if query:
            self.result_status.set(f"{len(self.visible_changes)} 项匹配" if self.visible_changes else "没有匹配的文件")
        else:
            self.result_status.set(warning or ("选择文件查看差异" if self.change_rows else "暂无文件变更"))
        self.update_actions()

    def select_change(self, _event):
        selection = self.change_list.curselection()
        if not selection or self.busy or self.running:
            return
        change = self.visible_changes[selection[0]]

        def done(diff):
            current = self.change_list.curselection()
            if current != selection or self.visible_changes[current[0]] != change:
                self.select_change(None)
                return
            self.diff_text = diff
            added, removed = self.render_diff(self.diff)
            self.result_status.set(f"+{added} / −{removed}   {change.path}" if added or removed else change.path)

        self.worker("读取差异", lambda: self.changes.diff(change), done)

    def render_diff(self, box):
        added, removed = 0, 0
        box.configure(state="normal")
        try:
            box.delete("1.0", "end")
            for line in self.diff_text.splitlines(keepends=True):
                tag = ""
                if line.startswith(("diff ", "index ", "@@", "〔", "+++ ", "--- ")):
                    tag = "heading"
                elif line.startswith("+"):
                    tag = "add"
                    added += 1
                elif line.startswith("-"):
                    tag = "remove"
                    removed += 1
                box.insert("end", line, tag)
        finally:
            box.configure(state="disabled")
        box.see("1.0")
        return added, removed

    def copy_diff(self):
        if self.diff_text:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.diff_text)

    def expand_diff(self):
        if not self.diff_text:
            return
        window = tk.Toplevel(self.root)
        window.title("文件差异")
        window.transient(self.root)
        window.geometry("960x640")
        self.skin(window, "panel", None)
        self.label(window, variable=self.result_status, bold=True).pack(fill="x", padx=16, pady=10)
        holder, box = self.text_panel(window, wrap="none", mono=True)
        holder.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.button(window, "复制 Diff", self.copy_diff).pack(anchor="e", padx=16, pady=(0, 12))
        window.bind("<Escape>", lambda _event: window.destroy())
        self.apply_theme()
        self.diff_theme(box, self.colors)
        self.render_diff(box)
        window.grab_set()

    def close(self):
        if self.busy and not self.running:
            self.schedule("close", 200, self.close)
            return
        if self.running and not messagebox.askyesno("关闭应用", "停止当前任务并退出？", parent=self.root):
            return
        self.closing = True
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        if self.account_client:
            self.account_client.clear_open_webui_access()
        self.end_reply()
        for timer in self.timers.values():
            self.root.after_cancel(timer)
        self.timers.clear()
        if self.approval_restore_after:
            self.root.after_cancel(self.approval_restore_after)
            self.approval_restore_after = None
        self.stop_requested.set()
        self.save_history()
        try:
            if self.engine:
                self.engine.close()
        finally:
            if self.connection_mode == "direct":
                self.token = ""
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(prog="CCSLab Labcoding")
    parser.add_argument("--smoke-test", action="store_true", help="initialize the UI and exit")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass
    root = tk.Tk()
    app = App(root, args.data_dir, args.smoke_test)
    if args.smoke_test:
        app.schedule("smoke", 350, app.close)
    root.mainloop()


if __name__ == "__main__":
    main()
