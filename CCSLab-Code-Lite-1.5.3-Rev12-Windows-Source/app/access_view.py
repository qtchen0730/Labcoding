"""Sign-in and role-specific registration in the main application window."""

import tkinter as tk
from tkinter import ttk

from account_client import AccountError, PASSWORD_RANGE, normalize_server, valid_account_password

try:
    from certificate_view import build_certificate_dialog
except ImportError:
    build_certificate_dialog = None


MODES = {
    "login": ("登录", "登录实验室账号", "登录"),
    "user": ("普通用户注册", "注册普通用户", "申请普通用户账号"),
    "admin": ("管理员注册", "注册管理员", "验证并注册管理员"),
}


def build_access_page(app, parent):
    return AccessPage(app, parent)


def prompt_role_switch(app, target_role, submit):
    if app.busy or app.running or app.closing:
        return None
    if target_role == "user":
        submit("")
        return None
    destination = {"admin": "管理员", "user": "普通用户"}[target_role]
    window = tk.Toplevel(app.root)
    window.title("切换登录身份")
    window.transient(app.root)
    window.resizable(False, False)
    app.skin(window, "panel", None)
    body = app.frame(window)
    body.pack(fill="both", expand=True, padx=22, pady=18)
    app.label(body, f"切换为{destination}", size=15, bold=True).pack(fill="x", pady=(0, 10))
    app.label(body, "管理员授权密码").pack(fill="x", pady=(0, 4))
    authorization = tk.StringVar(master=window)
    entry = ttk.Entry(body, textvariable=authorization, show="•", width=32)
    entry.pack(fill="x")
    notice = tk.StringVar(master=window)
    app.label(body, variable=notice, color="remove").pack(fill="x", pady=(5, 8))
    buttons = app.frame(body)
    buttons.pack(anchor="e")

    def cancel():
        authorization.set("")
        window.destroy()

    def confirm(_event=None):
        value = authorization.get().strip()
        if value:
            cancel()
            submit(value)
        else:
            notice.set("请输入管理员授权密码。")
            entry.focus_set()
        return "break"

    app.button(buttons, "取消", cancel).pack(side="left", padx=(0, 8))
    app.button(buttons, "确认切换", confirm, "Accent.TButton").pack(side="left")
    window.protocol("WM_DELETE_WINDOW", cancel)
    window.bind("<Escape>", lambda _event: cancel())
    window.bind("<Return>", confirm)
    app.apply_theme()
    window.grab_set()
    entry.focus_set()
    return window


