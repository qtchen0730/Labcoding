"""Embedded account and model administration for the desktop shell."""

from datetime import datetime
from time import monotonic
import tkinter as tk
from tkinter import messagebox, ttk

from account_client import ACCOUNT_STATUS
from quota_view import balance_text, parse_k, snapshot


UNAVAILABLE = "暂不可用"
CONTEXT_OPTIONS = ("32768", "65536", "131072", "262144")
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
    if type(timestamp) is int:
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
        self.review_ticket = None
        self.review_deadline = None
        self.control_detail = ""
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
        scroll = ttk.Scrollbar(roster, orient="vertical", command=self.user_viewport.yview)
        scroll.pack(side="right", fill="y")
        self.user_viewport.configure(yscrollcommand=scroll.set)
        self.user_viewport.pack(side="left", fill="both", expand=True)
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
        app.label(card, f"{role}  ·  注册于 {registered}", bg="field", muted=True).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        if user["role"] == "user":
            actions = app.frame(card, "field")
            actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
            for title, status in USER_STATUS_ACTIONS[user["status"]]:
                button = app.button(actions, title, lambda target=status: self.update_status(identity, target),
                                    "Accent.TButton" if status == "active" else "TButton")
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
        if not app.is_admin or app.busy or app.running or app.closing:
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

    def reveal_user_action(self, event):
        viewport = self.user_viewport
        top = event.widget.winfo_rooty() - viewport.winfo_rooty()
        bottom = top + event.widget.winfo_height() - viewport.winfo_height()
        if top < 0:
            viewport.yview_scroll(top // USER_SCROLL_STEP, "units")
        elif bottom > 0:
            viewport.yview_scroll((bottom + USER_SCROLL_STEP - 1) // USER_SCROLL_STEP, "units")

    def build_model(self):
        app = self.app
        frame = app.frame(self.model_tab)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        heading = app.frame(frame)
        heading.pack(fill="x", pady=(0, 8))
        self.model_title = tk.StringVar(frame, "模型运行状态")
        self.model_meta = tk.StringVar(frame)
        app.label(heading, variable=self.model_title, size=15, bold=True).pack(side="left")
        app.button(heading, "刷新状态", self.refresh_model).pack(side="right")
        app.label(frame, variable=self.model_meta, muted=True).pack(fill="x", pady=(0, 9))
        self.model_metrics = self.metrics(frame, ("总上下文 · Token", "并发槽位", "每槽上下文 · Token", "GPU 显存占用"))

        fields = app.frame(frame)
        fields.pack(fill="x", pady=(13, 7))
        self.total = tk.StringVar(frame)
        self.slots = tk.StringVar(frame)
        app.label(fields, "目标总上下文", bold=True).pack(side="left")
        ttk.Combobox(fields, textvariable=self.total, values=CONTEXT_OPTIONS,
                     state="readonly", width=10).pack(side="left", padx=(8, 14))
        app.label(fields, "目标并发槽位", bold=True).pack(side="left")
        ttk.Combobox(fields, textvariable=self.slots, values=SLOT_OPTIONS,
                     state="readonly", width=5).pack(side="left", padx=(8, 0))
        self.preview_button = app.button(fields, "预览变更", self.preview, "Accent.TButton")
        self.preview_button.pack(side="right")

        self.model_note = tk.StringVar(frame, "正在读取服务状态…")
        note = app.label(frame, variable=self.model_note, muted=True)
        note.pack(fill="x")
        note.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(event.width - 8, 100)))

        self.review_card = app.frame(frame, "field")
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
            app.label(card, variable=value, bg="field", size=14, bold=True,
                      color="heading").pack(fill="x", padx=11, pady=(0, 7))
            values.append(value)
        return values

    def owns_session(self):
        return self.parent.winfo_exists() and self.app.is_admin and self.app.account_client is self.client

    def call(self, caption, method, path, body, done, *, timeout=10, error_text=None):
        app = self.app
        if app.busy or app.running or not self.owns_session():
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

        app.worker(caption, lambda: self.client.request(method, path, body, timeout=timeout), complete, failed)

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
        if self.app.busy or self.app.running or not self.owns_session():
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
        ready = self.total.get() in CONTEXT_OPTIONS and self.slots.get() in SLOT_OPTIONS
        self.preview_button.configure(state="normal" if ready else "disabled")

    def refresh_model(self, message=None):
        def draw(reply):
            model = reply.get("model", reply)
            self.has_model = True
            self.model_title.set(model_name(model))
            self.model_meta.set(f"{model_state(model)}  ·  模型 ID {model.get('model_id') or UNAVAILABLE}"
                                f"  ·  网关输出上限 {count(model.get('output_token_limit'))} Token")
            for variable, value in zip(self.model_metrics, (
                count(model.get("total_context")), count(model.get("slots")),
                count(model.get("effective_context_limit")), gpu_memory(model)
            )):
                variable.set(value)
            total, slots = str(model.get("total_context")), str(model.get("slots"))
            self.total.set(total if total in CONTEXT_OPTIONS else "")
            self.slots.set(slots if slots in SLOT_OPTIONS else "")
            self.control_detail = model.get("detail") or "配置变更由服务器校验，并可能中断当前推理。"
            self.model_note.set(f"{message}  {self.control_detail}" if message else self.control_detail)
            self.app.update_model(model)
        self.call("刷新模型状态", "GET", "/api/model", None, draw, error_text=self.model_note)

    def preview(self):
        if self.total.get() not in CONTEXT_OPTIONS or self.slots.get() not in SLOT_OPTIONS:
            return
        requested = {"total_context": int(self.total.get()), "slots": int(self.slots.get())}
        self.clear_review()

        def draw(reply):
            shown = {"total_context": self.total.get(), "slots": self.slots.get()}
            if shown != {key: str(value) for key, value in requested.items()}:
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
            self.review_text.set(
                f"{before_label}  →  {after_label}    ·    {restart}\n"
                f"预计每槽 {count(reply.get('estimated_slot_context'))} Token（估算）    ·    显存风险：{risk}\n"
                f"在线用户影响：{reply.get('user_impact') or '当前推理可能中断。'}"
            )
            self.review_ticket = ticket
            self.review_deadline = monotonic() + seconds
            self.review_card.pack(fill="x", pady=(9, 0))
            self.model_note.set(f"请核对配置，预览可在 {seconds} 秒内确认。")
        self.call("预览模型配置", "POST", "/api/admin/model/preview", requested, draw, error_text=self.model_note)

    def clear_review(self):
        self.review_ticket = None
        self.review_deadline = None
        self.review_card.pack_forget()

    def cancel_preview(self):
        self.clear_review()
        self.model_note.set(self.control_detail)

    def apply_preview(self):
        if self.app.busy or not self.owns_session() or not self.review_ticket:
            return
        ticket, deadline = self.review_ticket, self.review_deadline
        self.clear_review()
        if deadline <= monotonic():
            self.model_note.set("预览已过期，请重新获取。")
            return

        def applied(reply):
            updated = reply.get("model")
            if isinstance(updated, dict):
                self.app.update_model(updated)
            self.refresh_model("配置已应用。")
        self.model_note.set("正在应用，服务会验证实际配置。")
        self.call("应用模型配置", "POST", "/api/admin/model/apply", {"confirmation": ticket},
                  applied, timeout=280, error_text=self.model_note)
