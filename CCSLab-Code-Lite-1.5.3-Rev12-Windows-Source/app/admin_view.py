"""Embedded account and model administration for the desktop shell."""

from datetime import datetime
from time import monotonic
import tkinter as tk
from tkinter import messagebox, ttk

from account_client import ACCOUNT_STATUS
from quota_view import balance_text, parse_k, snapshot


UNAVAILABLE = "暂不可用"
CONTEXT_OPTIONS = ("32768", "65536", "131072", "147456", "163840", "196608")
CONTEXT_LABELS = {value: f"{int(value) // 1024}K（{value}）" for value in CONTEXT_OPTIONS}
SLOT_OPTIONS = tuple(str(value) for value in range(1, 11))
USER_SCROLL_STEP = 18
USER_STATUS_ACTIONS = {
    "pending": (("激活", "active"), ("禁用", "disabled"), ("拒绝", "rejected")),
    "active": (("禁用", "disabled"),),
    "rejected": (("重新激活", "active"),),
    "disabled": (("重新激活", "active"),),
}
USER_STATUS_COLORS = {
    "pending": ("soft", "heading"),
    "active": ("add_bg", "add"),
    "rejected": ("remove_bg", "remove"),
    "disabled": ("panel", "muted"),
}


def count(value):
    return f"{value:,}" if type(value) is int else UNAVAILABLE


def model_name(model):
    return str(model.get("model_name") or model.get("model_id") or "模型")


def model_state(model):
    return {"running": "运行中", "sleeping": "休眠",
            "unavailable": UNAVAILABLE}.get(model.get("status"), "待验证")


def gpu_memory(model):
    gpu = model.get("gpu")
    if isinstance(gpu, dict) and type(gpu.get("used_mib")) is int and type(gpu.get("total_mib")) is int:
        return f"{gpu['used_mib']:,} / {gpu['total_mib']:,} MiB"
    return UNAVAILABLE


def account_time(timestamp):
    if type(timestamp) in (int, float) and timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            pass
    return "—"


def build_admin_page(app, parent):
    """Mount the authenticated administrator console into the current page."""
    if not app.is_admin:
        return None
    page = AdminPage(app, parent)
    app.apply_theme()
    page.refresh_users()
    return page


