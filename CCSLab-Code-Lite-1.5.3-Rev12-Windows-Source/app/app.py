"""CCSLab Code Lite desktop application."""

import argparse
from collections import deque
from copy import copy
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic, time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk
from uuid import uuid4
from versioning import read_version_info
from update_system import (
    UpdateError, UpdateNetworkError, UpdateService, UpdateTrustError,
)

from changes import Change, Changes
from tasks import ACTIVE, EXECUTING, ProjectContext, SessionTask, TaskScheduler, canonical
from session_indicators import SessionIndicators
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
from output_settings import load_output_limit, set_output_limit


FONT = "Microsoft YaHei UI"
APP_VERSION_INFO = read_version_info()
WORKER_TIMEOUT = 90
SHUTDOWN_TIMEOUT = 8
IDENTITY_TRANSITION_TIMEOUT = 15
POLL_TIMEOUT = 45


def _colorref(value):
    value = value.lstrip("#")
    red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def apply_windows_titlebar(window, colors):
    """Apply supported DWM title-bar attributes to Tk's actual top-level HWND."""
    try:
        if window.tk.call("tk", "windowingsystem") not in ("win", "win32"):
            return False
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd = user32.GetAncestor(wintypes.HWND(window.winfo_id()), 2)
        if not hwnd:
            return False
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = wintypes.HRESULT

        def set_attribute(attribute, value):
            return dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0

        rgb = colors["bg"].lstrip("#")
        dark = wintypes.BOOL(sum(int(rgb[index:index + 2], 16) for index in (0, 2, 4)) < 435)
        dark_ok = set_attribute(20, dark) or set_attribute(19, dark)
        caption = wintypes.DWORD(_colorref(colors["header"]))
        foreground = wintypes.DWORD(_colorref(colors["text"]))
        caption_ok = set_attribute(35, caption)
        text_ok = set_attribute(36, foreground)
        return dark_ok or caption_ok or text_ok
    except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
        # Unsupported Windows builds retain the native title bar and all WM behavior.
        return False


class OverlayScrollbar:
    """A narrow, auto-hiding Canvas thumb layered over an existing y/x view."""

    def __init__(self, app, parent, target, orient="vertical", width=7, *,
                 always_visible=False, show_on_hover=True, opacity=1.0):
        self.app = app
        self.parent = parent
        self.target = target
        self.orient = orient
        self.width = width
        self.always_visible, self.show_on_hover, self.opacity = always_visible, show_on_hover, opacity
        self.colors = app.colors
        self.first = 0.0
        self.last = 1.0
        self.thumb_start = 0
        self.thumb_size = 0
        self.drag_offset = 0
        self.dragging = False
        self.hovered = False
        self.wheel_delta = 0
        self.hide_after = None
        self.hide_token = None
        self.closed = False
        self.canvas = tk.Canvas(parent, width=width if orient == "vertical" else 1,
                                height=width if orient == "horizontal" else 1,
                                borderwidth=0, highlightthickness=0, takefocus=False)
        self.thumb = self.canvas.create_rectangle(0, 0, 0, 0, outline="")
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        target_bindings = []
        for sequence, callback in (("<Enter>", self._enter), ("<Leave>", self._leave),
                                   ("<Destroy>", self._target_destroy)):
            binding = target.bind(sequence, callback, add="+")
            if binding:
                target_bindings.append((sequence, binding))
        self.target_bindings = target_bindings
        parent.bind("<Destroy>", self._parent_destroy, add="+")
        self.parent_binding = None
        self.refresh_command = target.yview if orient == "vertical" else target.xview
        target.configure(**{"yscrollcommand" if orient == "vertical" else "xscrollcommand": self.set})
        self.theme(self.colors)

    def theme(self, colors):
        self.colors = colors
        try:
            self.canvas.configure(background=self.parent.cget("background"))
            self.canvas.itemconfigure(self.thumb, fill=self._thumb_color())
            self._draw()
        except tk.TclError:
            self.close()

    def _place(self):
        if self.closed:
            return
        if self.orient == "vertical":
            self.canvas.place(relx=1.0, x=-self.width, y=2, relheight=1.0,
                              height=-4, width=self.width)
        else:
            self.canvas.place(relx=0.0, rely=1.0, x=2, y=-self.width,
                              relwidth=1.0, width=-4, height=self.width)
        self.canvas.tk.call("raise", self.canvas._w)

    def set(self, first, last):
        if self.closed:
            return
        try:
            self.first, self.last = float(first), float(last)
            indicators = getattr(self.app, "session_indicators", None)
            if indicators and self.target is indicators.listing:
                indicators.draw()
            if self.last - self.first >= .999 and not self.always_visible:
                self._cancel_hide()
                self.canvas.place_forget()
                return
            self._place()
            self._draw()
            if not self.dragging and (not self.hovered or not self.show_on_hover):
                self._schedule_hide()
        except (tk.TclError, TypeError, ValueError):
            self.close()

    def _draw(self):
        if self.closed or not self.canvas.winfo_exists():
            return
        if self.orient == "vertical":
            length = max(0, self.canvas.winfo_height())
        else:
            length = max(0, self.canvas.winfo_width())
        if length <= 1:
            return
        minimum = min(24, length)
        self.thumb_size = max(minimum, round(length * max(0.0, self.last - self.first)))
        self.thumb_size = min(length, self.thumb_size)
        travel = max(0, length - self.thumb_size)
        self.thumb_start = round(travel * self.first / max(.0001, 1.0 - (self.last - self.first)))
        self.thumb_start = min(travel, max(0, self.thumb_start))
        if self.orient == "vertical":
            self.canvas.coords(self.thumb, 1, self.thumb_start, self.width - 1,
                               self.thumb_start + self.thumb_size)
        else:
            self.canvas.coords(self.thumb, self.thumb_start, 1,
                               self.thumb_start + self.thumb_size, self.width - 1)

    def _thumb_color(self, hover=False):
        foreground = self.canvas.winfo_rgb(self.colors["scrollbar_hover" if hover else "scrollbar_thumb"])
        background = self.canvas.winfo_rgb(self.canvas.cget("background"))
        return "#" + "".join(f"{round((a*self.opacity+b*(1-self.opacity))/257):02x}"
                              for a, b in zip(foreground, background))

    def _enter(self, _event=None):
        self.hovered = True
        if self.show_on_hover:
            self._cancel_hide()
        if (self.always_visible or self.last - self.first < .999) and (
                self.show_on_hover or self.canvas.winfo_ismapped()):
            self._place()
            self._draw()
            self.canvas.itemconfigure(self.thumb, fill=self._thumb_color(True))

    def _leave(self, _event=None):
        self.hovered = False
        if not self.dragging:
            self._schedule_hide()

    def _schedule_hide(self):
        self._cancel_hide()
        if self.always_visible:
            return
        try:
            self.app.overlay_hide_serial += 1
            token = str(self.app.overlay_hide_serial)
            self.app.overlay_hide_targets[token] = self
            self.hide_token = token
            self.hide_after = self.app.root.tk.call(
                "after", 850, self.app.overlay_hide_command, token)
        except tk.TclError:
            self.close()

    def _cancel_hide(self):
        if self.hide_after is not None:
            try:
                self.app.root.tk.call("after", "cancel", self.hide_after)
            except tk.TclError:
                pass
            self.hide_after = None
        if self.hide_token is not None:
            self.app.overlay_hide_targets.pop(self.hide_token, None)
            self.hide_token = None

    def _hide(self):
        self._cancel_hide()
        if (not self.always_visible and (not self.hovered or not self.show_on_hover)
                and not self.dragging and not self.closed):
            try:
                self.canvas.place_forget()
            except tk.TclError:
                self.close()

    def _press(self, event):
        coordinate = event.y if self.orient == "vertical" else event.x
        if not (self.thumb_start <= coordinate <= self.thumb_start + self.thumb_size):
            self._move_to(coordinate - self.thumb_size / 2)
        self.dragging = True
        self.drag_offset = coordinate - self.thumb_start
        self.canvas.itemconfigure(self.thumb, fill=self._thumb_color(True))

    def _drag(self, event):
        if not self.dragging:
            return
        coordinate = event.y if self.orient == "vertical" else event.x
        self._move_to(coordinate - self.drag_offset)

    def _move_to(self, start):
        length = self.canvas.winfo_height() if self.orient == "vertical" else self.canvas.winfo_width()
        travel = max(1, length - self.thumb_size)
        fraction = min(1.0, max(0.0, start / travel))
        try:
            view = self.target.yview_moveto if self.orient == "vertical" else self.target.xview_moveto
            view(fraction)
        except tk.TclError:
            self.close()

    def _release(self, _event):
        self.dragging = False
        if self.hovered:
            self.canvas.itemconfigure(self.thumb, fill=self._thumb_color(True))
        else:
            self.canvas.itemconfigure(self.thumb, fill=self._thumb_color())
        if not self.hovered or not self.show_on_hover:
            self._schedule_hide()

    def _wheel(self, event):
        if self.orient == "vertical" and event.delta:
            self.wheel_delta += event.delta
            steps = int(self.wheel_delta / 120)
            self.wheel_delta -= steps * 120
            if not steps:
                return "break"
            try:
                self.target.yview_scroll(-steps, "units")
            except tk.TclError:
                self.close()
            return "break"
        return None

    def _target_destroy(self, event):
        if event.widget is self.target:
            self.close()

    def _parent_destroy(self, event):
        if event.widget is self.parent:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cancel_hide()
        for sequence, binding in self.target_bindings:
            try:
                self.target.unbind(sequence, binding)
            except tk.TclError:
                pass
        try:
            if self.canvas.winfo_exists():
                self.canvas.destroy()
        except tk.TclError:
            pass
        self.app.overlay_scrollbars = [item for item in self.app.overlay_scrollbars if item is not self]


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


def update_check_error_message(error):
    reason = " ".join(str(error).split())[:180]
    if isinstance(error, UpdateNetworkError):
        return f"网络连接失败：{reason}"
    if isinstance(error, UpdateTrustError):
        return f"更新检查失败：安全验证未通过（{reason}）"
    if isinstance(error, UpdateError):
        return f"更新检查失败：{reason}"
    return f"更新检查失败：{reason or type(error).__name__}"