class AccessPage:
    def __init__(self, app, parent):
        self.app = app
        self.container = app.frame(parent, "bg")
        self.container.pack(fill="both", expand=True)
        for index in (0, 2):
            self.container.columnconfigure(index, weight=1)
            self.container.rowconfigure(index, weight=1)
        body = app.frame(self.container)
        body.grid(row=1, column=1, padx=16, pady=14)
        overview = app.frame(body, "soft")
        overview.grid(row=0, column=0, sticky="ns")
        panel = app.frame(body)
        panel.grid(row=0, column=1, sticky="nsew")
        self.card = app.frame(panel)
        self.card.pack(fill="both", padx=26, pady=20)
        self.mode = "login"
        self.request_pending = False
        self.credential_origin = None
        self.username = tk.StringVar(master=body)
        self.password = tk.StringVar(master=body)
        self.confirmation = tk.StringVar(master=body)
        self.authorization = tk.StringVar(master=body)
        self.admin_login = tk.BooleanVar(master=body, value=False)
        self.remember = tk.BooleanVar(master=body, value=app.saved_login.path.exists())
        self.server_label = tk.StringVar(master=body)
        self.headline = tk.StringVar(master=body)
        self.feedback = tk.StringVar(master=body)
        self.role_note = tk.StringVar(master=body)
        self.build_overview(overview)
        self.build_form()
        self.admin_login.trace_add("write", self.update_role_fields)
        self.select("login")
        self.refresh()

    def _open_certificate_dialog(self):
        """Open the certificate management dialog."""
        if build_certificate_dialog is None:
            return
        server_url_var = getattr(self.app, "server_url_var", None)
        address_var = self.server_label if hasattr(self, 'server_label') else None
        build_certificate_dialog(self.app, self.container,
                                 server_url_var or tk.StringVar(value=""),
                                 address_var=address_var)

    def build_overview(self, panel):
        app = self.app
        content = app.frame(panel, "soft")
        content.pack(fill="both", expand=True, padx=23, pady=(26, 18))
        title = app.label(content, "本地开发工作区", bg="soft", size=22, bold=True)
        title.pack(fill="x", pady=(0, 12))
        subtitle = app.label(content, "登录后打开项目，查看 Git 变更，并通过实验室模型执行开发任务。",
                             bg="soft", color="muted", size=11)
        subtitle.configure(wraplength=248)
        subtitle.pack(fill="x")
        location = app.frame(content, "soft")
        location.pack(side="bottom", fill="x", pady=(24, 0))
        app.label(location, "账号服务器", bg="soft", color="heading", bold=True).pack(anchor="w")
        address = app.label(location, variable=self.server_label, bg="soft", muted=True, size=9)
        address.configure(wraplength=250)
        address.pack(anchor="w", pady=(2, 4))
        for variable, color in ((app.gateway_status, "heading"), (app.gateway_error, "remove"),
                                (app.gateway_report_status, "muted")):
            status = app.label(location, variable=variable, bg="soft", color=color, size=9)
            status.configure(wraplength=255)
            status.pack(anchor="w")
        controls = app.frame(location, "soft")
        controls.pack(fill="x", pady=(5, 0))
        app.button(controls, "重新测试", app.check_account_connection).pack(side="left")
        self.settings_button = app.button(controls, "服务器设置",
                                          lambda: app.show_page("settings", section="connection"))
        self.settings_button.pack(side="left", padx=(6, 0))
        reports = app.frame(location, "soft")
        reports.pack(fill="x", pady=(5, 0))
        app.button(reports, "复制诊断报告", app.copy_account_diagnostics).pack(side="left")
        app.button(reports, "技术详情", app.show_account_diagnostics, "Nav.TButton").pack(side="left", padx=(6, 0))
        self.public_button = app.button(location, "使用 Open WebUI 模型",
                                        lambda: app.select_connection("direct"), "Nav.TButton")
        self.public_button.pack(anchor="w", pady=(5, 0))
        self.cert_button = app.button(location, "证书配置",
                                      lambda: self._open_certificate_dialog(), "Nav.TButton")
        self.cert_button.pack(anchor="w", pady=(2, 0))

    def build_form(self):
        app = self.app
        nav = app.frame(self.card)
        nav.pack(fill="x", pady=(0, 15))
        self.tabs = {}
        for mode, (label, _, __) in MODES.items():
            button = app.button(nav, label, lambda mode=mode: self.select(mode))
            button.pack(side="left", padx=(0, 5))
            self.tabs[mode] = button
        app.label(self.card, variable=self.headline, size=17, bold=True).pack(fill="x")
        note = app.label(self.card, variable=self.role_note, muted=True)
        note.configure(wraplength=382)
        note.pack(fill="x", pady=(5, 11))
        self.fields = app.frame(self.card)
        self.fields.pack(fill="x")
        self.fields.columnconfigure(1, weight=1)
        self.user_entry = self.field(0, "用户名", self.username)
        self.password_entry = self.field(1, f"账号密码（{PASSWORD_RANGE}）", self.password, masked=True)
        for field in (self.user_entry[1], self.password_entry[1]):
            field.bind("<KeyPress>", lambda _event: self.app.cancel_saved_login(), add=True)
        self.confirm_entry = self.field(2, "确认密码", self.confirmation, masked=True)
        self.role_checkbox = ttk.Checkbutton(self.fields, text="以管理员身份登录", variable=self.admin_login)
        self.role_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        self.admin_entry = self.field(3, "管理员授权密码", self.authorization, masked=True)
        self.remember_checkbox = ttk.Checkbutton(self.fields, text="保存账号密码，下次启动自动登录",
                                                variable=self.remember, command=self.remember_changed)
        self.remember_checkbox.grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        feedback = app.label(self.card, variable=self.feedback, color="heading")
        feedback.configure(wraplength=382)
        feedback.pack(fill="x", pady=(7, 3))
        self.action = app.button(self.card, "", self.submit, "Accent.TButton")
        self.action.pack(anchor="e", pady=(5, 0))

    def field(self, index, label, variable, *, masked=False):
        caption = self.app.label(self.fields, label)
        caption.grid(row=index, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(self.fields, width=26, textvariable=variable, show="•" if masked else "")
        entry.grid(row=index, column=1, sticky="ew", pady=5)
        entry.bind("<Return>", self.submit)
        return caption, entry

    def select(self, mode, *, message=""):
        if self.request_pending or mode not in MODES:
            return
        self.mode = mode
        self.clear_secrets()
        self.admin_login.set(False)
        _, headline, _ = MODES[mode]
        self.headline.set(headline)
        self.role_note.set({
            "login": "默认以普通用户身份登录。",
            "user": "注册后可登录并查看工作台。账号激活后可使用 Coding Agent。",
            "admin": "需要实验室分发的管理员授权密码。",
        }[mode])
        self.feedback.set(message)
        if mode == "login":
            self.restore_saved_fields()
        for key, button in self.tabs.items():
            button.configure(style="Accent.TButton" if key == mode else "TButton")
        target = self.password_entry[1] if self.username.get() else self.user_entry[1]
        target.focus_set()

    def update_role_fields(self, *_args):
        is_login = self.mode == "login"
        administrator = is_login and self.admin_login.get()
        show_authorization = administrator or self.mode == "admin"
        for widgets, visible in (
            ((self.role_checkbox,), is_login),
            ((self.remember_checkbox,), is_login),
            (self.confirm_entry, not is_login),
            (self.admin_entry, show_authorization),
        ):
            for widget in widgets:
                if visible:
                    widget.grid()
                else:
                    widget.grid_remove()
        if not show_authorization:
            self.authorization.set("")
        self.action.configure(text="管理员登录" if administrator else MODES[self.mode][2])

    def refresh(self):
        try:
            server = normalize_server(self.app.account_connection[0])
            self.server_label.set(server)
            if self.credential_origin is not None and self.credential_origin != server:
                self.username.set("")
                self.clear_secrets()
            self.credential_origin = server
            if self.mode == "login":
                self.restore_saved_fields()
        except AccountError:
            self.server_label.set("地址无效，请检查服务器设置")

    def restore_saved_fields(self):
        if not self.remember.get():
            return
        try:
            record = self.app.saved_login.load(self.app.account_connection[0])
        except (OSError, ValueError, AccountError):
            self.feedback.set("无法读取本机保存的登录，请重新输入或取消保存。")
            return
        if record and (not self.username.get() or self.username.get() == record["username"]):
            self.username.set(record["username"])
            self.password.set(record["password"])

    def remember_changed(self):
        if not self.remember.get():
            self.app.cancel_saved_login()
            try:
                self.app.saved_login.forget()
            except OSError:
                self.feedback.set("无法清除本机保存的登录，请检查数据目录权限。")

    def reset(self):
        self.username.set("")
        self.remember.set(self.app.saved_login.path.exists())
        self.select("login")
        self.refresh()

    def clear_secrets(self):
        for variable in (self.password, self.confirmation, self.authorization):
            variable.set("")

    def set_busy(self, value):
        self.request_pending = value
        state = "disabled" if value else "normal"
        for widget in (*self.tabs.values(), self.action, self.settings_button, self.public_button,
                       self.user_entry[1], self.password_entry[1],
                       self.confirm_entry[1], self.admin_entry[1], self.role_checkbox, self.remember_checkbox):
            widget.configure(state=state)

    def submit(self, _event=None):
        if self.request_pending or self.app.busy or self.app.closing:
            return "break"
        self.app.cancel_saved_login()
        name = self.username.get().strip()
        password = self.password.get()
        role = self.mode
        administrator = role == "login" and self.admin_login.get()
        requires_authorization = administrator or role == "admin"
        authorization = self.authorization.get().strip() if requires_authorization else ""
        if not name or not password:
            self.feedback.set("请输入用户名和账号密码。")
            return "break"
        if not valid_account_password(password):
            self.feedback.set(f"账号密码须为 {PASSWORD_RANGE}。")
            self.password_entry[1].focus_set()
            return "break"
        if role != "login" and self.confirmation.get() != password:
            self.feedback.set("两次输入的账号密码不一致。")
            self.confirm_entry[1].focus_set()
            return "break"
        if requires_authorization and not authorization:
            self.feedback.set("请输入管理员授权密码。")
            self.admin_entry[1].focus_set()
            return "break"
        connection = self.app.account_connection
        remember = self.remember.get()
        self.feedback.set("正在连接账号服务器…")
        self.set_busy(True)

        def request():
            client = self.app.get_account_client(connection)
            if role == "admin":
                return client, client.register_admin(name, password, authorization)["user"]
            if role == "user":
                return client, client.register(name, password)["user"]
            if administrator:
                return client, client.login(name, password, authorization_code=authorization)
            return client, client.login(name, password)

        def done(result):
            if not self.container.winfo_exists():
                return
            self.set_busy(False)
            self.clear_secrets()
            client, user = result
            if role != "login":
                self.username.set(user["username"])
                message = "普通用户已注册。登录后可查看审批状态。" if role == "user" else "管理员账号已注册，请登录。"
                self.select("login", message=message)
                return
            try:
                self.app.signed_in(client, user)
                if remember:
                    self.app.saved_login.save(client.base, name, password, client.token)
                else:
                    self.app.saved_login.forget()
            except OSError:
                self.app.show_error("登录信息保存失败", "无法写入本机数据目录或加密凭据，请检查目录权限。")

        def failed(error):
            if not self.container.winfo_exists():
                return
            self.set_busy(False)
            self.authorization.set("")
            message = str(error)
            for secret in (password, authorization):
                if secret:
                    message = message.replace(secret, "••••")
            self.feedback.set(message[:420])

        self.app.worker("登录" if role == "login" else "注册", request, done, failed)
        return "break"