class AdminPage:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.client = app.account_client
        self.users = {}
        self.user_cards = {}
        self.user_wheel_delta = 0
        self.has_model = False
        self.control_available = False
        self.review_ticket = None
        self.review_deadline = None
        self.control_detail = ""
        self.server_configuration = {}
        self.server_version = None
        self.review_requested = None
        self.notice = tk.StringVar(parent)
        self.build()

    def build(self):
        app, parent = self.app, self.parent
        frame = app.frame(parent)
        frame.pack(fill="both", expand=True, padx=20, pady=(18, 14))
        headline = app.frame(frame)
        headline.pack(fill="x", pady=(0, 10))
        app.label(headline, "管理控制台", size=20, bold=True).pack(side="left")
        app.label(headline, variable=self.notice, color="heading", size=9).pack(side="right", padx=(12, 0))

        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill="both", expand=True)
        self.users_tab, self.model_tab = app.frame(self.tabs), app.frame(self.tabs)
        self.tabs.add(self.users_tab, text="用户审批")
        self.tabs.add(self.model_tab, text="模型管理")
        self.build_users()
        self.build_model()
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed)

    def build_users(self):
        app = self.app
        frame = app.frame(self.users_tab)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        self.account_overview = self.metrics(frame, tuple(ACCOUNT_STATUS.values()))

        tools = app.frame(frame)
        tools.pack(fill="x", pady=(12, 7))
        self.search = tk.StringVar(frame)
        self.status_filter = tk.StringVar(frame, "全部状态")
        app.label(tools, "搜索", muted=True).pack(side="left")
        ttk.Entry(tools, textvariable=self.search, width=27).pack(side="left", padx=(6, 8))
        ttk.Combobox(tools, textvariable=self.status_filter, values=("全部状态", *ACCOUNT_STATUS.values()),
                     state="readonly", width=11).pack(side="left")
        app.button(tools, "刷新列表", self.refresh_users).pack(side="right")

        roster = app.frame(frame)
        roster.pack(fill="both", expand=True)
        self.user_viewport = app.skin(tk.Canvas(roster, highlightthickness=0, borderwidth=0,
                                                yscrollincrement=USER_SCROLL_STEP), "panel", None)
        self.user_viewport.pack(side="left", fill="both", expand=True)
        app.overlay_scrollbar(roster, self.user_viewport)
        self.user_list = app.frame(self.user_viewport)
        window = self.user_viewport.create_window(0, 0, anchor="nw", window=self.user_list)
        self.user_viewport.bind("<Configure>", lambda event: self.user_viewport.itemconfigure(window, width=event.width))
        self.user_list.bind("<Configure>", self.resize_user_list)
        self.user_viewport.bind("<MouseWheel>", self.scroll_users)
        self.user_list.bind("<MouseWheel>", self.scroll_users)
        self.no_users = app.label(self.user_list, muted=True)

        footer = app.frame(frame)
        footer.pack(fill="x", pady=(9, 0))
        self.user_summary = tk.StringVar(frame, "正在读取账号…")
        app.label(footer, variable=self.user_summary, muted=True).pack(side="left")
        self.search.trace_add("write", self.filter_users)
        self.status_filter.trace_add("write", self.filter_users)

    def resize_user_list(self, _event):
        self.user_viewport.configure(scrollregion=self.user_viewport.bbox("all"))

    def scroll_users(self, event):
        self.user_wheel_delta += event.delta
        steps = int(self.user_wheel_delta / 120)
        self.user_wheel_delta -= steps * 120
        self.user_viewport.yview_scroll(-3 * steps, "units")
        return "break"

    def build_user_card(self, identity, user):
        app = self.app
        card = app.skin(tk.Frame(self.user_list, highlightthickness=1, padx=16, pady=13),
                        "field", None, highlightbackground="border", highlightcolor="border")
        card.columnconfigure(0, weight=1)
        name = app.label(card, user["username"], bg="field", size=13, bold=True)
        name.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        def wrap_name(event):
            width = max(event.width - 4, 24)
            if int(name.cget("wraplength")) != width:
                name.configure(wraplength=width)

        name.bind("<Configure>", wrap_name)
        background, color = USER_STATUS_COLORS[user["status"]]
        badge = app.label(card, ACCOUNT_STATUS[user["status"]], bg=background, color=color, size=9, bold=True)
        badge.configure(padx=8, pady=3)
        badge.grid(row=0, column=1, sticky="ne")

        role = "管理员" if user["role"] == "admin" else "普通用户"
        registered = account_time(user.get("created_at"))
        app.label(card, f"{role}  ·  注册于 {registered}  ·  执行任务 {count(user.get('running_tasks'))}", bg="field", muted=True).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        if user["role"] == "user":
            actions = app.frame(card, "field")
            actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
            for title, status in USER_STATUS_ACTIONS[user["status"]]:
                style = ("Accent.TButton" if status == "active" else
                         "Danger.TButton" if status == "rejected" else "TButton")
                button = app.button(actions, title, lambda target=status: self.update_status(identity, target),
                                    style)
                button.pack(side="left", padx=(0, 8))
            app.button(actions, "重置登录密码", lambda: self.reset_password(identity)).pack(side="left")
            for button in actions.winfo_children():
                button.bind("<MouseWheel>", self.scroll_users)
                button.bind("<FocusIn>", self.reveal_user_action)
            quota = app.frame(card, "field")
            quota.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            app.label(quota, balance_text(user), bg="field", muted=True).pack(side="left")
            app.button(quota, "设置额度", lambda: self.edit_quota(identity)).pack(side="left", padx=(12, 0))

        card.bind("<MouseWheel>", self.scroll_users)
        for child in card.winfo_children():
            child.bind("<MouseWheel>", self.scroll_users)
        return card

    def edit_quota(self, identity):
        app = self.app
        if not app.is_admin or app.busy or app.closing:
            return
        user = self.users.get(identity)
        if not user or user["role"] != "user":
            return
        window = tk.Toplevel(app.root)
        window.title(f"设置 Token 额度 · {user['username']}")
        window.transient(app.root)
        window.resizable(False, False)
        app.skin(window, "panel", None)
        body = app.frame(window)
        body.pack(padx=22, pady=18, fill="both")
        current = snapshot(user)
        mode = tk.StringVar(window, current["mode"] if current else "unlimited")
        amount = tk.StringVar(window, str(current["limit_tokens"] / 1000) if current and current["mode"] == "limited" else "100")
        app.label(body, f"{user['username']} 的 Token 额度", size=14, bold=True).pack(anchor="w", pady=(0, 12))
        ttk.Radiobutton(body, text="不限额", variable=mode, value="unlimited").pack(anchor="w")
        row = app.frame(body)
        row.pack(fill="x", pady=7)
        ttk.Radiobutton(row, text="限额", variable=mode, value="limited").pack(side="left")
        entry = ttk.Entry(row, textvariable=amount, width=17)
        entry.pack(side="left", padx=(8, 5))
        app.label(row, "k Token").pack(side="left")
        mode.trace_add("write", lambda *_: entry.configure(state="normal" if mode.get() == "limited" else "disabled"))
        entry.configure(state="normal" if mode.get() == "limited" else "disabled")
        app.label(body, "1k = 1000 Token，按实际输入 + 输出扣减。\n保存将重新发放所填额度，之前用量保留审计记录。", muted=True).pack(anchor="w", pady=(4, 10))
        notice = tk.StringVar(window)
        app.label(body, variable=notice, color="remove").pack(anchor="w")

        def save():
            if not app.is_admin or app.busy or app.closing or app.account_client is not self.client:
                return
            try:
                limit = parse_k(amount.get()) if mode.get() == "limited" else None
            except ValueError as error:
                notice.set(str(error))
                return
            selected_mode = mode.get()

            def done(_reply):
                if window.winfo_exists():
                    window.destroy()
                if self.parent.winfo_exists():
                    self.refresh_users()

            def failed(error):
                if window.winfo_exists():
                    notice.set(str(error))

            app.worker("保存用户额度", lambda: self.client.set_user_quota(user["id"], selected_mode, limit), done, failed)

        app.button(body, "保存额度", save, "Accent.TButton").pack(anchor="e", pady=(10, 0))
        window.bind("<Escape>", lambda _event: window.destroy())
        app.apply_theme()
        window.grab_set()

    def reveal_user_action(self, event, viewport=None):
        viewport = viewport or self.user_viewport
        top = event.widget.winfo_rooty() - viewport.winfo_rooty()
        bottom = top + event.widget.winfo_height() - viewport.winfo_height()
        if top < 0:
            viewport.yview_scroll(top // USER_SCROLL_STEP, "units")
        elif bottom > 0:
            viewport.yview_scroll((bottom + USER_SCROLL_STEP - 1) // USER_SCROLL_STEP, "units")

    def build_model(self):
        app = self.app
        self.model_viewport = app.skin(tk.Canvas(self.model_tab, highlightthickness=0, borderwidth=0,
                                                yscrollincrement=USER_SCROLL_STEP), "panel", None)
        self.model_viewport.pack(fill="both", expand=True, padx=12, pady=12)
        scrollbar = app.overlay_scrollbar(self.model_tab, self.model_viewport)
        frame = app.frame(self.model_viewport)
        content = self.model_viewport.create_window(0, 0, anchor="nw", window=frame)
        self.model_viewport.bind("<Configure>", lambda event: self.model_viewport.itemconfigure(content, width=event.width))
        frame.bind("<Configure>", lambda _event: self.model_viewport.configure(scrollregion=self.model_viewport.bbox("all")))
        self.model_fields, self.model_field_labels = {}, {}
        app.label(frame, "模型基本信息", muted=True, size=9).pack(fill="x", pady=(0, 5))
        heading = app.frame(frame)
        heading.pack(fill="x", pady=(0, 8))
        self.model_title = tk.StringVar(frame, "模型运行状态")
        self.model_meta = tk.StringVar(frame)
        self.refresh_model_button = app.button(heading, "刷新状态", self.refresh_model)
        self.refresh_model_button.pack(side="right", padx=(12, 0))
        title = app.label(heading, variable=self.model_title, size=15, bold=True)
        title.pack(side="left", fill="x", expand=True)
        self.wrap_model_label(title)
        meta = app.label(frame, variable=self.model_meta, muted=True)
        meta.pack(fill="x", pady=(0, 9))
        meta.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(event.width - 8, 100)))
        metric_section = self.model_section(frame, "核心运行指标")
        metrics = app.frame(metric_section)
        metrics.pack(fill="x")
        cards = []
        self.model_metrics = []
        for text in ("总上下文 · Token", "并发槽位", "每槽上下文 · Token", "Gateway 输出上限", "空余槽位", "GPU 显存使用率"):
            card = app.frame(metrics, "field")
            caption = app.label(card, text, bg="field", muted=True, size=9)
            caption.pack(fill="x", padx=12, pady=(10, 3))
            self.wrap_model_label(caption)
            value = tk.StringVar(card, "—")
            label = app.label(card, variable=value, bg="field", size=15, bold=True, color="heading")
            label.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
            self.wrap_model_label(label)
            self.model_metrics.append(value)
            cards.append(card)
        self.model_flow(metrics, cards, 150, column_options=(6, 3, 2, 1))

        service = self.model_section(frame, '模型服务控制')
        for title, fields, columns in (
            ('模型与部署', (('service_model', '当前模型名称'), ('service_backend', '推理后端'), ('service_gpu', '部署 GPU')), (3, 2, 1)),
            ('服务运行状态', (('service_state', '模型服务运行状态'), ('service_api', 'API 可用状态'), ('service_pid', '模型进程 PID')), (3, 2, 1)),
            ('显存与启动评估', (('service_memory', '模型进程显存占用'), ('service_release', '预计可释放显存（估算）'),
                ('service_budget', '启动显存需求（历史样本 + 余量）'), ('service_released', '上次停止的GPU已用显存变化（实测）')), (2, 1)),
            ('时间记录', (('service_started', '最近一次启动时间'), ('service_checked', '最近一次状态核验时间')), (2, 1)),
        ):
            app.label(service, title, muted=True, size=9).pack(fill='x', pady=(5, 4))
            group = app.frame(service)
            group.pack(fill='x', pady=(0, 4))
            self.model_flow(group, [self.model_field(group, key, name, bg='panel') for key, name in fields],
                            265, column_options=columns)
        service_actions = app.frame(service)
        service_actions.pack(fill='x', pady=(8, 8))
        self.service_buttons = {}
        self.service_busy = False
        for action, name in (('start', '启动模型'), ('stop', '停止模型'), ('restart', '重启模型')):
            self.service_buttons[action] = app.button(service_actions, name, lambda action=action: self.service_preview(action))
            self.service_buttons[action].configure(state='disabled')
        self.model_flow(service_actions, list(self.service_buttons.values()), 180)
        self.service_note = tk.StringVar(frame, '正在读取真实服务状态…')
        service_note = app.label(service, variable=self.service_note)
        service_note.pack(fill='x')
        self.wrap_model_label(service_note)

        parameters = self.model_section(frame, "模型参数设置")
        fields = app.frame(parameters)
        fields.pack(fill="x")
        self.total = tk.StringVar(frame)
        self.slots = tk.StringVar(frame)
        self.context_choice = tk.StringVar(frame)
        choices = []
        for title_text in ("目标总上下文", "目标并发槽位", "预计每槽上下文（估算）"):
            cell = app.frame(fields)
            caption = app.label(cell, title_text, muted=True, size=9)
            caption.pack(fill="x", pady=(0, 5))
            self.wrap_model_label(caption)
            choices.append(cell)
        self.context_selector = ttk.Combobox(choices[0], textvariable=self.context_choice,
                                             values=tuple(CONTEXT_LABELS.values()), state="readonly", width=18)
        self.context_selector.pack(fill="x")
        self.context_selector.bind("<<ComboboxSelected>>", lambda _event: self.total.set(next(
            (value for value, label in CONTEXT_LABELS.items() if label == self.context_choice.get()), "")))
        ttk.Combobox(choices[1], textvariable=self.slots, values=SLOT_OPTIONS,
                     state="readonly", width=5).pack(fill="x")
        self.estimated_context = tk.StringVar(frame, "—")
        estimate = app.label(choices[2], variable=self.estimated_context, bold=True, color="heading")
        estimate.pack(fill="x", pady=4)
        self.wrap_model_label(estimate)
        self.model_flow(fields, choices, 230)
        hint = app.label(parameters, "估算仅供选择参考，实际生效容量以后端核验结果为准。", muted=True, size=9)
        hint.pack(fill="x", pady=(2, 9))
        self.wrap_model_label(hint)
        actions = app.frame(parameters)
        actions.pack(fill="x", pady=(0, 9))
        self.default_model_button = app.button(actions, "恢复默认值", lambda: self.total.set("131072"))
        self.resync_button = app.button(actions, "重新同步", self.resynchronize)
        self.preview_button = app.button(actions, "保存服务器设置", self.preview, "Accent.TButton")
        self.model_flow(actions, [self.default_model_button, self.resync_button, self.preview_button], 180)
        self.sync_state = tk.StringVar(frame, "状态未知")
        sync = self.model_section(frame, "上下文配置与同步状态")
        self.sync_state_label = app.label(sync, variable=self.sync_state, bold=True)
        self.sync_state_label.pack(fill="x", pady=(0, 10))
        self.wrap_model_label(self.sync_state_label)
        self.sync_state.trace_add("write", lambda *_: self.model_tone(self.sync_state_label, self.sync_state.get()))
        backend = self.model_shelf(sync, "llama.cpp 后端")
        backend.pack(fill="x", pady=(0, 12))
        backend_fields = app.frame(backend, "field")
        backend_fields.pack(fill="x")
        cells = [self.model_field(backend_fields, key, name) for key, name in (
            ("saved_context", "已保存总上下文"), ("backend_context", "实际每槽上下文"),
            ("backend_slots", "并发槽位"), ("backend_state", "后端核验状态"))]
        self.model_flow(backend_fields, cells, 175)
        columns = app.frame(sync)
        columns.pack(fill="x")
        shelves = []
        for prefix, name, names in (
            ("builtin", "Windows 内置 OpenCode", ("新任务目标上下文", "当前配置", "实际加载上下文", "配置同步状态")),
            ("independent", "Ubuntu 独立 OpenCode", ("目标上下文", "当前配置文件值", "实际加载上下文", "配置同步状态")),
        ):
            shelf = self.model_shelf(columns, name)
            for suffix, text in zip(("target", "configured", "loaded", "state"), names):
                self.model_field(shelf, f"{prefix}_{suffix}", text).pack(fill="x")
            shelves.append(shelf)
        self.model_flow(columns, shelves, 400)

        gpu = self.model_section(frame, "服务器 GPU 资源")
        gpu_summary = app.frame(gpu)
        gpu_summary.pack(fill="x")
        cells = [self.model_field(gpu_summary, key, name, bg="panel") for key, name in (
            ("gpu_name", "GPU 编号及型号"), ("gpu_memory", "已用显存 / 总显存"), ("gpu_percent", "显存使用率"))]
        self.model_flow(gpu_summary, cells, 230)
        self.gpu_usage = tk.DoubleVar(frame, 0)
        self.gpu_bar = ttk.Progressbar(gpu, variable=self.gpu_usage, maximum=100, length=1,
                                       style="Context.Horizontal.TProgressbar")
        self.gpu_bar.pack(fill="x", pady=(0, 12))
        gpu_details = app.frame(gpu)
        gpu_details.pack(fill="x")
        cells = [self.model_field(gpu_details, key, name, bg="panel") for key, name in (
            ("gpu_free", "驱动报告剩余显存"), ("gpu_process", "llama-server 进程显存"),
            ("gpu_difference", "显存统计差额 · total − used − free"), ("gpu_sampled", "最近采样时间 · 服务器"))]
        self.model_flow(gpu_details, cells, 280)

        times = self.model_section(frame, "配置时间及高级详情")
        time_fields = app.frame(times)
        time_fields.pack(fill="x")
        self.model_flow(time_fields, [self.model_field(time_fields, key, name, bg="panel") for key, name in (
            ("applied_at", "最近应用时间"), ("checked_at", "最近核验时间"))], 280)
        self.advanced_toggle = app.button(times, "▸ 高级配置详情", self.toggle_model_details)
        self.advanced_toggle.pack(anchor="w", pady=(3, 0))
        self.advanced_card = app.frame(times, "field")
        self.advanced_text = tk.StringVar(frame)
        details = app.label(self.advanced_card, variable=self.advanced_text, bg="field", size=9)
        details.pack(fill="x", padx=12, pady=(10, 5))
        self.wrap_model_label(details)
        self.copy_model_button = app.button(self.advanced_card, "复制配置详情", self.copy_model_details)
        self.copy_model_button.pack(anchor="w", padx=12, pady=(0, 10))

        app.label(parameters, "操作提示", muted=True, size=9).pack(fill="x", pady=(2, 3))
        self.model_note = tk.StringVar(frame, "正在读取服务状态…")
        note = app.label(parameters, variable=self.model_note)
        note.pack(fill="x")
        self.wrap_model_label(note)
        self.model_note.trace_add("write", lambda *_: self.model_tone(note, self.model_note.get()))

        self.review_card = app.frame(parameters, "field")
        review = self.review_card
        app.label(review, "核对配置变更", bg="field", size=12, bold=True).pack(fill="x", padx=13, pady=(9, 4))
        self.review_text = tk.StringVar(frame)
        details = app.label(review, variable=self.review_text, bg="field")
        details.pack(fill="x", padx=13)
        details.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(event.width - 8, 100)))
        buttons = app.frame(review, "field")
        buttons.pack(fill="x", padx=13, pady=(6, 9))
        app.button(buttons, "确认应用", self.apply_preview, "Accent.TButton").pack(side="right")
        app.button(buttons, "取消预览", self.cancel_preview).pack(side="right", padx=(0, 8))
        self.total.trace_add("write", self.parameters_changed)
        self.slots.trace_add("write", self.parameters_changed)
        self.parameters_changed()

        def bind_scroll(widget):
            widget.bind("<MouseWheel>", scrollbar._wheel)
            if isinstance(widget, (ttk.Button, ttk.Combobox)):
                widget.bind("<FocusIn>", lambda event: self.reveal_user_action(event, self.model_viewport))
            for child in widget.winfo_children():
                bind_scroll(child)
        bind_scroll(self.model_viewport)

    @staticmethod
    def wrap_model_label(label):
        # The parent allocates width. Text must not grow its own requested width
        # on every wrap/Configure pass, including while the tab is unmapped.
        label.configure(width=1, wraplength=240)

        def wrap(event):
            width = max(1, event.width - 4)
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)

        label.bind("<Configure>", wrap)

    def model_section(self, parent, title):
        section = self.app.frame(parent)
        section.pack(fill="x", pady=(16, 0))
        self.app.label(section, title, size=12, bold=True).pack(fill="x", pady=(0, 10))
        return section

    def model_shelf(self, parent, title):
        shelf = self.app.frame(parent, "field")
        self.app.label(shelf, title, bg="field", size=11, bold=True).pack(fill="x", padx=12, pady=(10, 7))
        return shelf

    def model_field(self, parent, key, title, bg="field"):
        cell = self.app.frame(parent, bg)
        label = self.app.label(cell, title, bg=bg, muted=True, size=9)
        label.pack(fill="x", padx=12, pady=(0, 3))
        self.wrap_model_label(label)
        value = self.model_fields[key] = tk.StringVar(cell, "尚未读取")
        label = self.app.label(cell, variable=value, bg=bg)
        label.pack(fill="x", padx=12, pady=(0, 10))
        self.wrap_model_label(label)
        self.model_field_labels[key] = label
        return cell

    @staticmethod
    def model_flow(parent, cells, minimum, column_options=None):
        """Reflow only model-page groups; the existing account cards stay unchanged."""
        previous = 0

        def layout(event):
            nonlocal previous
            columns = max(1, min(len(cells), event.width // minimum))
            if column_options:
                columns = max(option for option in column_options if option <= columns)
            if columns == previous:
                return
            for column in range(len(cells)):
                parent.columnconfigure(column, weight=1 if column < columns else 0,
                                       uniform="model-flow" if column < columns else "")
            for index, cell in enumerate(cells):
                cell.grid(row=index // columns, column=index % columns, sticky="nsew",
                          padx=(0, 10 if index % columns < columns - 1 else 0), pady=(0, 8))
            previous = columns

        parent.bind("<Configure>", layout)

    def model_tone(self, widget, text, tone=None):
        if tone is None:
            tone = ("error" if any(word in text for word in ("失败", "不一致")) else
                    "success" if text.startswith("配置已就绪") else
                    "warning" if any(word in text for word in ("待", "部分", "正在", "忙碌", "未保存")) else
                    "success" if any(word in text for word in ("已同步", "已核验", "已加载")) else "muted")
        for index, (item, bg, _fg, extra) in enumerate(self.app.themed):
            if item is widget:
                self.app.themed[index] = (item, bg, tone, extra)
                break
        widget.configure(foreground=self.app.colors[tone])

    def set_model_field(self, key, text, tone="text"):
        self.model_fields[key].set(text)
        self.model_tone(self.model_field_labels[key], text, tone)

    def toggle_model_details(self):
        opened = bool(self.advanced_card.winfo_manager())
        if opened:
            self.advanced_card.pack_forget()
        else:
            self.advanced_card.pack(fill="x", pady=(8, 0))
        self.advanced_toggle.configure(text=("▸" if opened else "▾") + " 高级配置详情")

    def copy_model_details(self):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(self.advanced_text.get())
        self.copy_model_button.configure(text="已复制配置详情")

    def draw_model_fields(self, model, saved, backend, builtin, independent):
        def tokens(value, unknown="尚未读取"):
            return f"{value:,} Token" if type(value) is int and value > 0 else unknown

        def memory(value):
            return f"{value:,} MiB" if type(value) is int else "未知"

        engine = self.app.engine
        limits = getattr(engine, "limits", (None,))
        configured = limits[0] if limits else None
        loaded = getattr(engine, "loaded_limits", None) or {}
        process = getattr(engine, "process", None)
        running = process is not None and process.poll() is None
        loaded_context = loaded.get("context") if running else None
        target = builtin.get("context_limit") or backend.get("effective_context_limit")
        local_sync = getattr(self.app, "model_engine_sync", {})
        if (local_sync.get("limits") != self.app.model_limits(model, self.app.output_limit)
                or local_sync.get("generation") != self.app.account_generation):
            local_sync = {}
        builtin_state = ("已加载" if loaded_context == target and loaded_context else
                         "待重新加载" if loaded_context else "尚未核验" if running else "未启动")
        self.set_model_field("saved_context", tokens(saved.get("total_context")))
        self.set_model_field("backend_context", tokens(backend.get("effective_context_limit"), "尚未核验"))
        self.set_model_field("backend_slots", count(model.get("slots")))
        backend_state = {"synced": "已核验", "out_of_sync": "配置不一致"}.get(backend.get("status"), "尚未核验")
        if backend_state == "已核验" and not backend.get("effective_context_limit"):
            backend_state = "尚未核验"
        self.set_model_field("backend_state", backend_state, None)
        self.set_model_field("builtin_target", tokens(target))
        self.set_model_field("builtin_configured", tokens(configured, "预算已就绪，待引擎启动" if target else "尚未读取"))
        self.set_model_field("builtin_loaded", tokens(loaded_context, "尚未核验" if running else "未启动"))
        builtin_sync = ("正在同步" if local_sync.get("status") == "syncing" else
                        "同步失败" if local_sync.get("status") == "error" else
                        "配置已就绪，待引擎启动" if not running and target else
                        "待重新加载" if local_sync.get("status") == "pending_reload" else
                        "配置不一致" if configured and target and configured != target else
                        "已同步" if builtin_state == "已加载" and configured == target else
                        "待重新加载" if configured and target and loaded_context else "尚未核验")
        self.set_model_field("builtin_state", builtin_sync, None)
        independent_target = target or backend.get("effective_context_limit")
        self.set_model_field("independent_target", tokens(independent_target))
        self.set_model_field("independent_configured", tokens(independent.get("configured_context_limit"), "未知"))
        self.set_model_field("independent_loaded", tokens(independent.get("effective_context_limit"), "尚未核验"))
        independent_state = {"pending_reload": "待重新加载", "error": "同步失败", "out_of_sync": "配置不一致",
                             "ready": "配置已就绪，待启动", "syncing": "正在同步", "partial": "部分同步",
                             "unmanaged": "未纳管", "unverified": "尚未核验"}.get(independent.get("status"), "尚未核验")
        if independent.get("status") == "ready":
            if independent.get("instance_count") == 0 and independent.get("configured_context_limit") == independent_target:
                self.set_model_field("independent_loaded", "未启动")
            else:
                independent_state = "尚未核验"
        if independent.get("status") == "synced":
            independent_state = ("已同步" if independent_target and
                                 independent.get("effective_context_limit") == independent_target and
                                 independent.get("configured_context_limit") == independent_target else "尚未核验")
        self.set_model_field("independent_state", independent_state, None)

        gpu = model.get("gpu") if isinstance(model.get("gpu"), dict) else {}
        used, total = gpu.get("used_mib"), gpu.get("total_mib")
        valid = type(used) is int and type(total) is int and total > 0 and 0 <= used <= total
        percentage = used / total * 100 if valid else None
        percent_text = f"{percentage:.1f}%" if percentage is not None else "未知"
        for variable, value in zip(self.model_metrics, (
            count(model.get("total_context")), count(model.get("slots")), count(model.get("effective_context_limit")),
            count(model.get("output_token_limit")), count(model.get("available_slots")), percent_text)):
            variable.set(value)
        gpu_name = f"GPU {gpu['index']} · {gpu.get('name') or '型号未知'}" if type(gpu.get("index")) is int else "未知"
        self.set_model_field("gpu_name", gpu_name)
        self.set_model_field("gpu_memory", gpu_memory(model) if valid else "未知")
        self.set_model_field("gpu_percent", percent_text)
        self.gpu_usage.set(percentage if percentage is not None else 0)
        if valid and not self.gpu_bar.winfo_manager():
            self.gpu_bar.pack(fill="x", pady=(0, 12), before=self.gpu_bar.master.winfo_children()[-1])
        elif not valid:
            self.gpu_bar.pack_forget()
        for key, value in (("gpu_free", "free_mib"), ("gpu_process", "model_process_mib"),
                           ("gpu_difference", "unaccounted_mib")):
            self.set_model_field(key, memory(gpu.get(value)))
        sampled = gpu.get("sampled_at")
        sampled_text = account_time(sampled)
        if sampled_text != "—":
            sampled_text = datetime.fromtimestamp(sampled).strftime("%Y-%m-%d %H:%M:%S")
        self.set_model_field("gpu_sampled", sampled_text if sampled_text != "—" else "未知")
        self.set_model_field("applied_at", account_time(model.get("last_applied_at")))
        self.set_model_field("checked_at", account_time(backend.get("ready_checked_at")))
        result = model.get("last_configuration_result") or {}
        self.advanced_text.set(
            f"配置版本\n{model.get('configuration_version') or '未知'}\n\n"
            f"后端诊断\n{backend.get('detail') or backend_state}\n\n"
            f"Windows 引擎\n配置 limits：{limits}；加载 limits：{loaded if running else '未启动'}\n"
            f"{getattr(self, 'builtin_read_error', '') or local_sync.get('detail') or builtin_sync}\n\n"
            f"Ubuntu 独立 OpenCode\n{independent.get('detail') or independent_state}\n\n"
            + ''.join(f"PID {item.get('pid')}：实际 {tokens(item.get('effective_context_limit'), '尚未核验')}\n"
                      for item in independent.get('instances', []) if isinstance(item, dict)) + '\n'
            +
            f"最近操作结果\n{result or '无记录'}\n\n"
            f"服务诊断\n{model.get('detail') or '无额外诊断'}")
        self.copy_model_button.configure(text="复制配置详情")
        return builtin_state

    def model_synchronization_state(self, model, backend):
        last_result = (model.get("last_configuration_result") or {}).get("status")
        if last_result in {"rollback_failed", "rolled_back"}:
            return "恢复失败" if last_result == "rollback_failed" else "应用失败，已恢复原配置"
        if type(model.get("running_tasks")) is int and model['running_tasks'] > 0:
            return "服务器忙碌"
        if backend.get('status') != 'synced' or not backend.get('effective_context_limit'):
            return "配置不一致" if backend.get('status') == 'out_of_sync' else "状态未知"
        states = [self.model_fields[key].get() for key in ('builtin_state', 'independent_state')]
        if any(state in {'同步失败', '配置不一致', '部分同步'} for state in states):
            return '部分同步'
        if '正在同步' in states:
            return '正在同步'
        ready = {'已同步', '配置已就绪，待引擎启动', '配置已就绪，待启动'}
        if all(state in ready for state in states):
            return '已同步（当前客户端及受管独立实例）' if states == ['已同步', '已同步'] else '配置已就绪'
        return '待重新加载' if '待重新加载' in states else '尚未核验'

    def metrics(self, parent, names):
        app = self.app
        row = app.frame(parent)
        row.pack(fill="x")
        values = []
        for column, title in enumerate(names):
            row.columnconfigure(column, weight=1, uniform="metric")
            card = app.frame(row, "field")
            card.grid(row=0, column=column, sticky="ew", padx=(0, 8 if column < len(names) - 1 else 0))
            app.label(card, title, bg="field", muted=True, size=9).pack(fill="x", padx=11, pady=(7, 0))
            value = tk.StringVar(card, "—")
            label = app.label(card, variable=value, bg="field", size=14, bold=True, color="heading")
            label.pack(fill="x", padx=11, pady=(0, 7))
            label.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(event.width - 4, 40)))
            values.append(value)
        return values

    def owns_session(self):
        return self.parent.winfo_exists() and self.app.is_admin and self.app.account_client is self.client

    def call(self, caption, method, path, body, done, *, timeout=10, error_text=None, on_error=None):
        app = self.app
        if app.closing or not self.owns_session():
            return
        if app.busy:
            if method == "GET":
                app.schedule(f"admin_read:{id(self)}:{path}", 100,
                             lambda: self.call(caption, method, path, body, done, timeout=timeout, error_text=error_text))
            return
        if path in {"/api/admin/model/preview", "/api/admin/model/apply"} and app.tasks.active():
            self.notice.set("请先结束客户端后台任务，再调整模型配置。")
            return
        self.notice.set(caption + "…")

        def complete(result):
            if self.owns_session():
                self.notice.set("")
                done(result)

        def failed(error):
            if self.owns_session():
                if error_text is not None:
                    error_text.set(str(error)[:650])
                    self.notice.set("")
                else:
                    self.notice.set(str(error)[:110])
                if on_error:
                    on_error(error)

        engine = app.engine

        def request():
            result = self.client.request(method, path, body, timeout=timeout)
            if method == 'GET' and path == '/api/model':
                self.builtin_read_error = ''
                process = getattr(engine, 'process', None)
                verify = getattr(engine, 'verify_loaded_limits', None)
                if engine is app.engine and process is not None and process.poll() is None and verify:
                    from engine import EngineError
                    try:
                        verify()  # Read-only /config, never a restart or write.
                    except (EngineError, OSError):
                        self.builtin_read_error = '无法读回内置引擎实际配置，尚未核验；可在空闲时重新同步。'
            return result

        app.worker(caption, request, complete, failed,
                   timeout=timeout + 30)

    def refresh_users(self):
        def draw(reply):
            users = {user["id"]: user for user in reply.get("users", []) if self.valid_user(user)}
            created = False
            for identity, card in tuple(self.user_cards.items()):
                if users.get(identity) != self.users.get(identity):
                    card.destroy()
                    del self.user_cards[identity]
            for identity, user in users.items():
                if identity not in self.user_cards:
                    self.user_cards[identity] = self.build_user_card(identity, user)
                    created = True
            self.users = users
            if created:
                self.app.apply_theme()
            for variable, status in zip(self.account_overview, ACCOUNT_STATUS):
                variable.set(count(sum(user["status"] == status for user in self.users.values())))
            self.draw_users()
            self.tab_changed(None)
        self.call("刷新账号", "GET", "/api/admin/users", None, draw, error_text=self.user_summary)

    def update_task_counts(self, stats):
        if not self.owns_session():
            return
        counts = stats.get("users")
        created = False
        for identity, user in self.users.items():
            count_now = counts.get(str(identity), counts.get(identity, 0)) if isinstance(counts, dict) else None
            if user.get("running_tasks") != count_now:
                user["running_tasks"] = count_now
                self.user_cards[identity].destroy()
                self.user_cards[identity] = self.build_user_card(identity, user)
                created = True
        if created:
            self.app.apply_theme()
        self.draw_users()
        self.model_metrics[4].set(count(stats.get("available_slots")))

    @staticmethod
    def valid_user(user):
        if not isinstance(user, dict):
            return False
        identity = user.get("id")
        return (type(identity) is int and 1 <= identity <= 2**63 - 1 and
                isinstance(user.get("username"), str) and user.get("role") in ("admin", "user") and
                isinstance(user.get("status"), str) and user["status"] in ACCOUNT_STATUS)

    def filter_users(self, *_args):
        self.draw_users()
        self.user_viewport.yview_moveto(0)

    def draw_users(self):
        query = self.search.get().strip().casefold()
        status = self.status_filter.get()
        displayed = []
        for identity, user in self.users.items():
            status_label = ACCOUNT_STATUS[user["status"]]
            if (query and query not in user["username"].casefold()) or (status != "全部状态" and status != status_label):
                continue
            displayed.append(self.user_cards[identity])
        packed = self.user_list.pack_slaves()
        if packed != (displayed or [self.no_users]):
            for card in packed:
                card.pack_forget()
            for card in displayed:
                card.pack(fill="x", padx=(0, 8), pady=(0, 12))
            if not displayed:
                self.no_users.pack(fill="x", padx=12, pady=12)
        if not displayed:
            self.no_users.configure(text="没有符合筛选条件的账号。" if self.users else "暂无账号。")
        self.user_summary.set(f"显示 {len(displayed)} / {len(self.users)} 个账号")

    def manageable_user(self, identity):
        if self.app.busy or not self.owns_session():
            return None
        user = self.users.get(identity)
        if user and user["role"] == "user":
            return user
        return None

    def update_status(self, identity, status):
        user = self.manageable_user(identity)
        if not user or not any(target == status for _, target in USER_STATUS_ACTIONS[user["status"]]):
            return
        if not messagebox.askyesno("确认账号状态",
                                  f"将 {user['username']} 的状态改为「{ACCOUNT_STATUS[status]}」？",
                                  parent=self.app.root):
            return
        if self.manageable_user(identity) != user:
            return
        path = f"/api/admin/users/{identity}/status"
        self.call("更新账号状态", "POST", path, {"status": status}, lambda _reply: self.refresh_users(),
                  error_text=self.user_summary)

    def reset_password(self, identity):
        if self.manageable_user(identity):
            self.app.password_reset_target = identity
            self.app.show_page("settings", section="passwords")

    def tab_changed(self, _event):
        if self.tabs.select() == str(self.model_tab) and not self.has_model:
            self.refresh_model()

    def parameters_changed(self, *_args):
        self.clear_review()
        self.context_choice.set(CONTEXT_LABELS.get(self.total.get(), ""))
        total, slots = self.total.get(), self.slots.get()
        self.estimated_context.set(f"{int(total) // int(slots):,} Token" if total in CONTEXT_OPTIONS
                                   and slots in SLOT_OPTIONS else "—")
        ready = (self.control_available and not getattr(self, 'service_busy', False) and self.total.get() in CONTEXT_OPTIONS
                 and self.slots.get() in SLOT_OPTIONS)
        self.preview_button.configure(state="normal" if ready else "disabled")
        self.resync_button.configure(state="normal" if self.control_available and self.server_configuration and not getattr(self, 'service_busy', False) else "disabled")
        draft = {"total_context": self.total.get(), "slots": self.slots.get()}
        saved = {key: str(value) for key, value in self.server_configuration.items()}
        self.sync_state.set("有未保存的更改" if self.has_model and draft != saved else
                            getattr(self, "server_sync_state", "状态未知"))

    def refresh_model(self, message=None, expected=None, apply_status=None):
        def draw(reply):
            model = reply.get("model", reply)
            draft = {"total_context": self.total.get(), "slots": self.slots.get()}
            preserve_draft = self.has_model and draft != {key: str(value) for key, value in self.server_configuration.items()}
            self.clear_review()
            self.has_model = True
            self.control_available = model.get("control_available") is True
            self.model_title.set(model_name(model))
            self.model_meta.set(f"{model_state(model)}  ·  模型 ID {model.get('model_id') or UNAVAILABLE}")
            sync = model.get("context_sync") or {}
            saved = sync.get("saved_configuration") or {}
            independent = sync.get("opencode") or {}
            builtin = sync.get("builtin") or {}
            backend = sync.get("backend") or {}
            builtin_state = self.draw_model_fields(model, saved, backend, builtin, independent)
            self.draw_model_service(model)
            configuration = saved or {"total_context": model.get("total_context"), "slots": model.get("slots")}
            total, slots = str(configuration.get("total_context")), str(configuration.get("slots"))
            self.server_configuration = configuration
            self.server_version = model.get("configuration_version")
            self.server_sync_state = ('模型服务已停止，实际上下文未加载' if (model.get('model_service') or {}).get('state') == 'stopped'
                                      else self.model_synchronization_state(model, backend))
            if not preserve_draft:
                self.total.set(total if total in CONTEXT_OPTIONS else "")
                self.slots.set(slots if slots in SLOT_OPTIONS else "")
            self.parameters_changed()
            self.control_detail = model.get("detail") or (
                "配置变更由服务器校验，并可能中断当前推理。" if self.control_available
                else "当前仅可查看运行状态；服务器未开放模型配置修改，请联系运维。"
            )
            status_message = message or ""
            if expected is not None:
                if (model.get("total_context"), model.get("slots")) == (
                        expected["total_context"], expected["slots"]):
                    status_message = ("部分完成：模型配置已保存且生效；独立 OpenCode 同步失败，可用相同目标重试。"
                                      if apply_status == "partially_applied" else
                                      "配置已保存；Gateway 重新读取的运行参数与目标一致。")
                else:
                    status_message = (
                        "服务端报告配置已应用，但当前运行状态未匹配目标；"
                        f"目标 {count(expected['total_context'])} Token / {count(expected['slots'])} 槽，"
                        f"运行状态 {count(model.get('total_context'))} Token / "
                        f"{count(model.get('slots'))} 槽，尚未确认生效。"
                    )
            if self.model_fields['builtin_state'].get() == '同步失败':
                status_message += ' ' + getattr(self.app, 'model_engine_sync', {}).get('detail', '内置引擎同步失败。')
            self.model_note.set(f"{status_message}  {self.control_detail}" if status_message
                                else self.control_detail)
            self.app.update_model(model)
        self.call("刷新模型状态", "GET", "/api/model", None, draw, error_text=self.model_note)

    def draw_model_service(self, model):
        service = model.get('model_service') or {}
        self.service_snapshot = service
        unknown = '未知'
        memory = lambda value: f'{value:,} MiB' if type(value) is int else unknown
        budget = service.get('startup_estimate') or {}
        values = dict(service_model=service.get('model_name') or model.get('model_name') or unknown,
                      service_backend=service.get('backend') or unknown,
                      service_gpu=f"GPU{service['gpu_index']}" if type(service.get('gpu_index')) is int else unknown,
                      service_state=service.get('state_label') or '状态未知',
                      service_api='可用' if service.get('api_available') else '不可用' if 'api_available' in service else unknown,
                      service_pid=str(service.get('pid') or ('未运行' if service.get('state') == 'stopped' else unknown)),
                      service_memory=memory(service.get('model_process_mib')),
                      service_release=memory(service.get('estimated_release_mib')),
                      service_released=memory(service.get('observed_released_mib')),
                      service_started=service.get('last_started_at') or unknown,
                      service_checked=account_time(service.get('checked_at')),
                      service_budget=memory(budget.get('required_mib')))
        if service.get('state') == 'stopped':
            values.update(service_memory='未加载', service_release='未加载')
            for metric in self.model_metrics[:3]:
                metric.set('未加载')
            self.set_model_field('backend_context', '未加载')
            self.set_model_field('backend_slots', '未运行')
            self.set_model_field('backend_state', '模型已停止', 'muted')
            self.set_model_field('gpu_process', '未运行')
        for key, value in values.items():
            self.set_model_field(key, value)
        phase = service.get('state')
        self.set_model_field('service_state', values['service_state'], 'success' if phase == 'running' else
                             'warning' if phase in {'starting','stopping'} else 'error' if phase in {'error','start_failed'} else 'muted')
        for action, button in self.service_buttons.items():
            button.configure(state='normal' if service.get('can_' + action) and not self.service_busy else 'disabled')
        self.service_note.set(getattr(self, 'service_result', '') or service.get('detail') or ('模型离线期间保留登录、账号及额度服务；参数应用和重新同步暂停。'
                             if service.get('state') == 'stopped' else '服务状态来自服务器实时核验。'))

    def service_preview(self, operation):
        if self.service_busy or not self.owns_session():
            return
        names = {'start': '启动模型', 'stop': '停止模型', 'restart': '重启模型'}
        def confirm(reply):
            service = reply.get('service') or {}
            release = service.get('estimated_release_mib')
            estimate = service.get('startup_estimate') or {}
            text = (f"操作：{names[operation]}\n当前活动任务/租约：{count(reply.get('running_tasks'))}\n"
                    f"预计释放显存：{count(release)} MiB（估算）\n"
                    f"启动需求：{count(estimate.get('required_mib'))} MiB（历史样本及安全余量）\n"
                    f"GPU可用：{count(service.get('gpu_free_mib'))} MiB\n\n{reply.get('impact', '')}")
            if not messagebox.askyesno(names[operation], text, parent=self.parent):
                return
            self.service_busy = True
            self.clear_review()
            for button in self.service_buttons.values():
                button.configure(state='disabled')
            self.preview_button.configure(state='disabled')
            self.resync_button.configure(state='disabled')
            self.set_model_field('service_state', '正在停止' if operation == 'stop' else '正在启动' if operation == 'start' else '正在停止 → 正在启动')
            self.service_note.set('正在执行受控服务事务，请等待真实状态核验；请勿重复提交。')
            def finished(result):
                self.service_busy = False
                self.service_result = '最近操作：' + names[operation] + '完成，已核验真实服务状态。'
                updated = result.get('model')
                if updated:
                    self.app.update_model(updated)
                def refreshed():
                    self.refresh_model(message=names[operation] + '完成，已重新核验状态。')
                if operation != 'stop':
                    self.app.reload_idle_model_engine(on_done=refreshed)
                else:
                    refreshed()
            def failed(error):
                self.service_busy = False
                self.service_result = '最近操作失败：' + str(error)
                self.refresh_model(message=str(error))
            self.call(names[operation], 'POST', '/api/admin/model/service/apply',
                      {'confirmation': reply.get('confirmation')}, finished, timeout=280,
                      error_text=self.service_note, on_error=failed)
        self.call('预览模型服务操作', 'POST', '/api/admin/model/service/preview', {'operation': operation},
                  confirm, error_text=self.service_note)

    def resynchronize(self):
        target = self.server_configuration
        if str(target.get("total_context")) in CONTEXT_OPTIONS and str(target.get("slots")) in SLOT_OPTIONS:
            self.preview(requested=dict(target), resync=True)

    def preview(self, requested=None, resync=False):
        if not self.control_available:
            self.model_note.set(self.control_detail)
            return
        if not resync and (self.total.get() not in CONTEXT_OPTIONS or self.slots.get() not in SLOT_OPTIONS):
            return
        requested = requested or {"total_context": int(self.total.get()), "slots": int(self.slots.get())}
        version = self.server_version
        self.clear_review()

        def draw(reply):
            shown = {"total_context": self.total.get(), "slots": self.slots.get()}
            if not resync and shown != {key: str(value) for key, value in requested.items()}:
                return
            if version != self.server_version:
                return
            ticket = reply.get("confirmation")
            seconds = reply.get("expires_in")
            if not ticket or type(seconds) is not int or seconds <= 0:
                self.model_note.set(str(reply.get("reason") or "服务端当前未开放配置应用。"))
                return
            before, after = reply.get("current", {}), reply.get("requested", requested)
            risk = reply.get("gpu_risk") or "暂不可用"
            if isinstance(risk, list):
                risk = "，".join(map(str, risk))
            before_label = f"{count(before.get('total_context'))} Token / {count(before.get('slots'))} 槽"
            after_label = f"{count(after.get('total_context'))} Token / {count(after.get('slots'))} 槽"
            restart = "需重启模型服务" if reply.get("restart_required", True) else "无需重启"
            independent = reply.get("opencode") or {}
            target_scope = ("同步受管 Ubuntu 独立 OpenCode；重新启动/加载后使用新配置。"
                            if independent.get("managed") is True else
                            "Ubuntu 独立 OpenCode 未纳管，不修改。" if independent.get("managed") is False else
                            "Ubuntu 独立 OpenCode 纳管状态未核实。")
            self.review_text.set(
                f"{before_label}  →  {after_label}    ·    {restart}\n"
                f"预计每槽 {count(reply.get('estimated_slot_context'))} Token（估算）    ·    显存风险：{risk}\n"
                f"在线用户影响：{reply.get('user_impact') or '当前推理可能中断。'}\n"
                f"Labcoding 内置 OpenCode 新任务使用实际每槽上下文。{target_scope}"
            )
            self.review_ticket = ticket
            self.review_requested = dict(requested)
            self.review_deadline = monotonic() + seconds
            self.review_card.pack(fill="x", pady=(9, 0))
            self.model_note.set(f"预览不会保存配置；请在 {seconds} 秒内点击“确认应用”完成保存。")
        body = dict(requested)
        if version:
            body["expected_version"] = version
        self.call("预览模型配置", "POST", "/api/admin/model/preview", body, draw, error_text=self.model_note)

    def clear_review(self):
        self.review_ticket = None
        self.review_requested = None
        self.review_deadline = None
        self.review_card.pack_forget()

    def cancel_preview(self):
        self.clear_review()
        self.model_note.set(self.control_detail)

    def apply_preview(self):
        if self.app.busy or not self.owns_session() or not self.review_ticket:
            return
        ticket, deadline = self.review_ticket, self.review_deadline
        expected = dict(self.review_requested)
        self.clear_review()
        if deadline <= monotonic():
            self.model_note.set("预览已过期，请重新获取。")
            return

        def applied(reply):
            if reply.get("status") not in {"applied", "partially_applied"}:
                self.model_note.set("服务端未确认配置已应用；请刷新模型状态并检查服务端响应。")
                return
            updated = reply.get("model")
            if isinstance(updated, dict):
                self.app.update_model(updated)
            self.sync_state.set("正在同步")
            self.set_model_field("builtin_state", "正在同步", None)
            self.app.reload_idle_model_engine(on_done=lambda: self.refresh_model(
                "正在核对模型运行状态。", expected=expected, apply_status=reply.get("status")))
        self.model_note.set("正在应用，服务会验证实际配置。")
        self.sync_state.set("正在应用")
        self.call("应用模型配置", "POST", "/api/admin/model/apply", {"confirmation": ticket},
                  applied, timeout=280, error_text=self.model_note,
                  on_error=lambda error: self.refresh_model(message=str(error)[:650]))