class App:
    def __init__(self, root, data_dir=None, smoke=False):
        self.root = root
        self.data_root = data_dir or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CCSLabCodeLite" / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.version_info = APP_VERSION_INFO
        self.update_service = UpdateService(self.data_root, self.version_info)
        self.update_candidate = None
        self.update_package = None
        self.update_cancel = Event()
        self.update_checking = False
        self.update_downloading = False
        self.update_snoozed_until = 0
        self.update_status = tk.StringVar(value="尚未检查")
        self.update_found_version = tk.StringVar(value="")
        self.update_release_notes = tk.StringVar(value="")
        self.update_security_notice = tk.StringVar(value="")
        self.update_download_status = tk.StringVar(value="")
        self.settings_update_cleanup = None
        self.update_settings_release_section = None
        self.update_settings_notes_frame = None
        self.update_settings_notes_text = None
        self.update_settings_download_status_label = None
        self.update_settings_download_actions = None
        self.state = self.data_root
        self.prefs_path = self.data_root / "preferences.json"
        self.prefs = read_json(self.prefs_path, {})
        self.output_limit = load_output_limit(self.prefs)
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
        self.task_history_index = None
        self.task_history_scope = None
        self.task_history_draft = ""
        self.session_titles = {}
        self.drafts = {}
        self.events = Queue()
        self.tasks = TaskScheduler()
        self.task_stats = {}
        self.task_stats_polling = False
        self.project_contexts = {}
        self.collapsed_projects = set()
        self.search_collapsed_projects = set()
        self.tree_search_query = ""
        self.dirty_contexts = {}
        self.identity_transition = False
        self.shutdown_waiting = False
        self.approval_queue = deque()
        self.active_approval = None
        self.approval_tickets = {}
        self.approval_lock = Lock()
        self.session_grants = SessionGrants()
        self.active_approval_scope = None
        self.timers = {}
        self.busy = ""
        self.worker_job = None
        self.shutdown_deadline = None
        self.running = False  # Selected-session presentation only; TaskScheduler owns execution state.
        self.closing = False
        self.stop_requested = Event()
        self.live_text = False
        self.reply_copy_icons = []
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
        self.overlay_scrollbars = []
        self.overlay_hide_targets = {}
        self.overlay_hide_serial = 0
        self.overlay_hide_command = self.root.register(self._dispatch_overlay_hide)
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.colors = THEMES[theme_key(self.theme.get())][1]
        self.titlebar_after = {}
        root.title("CCSLab Labcoding")
        apply_window_icon(root)
        root.geometry(f"{min(1748, root.winfo_screenwidth() - 70)}x{min(1228, root.winfo_screenheight() - 130)}")
        root.minsize(760, 540)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind_all("<Map>", self.apply_titlebar_to_mapped_window, add="+")
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
            self.schedule("task_stats", 100, self.poll_task_stats)
            self.schedule("legacy", 100, self.remove_legacy_key)
            self.schedule("connection", 3500, self.monitor_account)
            self.schedule("gateway_check", 100, self.check_account_connection)
            self.schedule("saved_login", 250, self.restore_saved_login)
            self.schedule("update_check", 6000, lambda: self.check_for_updates(manual=False))
            self.schedule("update_periodic", 24 * 60 * 60 * 1000, self.periodic_update_check)

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
        semantic = dict(kwargs)
        for option in ("background", "highlightbackground", "highlightcolor"):
            value = kwargs.get(option)
            if isinstance(value, str) and value in self.colors:
                kwargs[option] = self.colors[value]
        semantic_colors = {key: value for key, value in semantic.items()
                           if key in {"background", "highlightbackground", "highlightcolor"}
                           and isinstance(value, str) and value in self.colors}
        return self.skin(tk.Frame(parent, **kwargs), bg, None, **semantic_colors)

    def button(self, parent, text, command, style="TButton"):
        return ttk.Button(parent, text=text, command=command, style=style)

    def text_panel(self, parent, *, wrap="word", mono=False, editable=False, height=6,
                   background="field", text_size=10):
        holder = self.frame(parent, background)
        box = tk.Text(holder, wrap=wrap, undo=editable, height=height, borderwidth=0,
                      highlightthickness=0, padx=12, pady=9,
                      font=("Consolas" if mono else FONT, text_size), spacing1=2, spacing3=3)
        self.skin(box, background, "text", insertbackground="text", selectbackground="border")
        self.overlay_scrollbar(holder, box)
        if wrap == "none":
            self.overlay_scrollbar(holder, box, orient="horizontal")
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
        if ticket.get("task_id"):
            task = self.tasks.tasks.get(ticket["task_id"])
            if (not task or task.cancel.is_set() or self.closing or ticket["completed"].is_set()):
                self.cancel_approval_ticket(ticket)
                return
            self.approval_queue.append(ticket)
            self.notify_approval(ticket)
            self.refresh_task_sidebar()
            self.session_indicators.draw()
            self.show_next_approval()
            return
        if (ticket["completed"].is_set() or self.closing or self.stop_requested.is_set()
                or ticket["scope"] != self.active_approval_scope):
            self.cancel_approval_ticket(ticket)
            return
        self.approval_queue.append(ticket)
        self.notify_approval(ticket)
        self.show_next_approval()

    def notify_approval(self, ticket):
        if ticket.get("sound_notified"):
            return
        ticket["sound_notified"] = True
        try:
            import winsound
            # MessageBeep queues the Windows sound without waiting for playback.
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError) as error:
            self.record_client_state("approval_sound_unavailable", error=error)

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
        for _ in range(len(self.approval_queue)):
            if self.active_approval:
                break
            ticket = self.approval_queue.popleft()
            if ticket["completed"].is_set():
                continue
            if ticket.get("task_id"):
                task = self.tasks.tasks.get(ticket["task_id"])
                if not task or task.cancel.is_set():
                    self.cancel_approval_ticket(ticket)
                    continue
                if not self.task_is_current(task) or self.current_page != "workspace":
                    self.approval_queue.append(ticket)
                    continue
            elif ticket["scope"] != self.active_approval_scope:
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
        task = self.tasks.tasks.get(ticket.get("task_id")) if getattr(self, "tasks", None) else None
        preview = (task.redact if task else self.redact)("\n\n".join(details))
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
        self.refresh_task_sidebar()
        self.session_indicators.draw()
        self.task_state.set("等待审批")
        self.approval_panel.pack(fill="x", padx=12, pady=(4, 0), before=self.composer_anchor)
        if self.approval_key_bindings is None:
            self.approval_key_bindings = (
                self.root.bind_all("<KeyPress-Return>", self.approval_enter, add="+"),
                self.root.bind_all("<KeyRelease-Return>", self.approval_enter_released, add="+"),
                self.root.bind_all("<KeyPress-Escape>", self.approval_escape, add="+"),
                self.root.bind_all("<KeyRelease-Escape>", self.approval_enter_released, add="+"),
            )
        self.schedule("approval_focus", 0, lambda ticket_id=ticket["id"]: self.focus_approval(ticket_id))

    def focus_approval(self, ticket_id):
        ticket = self.active_approval
        if (ticket and ticket["id"] == ticket_id and self.approval_panel.winfo_exists()
                and self.approval_panel.winfo_ismapped()):
            self.root.focus_force()
            self.approval_once_button.focus_set()

    def finish_approval(self, allow=False, *, session=False, ticket_id=None):
        ticket = self.active_approval
        if ticket and ticket.get("task_id"):
            task = self.tasks.tasks.get(ticket["task_id"])
            if not task or not self.task_is_current(task) or self.current_page != "workspace":
                return
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
        self.refresh_task_sidebar()
        self.session_indicators.draw()
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
        return self.skin(result, background, "text", selectbackground="selection", selectforeground="heading")

    def scroll_list(self, parent, height, background="field", *, horizontal=False):
        holder = self.frame(parent, background)
        listing = self.list_widget(holder, height, background)
        listing.pack(side="left", fill="both", expand=True, pady=(0, 9) if horizontal else 0)
        self.overlay_scrollbar(holder, listing, show_on_hover=not horizontal, opacity=.55 if horizontal else 1)
        if horizontal:
            self.overlay_scrollbar(holder, listing, orient="horizontal", always_visible=True, opacity=.55)
        return holder, listing

    def overlay_scrollbar(self, parent, target, orient="vertical", **options):
        scrollbar = OverlayScrollbar(self, parent, target, orient=orient, **options)
        self.overlay_scrollbars.append(scrollbar)
        return scrollbar

    def _dispatch_overlay_hide(self, token):
        scrollbar = self.overlay_hide_targets.pop(token, None)
        if scrollbar is not None:
            scrollbar._hide()

    def apply_titlebar_to_mapped_window(self, event):
        try:
            window = event.widget
            if isinstance(window, (tk.Tk, tk.Toplevel)) and window.winfo_exists():
                self.schedule_titlebar(window)
                if window is self.root and self.current_page == "workspace":
                    self.schedule_workspace_layout(650)
        except tk.TclError:
            pass

    def schedule_titlebar(self, window):
        key = str(window)
        previous = self.titlebar_after.pop(key, None)
        if previous:
            try:
                previous[0].after_cancel(previous[1])
            except tk.TclError:
                pass

        def apply():
            self.titlebar_after.pop(key, None)
            try:
                if window.winfo_exists():
                    apply_windows_titlebar(window, self.colors)
            except tk.TclError:
                pass

        try:
            self.titlebar_after[key] = (window, window.after_idle(apply))
        except tk.TclError:
            pass

    def build(self):
        self.skin(self.root, "bg", None)
        bar = self.frame(self.root, "header")
        bar.pack(fill="x")
        self.label(bar, "CCSLab Labcoding", bg="header", size=14, bold=True).pack(
            side="left", padx=(16, 16), pady=10)
        self.workspace_button = self.button(bar, "工作台", lambda: self.show_page("workspace"), "Header.Nav.TButton")
        self.workspace_button.pack(side="left", padx=(0, 4))
        footer = self.frame(self.root)
        footer.pack(side="bottom", fill="x")
        self.settings_button = self.button(footer, "设置", lambda: self.show_page("settings"), "Nav.TButton")
        self.settings_button.configure(compound="right", width=0)
        self.settings_button.pack(side="left", padx=(12, 10), pady=7)
        self.settings_icon_palette = None
        self.quota_label = self.label(footer, variable=self.quota_text, color="heading", size=9)
        self.admin_button = self.button(bar, "管理控制台", lambda: self.show_page("admin"), "Header.Nav.TButton")
        self.connection_button = self.button(bar, "登录 / 注册", self.account_action, "Header.Nav.TButton")
        self.connection_button.pack(side="right", padx=(7, 15))
        self.role_button = self.button(bar, "切换角色", self.change_role, "Header.Nav.TButton")
        theme_gap = round((72 / 2.54) * float(self.root.tk.call("tk", "scaling")))
        identity = self.frame(bar, "header")
        identity.pack(side="right", padx=(6, 4))
        self.theme_control = self.frame(identity, "header")
        self.theme_control.pack(side="left")
        self.theme_label = self.button(self.theme_control, "更换主题色", self.cycle_theme,
                                       "HeaderTheme.TButton")
        self.theme_label.pack(side="left", padx=(0, 4))
        self.theme_label.configure(cursor="hand2")
        self.theme_icon = ActionIcon(self, self.theme_control, "sun", self.cycle_theme)
        self.theme_icon.pack(side="left")
        self.status_label = self.label(identity, variable=self.status, muted=True, bg="header")
        self.status_label.pack(side="left", padx=(theme_gap, 0))
        self.page_host = self.frame(self.root, "bg")
        self.page_host.pack(fill="both", expand=True)
        panes = self.workspace_view = tk.PanedWindow(
            self.page_host, orient="horizontal", bd=0, sashwidth=1, sashpad=4,
            sashcursor="sb_h_double_arrow", showhandle=False, opaqueresize=True,
        )
        self.workspace_sizes = {name: value for name in ("left", "right")
                                if type(value := self.prefs.get(f"workspace_{name}_width")) is int
                                and 160 <= value <= 10000}
        self.workspace_dragging = False
        self.task_resize_start = None
        panes.bind("<ButtonPress-1>", self.begin_workspace_resize, add="+")
        panes.bind("<ButtonRelease-1>", self.end_workspace_resize, add="+")
        self._workspace_width = 0
        self.workspace_layout_attempts = 0
        self.root.bind("<Configure>", self.on_root_configure, add="+")
        self.current_page = None
        self.page_panel = None
        self.page_controller = None
        self.settings_admin = False
        self.skin(panes, "bg", None)
        side = self.workspace_side = self.frame(panes, "side")
        center = self.frame(panes)
        right = self.results_pane = self.frame(panes, "side")
        panes.add(side, width=296, minsize=160, stretch="always")
        panes.add(center, minsize=320, stretch="always")
        if self.results_visible:
            panes.add(right, width=300, minsize=205, stretch="always")
        self.project_group = self.frame(side, "sidebar_card", highlightthickness=1,
                                        highlightbackground="sidebar_card_line", highlightcolor="focus")
        side.rowconfigure(0, weight=1)
        side.columnconfigure(0, weight=1)
        self.project_group.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        project_head = self.frame(self.project_group, "sidebar_card")
        project_head.pack(fill="x", padx=10, pady=(9, 3))
        self.label(project_head, "项目", bg="sidebar_card", size=12, bold=True).pack(side="left")
        project_actions = self.frame(self.project_group, "sidebar_card")
        project_actions.pack(fill="x", padx=8, pady=(3, 5))
        project_actions.columnconfigure(0, weight=1, uniform="project-buttons")
        project_actions.columnconfigure(1, weight=1, uniform="project-buttons")
        self.new_project_button = self.button(project_actions, "＋ 新建项目", self.new_project)
        self.new_project_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.open_button = self.button(project_actions, "打开项目", self.choose_project)
        self.open_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        recent_head = self.frame(self.project_group, "sidebar_card")
        recent_head.pack(fill="x", padx=8, pady=(3, 4))
        recent_head.columnconfigure(0, weight=1, uniform="project-buttons")
        recent_head.columnconfigure(1, weight=1, uniform="project-buttons")
        self.label(recent_head, "最近项目", bold=True, bg="sidebar_card", muted=True).grid(
            row=0, column=0, sticky="w")
        self.remove_recent_button = self.button(recent_head, "删除", self.remove_recent, "Danger.TButton")
        self.remove_recent_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.label(self.project_group, "搜索项目或会话", bg="sidebar_card", muted=True).pack(
            fill="x", padx=8, pady=(3, 4), before=recent_head)
        self.project_search_entry = ttk.Entry(self.project_group, textvariable=self.session_search)
        self.project_search_entry.pack(fill="x", padx=8, pady=(0, 6), before=recent_head)
        tree_panel = self.frame(self.project_group, "sidebar_card")
        tree_panel.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.project_tree = ttk.Treeview(tree_panel, columns=("action",), show="tree", selectmode="browse", height=8,
                                        style="Projects.Treeview")
        self.project_tree.column("#0", width=170, minwidth=60, stretch=True)
        self.project_tree.column("action", width=38, minwidth=38, stretch=False, anchor="center")
        self.project_tree.pack(fill="both", expand=True, padx=(0, 8), pady=(0, 9))
        self.overlay_scrollbar(tree_panel, self.project_tree)
        self.overlay_scrollbar(tree_panel, self.project_tree, orient="horizontal", always_visible=True, opacity=.55)
        self.tree_rows = {}
        self.project_tree.bind("<<TreeviewSelect>>", self.select_project_row)
        self.project_tree.bind("<Button-1>", self.click_project_row, add="+")
        self.project_tree.bind("<Button-3>", self.show_session_menu)
        self.project_tree.bind("<F2>", lambda _event: self.rename_session())
        self.project_tree.bind("<Double-Button-1>", lambda _event: "break")
        self.project_tree.bind("<Left>", lambda _event: "break")
        self.session_menu = self.skin(tk.Menu(self.root, tearoff=False, font=(FONT, 10)),
                                      "panel", "text", activebackground="soft", activeforeground="heading",
                                      disabledforeground="muted")
        self.session_menu.add_command(label="重命名")
        self.session_menu.add_command(label="删除会话", command=self.delete_session)
        self.session_search.trace_add("write", lambda *_args: self.render_session_list())
        self.session_group = self.frame(side, "sidebar_card", highlightthickness=1,
                                        highlightbackground="sidebar_card_line", highlightcolor="focus")
        self.session_group.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.task_resize_handle = self.frame(side, "side", height=12, cursor="sb_v_double_arrow")
        self.task_resize_handle.grid(row=1, column=0, sticky="ew", padx=8)
        task_resize_line = self.frame(self.task_resize_handle, "line", height=1, cursor="sb_v_double_arrow")
        task_resize_line.place(relx=0, rely=.5, relwidth=1, height=1)
        for grip in (self.task_resize_handle, task_resize_line, self.session_group):
            grip.bind("<ButtonPress-1>", self.begin_task_resize)
            grip.bind("<B1-Motion>", self.drag_task_resize)
            grip.bind("<ButtonRelease-1>", self.end_task_resize)
        self.label(self.session_group, "任务执行统计", bg="sidebar_card", size=12, bold=True).pack(
            fill="x", padx=10, pady=(9, 5))
        self.task_stats_text = tk.StringVar(value="服务器执行中：—\n剩余槽位：—\n本用户执行中：—")
        self.label(self.session_group, variable=self.task_stats_text, bg="sidebar_card").pack(
            fill="x", padx=10, pady=(0, 6))
        task_panel, self.running_task_list = self.scroll_list(self.session_group, 2, "sidebar_card", horizontal=True)
        self.task_panel_height = self.running_task_list.winfo_reqheight() + 9 + round(self.root.winfo_fpixels("2c"))
        saved_height = self.prefs.get("task_panel_height")
        if type(saved_height) is int and 20 <= saved_height <= 10000:
            self.task_panel_height = saved_height
        task_panel.configure(height=self.task_panel_height)
        task_panel.pack_propagate(False)
        task_panel.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.running_task_list.bind("<<ListboxSelect>>", self.select_running_task)
        self.running_task_rows = []
        notice_panel, self.approval_notices = self.text_panel(self.session_group, height=2,
                                                             wrap="none", background="sidebar_card", text_size=9)
        notice_panel.pack(fill="x", padx=8, pady=(0, 8))
        self.approval_notice_panel = notice_panel
        notice_panel.pack_forget()
        self.approval_notice_rows = []
        self.session_indicators = SessionIndicators(self, tree_panel, self.project_tree)
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
        self.update_banner = self.frame(center, "soft")
        self.update_message = tk.StringVar(value="")
        update_text = self.label(self.update_banner, variable=self.update_message, bg="soft", color="heading")
        update_text.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        self.update_download_button = self.button(self.update_banner, "\u4e0b\u8f7d\u66f4\u65b0", self.download_update,
                                                  "Accent.TButton")
        self.update_download_button.pack(side="right", padx=(4, 8), pady=5)
        self.update_later_button = self.button(self.update_banner, "\u7a0d\u540e\u63d0\u9192", self.snooze_update)
        self.update_later_button.pack(side="right", padx=4, pady=5)
        self.update_open_folder_button = self.button(
            self.update_banner, "\u6253\u5f00\u5b89\u88c5\u5305\u6240\u5728\u6587\u4ef6\u5939", self.open_update_folder)
        self.update_open_folder_button.pack(side="right", padx=4, pady=5)
        self.update_banner.pack_forget()
        conversation, self.chat = self.text_panel(center, height=1, background="panel", text_size=11)
        conversation.pack(fill="both", expand=True, padx=10)
        self.show_welcome()
        self.build_approval_panel(center)
        self.composer_anchor = self.frame(center, "bg", height=1)
        self.composer_anchor.pack(fill="x", padx=14)
        self.label(center, "输入开发任务", bold=True).pack(fill="x", padx=16, pady=(10, 5))
        composer, self.input = self.text_panel(center, editable=True, height=4)
        self.quota_banner = self.label(self.input, "", color="remove", bg="field")
        self.quota_banner.configure(wraplength=400)
        composer.pack(fill="x", padx=14)
        self.input.bind("<Control-Return>", self.submit_shortcut)
        self.input.bind("<Up>", self.task_history_up)
        self.input.bind("<Down>", self.task_history_down)
        actions = self.frame(center)
        actions.pack(fill="x", padx=14, pady=(7, 12))
        self.send_button = self.button(actions, "发送任务", self.submit, "Accent.TButton")
        self.send_button.pack(side="right")
        self.stop_button = self.button(actions, "停止", self.stop, "Danger.TButton")
        self.stop_button.pack(side="right", padx=8)
        self.mode_picker = ttk.Combobox(actions, textvariable=self.mode, values=("执行", "方案"),
                                        state="readonly", width=6)
        self.mode_picker.pack(side="left")
        self.mode_hint = tk.StringVar(value="允许编辑项目 · Ctrl+Enter")
        self.label(actions, variable=self.mode_hint, muted=True).pack(side="left", padx=(8, 0))
        self.mode.trace_add("write", self.mode_changed)

        rs = self.frame(right, "side")
        rs.pack(fill="x", padx=13, pady=(12, 7))
        self.label(rs, "项目结果", size=13, bold=True, bg="side").pack(side="left")
        self.refresh_button = self.button(rs, "刷新", self.refresh_changes, "Side.TButton")
        self.refresh_button.pack(side="right")
        self.label(right, variable=self.branch, muted=True, bg="side").pack(
            fill="x", padx=13, pady=(0, 8))
        tabs = self.result_tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True, padx=13, pady=(0, 13))
        changes_tab = self.frame(tabs, "result_panel")
        tools_tab = self.frame(tabs, "result_panel")
        tabs.add(changes_tab, text="文件变更")
        tabs.add(tools_tab, text="执行记录")
        tabs.select(tools_tab)
        search = self.frame(changes_tab, "result_panel")
        search.pack(fill="x", pady=(7, 0))
        self.label(search, "筛选", muted=True, size=9, bg="result_panel").pack(side="left", padx=(0, 6))
        ttk.Entry(search, textvariable=self.change_filter).pack(side="left", fill="x", expand=True)
        self.change_filter.trace_add("write", lambda *_args: self.render_change_list())
        changes_panel, self.change_list = self.scroll_list(changes_tab, 7, "result_panel")
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
        if self.closing:
            return
        if page == "workspace" and not (self.can_open_project or self.account_pending):
            page = "direct" if self.connection_mode == "direct" else ("status" if self.account else "access")
        if page == "admin" and not self.is_admin:
            return
        if page == self.current_page and not (force and page == "direct"):
            return
        if self.current_page == "workspace" and page != "workspace":
            self.park_approvals()
        if self.page_panel is self.workspace_view:
            self.workspace_view.pack_forget()
        elif self.page_panel is not None:
            if self.current_page == "settings":
                if self.settings_update_cleanup:
                    self.settings_update_cleanup()
                    self.settings_update_cleanup = None
                for name in ("update_check_button", "update_settings_download_button",
                             "update_settings_later_button", "update_settings_open_folder_button",
                             "update_settings_status_label", "update_settings_version_label",
                             "update_settings_release_section",
                             "update_settings_notes_frame", "update_settings_notes_text",
                             "update_settings_security_label", "update_settings_download_status_label",
                             "update_settings_download_actions"):
                    setattr(self, name, None)
            self.page_panel.destroy()
        self.current_page = page
        self.page_controller = None
        if page == "workspace":
            self.page_panel = self.workspace_view
            self.workspace_view.pack(fill="both", expand=True)
            self._workspace_width = 0
            self.schedule_workspace_layout(250)
            if self.update_candidate:
                self.update_banner.pack(fill="x", padx=18, pady=(0, 8), before=self.context_meter.panel)
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

        if page == "workspace":
            self.show_next_approval()

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
                self.workspace_view.add(self.results_pane, width=300, minsize=205, stretch="always")
            else:
                self.workspace_view.forget(self.results_pane)
            self.results_visible = visible
        self.results_button.configure(text="隐藏结果" if visible else "显示结果")
        self._workspace_width = 0
        self.schedule_workspace_layout(180)

    def on_root_configure(self, event):
        if event.widget is self.root and self.current_page == "workspace":
            self.schedule_workspace_layout(650)

    def schedule_workspace_layout(self, delay=180):
        if not hasattr(self, "root") or not hasattr(self, "timers"):
            return
        self.workspace_layout_attempts = 0
        self.schedule("resize_workspace", delay, self._layout_workspace_panes)

    def _layout_workspace_panes(self):
        if (self.closing or self.current_page != "workspace"
                or self.workspace_dragging or self.task_resize_start is not None):
            return
        try:
            self.root.update_idletasks()
            self._workspace_width = 0
            self.resize_workspace()
            self.resize_task_sidebar()
            self.root.update_idletasks()
            width = self.workspace_view.winfo_width()
            target_sashes = self.workspace_sash_positions(width)
            actual_sashes = tuple(self.workspace_view.sash_coord(index)[0]
                                  for index in range(len(target_sashes)))
            if any(abs(actual - target) > 2
                   for actual, target in zip(actual_sashes, target_sashes)):
                self.workspace_layout_attempts += 1
                if self.workspace_layout_attempts < 8:
                    self.schedule("resize_workspace", 180, self._layout_workspace_panes)
                else:
                    self.workspace_layout_attempts = 0
            else:
                self.workspace_layout_attempts = 0
        except tk.TclError:
            pass

    def task_sidebar_height_bounds(self):
        panel = self.running_task_list.master
        natural = self.running_task_list.winfo_reqheight() + 9
        project_header = self.project_group.winfo_reqheight() - self.project_tree.winfo_reqheight()
        stats_header = self.session_group.winfo_reqheight() - panel.winfo_reqheight()
        row_height = int(self.style.lookup('Treeview', 'rowheight') or 25)
        available = self.workspace_side.winfo_height() - project_header - stats_header - 28 - row_height
        return natural, max(natural, available)

    def resize_task_sidebar(self):
        panel = self.running_task_list.master
        natural, available = self.task_sidebar_height_bounds()
        height = max(natural, min(self.task_panel_height, available))
        if int(panel.cget('height')) != height:
            panel.configure(height=height)

    def begin_workspace_resize(self, event):
        if self.workspace_view.identify(event.x, event.y):
            self.workspace_dragging = True

    def end_workspace_resize(self, _event):
        if self.workspace_dragging:
            self.workspace_dragging = False
            self.schedule("save_workspace_sizes", 0, self.remember_workspace_sizes)

    def remember_workspace_sizes(self):
        self.workspace_view.update_idletasks()
        self.workspace_sizes["left"] = self.workspace_side.winfo_width()
        if self.results_visible:
            self.workspace_sizes["right"] = self.results_pane.winfo_width()
        self.save_workspace_sizes()
        self.schedule_workspace_layout(0)

    def begin_task_resize(self, event):
        if self.closing or (event.widget is self.session_group and event.y > 3):
            return
        self.task_resize_start = event.y_root, self.running_task_list.master.winfo_height()
        self.task_resize_handle.grab_set()
        return "break"

    def drag_task_resize(self, event):
        if self.task_resize_start is None:
            return
        origin, height = self.task_resize_start
        minimum, maximum = self.task_sidebar_height_bounds()
        self.task_panel_height = max(minimum, min(maximum, height + origin - event.y_root))
        self.resize_task_sidebar()
        return "break"

    def end_task_resize(self, event):
        if self.task_resize_start is None:
            return
        self.drag_task_resize(event)
        self.task_resize_start = None
        self.task_resize_handle.grab_release()
        self.save_workspace_sizes()
        self.schedule_workspace_layout(0)
        return "break"

    def save_workspace_sizes(self):
        self.prefs.update({f"workspace_{name}_width": value for name, value in self.workspace_sizes.items()})
        self.prefs["task_panel_height"] = self.task_panel_height
        try:
            self.save_prefs()
        except OSError as error:
            self.show_error("布局保存失败", error)

    def workspace_sash_positions(self, width):
        pad = int(self.workspace_view.cget("sashpad"))
        sash = int(self.workspace_view.cget("sashwidth"))
        gap = pad * 2 + sash
        if self.results_visible:
            side = self.workspace_sizes.get("left", min(320, max(176, int(width * 0.25))))
            right = self.workspace_sizes.get("right", min(336, max(205, int(width * 0.25))))
            side = max(160, min(side, width - 205 - 320 - gap * 2))
            right = max(205, min(right, width - side - 320 - gap * 2))
            return side + pad, width - right - pad - sash
        side = self.workspace_sizes.get("left", min(340, max(176, int(width * 0.30))))
        return (max(160, min(side, width - 320 - gap)) + pad,)

    def resize_workspace(self, _event=None):
        """Keep the three panes usable in compact windows without constraining large ones."""
        try:
            width = self.workspace_view.winfo_width()
        except (AttributeError, tk.TclError):
            return
        if width < 400 or width == self._workspace_width:
            return
        self._workspace_width = width
        sashes = self.workspace_sash_positions(width)
        pad = int(self.workspace_view.cget("sashpad"))
        sash = int(self.workspace_view.cget("sashwidth"))
        self.workspace_view.paneconfigure(self.workspace_side, width=sashes[0] - pad)
        if self.results_visible:
            self.workspace_view.paneconfigure(self.results_pane, width=width - sashes[1] - pad - sash)
        self.workspace_view.update_idletasks()
        for index, position in enumerate(sashes):
            self.workspace_view.sash_place(index, position, 0)
        self.schedule("resize_workspace_idle", 0, self.workspace_view.update_idletasks)

    def apply_theme(self):
        c = self.colors = THEMES[theme_key(self.theme.get())][1]
        self.root.configure(background=c["bg"])
        self.style.configure(".", background=c["panel"], foreground=c["text"], font=(FONT, 10))
        self.style.configure("TButton", padding=(9, 5), background=c["panel"],
                             bordercolor=c["line"], borderwidth=0, relief="flat",
                             focusthickness=2, focuscolor=c["focus"])
        self.style.map("TButton", background=[("disabled", c["panel"]),
                                               ("pressed", c["active"]), ("active", c["hover"])],
                       foreground=[("disabled", c["muted"]), ("active", c["text"])],
                       bordercolor=[("focus", c["focus"])], relief=[("pressed", "flat")])
        self.style.configure("Side.TButton", padding=(9, 5), background=c["side"],
                             foreground=c["muted"], borderwidth=0, relief="flat",
                             focusthickness=2, focuscolor=c["focus"])
        self.style.map("Side.TButton", background=[("disabled", c["side"]),
                                                      ("pressed", c["active"]), ("active", c["hover"])],
                       foreground=[("disabled", c["muted"]), ("active", c["text"])])
        self.style.configure("Accent.TButton", background=c["accent"], foreground=c["ink"],
                             bordercolor=c["accent"], borderwidth=0, relief="flat",
                             padding=(11, 5), focusthickness=2, focuscolor=c["focus"])
        self.style.map("Accent.TButton",
                       background=[("disabled", c["field"]), ("pressed", c["active"]),
                                   ("active", c["accent"])],
                       foreground=[("disabled", c["muted"]), ("active", c["ink"])],
                       bordercolor=[("focus", c["focus"])])
        self.style.configure("Danger.TButton", padding=(8, 5), background=c["danger_bg"],
                             foreground=c["danger"], bordercolor=c["danger_bg"],
                             borderwidth=0, relief="flat", focusthickness=2, focuscolor=c["focus"])
        self.style.map("Danger.TButton", background=[("disabled", c["panel"]),
                                                       ("pressed", c["remove_bg"]),
                                                       ("active", c["remove_bg"])],
                       foreground=[("disabled", c["muted"]), ("active", c["danger"])],
                       bordercolor=[("focus", c["focus"])])
        self.style.configure("Nav.TButton", anchor="w", relief="flat", background=c["panel"],
                             foreground=c["muted"], borderwidth=0, padding=(9, 5), focusthickness=2,
                             focuscolor=c["focus"])
        self.style.map("Nav.TButton", background=[("pressed", c["active"]), ("active", c["hover"])],
                       foreground=[("active", c["text"]), ("disabled", c["muted"])])
        self.style.configure("Header.Nav.TButton", anchor="w", relief="flat", background=c["header"],
                             foreground=c["muted"], borderwidth=0, padding=(9, 5),
                             focusthickness=2, focuscolor=c["focus"])
        self.style.map("Header.Nav.TButton",
                       background=[("pressed", c["active"]), ("active", c["hover"])],
                       foreground=[("active", c["text"]), ("disabled", c["muted"])])
        self.style.configure("Selected.Nav.TButton", background=c["selection"], foreground=c["heading"],
                             font=(FONT, 10, "bold"))
        self.style.map("Selected.Nav.TButton", background=[("pressed", c["active"]),
                                                           ("active", c["selection"])],
                       foreground=[("active", c["heading"])])
        self.style.configure("HeaderTheme.TButton", anchor="w", relief="flat", borderwidth=0,
                             background=c["header"], foreground=c["muted"], padding=(0, 3),
                             focusthickness=2, focuscolor=c["focus"])
        self.style.map("HeaderTheme.TButton", background=[("active", c["hover"])],
                       foreground=[("active", c["heading"])])
        self.style.configure("TNotebook", borderwidth=0, bordercolor=c["line"],
                             lightcolor=c["side"], darkcolor=c["side"],
                             background=c["side"], tabmargins=(2, 4, 2, 0), relief="flat")
        # Clam's client element draws a box even with borderwidth=0.
        self.style.layout("TNotebook", [("Notebook.padding", {"sticky": "nswe"})])
        # Clam's Notebook.tab ignores borderwidth and still draws its own outline.
        # Draw text and spacing only; keyboard focus uses an underline, not a box.
        self.style.layout("TNotebook.Tab", [("Notebook.padding", {
            "sticky": "nswe", "children": [("Notebook.label", {"sticky": "nswe"})],
        })])
        self.style.configure("TNotebook.Tab", padding=(9, 5), font=(FONT, 10),
                             background=c["side"], foreground=c["muted"])
        self.style.map("TNotebook.Tab",
                       foreground=[("disabled", c["muted"]), ("selected", c["heading"]),
                                   ("active", c["text"])],
                       font=[("focus", (FONT, 10, "underline"))])
        self.style.configure("TEntry", fieldbackground=c["input"], foreground=c["text"],
                             insertcolor=c["text"], padding=(7, 5), bordercolor=c["line"],
                             lightcolor=c["input"], darkcolor=c["input"], borderwidth=1,
                             focusthickness=1, focuscolor=c["focus"])
        self.style.map("TEntry", fieldbackground=[("disabled", c["panel"])],
                       foreground=[("disabled", c["muted"])],
                       bordercolor=[("focus", c["focus"])])
        self.style.configure("TCombobox", fieldbackground=c["input"], foreground=c["text"],
                             padding=(7, 4), arrowcolor=c["muted"], bordercolor=c["line"],
                             lightcolor=c["input"], darkcolor=c["input"], borderwidth=1,
                             focusthickness=1, focuscolor=c["focus"])
        self.style.map("TCombobox", fieldbackground=[("readonly", c["field"]), ("disabled", c["panel"])],
                       foreground=[("disabled", c["muted"])],
                       selectbackground=[("readonly", c["selection"])],
                       selectforeground=[("readonly", c["heading"])])
        self.style.configure("TSpinbox", fieldbackground=c["input"], foreground=c["text"],
                             arrowcolor=c["muted"], bordercolor=c["line"], padding=(7, 4),
                             focusthickness=1, focuscolor=c["focus"])
        self.style.configure("TCheckbutton", background=c["panel"], foreground=c["text"],
                             focuscolor=c["focus"], padding=(2, 3),
                             indicatorbackground=c["input"], indicatorforeground=c["text"],
                             upperbordercolor=c["line"], lowerbordercolor=c["line"])
        self.style.map("TCheckbutton", background=[("active", c["panel"])],
                       foreground=[("disabled", c["muted"])],
                       indicatorbackground=[("selected", c["accent"]), ("active", c["hover"])],
                       indicatorforeground=[("selected", c["ink"])])
        self.style.configure("TRadiobutton", background=c["panel"], foreground=c["text"],
                             focuscolor=c["focus"], padding=(2, 3),
                             indicatorbackground=c["input"], indicatorforeground=c["text"],
                             upperbordercolor=c["line"], lowerbordercolor=c["line"])
        self.style.map("TRadiobutton", background=[("active", c["panel"])],
                       foreground=[("disabled", c["muted"])],
                       indicatorbackground=[("selected", c["accent"]), ("active", c["hover"])],
                       indicatorforeground=[("selected", c["ink"])])
        self.style.configure("TSeparator", background=c["separator"])
        self.style.configure("TPanedwindow", background=c["bg"])
        try:
            self.workspace_view.configure(background=c["line"], sashwidth=1, sashrelief="flat")
        except tk.TclError:
            pass
        self.style.configure("Treeview", background=c["field"], fieldbackground=c["field"],
                             foreground=c["text"], borderwidth=0, bordercolor=c["line"],
                             lightcolor=c["field"], darkcolor=c["field"], focuscolor=c["line"],
                             relief="flat", rowheight=25)
        self.style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        self.style.layout("Projects.Treeview.Item", [("Treeitem.padding", {
            "sticky": "nswe", "children": [("Treeitem.image", {"side": "left", "sticky": ""}),
                                             ("Treeitem.text", {"side": "left", "sticky": ""})],
        })])
        self.style.map("Treeview", background=[("selected", c["selection"])],
                       foreground=[("selected", c["heading"])])
        self.style.configure("Treeview.Heading", background=c["panel"], foreground=c["muted"],
                             padding=(7, 4), borderwidth=0, relief="flat")
        self.style.map("Treeview.Heading", background=[("active", c["hover"])])
        for orientation, direction, arrows in (
            ("Vertical", "ns", ("uparrow", "downarrow")),
            ("Horizontal", "we", ("leftarrow", "rightarrow")),
        ):
            self.style.layout(f"{orientation}.TScrollbar", [
                (f"{orientation}.Scrollbar.trough", {
                    "sticky": direction,
                    "children": [(f"{orientation}.Scrollbar.thumb", {"sticky": "nswe"})],
                }),
            ])
        for style in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            self.style.configure(style, width=8, background=c["scrollbar"],
                                 troughcolor=c["scroll_track"], bordercolor=c["scroll_track"],
                                 lightcolor=c["scrollbar"], darkcolor=c["scrollbar"],
                                 gripcount=0, arrowsize=0, borderwidth=0, relief="flat")
            self.style.map(style, background=[("pressed", c["focus"]),
                                              ("active", c["scrollbar_hover"])])
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
        active_scrollbars = []
        for scrollbar in self.overlay_scrollbars:
            if not scrollbar.closed:
                try:
                    if scrollbar.parent.winfo_exists():
                        scrollbar.theme(c)
                        active_scrollbars.append(scrollbar)
                except tk.TclError:
                    scrollbar.close()
        self.overlay_scrollbars = active_scrollbars
        for tag in ("muted", "accent", "heading", "add", "remove"):
            self.chat.tag_configure(tag, foreground=c[tag])
        self.chat.tag_configure("speaker", foreground=c["heading"], font=(FONT, 10, "bold"),
                                spacing1=13, spacing3=4)
        self.chat.tag_configure("user", background=c["user_message"], lmargin1=12, lmargin2=12,
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
        for icon in self.reply_copy_icons:
            icon.theme(c)
        if hasattr(self, "session_indicators"):
            self.session_indicators.draw()
        try:
            for window in (self.root, *(item for item in self.root.winfo_children()
                                        if isinstance(item, tk.Toplevel))):
                self.schedule_titlebar(window)
        except tk.TclError:
            pass

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
        try:
            for _ in range(200):
                try:
                    action = self.events.get_nowait()
                except Empty:
                    break
                action()
            self.pump_tasks()
        finally:
            if not self.closing:
                self.schedule("pump", 28, self.pump)

    def record_client_state(self, event, job=None, error=None):
        """Bounded diagnostics: state and stack locations only, never values or exception messages."""
        row = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event,
               "busy": self.busy, "closing": self.closing, "online": self.account_online,
               "engine_ready": self.engine is not None, "identity_transition": self.identity_transition,
               "active_tasks": len(self.tasks.active()), "account_polling": bool(self.account_polling),
               "task_stats_polling": bool(self.task_stats_polling)}
        if error:
            row["error_type"] = type(error).__name__
        stack = traceback.extract_tb(error.__traceback__) if error else []
        if job:
            row.update(operation=job["caption"], elapsed=round(monotonic() - job["started"], 3))
            frame = sys._current_frames().get(job["thread"].ident)
            if frame:
                stack = traceback.extract_stack(frame)
        if stack:
            row["stack"] = [{"file": Path(item.filename).name, "line": item.lineno, "function": item.name}
                            for item in stack]
        try:
            path = self.data_root / "client-diagnostics.jsonl"
            if path.exists() and path.stat().st_size > 1_000_000:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def abandon_worker(self):
        job, self.worker_job = self.worker_job, None
        self.busy = ""
        timer = self.timers.pop("worker_deadline", None)
        if timer:
            self.root.after_cancel(timer)
        if job:
            with job["lock"]:
                job["abandoned"] = True
                result, job["result"] = job["result"], None
            if result and not result[1] and job["discard"]:
                job["discard"](result[0])

    def worker(self, caption, fn, on_done, on_error=None, *, timeout=None, discard=None):
        if self.busy or self.closing:
            return
        job = {"caption": caption, "started": monotonic(), "lock": Lock(), "abandoned": False,
               "result": None, "discard": discard, "generation": self.account_generation}
        self.worker_job = job
        self.busy = caption

        def finish():
            if self.worker_job is not job:
                return
            with job["lock"]:
                result, failed = job["result"]
                job["result"] = None
            self.worker_job = None
            self.busy = ""
            timer = self.timers.pop("worker_deadline", None)
            if timer:
                self.root.after_cancel(timer)
            self.update_actions()
            if self.closing or job["generation"] != self.account_generation:
                if not failed and discard:
                    discard(result)
                return
            try:
                if failed:
                    self.record_client_state("worker_failed", job, result)
                    if on_error:
                        on_error(result)
                    else:
                        self.show_error(caption, result)
                else:
                    on_done(result)
            finally:
                self.update_actions()

        def expired():
            if self.worker_job is not job:
                return
            self.record_client_state("worker_timeout", job)
            self.abandon_worker()
            try:
                error = TimeoutError("操作等待超时，结果尚未确认。请刷新状态后重试；正在执行的任务不会被标记完成。")
                if on_error:
                    on_error(error)
                else:
                    self.status.set(caption + "等待超时，请重试；详情见 client-diagnostics.jsonl")
            finally:
                self.update_actions()

        def run():
            try:
                result, failed = fn(), False
            except Exception as error:
                result, failed = error, True
            with job["lock"]:
                abandoned = job["abandoned"]
                if not abandoned:
                    job["result"] = result, failed
                    self.events.put(finish)
            if abandoned and not failed and discard:
                discard(result)

        job["thread"] = Thread(target=run, daemon=True, name="labcoding-worker")
        try:
            self.update_actions()
            self.schedule("worker_deadline", max(1, int(1000 * (WORKER_TIMEOUT if timeout is None else timeout))), expired)
            job["thread"].start()
        except Exception as error:
            with job["lock"]:
                job["result"] = error, True
            finish()

    def show_error(self, title, error):
        message = str(error).replace(self.token, "••••") if self.token else str(error)
        messagebox.showerror(title, message[:6500], parent=self.root)

    def project_context(self):
        if not self.project:
            return None
        key = str(self.state), canonical(self.project)
        context = self.project_contexts.get(key)
        if context is None:
            path = self.history_path or self.state / (hashlib.sha256(key[1].encode()).hexdigest() + ".json")
            context = ProjectContext(key[0], self.project, path, self.history, self.session_titles,
                                     list(self.sessions), self.changes)
            self.project_contexts[key] = context
        return context

    def current_task(self):
        scheduler = getattr(self, "tasks", None)
        return scheduler.selected(str(self.state), self.project, self.session) if scheduler else None

    def session_task_state(self, session):
        task = self.tasks.selected(str(self.state), self.project, session)
        if task:
            return task.metadata()
        return self.history.get(session, {}).get("task", {"status": "idle"})

    def task_is_current(self, task):
        return task.key == (str(self.state), canonical(self.project), self.session)

    def save_context_later(self, context):
        self.dirty_contexts[context.key] = context
        if not self.closing:
            self.schedule("task_history", 400, self.flush_contexts)

    def flush_contexts(self):
        for key, context in tuple(self.dirty_contexts.items()):
            try:
                write_json(context.history_path, context.history)
            except OSError as error:
                self.status.set(f"历史保存失败 · {error.strerror}")
            else:
                self.dirty_contexts.pop(key, None)

    def task_changed(self, task):
        record = task.context.history[task.session_id]
        metadata = task.metadata()
        record["task"] = metadata
        summaries = record.setdefault("tasks", [])
        previous = next((index for index, item in enumerate(summaries)
                         if item.get("task_id") == task.task_id), None)
        if previous is None:
            summaries.append(metadata)
        else:
            summaries[previous] = metadata
        self.save_context_later(task.context)
        if self.task_is_current(task):
            self.bind_task_view()
        self.render_project_tree()
        self.refresh_task_sidebar()
        self.session_indicators.draw()
        self.schedule("task_stats", 100, self.poll_task_stats)

    def bind_task_view(self):
        task = self.current_task()
        self.running = bool(task and task.status in ACTIVE)
        self.stop_requested = task.cancel if task else Event()
        self.active_approval_scope = task.scope if self.running else None
        self.session_grants = task.grants if task else SessionGrants()
        self.task_started = task.started_mono if task and task.status in EXECUTING else None
        state = task.metadata() if task else self.history.get(self.session, {}).get("task", {})
        labels = {"queued": "等待服务器空余槽位",
                  "running": "方案分析中" if task and task.read_only else "任务执行中",
                  "cancelling": "正在取消 · 等待模型与工具结束",
                  "completed": "方案已生成" if state.get("mode") == "方案" else "任务已完成",
                  "failed": "任务失败" if not state.get("interrupted") else "任务已中断",
                  "cancelled": "已取消"}
        self.task_state.set(labels.get(state.get("status"), ""))
        if state.get("started_at"):
            seconds = max(0, int((state.get("ended_at") or time()) - state["started_at"]))
            self.task_elapsed.set(f"{seconds // 60:02d}:{seconds % 60:02d}")
        else:
            self.task_elapsed.set("")
        if not self.running:
            self.input.configure(state="normal")
        self.update_session_grant_ui()
        self.show_next_approval()
        self.update_actions()

    def park_approvals(self):
        # Changing the viewed session revokes grants as before, but keeps pending requests alive.
        self.session_grants.clear()
        if self.active_approval:
            self.approval_queue.appendleft(self.active_approval)
            self.active_approval = None
        self.approval_panel.pack_forget()
        self.active_approval_scope = None
        self.clear_approval_key_bindings_if_idle()

    def approve_task(self, task, request):
        if task.cancel.is_set():
            return False
        if task.grants.allows(task.scope, request):
            return True
        ticket = {"id": uuid4().hex, "request": dict(request), "scope": task.scope,
                  "task_id": task.task_id, "owner": task.context.owner,
                  "result": {"allow": False}, "completed": Event(), "guard": Lock()}
        with self.approval_lock:
            self.approval_tickets[ticket["id"]] = ticket
        self.events.put(lambda: self.enqueue_approval(ticket))
        while not ticket["completed"].wait(.1):
            if task.cancel.is_set() or self.closing:
                self.events.put(lambda: self.cancel_approval_ticket(ticket))
                return False
        return ticket["result"]["allow"]

    def execute_task(self, task):
        """Background-only: every credential, project, callback and native job is captured."""
        engine = None
        heartbeat_done = Event()
        heartbeat = None
        try:
            model = task.model
            if task.client:
                if task.direct:
                    task.client.check_open_webui_access()
                else:
                    self.require_active(task.client, check_quota=True)
                    while not task.cancel.is_set():
                        if task.client.acquire_task(task.task_id):
                            task.admitted = True
                            task.events.put({"type": "_admitted"})
                            break
                        task.cancel.wait(1)
                    if task.cancel.is_set():
                        return
                    def keep_slot():
                        while not heartbeat_done.wait(10):
                            try:
                                task.client.heartbeat_task(task.task_id)
                            except AccountError as error:
                                task.events.put({"type": "error", "text": "任务槽位续期失败：" + str(error)})
                                self.events.put(lambda: self.cancel_task(task))
                                return
                    heartbeat = Thread(target=keep_slot, daemon=True, name="labcoding-slot-heartbeat")
                    heartbeat.start()
                    try:
                        model = task.client.model()
                    except AccountError as error:
                        if error.status in {401, 403}:
                            raise
            if task.cancel.is_set():
                return
            task.model = model
            task.events.put({"type": "_model", "model": model})
            engine = task.engine = self.create_task_engine(task, self.model_limits(model, self.output_limit))
            if task.cancel.is_set():
                return
            authorize = ((task.client.check_open_webui_access if task.direct else
                          lambda: self.require_active(task.client)) if task.client else None)
            engine.run(task.session_id, task.prompt,
                       lambda event: task.events.put(task.redact(event)),
                       lambda request: self.approve_task(task, request),
                       strict=task.strict, read_only=task.read_only, authorize=authorize,
                       cancel_event=task.cancel)
            if task.context.changes and not task.cancel.is_set():
                task.result = task.context.changes.status()
        except AccountError:
            task.auth_failure = True
            raise
        finally:
            try:
                if engine is not None:
                    engine.close()
            finally:
                task.engine = None
                heartbeat_done.set()
                if heartbeat:
                    heartbeat.join(11)
                if task.admitted:
                    try:
                        task.client.release_task(task.task_id)
                    except AccountError as error:
                        task.events.put({"type": "info", "text": "槽位释放未确认，服务器将自动回收：" + str(error)})

    @staticmethod
    def create_task_engine(task, limits):
        from engine import Engine
        proxy = task.client.engine_proxy_url() if task.client and not task.direct else None
        engine = Engine(Path(task.context.owner), task.api_base, task.token,
                        limits=limits, ca_bundle=task.ca_path, proxy_url=proxy,
                        task_id=task.task_id if task.admitted else None)
        try:
            engine.open_project(task.context.project)
            return engine
        except Exception:
            engine.close()
            raise

    def track_task(self, task, event):
        event = task.redact(event)
        if event.get("type") == "_admitted":
            if not task.cancel.is_set():
                task.status = "running"
                task.started_at, task.started_mono = time(), monotonic()
                self.task_changed(task)
            return
        if event.get("type") == "_model":
            if self.task_is_current(task):
                self.update_model(event["model"])
            return
        if event.get("type") == "_finished":
            self.tasks.finish(task, event.get("error", ""))
            if task.error:
                self.track_task(task, {"type": "error", "text": task.error})
            label = {"completed": "方案已生成" if task.read_only else "任务已完成",
                     "failed": "任务失败", "cancelled": "已停止"}[task.status]
            self.track_task(task, {"type": "info", "text": label})
            if task.result:
                branch, files = task.result
                task.context.history[task.session_id]["changes"] = {
                    "branch": branch, "files": [vars(item) for item in files]}
                self.track_task(task, {"type": "tool", "name": "任务结束时的项目状态", "status": "completed",
                                       "output": "\n".join([branch, *(f"{item.status}  {item.path}" for item in files[:200])])})
                if self.task_is_current(task):
                    self.show_changes(task.result)
            self.task_changed(task)
            if task.direct and task.context.owner == str(self.state) and self.connection_mode == "direct":
                if task.auth_failure and task.error:
                    self.direct_grant_failed(AccountError(task.error, event.get("status_code", 0)))
                    return
                if task.status == "completed":
                    self.direct_state("已连接")
            if (task.error and task.direct and task.context.owner == str(self.state)
                    and self.connection_mode == "direct"):
                status = event.get("status_code", 0)
                if status in {401, 403}:
                    self.direct_ready = False
                    self.direct_state("需要重新登录" if status == 401 else "无模型调用权限")
                elif status == 429:
                    self.direct_state("请求限流")
                elif status in {500, 502, 503, 504}:
                    self.direct_state("服务暂不可用")
                elif event.get("category") == "network":
                    self.direct_state("连接超时或断开")
                self.direct_feedback.set(task.error)
                self.update_actions()
            if task.context.owner == str(self.state) and not self.closing:
                self.poll_account()
            return
        record = task.context.history[task.session_id]
        items = record.setdefault("events", [])
        event.update(task_id=task.task_id, session_id=task.session_id)
        if (event.get("type") == "delta" and items and items[-1].get("type") == "delta"
                and items[-1].get("task_id") == task.task_id):
            items[-1]["text"] += event.get("text", "")
        else:
            items.append(dict(event))
        if self.task_is_current(task) and not self.closing:
            self.render_event(event)
        self.save_context_later(task.context)

    def pump_tasks(self):
        for task in tuple(self.tasks.tasks.values()):
            pending_text = []
            for _ in range(96):
                try:
                    event = task.events.get_nowait()
                except Empty:
                    break
                if event.get("type") == "delta":
                    pending_text.append(event.get("text", ""))
                    continue
                if pending_text:
                    self.track_task(task, {"type": "delta", "text": "".join(pending_text)})
                    pending_text.clear()
                self.track_task(task, event)
            if pending_text:
                self.track_task(task, {"type": "delta", "text": "".join(pending_text)})
            if task.status not in ACTIVE and task.ended_at and time() - task.ended_at > 3 and task.events.empty():
                self.tasks.tasks.pop(task.task_id, None)
                if self.tasks.sessions.get(task.key) is task:
                    self.tasks.sessions.pop(task.key, None)
        if not self.closing:
            for task in self.tasks.start_pending(self.execute_task):
                self.task_changed(task)

    def cancel_task(self, task):
        if not self.tasks.cancel_task(task):
            return
        with self.approval_lock:
            tickets = [ticket for ticket in self.approval_tickets.values()
                       if ticket.get("task_id") == task.task_id]
        for ticket in tickets:
            self.cancel_approval_ticket(ticket)
        if task.status == "cancelled":
            self.track_task(task, {"type": "info", "text": "已取消排队"})
        elif task.engine:
            engine = task.engine
            def abort():
                try:
                    engine.stop(task.session_id)
                except Exception as error:
                    task.events.put({"type": "info", "text": "取消请求未确认，等待执行线程结束：" + task.redact(str(error))})
            Thread(target=abort, daemon=True).start()
        self.task_changed(task)

    def confirm_task_transition(self, label, resume):
        active = self.tasks.active()
        if not active:
            return False
        if self.identity_transition:
            return True
        if not messagebox.askyesno(label, f"{label}会使任务凭据失效。取消 {len(active)} 个后台/排队任务后继续？",
                                  parent=self.root, default=messagebox.NO):
            return True
        self.identity_transition = True
        self.tasks.accepting = False
        for task in active:
            self.cancel_task(task)
        self.status.set("正在取消后台任务，等待身份切换…")
        deadline = monotonic() + IDENTITY_TRANSITION_TIMEOUT
        def wait():
            self.pump_tasks()
            if self.tasks.active():
                if monotonic() >= deadline:
                    self.identity_transition = False
                    self.tasks.accepting = not self.closing
                    self.status.set("任务尚未停止，身份未切换；请稍后重试或关闭应用。")
                    self.record_client_state("identity_transition_timeout")
                    self.update_actions()
                    return
                self.schedule("identity_transition", 100, wait)
                return
            self.identity_transition = False
            self.tasks.accepting = not self.closing
            if not self.closing:
                resume()
        self.schedule("identity_transition", 100, wait)
        self.update_actions()
        return True

    @staticmethod
    def retire_engine(engine):
        if engine:
            Thread(target=engine.close, daemon=True, name="labcoding-engine-close").start()

    def update_actions(self):
        free = not self.busy and not self.closing
        task = self.current_task()
        session_free = not (task and task.status in ACTIVE) and not self.identity_transition
        local = free and self.can_open_project
        for button, active in (
            (self.open_button, local), (self.new_project_button, local), (self.connection_button, free),
            (self.admin_button, not self.closing and self.is_admin),
            (self.role_button, free and self.can_switch_role),
            (self.settings_button, not self.closing), (self.workspace_button, not self.closing),
            (self.send_button, free and session_free and self.engine and self.can_use_agent),
            (self.stop_button, self.running and not self.stop_requested.is_set()),
            (self.refresh_button, free and self.changes),
            (self.diff_copy, bool(self.diff_text)), (self.diff_expand, bool(self.diff_text)),
        ):
            button.configure(state="normal" if active else "disabled")
        self.update_delete_actions()
        if getattr(self, "update_download_button", None):
            downloadable = bool(self.update_candidate and not self.update_downloading and not self.update_package)
            for button in (getattr(self, "update_download_button", None),
                           getattr(self, "update_settings_download_button", None)):
                if button:
                    button.configure(state="normal" if downloadable else "disabled")
            for button in (getattr(self, "update_later_button", None),
                           getattr(self, "update_settings_later_button", None)):
                if button:
                    button.configure(state="normal" if self.update_candidate else "disabled")
            for button in (getattr(self, "update_open_folder_button", None),
                           getattr(self, "update_settings_open_folder_button", None)):
                if button:
                    button.configure(state="normal" if self.update_package else "disabled")
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
        if task:
            self.input.configure(state="disabled" if not session_free or message or self.account_pending else "normal")
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

    def check_for_updates(self, manual=True):
        if self.update_checking or self.closing:
            return
        if not manual and self.update_snoozed_until > time():
            return
        self.update_checking = True
        self.update_candidate = None
        self._hide_update_banner()
        self.update_status.set("正在检查更新…")
        self.update_message.set("正在检查更新…")
        self.update_found_version.set("")
        self.update_release_notes.set("")
        self.update_security_notice.set("")
        self.update_download_status.set("")
        self.refresh_update_settings_view()
        self._set_update_check_button_state("disabled")

        def work():
            try:
                result = self.update_service.check()
                self.events.put(lambda result=result: self._update_check_done(result, manual))
            except Exception as error:
                self.events.put(lambda error=error: self._update_check_done(error, manual))

        try:
            Thread(target=work, daemon=True).start()
        except Exception as error:
            self._update_check_done(error, manual)

    def periodic_update_check(self):
        self.check_for_updates(manual=False)
        if not self.closing:
            self.schedule("update_periodic", 24 * 60 * 60 * 1000, self.periodic_update_check)

    def _update_check_done(self, result, manual):
        self.update_checking = False
        self._set_update_check_button_state("normal")
        if isinstance(result, Exception):
            message = update_check_error_message(result)
            self.update_status.set(message)
            show_banner = manual and self.current_page == "workspace"
            self.update_message.set(message if show_banner else "")
            self.update_found_version.set("")
            self.update_release_notes.set("")
            self.update_security_notice.set("")
            self.refresh_update_settings_view()
            if show_banner:
                self.update_banner.pack(fill="x", padx=18, pady=(0, 8), before=self.context_meter.panel)
            self.update_actions()
            return
        self.update_candidate = result
        self.update_package = None
        self.update_download_status.set("")
        if result is None:
            message = "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c"
            self.update_status.set(message)
            self.update_message.set(message if manual and self.current_page == "workspace" else "")
            self.update_found_version.set("")
            self.update_release_notes.set("")
            self.update_security_notice.set("")
            self.refresh_update_settings_view()
            self.update_actions()
            return
        message = "\u53d1\u73b0\u65b0\u7248\u672c"
        banner_message = (f"\u53d1\u73b0\u65b0\u7248\u672c {result.version}\u3002"
                          "\u5b89\u88c5\u5305\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1\uff0c"
                          "\u8bf7\u786e\u8ba4\u6765\u6e90\u540e\u624b\u52a8\u5b89\u88c5\u3002")
        security_notice = ("\u5b89\u88c5\u5305\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1\uff0c"
                           "\u8bf7\u786e\u8ba4\u6765\u6e90\u540e\u624b\u52a8\u5b89\u88c5\u3002")
        if result.sha256 is None:
            security_notice += (" GitHub Release Asset \u672a\u63d0\u4f9b SHA-256 \u6458\u8981\uff0c"
                                "\u672a\u8fdb\u884c\u72ec\u7acb\u6458\u8981\u6821\u9a8c\u3002")
        self.update_status.set(message)
        self.update_message.set(banner_message)
        self.update_found_version.set(result.version)
        self.update_release_notes.set(result.notes or "")
        self.update_security_notice.set(security_notice)
        self.refresh_update_settings_view()
        if self.current_page == "workspace":
            self.update_banner.pack(fill="x", padx=18, pady=(0, 8), before=self.context_meter.panel)
        self.update_actions()

    def refresh_update_settings_view(self):
        section = getattr(self, "update_settings_release_section", None)
        if section is None:
            return
        try:
            if not section.winfo_exists():
                return
            if self.update_candidate is None:
                section.pack_forget()
            else:
                section.pack(fill="x", padx=15, pady=(0, 8),
                             before=self.update_settings_download_actions)
            notes = getattr(self, "update_settings_notes_text", None)
            if notes is not None and notes.winfo_exists():
                notes.configure(state="normal")
                notes.delete("1.0", "end")
                notes.insert("1.0", self.update_release_notes.get() or
                             "\u6b64\u7248\u672c\u672a\u63d0\u4f9b\u66f4\u65b0\u8bf4\u660e\u3002")
                notes.configure(state="disabled")
                notes.yview_moveto(0)
            download_status = getattr(self, "update_settings_download_status_label", None)
            if download_status is not None and download_status.winfo_exists():
                if self.update_download_status.get():
                    download_status.pack(fill="x", padx=15, pady=(0, 8),
                                         before=self.update_settings_download_actions)
                else:
                    download_status.pack_forget()
        except tk.TclError:
            return

    def set_update_download_feedback(self, message):
        self.update_download_status.set(message)
        if self.current_page == "workspace":
            self.update_message.set(message)
        self.refresh_update_settings_view()

    def _hide_update_banner(self):
        banner = getattr(self, "update_banner", None)
        if banner is None:
            return
        try:
            if banner.winfo_manager():
                banner.pack_forget()
        except tk.TclError:
            pass

    def _set_update_check_button_state(self, state):
        button = getattr(self, "update_check_button", None)
        if button is None:
            return
        try:
            if button.winfo_exists():
                button.configure(state=state)
        except tk.TclError:
            pass

    def download_update(self):
        candidate = self.update_candidate
        if not candidate or self.update_downloading or self.update_package:
            return
        self.update_downloading = True
        self.update_cancel.clear()
        self._set_update_download_controls("\u53d6\u6d88\u4e0b\u8f7d", self.cancel_update_download, "normal")
        self.set_update_download_feedback(f"\u6b63\u5728\u4e0b\u8f7d {candidate.version}\u2026")

        def progress(received, total):
            self.events.put(lambda: self.set_update_download_feedback(
                f"\u6b63\u5728\u4e0b\u8f7d {candidate.version}\uff1a{received // 1024} / {total // 1024} KB"))

        def work():
            try:
                package = self.update_service.download(candidate, cancel=self.update_cancel, progress=progress)
                self.events.put(lambda package=package: self._update_download_done(package, None))
            except Exception as error:
                self.events.put(lambda error=error: self._update_download_done(None, error))

        try:
            Thread(target=work, daemon=True).start()
        except Exception as error:
            self._update_download_done(None, error)

    def cancel_update_download(self):
        self.update_cancel.set()
        self.set_update_download_feedback("\u6b63\u5728\u53d6\u6d88\u4e0b\u8f7d\u2026")

    def _update_download_done(self, package, error):
        self.update_downloading = False
        self.update_package = package
        if error:
            message = f"\u66f4\u65b0\u4e0b\u8f7d\u672a\u5b8c\u6210\uff1a{error}"
        else:
            if package.digest_verified:
                message = ("\u4e0b\u8f7d\u5b8c\u6210\uff1bGitHub Release Asset SHA-256 \u6458\u8981\u5339\u914d\uff08\u4ec5\u6821\u9a8c\u6587\u4ef6\u5b8c\u6574\u6027\uff0c\u4e0d\u662f\u53d1\u5e03\u8005\u8eab\u4efd\u8ba4\u8bc1\uff09\u3002")
            else:
                message = ("\u4e0b\u8f7d\u5b8c\u6210\uff1bGitHub \u672a\u63d0\u4f9b\u72ec\u7acb SHA-256 \u6458\u8981\uff0c\u672a\u8fdb\u884c\u72ec\u7acb\u6458\u8981\u6821\u9a8c\u3002")
            message += ("\n\u5b89\u88c5\u5305\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1\uff0c\u8bf7\u786e\u8ba4\u6765\u6e90\u540e\u624b\u52a8\u5b89\u88c5\u3002"
                       "\u8bf7\u9000\u51fa CCSLab Labcoding \u540e\u624b\u52a8\u8fd0\u884c\u5b89\u88c5\u5305\u3002")
        self.set_update_download_feedback(message)
        self._set_update_download_controls("\u4e0b\u8f7d\u66f4\u65b0", self.download_update, "normal")
        self.update_actions()

    def _set_update_download_controls(self, text, command, state):
        for button in (getattr(self, "update_download_button", None),
                       getattr(self, "update_settings_download_button", None)):
            if button:
                button.configure(text=text, command=command, state=state)

    def open_update_folder(self):
        candidate = self.update_candidate
        package = self.update_package
        if not candidate or not package:
            return
        try:
            verified = self.update_service.verify_local_package(candidate)
            if verified.path != package.path or verified.sha256 != package.sha256:
                raise UpdateTrustError("\u5df2\u4e0b\u8f7d\u5b89\u88c5\u5305\u4e0e\u9a8c\u8bc1\u7ed3\u679c\u4e0d\u4e00\u81f4")
            os.startfile(str(verified.path.parent))
        except Exception as error:
            self.update_package = None
            message = f"\u5b89\u88c5\u5305\u518d\u6b21\u9a8c\u8bc1\u5931\u8d25\uff0c\u5df2\u7981\u7528\u6253\u5f00\uff1a{error}"
            self.set_update_download_feedback(message)
            self.update_actions()

    def snooze_update(self):
        if self.update_candidate:
            self.update_snoozed_until = time() + 24 * 60 * 60
        self.update_banner.pack_forget()

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
        if not self.can_switch_role or self.busy:
            return
        if self.confirm_task_transition("切换登录角色", self.change_role):
            return
        target = "user" if self.is_admin else "admin"
        if target == "user":
            self.switch_role(target, "")
        else:
            prompt_role_switch(self, target, lambda code: self.switch_role(target, code))

    def switch_role(self, target, authorization_code):
        if not self.can_switch_role or self.busy or self.closing:
            return
        if self.confirm_task_transition("切换登录角色", lambda: self.switch_role(target, authorization_code)):
            return
        if target != ("user" if self.is_admin else "admin"):
            return
        client = self.account_client
        operation_client = copy(client)
        self.account_generation += 1
        self.account_polling = False

        def done(user):
            client.token = operation_client.token
            client.clear_open_webui_access()
            self.token = client.token
            self.session_grants.clear()
            for task in self.tasks.tasks.values():
                task.grants.clear()
            self.account, self.account_online = user, True
            try:
                self.saved_login.update_session(client.base, client.token)
            except (OSError, ValueError):
                self.status.set("角色已切换，保存的登录未能更新")
            if self.engine:
                self.retire_engine(self.engine)
                self.engine = None
            self.engine_limits = None
            self.account_state_changed()
            if self.current_page in {"settings", "admin"}:
                self.show_page("workspace", force=True)
            self.poll_account()

        def failed(error):
            self.show_error("切换角色失败", error)
            self.poll_account()

        self.worker("切换登录角色", lambda: operation_client.switch_role(target, authorization_code), done, failed)

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
                return copy(cached[1])  # Share the transport, not mutable login credentials.
            server, proxy_mode = scope
            client = AccountClient(server, mode=proxy_mode)
            if scope == self.account_connection:
                self.gateway_transport = scope, client
            return copy(client)

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
        self.render_project_tree()

    def selected_tree_row(self):
        selected = self.project_tree.selection()
        return self.tree_rows.get(selected[0]) if selected else None

    def update_delete_actions(self):
        free = not self.busy and not self.closing and self.can_open_project
        row = self.selected_tree_row()
        self.remove_recent_button.configure(state="normal" if free and row and not row[1] else "disabled")

    def render_project_tree(self):
        if not hasattr(self, "project_tree"):
            return
        query = self.session_search.get().strip().casefold()
        if query != self.tree_search_query:
            self.tree_search_query = query
            self.search_collapsed_projects.clear()
        collapsed = self.search_collapsed_projects if query else self.collapsed_projects
        scroll = self.project_tree.yview()[0]
        horizontal = self.project_tree.xview()[0]
        self.project_tree.delete(*self.project_tree.get_children())
        self.tree_rows.clear()
        for path in self.recents:
            context = self.project_contexts.get((str(self.state), canonical(path)))
            history_path = self.state / (hashlib.sha256(os.path.normcase(path).encode()).hexdigest() + ".json")
            history = context.history if context else read_json(history_path, {})
            titles = context.titles if context else read_json(history_path.with_suffix(".titles.json"), {})
            known = {item["id"]: dict(item) for item in context.sessions} if context else {}
            for key, record in history.items():
                if isinstance(record, dict):
                    known.setdefault(key, {"id": key, "title": record.get("title", "新会话")})
            for key, item in known.items():
                item["title"] = titles.get(key) or item.get("title") or "新会话"
            name = Path(path).name
            matches = [item for item in known.values() if query in item["title"].casefold()]
            if query and query not in name.casefold() and not matches:
                continue
            parent = self.project_tree.insert("", "end", text="📁 " + name, values=("＋",),
                                              open=(str(self.state), canonical(path)) not in collapsed)
            self.tree_rows[parent] = (path, None)
            for item in (matches if query and query not in name.casefold() else known.values()):
                child = self.project_tree.insert(parent, "end", text=item["title"])
                self.tree_rows[child] = (path, item["id"])
                if (path, item["id"]) == (self.project, self.session):
                    self.project_tree.selection_set(child)
            if path == self.project and not self.session:
                self.project_tree.selection_set(parent)
        font = tkfont.Font(root=self.root, font=(FONT, 10))
        width = max((font.measure(self.project_tree.item(key, "text")) + (60 if row[1] else 30)
                     for key, row in self.tree_rows.items()), default=170)
        self.project_tree.column("#0", width=max(170, width), minwidth=max(170, width))
        self.project_tree.yview_moveto(scroll)
        self.project_tree.xview_moveto(horizontal)
        self.update_delete_actions()
        if hasattr(self, "session_indicators"):
            self.session_indicators.draw()

    def select_project_row(self, _event=None):
        row = self.selected_tree_row()
        if row and row[1] and (row[0], row[1]) != (self.project, self.session):
            self.navigate_session(*row)
        self.update_delete_actions()

    def click_project_row(self, event):
        key = self.project_tree.identify_row(event.y)
        row = self.tree_rows.get(key)
        if row and not row[1] and self.project_tree.identify_column(event.x) == "#1":
            self.navigate_session(row[0], create=True)
            return "break"
        if row and not row[1]:
            expanded = not self.project_tree.item(key, "open")
            collapsed = self.search_collapsed_projects if self.session_search.get().strip() else self.collapsed_projects
            scope = str(self.state), canonical(row[0])
            collapsed.discard(scope) if expanded else collapsed.add(scope)
            self.project_tree.item(key, open=expanded)
            self.project_tree.focus(key)
            self.project_tree.selection_set(key)
            self.update_delete_actions()
            self.session_indicators.draw()
            return "break"

    def navigate_session(self, path, session=None, *, create=False):
        if self.closing or not self.can_open_project:
            return
        if self.busy:
            if self.busy not in {"读取项目", "启动本地 OpenCode", "读取会话"}:
                return
            if canonical(path) != canonical(self.project) or self.busy == "读取会话":
                self.abandon_worker()
        timer = self.timers.pop("navigate_session", None)
        if timer:
            self.root.after_cancel(timer)
        self.show_page("workspace")
        if canonical(path) != canonical(self.project):
            self.open_project(path, session_id=session)
        elif session:
            self.open_session(session)
        if create:
            owner = str(self.state)
            def ready():
                if self.closing or str(self.state) != owner or canonical(self.project) != canonical(path):
                    return
                if self.busy:
                    self.schedule("navigate_session", 50, ready)
                elif self.engine:
                    self.new_session()
            ready()

    def select_running_task(self, _event=None):
        selected = self.running_task_list.curselection()
        if selected and selected[0] < len(self.running_task_rows):
            task = self.running_task_rows[selected[0]]
            self.navigate_session(task.context.project, task.session_id)

    def pending_approval_tasks(self):
        with self.approval_lock:
            ids = {ticket.get("task_id") for ticket in self.approval_tickets.values()
                   if not ticket["completed"].is_set()}
        return [task for task in self.tasks.active() if task.task_id in ids and task.context.owner == str(self.state)]

    def refresh_task_sidebar(self):
        if not hasattr(self, "running_task_list"):
            return
        stats = self.task_stats
        show = lambda key: str(stats[key]) if type(stats.get(key)) is int else "—"
        self.task_stats_text.set(f"服务器执行中：{show('running_tasks')}\n剩余槽位：{show('available_slots')}\n"
                                 f"本用户执行中：{show('user_running_tasks')}")
        rows = [task for task in self.tasks.active() if task.context.owner == str(self.state)
                and task.status in EXECUTING]
        labels = [f"{Path(task.context.project).name} · "
                  f"{task.context.titles.get(task.session_id) or task.context.history.get(task.session_id, {}).get('title', '新会话')}"
                  for task in rows]
        if labels != list(self.running_task_list.get(0, "end")):
            top, left = self.running_task_list.yview()[0], self.running_task_list.xview()[0]
            self.running_task_list.delete(0, "end")
            for label in labels:
                self.running_task_list.insert("end", label)
            self.running_task_list.yview_moveto(top)
            self.running_task_list.xview_moveto(left)
        self.running_task_rows = rows
        pending = self.pending_approval_tasks()
        if [task.task_id for task in pending] != self.approval_notice_rows:
            self.approval_notice_rows = [task.task_id for task in pending]
            self.approval_notices.configure(state="normal")
            self.approval_notices.delete("1.0", "end")
            for task in pending:
                tag = "approval-" + task.task_id
                title = task.context.titles.get(task.session_id) or task.context.history.get(task.session_id, {}).get("title", "新会话")
                self.approval_notices.insert("end", f"{Path(task.context.project).name} 需要审批！{title}\n", tag)
                self.approval_notices.tag_configure(tag, foreground=self.colors["remove"])
                self.approval_notices.tag_bind(tag, "<Button-1>",
                    lambda _event, task=task: self.navigate_session(task.context.project, task.session_id))
            self.approval_notices.configure(state="disabled", height=min(2, max(1, len(pending))))
            if pending:
                self.approval_notice_panel.pack(fill="x", padx=8, pady=(0, 8))
            else:
                self.approval_notice_panel.pack_forget()
            self.schedule_workspace_layout(0)

    def poll_task_stats(self):
        if self.closing:
            return
        self.schedule("task_stats", 2000, self.poll_task_stats)
        client, generation = self.account_client, self.account_generation
        if not client or not self.account_online or self.connection_mode != "account" or self.task_stats_polling:
            return
        ticket = self.task_stats_polling = object()
        def done(result, error=None):
            if self.task_stats_polling is not ticket:
                return
            self.task_stats_polling = False
            timer = self.timers.pop("task_stats_deadline", None)
            if timer:
                self.root.after_cancel(timer)
            if not self.closing and generation == self.account_generation and client is self.account_client:
                if error:
                    self.record_client_state("task_stats_failed", error=error)
                self.task_stats = result
                self.refresh_task_sidebar()
                if self.current_page == "admin" and self.page_controller:
                    self.page_controller.update_task_counts(result)
        def read():
            try:
                result = client.request("GET", "/api/tasks")
            except Exception as error:
                self.events.put(lambda error=error: done({}, error))
            else:
                self.events.put(lambda: done(result))
        self.schedule("task_stats_deadline", max(1, int(POLL_TIMEOUT * 1000)),
                      lambda: done({}, TimeoutError()))
        try:
            Thread(target=read, daemon=True).start()
        except Exception as error:
            done({}, error)

    def remove_recent(self):
        row = self.selected_tree_row()
        if self.busy or self.closing or not self.can_open_project or not row or row[1]:
            return
        path = row[0]
        state = self.state
        if not messagebox.askyesno(
                "删除最近项目记录", f"从最近项目中移除以下记录？\n\n{path}\n\n项目文件和历史会话将保留。",
                parent=self.root, default=messagebox.NO):
            return
        if state != self.state or self.busy or self.closing or not self.can_open_project:
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
        if self.busy or self.closing:
            return
        if self.connection_mode == "direct":
            self.show_page("direct")
            return
        client = self.account_client
        if client is None:
            self.show_page("access")
            return
        if self.confirm_task_transition("退出登录", self.account_action):
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
        if mode not in {"direct", "account"} or self.busy or self.closing:
            return
        if mode == self.connection_mode:
            self.show_page("direct" if mode == "direct" else ("workspace" if self.account_client else "access"))
            return
        if self.confirm_task_transition("更换连接身份", lambda: self.select_connection(mode)):
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
        for task in self.tasks.active():
            self.cancel_task(task)
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
            self.retire_engine(self.engine)
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
        if self.connection_mode != "direct" or self.busy or self.closing:
            return False
        if self.confirm_task_transition("更改模型连接", lambda: self.check_direct_connection(
                address=address, save=save, restore=restore, email=email, password=password)):
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
            return client, user, record

        def done(result):
            if page.container.winfo_exists():
                page.set_busy(False)
                page.feedback.set("")
            if result and connection == self.account_connection and not self.account_client:
                client, user, record = result
                try:
                    self.saved_login.save(client.base, record["username"], record["password"], client.token)
                except OSError as error:
                    failed(error)
                    return
                self.signed_in(client, user)

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
        for task in self.tasks.active():
            self.cancel_task(task)
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.account_generation += 1
        self.account_polling = False
        self.task_stats_polling = False
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
        for task in self.tasks.active():
            self.cancel_task(task)
        self.reset_task_history_browse()
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
        self.task_stats = {}
        self.context_meter.set_model({})
        self.ca_path = None
        self.save_history()
        if self.engine:
            self.retire_engine(self.engine)
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
        self.bind_task_view()

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
        ticket = self.account_polling = object()
        generation = self.account_generation
        client_user_id = (self.account or {}).get("id")
        client_role = (self.account or {}).get("role")
        check_grant = direct and self.direct_unlocked
        refresh_model = monotonic() - self.model_checked > 12 or not self.model_checked

        def done(user, model, error):
            if (self.account_polling is not ticket or generation != self.account_generation
                    or client is not self.account_client or self.closing):
                return
            self.account_polling = False
            timer = self.timers.pop("account_poll_deadline", None)
            if timer:
                self.root.after_cancel(timer)
            if error:
                self.record_client_state("account_poll_failed", error=error)
                if not isinstance(error, AccountError):
                    error = AccountError("账号状态读取失败或超时，请稍后重试。")
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
                for task in self.tasks.active():
                    self.cancel_task(task)
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
            if client_user_id is not None and user.get("id") != client_user_id:
                self.require_account_login("登录身份已改变，请重新登录。")
                return
            changed = user != self.account or not self.account_online
            self.account, self.account_online = user, True
            if changed and (user["status"] != "active" or user.get("id") != client_user_id
                            or user.get("role") != client_role):
                for task in self.tasks.active():
                    self.cancel_task(task)
                    task.grants.clear()
                self.session_grants.clear()
            if changed:
                self.account_state_changed()
            if model is not None:
                self.update_model(model)
            if self.can_use_agent and self.engine is None and self.project and not self.busy and not self.running:
                self.start_engine(self.project)

        def request():
            try:
                user = client.me()
            except Exception as error:
                self.events.put(lambda error=error: done(None, None, error))
                return
            if direct:
                if check_grant and user["status"] == "active" and user["role"] == "admin":
                    try:
                        client.check_open_webui_access()
                    except Exception as error:
                        self.events.put(lambda error=error: done(user, None, error))
                        return
                self.events.put(lambda: done(user, None, None))
                return
            model = None
            if user["status"] == "active" and (refresh_model or not self.model_info):
                try:
                    model = client.model()
                except Exception as error:
                    self.events.put(lambda error=error: self.record_client_state("model_poll_failed", error=error))
            self.events.put(lambda: done(user, model, None))

        self.schedule("account_poll_deadline", max(1, int(POLL_TIMEOUT * 1000)),
                      lambda: done(None, None, TimeoutError()))
        try:
            Thread(target=request, daemon=True).start()
        except Exception as error:
            done(None, None, error)

    def update_model(self, model):
        self.model_info = model
        self.model_checked = monotonic()
        self.context_meter.set_model(model, output_limit=self.model_limits(model)[1])
        if "running_tasks" in model:
            self.task_stats = model
            self.refresh_task_sidebar()

    def choose_project(self):
        if self.busy or self.closing or not self.can_open_project:
            return
        path = filedialog.askdirectory(parent=self.root, title="选择项目文件夹", mustexist=True)
        if path:
            self.open_project(path)

    def new_project(self):
        if not self.busy and not self.closing and self.can_open_project:
            show_new_project(self)

    def open_project(self, path, *, session_id=None):
        if self.busy or self.closing or not self.can_open_project:
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
        self.park_approvals()
        self.reset_task_history_browse()
        self.session_grants.clear()
        self.active_approval_scope = None
        self.update_session_grant_ui()
        self.show_page("workspace")
        self.save_history()
        self.save_draft()
        self.session = None
        self.engine, old_engine = None, self.engine
        self.retire_engine(old_engine)
        self.project = path
        name = Path(path).name
        self.project_label.set(name if len(name) <= 28 else name[:27] + "…")
        self.branch.set("正在读取项目…")
        recents = self.recents if path in self.recents else [path, *self.recents]
        write_json(self.state / "recents.json", recents[:15])
        self.refresh_recents()
        self.history_path = self.state / (hashlib.sha256(os.path.normcase(path).encode()).hexdigest() + ".json")
        context = self.project_contexts.get((str(self.state), canonical(path)))
        if context is None:
            self.history = read_json(self.history_path, {})
            self.load_session_titles()
            context = ProjectContext(str(self.state), path, self.history_path, self.history, self.session_titles)
            self.project_contexts[context.key] = context
            if context.recover():
                self.save_context_later(context)
        else:
            self.history, self.session_titles = context.history, context.titles
        self.update_sessions(context.sessions)
        self.session_search.set("")
        self.changes = None
        self.change_rows = []
        self.change_filter.set("")
        self.render_change_list()
        self.clear_text(self.diff)
        self.diff_text = ""
        self.reset_conversation()
        self.bind_task_view()
        self.restore_draft()
        if session_id:
            self.open_session(session_id)
        else:
            self.append(self.chat, path + "\n", "heading")
        generation = self.account_generation

        def work():
            changes = context.changes or Changes(path)
            return changes, changes.status()

        def done(result):
            if generation != self.account_generation or context.key != (str(self.state), canonical(self.project)):
                return
            self.changes, status = result
            context.changes = self.changes
            if not self.history.get(self.session, {}).get("changes"):
                self.show_changes(status)
            if self.can_use_agent:
                self.start_engine(path)
            else:
                self.append(self.chat, "模型连接恢复后可发送任务。\n", "muted")

        self.worker("读取项目", work, done)

    def start_engine(self, path):
        limits = self.model_limits(self.model_info)
        client = self.account_client if self.connection_mode == "account" else None
        owner, generation = self.state, self.account_generation
        connection = self.api_base, self.token, self.ca_path

        def work():
            proxy_url = client.engine_proxy_url() if client else None
            return self.create_engine(path, limits, proxy_url=proxy_url, owner=owner, connection=connection)

        def done(result):
            if owner != self.state or generation != self.account_generation or path != self.project:
                self.retire_engine(result[0])
                return
            self.engine, sessions = result
            self.engine_limits = limits
            self.update_sessions(sessions)
            if self.session and not self.history.get(self.session):
                self.open_session(self.session)

        self.worker("启动本地 OpenCode", work, done, discard=lambda result: self.retire_engine(result[0]))

    @staticmethod
    def model_limits(meta, configured_output=4096):
        limit = meta.get("effective_context_limit")
        caps = [v for key in ("output_token_limit", "output_limit")
                if type(v := meta.get(key)) is int and v > 0]
        output = min(caps) if caps else None
        context = limit if type(limit) is int and 1024 <= limit <= 2_000_000 else 24000
        max_output = output if type(output) is int and output > 0 else 4096
        configured_output = configured_output if type(configured_output) is int and configured_output > 0 else 4096
        return context, min(max_output, configured_output, context // 2)

    def output_command(self, prompt):
        parts = prompt.split()
        if self.busy:
            self.append(self.chat, "当前操作完成后再运行 /output。\n", "muted")
            return
        if not parts or len(parts) > 2 or parts[0] != "/output":
            self.append(self.chat, "用法：/output 或 /output <128–8192>\n", "muted")
            return
        value = None
        if len(parts) == 2:
            if not parts[1].isascii() or not parts[1].isdigit():
                self.append(self.chat, "用法：/output 或 /output <128–8192>\n", "muted")
                return
            value = int(parts[1])
            if not 128 <= value <= 8192:
                self.append(self.chat, "输出 Token 上限须为 128–8192。\n", "muted")
                return
            previous = self.output_limit
            self.output_limit = value
            had_saved_value = "output_limit" in self.prefs
            previous_saved_value = self.prefs.get("output_limit")
            set_output_limit(self.prefs, value)
            try:
                self.save_prefs()
            except OSError as error:
                self.output_limit = previous
                if had_saved_value:
                    self.prefs["output_limit"] = previous_saved_value
                else:
                    self.prefs.pop("output_limit", None)
                self.append(self.chat, f"Labcoding 设置保存失败：{error}\n", "error")
                return
        client = self.account_client if self.connection_mode == "account" else None
        meta = dict(self.model_info)
        self.append(self.chat, f"Labcoding 已设置 {self.output_limit} Token；新任务生效。\n", "muted")

        def inspect_remote():
            remote_limit = None
            remote_error = None
            gateway_limit = None
            gateway_error = None
            if client is None:
                remote_error = "请通过 CCSLab 账号 Gateway 连接后同步 Ubuntu OpenCode。"
            else:
                try:
                    if value is not None:
                        remote_limit = client.set_opencode_output_limit(value).get("output_limit")
                    else:
                        remote_limit = client.opencode_output_limit()
                except Exception as error:
                    remote_error = str(error)
                    if value is not None:
                        try:
                            remote_limit = client.opencode_output_limit()
                        except Exception as read_error:
                            remote_error = f"{error}；读取远端设置失败：{read_error}"
                try:
                    current = client.model()
                    gateway_limit = min((n for n in (current.get("output_token_limit"), current.get("output_limit"))
                                         if type(n) is int and n > 0), default=None)
                    meta.update(current)
                except Exception as error:
                    gateway_error = str(error)
            effective = self.model_limits(meta, self.output_limit)[1]
            if gateway_limit is not None:
                effective = min(effective, gateway_limit)
            return effective, remote_limit, remote_error, gateway_error

        def done(result):
            effective, remote_limit, remote_error, gateway_error = result
            if value is None:
                suffix = f"{effective} Token" if not gateway_error else f"读取失败：{gateway_error}"
                self.append(self.chat, f"Labcoding 设定值：{self.output_limit}；Gateway/上下文有效值：{suffix}。\n",
                            "muted" if not gateway_error else "error")
            elif not gateway_error and effective < self.output_limit:
                self.append(self.chat, f"Labcoding 已设置 {self.output_limit}，当前有效 {effective} Token。\n",
                            "heading")
            elif gateway_error:
                self.append(self.chat, f"Labcoding 已设置 {self.output_limit}；Gateway/上下文有效值读取失败：{gateway_error}\n",
                            "error")
            if remote_error:
                retry = "/output" if value is None else f"/output {value}"
                status = f"同步失败；请检查连接或权限后重试 {retry}"
                if remote_limit is not None:
                    status += f"（Ubuntu 当前仍为 {remote_limit} Token）"
                self.append(self.chat, f"Ubuntu 独立 OpenCode：{status}：{remote_error}\n", "error")
            elif remote_limit is not None:
                timing = "；OpenCode 新任务读取配置，已运行任务不变" if value is not None else ""
                self.append(self.chat, f"Ubuntu server 独立 OpenCode：{remote_limit} Token{timing}。\n", "muted")

        self.worker("读取输出上限" if value is None else "同步输出上限", inspect_remote, done,
                    on_error=lambda error: self.append(self.chat, f"Gateway 查询失败：{error}\n", "error"))

    def create_engine(self, path, limits, *, proxy_url=None, owner=None, connection=None):
        from engine import Engine
        base, token, ca = connection or (self.api_base, self.token, self.ca_path)
        engine = Engine(owner or self.state, base, token, limits=limits, ca_bundle=ca, proxy_url=proxy_url)
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
        if self.project:
            context = self.project_context()
            context.sessions = self.sessions
        self.render_session_list()

    def load_session_titles(self):
        saved = read_json(self.history_path.with_suffix(".titles.json"), {})
        self.session_titles = ({key: value.strip() for key, value in saved.items()
                                if isinstance(key, str) and valid_session_title(value)}
                               if isinstance(saved, dict) else {})

    def show_session_menu(self, event):
        key = self.project_tree.identify_row(event.y)
        row = self.tree_rows.get(key)
        if not row or not row[1]:
            return "break"
        self.project_tree.selection_set(key)
        if canonical(row[0]) != canonical(self.project):
            self.navigate_session(*row)
            return "break"
        enabled = not (self.busy or self.closing) and self.can_open_project and self.history_path
        active = self.session_task_state(row[1]).get("status") in ACTIVE
        self.session_menu.entryconfigure(0, state="normal" if enabled else "disabled",
                                         command=lambda: self.rename_session(row[1]))
        self.session_menu.entryconfigure(1, state="normal" if enabled and not active else "disabled")
        try:
            self.session_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.session_menu.grab_release()
        return "break"

    def rename_session(self, session_id=None):
        if self.busy or self.closing or not self.can_open_project or not self.history_path:
            return
        if session_id is None:
            row = self.selected_tree_row()
            if not row or not row[1] or canonical(row[0]) != canonical(self.project):
                return
            session_id = row[1]
        row = next((row for row in self.sessions if row["id"] == session_id), None)
        if row is None:
            return
        project, history_path, generation = self.project, self.history_path, self.account_generation
        title = simpledialog.askstring("重命名会话", "会话名称（1–100 个字符）：",
                                       initialvalue=row.get("title") or "新会话", parent=self.root)
        if title is None:
            return
        if (self.busy or self.closing or not self.can_open_project
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
        self.project_context().titles = updated
        if self.session_search.get().strip().casefold() not in title.casefold():
            self.session_search.set("")
        self.update_sessions(self.sessions)
    def render_session_list(self):
        query = self.session_search.get().strip().casefold()
        self.visible_sessions = [row for row in self.sessions if query in (row.get("title") or "").casefold()]
        self.render_project_tree()

    def delete_session(self):
        selected = self.selected_tree_row()
        if (self.busy or self.closing or not self.can_open_project or not self.engine or not selected
                or not selected[1] or canonical(selected[0]) != canonical(self.project)):
            return
        session_id = selected[1]
        row = next((row for row in self.sessions if row["id"] == session_id), {"id": session_id})
        if self.session_task_state(session_id).get("status") in ACTIVE:
            self.status.set("请先停止该会话的任务，再删除会话。")
            return
        engine, history, history_path, titles = self.engine, self.history, self.history_path, self.session_titles
        if not messagebox.askyesno(
                "删除历史会话", f"删除会话“{row.get('title') or '新会话'}”？\n\n"
                "此会话的聊天记录、执行记录和草稿将被删除，无法恢复。项目文件将保留。",
                parent=self.root, default=messagebox.NO):
            return
        if (engine is not self.engine or history is not self.history or self.busy
                or self.closing or not self.can_open_project
                or self.session_task_state(session_id).get("status") in ACTIVE):
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
            if self.project:
                self.project_context().titles = titles
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
        if not self.engine or not self.can_use_agent or self.busy or self.closing:
            return
        self.show_page("workspace")
        engine, context = self.engine, self.project_context()

        def created(session_id):
            if engine is not self.engine or context.key != (str(self.state), canonical(self.project)):
                return
            self.history[session_id] = {"title": "新会话", "events": []}
            self.session_search.set("")
            self.open_session(session_id)
            self.update_sessions(self.sessions)

        self.worker("新建会话", engine.new_session, created)

    def open_session(self, session_id):
        scope = str(self.state), canonical(self.project)
        self.collapsed_projects.discard(scope)
        self.search_collapsed_projects.discard(scope)
        self.park_approvals()
        self.reset_task_history_browse()
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
            task = self.current_task()
            if not task or task.status not in ACTIVE:
                self.end_reply()
            elif self.render_later is not None:
                self.root.after_cancel(self.render_later)
                self.draw_reply()
            saved = local.get("changes")
            if saved:
                self.show_changes((saved["branch"], [Change(**row) for row in saved["files"]]))
        if not local and self.engine:
            engine, context = self.engine, self.project_context()
            self.worker("读取会话", lambda: engine.history(session_id),
                        lambda messages: self.render_history(messages, context, session_id))
        self.bind_task_view()
        self.render_project_tree()

    def render_history(self, messages, context=None, session_id=None):
        events = []
        for message in messages:
            info = message.get("info", message)
            events.append({"type": "reply_end"})
            for part in message.get("parts", []):
                if part.get("type") == "text":
                    kind = "user" if info.get("role") == "user" else "delta"
                    events.append({"type": kind, "text": part.get("text", "")})
                if part.get("type") == "tool":
                    state = part.get("state", {})
                    events.append({"type": "tool", "key": part.get("id"), "name": part.get("tool"), **state})
        if context is not None:
            title = next((row["title"] for row in context.sessions if row["id"] == session_id), "历史会话")
            context.history.setdefault(session_id, {"title": title, "events": events})
            self.save_context_later(context)
            if context.key != (str(self.state), canonical(self.project)) or session_id != self.session:
                return
        for event in events:
            self.render_event(event)
        self.end_reply()
        self.context_meter.restore(messages)

    def submit_shortcut(self, _event):
        self.submit()
        return "break"

    def reset_task_history_browse(self):
        self.task_history_index = None
        self.task_history_scope = None
        self.task_history_draft = ""

    def task_history_identity(self):
        account = self.account or {}
        client = self.account_client
        return (
            self.connection_mode,
            getattr(client, "base", ""),
            account.get("id", self.direct_identity if self.connection_mode == "direct" else None),
            account.get("username"),
            os.path.normcase(self.project) if self.project else "",
            str(self.history_path) if self.history_path else "",
            self.session,
        )

    def task_history_entries(self):
        if not self.session:
            return []
        record = self.history.get(self.session)
        if not isinstance(record, dict):
            return []
        return [event["text"] for event in record.get("events", [])
                if isinstance(event, dict) and event.get("type") == "user"
                and isinstance(event.get("text"), str)]

    def task_history_cursor_at_boundary(self, direction):
        if self.input.tag_ranges("sel"):
            return False
        if (self.task_history_index is not None
                and self.task_history_scope == self.task_history_identity()):
            return True
        if direction == "up":
            if self.input.compare("insert", "==", "1.0"):
                return True
            distance = self.input.count("1.0", "insert", "displaylines")
        else:
            if self.input.compare("insert", "==", "end-1c"):
                return True
            distance = self.input.count("insert", "end-1c", "displaylines")
        return bool(distance is not None and distance[0] == 0)

    def show_task_history_entry(self, text):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.input.mark_set("insert", "end-1c")
        self.input.see("insert")

    def task_history_up(self, _event=None):
        if str(self.input.cget("state")) != "normal" or not self.task_history_cursor_at_boundary("up"):
            return None
        entries = self.task_history_entries()
        if not entries:
            return None
        scope = self.task_history_identity()
        if self.task_history_index is None or self.task_history_scope != scope:
            self.task_history_scope = scope
            self.task_history_index = len(entries)
            self.task_history_draft = self.input.get("1.0", "end-1c")
        else:
            self.task_history_index = min(self.task_history_index, len(entries))
        self.task_history_index = max(0, self.task_history_index - 1)
        self.show_task_history_entry(entries[self.task_history_index])
        return "break"

    def task_history_down(self, _event=None):
        if (self.task_history_index is None or not self.task_history_cursor_at_boundary("down")
                or self.task_history_scope != self.task_history_identity()):
            return None
        entries = self.task_history_entries()
        if not entries:
            self.reset_task_history_browse()
            return None
        self.task_history_index = min(self.task_history_index + 1, len(entries))
        if self.task_history_index == len(entries):
            draft = self.task_history_draft
            self.reset_task_history_browse()
            self.show_task_history_entry(draft)
        else:
            self.show_task_history_entry(entries[self.task_history_index])
        return "break"

    def save_draft(self):
        key = (self.project, self.session)
        draft = self.input.get("1.0", "end-1c")
        if draft.strip():
            self.drafts[key] = draft
        else:
            self.drafts.pop(key, None)
        if self.project and self.session and self.session in self.history:
            self.history[self.session]["draft"] = draft
            self.save_context_later(self.project_context())

    def restore_draft(self):
        previous_state = self.input.cget("state")
        self.input.configure(state="normal")
        self.input.delete("1.0", "end")
        draft = self.drafts.get((self.project, self.session), self.history.get(self.session, {}).get("draft", ""))
        if draft:
            self.input.insert("1.0", draft)
        self.input.configure(state=previous_state)

    def submit(self):
        prompt = self.input.get("1.0", "end-1c").strip()
        if prompt.split(maxsplit=1)[:1] == ["/output"]:
            self.input.delete("1.0", "end")
            self.output_command(prompt)
            return
        task = self.current_task()
        if (not prompt or self.busy or self.closing or self.identity_transition or not self.engine
                or not self.can_use_agent or task and task.status in ACTIVE):
            return
        if len(prompt) > 24000:
            self.show_error("无法开始任务", "任务不能超过 24,000 个字符。")
            return
        if not self.session:
            engine, project, owner = self.engine, self.project, str(self.state)
            def created(session):
                if engine is self.engine and project == self.project and owner == str(self.state):
                    self.history[session] = {"title": "新会话", "events": []}
                    self.open_session(session)
                    self.run_task(prompt)
            self.worker("准备新会话", engine.new_session, created)
        else:
            self.run_task(prompt)

    @staticmethod
    def require_active(client, *, check_quota=False):
        user = client.me()
        if user["status"] != "active":
            raise AccountError("账号未激活或已禁用。", 403)
        if check_quota and blocked_message(user):
            raise AccountError(blocked_message(user), 402)

    def run_task(self, prompt):
        self.reset_task_history_browse()
        session = self.session
        if not session or not self.project or self.closing:
            return
        current = self.current_task()
        if current and current.status in ACTIVE:
            return
        context = self.project_context()
        identity = dict(self.account or {"id": self.direct_identity, "role": "admin"})
        task = SessionTask(context, session, prompt, identity, client=copy(self.account_client),
                           api_base=self.api_base, token=self.token, ca_path=self.ca_path,
                           model=dict(self.model_info), mode=self.mode.get(), strict=self.strict.get(),
                           direct=self.connection_mode == "direct", grants=self.session_grants)
        try:
            self.tasks.submit(task)
        except ValueError as error:
            self.show_error("无法开始任务", error)
            return
        self.input.delete("1.0", "end")
        self.drafts.pop((self.project, session), None)
        self.end_reply()
        record = self.history.setdefault(session, {"title": "新会话", "events": []})
        record["draft"] = ""
        if not record["events"] and record.get("title") == "新会话":
            record["title"] = " ".join(prompt.split())[:35]
        self.update_sessions([row for row in self.sessions if row["id"] != session])
        self.track_task(task, {"type": "user", "text": prompt, "mode": self.mode.get()})
        self.task_changed(task)
        self.pump_tasks()

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
        self.flush_contexts()

    def render_event(self, event):
        kind = event.get("type")
        value = event.get("text", "")
        if kind == "reply_end":
            self.end_reply()
        elif kind == "delta":
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
            if self.live_markdown.strip():
                follow = self.chat.yview()[1] >= .98
                text = self.live_markdown
                icon = ActionIcon(self, self.chat, "copy", lambda text=text: self.copy_reply(text))
                icon.theme(self.colors)
                self.chat.configure(state="normal")
                self.chat.insert("end-1c", "\n" if self.chat.get("end-2c", "end-1c") != "\n" else "")
                self.chat.window_create("end-1c", window=icon, align="center", pady=2)
                self.chat.insert("end-1c", "\n")
                self.chat.configure(state="disabled")
                self.reply_copy_icons.append(icon)
                if follow:
                    self.chat.see("end")
        self.live_text = False
        self.live_markdown = ""

    def copy_reply(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def reset_conversation(self):
        self.end_reply()
        for icon in self.reply_copy_icons:
            icon.destroy()
        icons = set(self.reply_copy_icons)
        self.themed = [row for row in self.themed if row[0] not in icons]
        self.reply_copy_icons.clear()
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
        task = self.current_task()
        if task:
            self.cancel_task(task)

    def refresh_changes(self):
        if self.changes:
            changes, owner = self.changes, str(self.state)
            def done(result):
                if changes is self.changes and owner == str(self.state):
                    self.show_changes(result)
            self.worker("刷新文件变更", changes.status, done)

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
        if not selection or self.busy:
            return
        change = self.visible_changes[selection[0]]
        changes, owner = self.changes, str(self.state)

        def done(diff):
            if changes is not self.changes or owner != str(self.state):
                return
            current = self.change_list.curselection()
            if current != selection or self.visible_changes[current[0]] != change:
                self.select_change(None)
                return
            self.diff_text = diff
            added, removed = self.render_diff(self.diff)
            self.result_status.set(f"+{added} / −{removed}   {change.path}" if added or removed else change.path)

        self.worker("读取差异", lambda: changes.diff(change), done)

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
        if self.shutdown_deadline is None:
            if self.tasks.active():
                if not messagebox.askyesno("关闭应用", f"停止 {len(self.tasks.active())} 个后台/排队任务并退出？",
                                          parent=self.root, default=messagebox.NO):
                    return
                self.shutdown_waiting = True
            elif self.running and not messagebox.askyesno("关闭应用", "停止当前任务并退出？", parent=self.root):
                return
            self.shutdown_deadline = monotonic() + SHUTDOWN_TIMEOUT
            self.closing = True
            self.tasks.accepting = False
            self.cancel_all_approvals()
            for task in self.tasks.active():
                self.cancel_task(task)
            self.update_actions()
        if self.tasks.active() or self.busy:
            self.pump_tasks()
            # Normal pump stops rescheduling during shutdown; metadata callbacks must still drain.
            for _ in range(200):
                try:
                    action = self.events.get_nowait()
                except Empty:
                    break
                try:
                    action()
                except Exception as error:
                    self.record_client_state("shutdown_callback_failed", error=error)
            self.status.set("正在取消任务并保存记录，等待模型与工具结束…")
            if (self.tasks.active() or self.busy) and monotonic() < self.shutdown_deadline:
                self.schedule("close", 100, self.close)
                return
            if self.tasks.active() or self.busy:
                self.record_client_state("shutdown_timeout", self.worker_job)
        self.abandon_worker()
        self.closing = True
        self.tasks.accepting = False
        self.cancel_all_approvals()
        self.session_grants.clear()
        self.active_approval_scope = None
        if self.account_client:
            self.account_client.clear_open_webui_access()
        self.end_reply()
        self.save_draft()
        self.session_indicators.close()
        for timer in self.timers.values():
            self.root.after_cancel(timer)
        self.timers.clear()
        for window, timer in self.titlebar_after.values():
            try:
                window.after_cancel(timer)
            except tk.TclError:
                pass
        self.titlebar_after.clear()
        for scrollbar in tuple(self.overlay_scrollbars):
            scrollbar.close()
        self.overlay_scrollbars.clear()
        if self.approval_restore_after:
            self.root.after_cancel(self.approval_restore_after)
            self.approval_restore_after = None
        self.stop_requested.set()
        self.save_history()
        try:
            self.retire_engine(self.engine)
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
