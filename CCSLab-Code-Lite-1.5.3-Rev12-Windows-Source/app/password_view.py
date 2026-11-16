"""Personal and administrator password controls for the current CCSLab session."""

import tkinter as tk
from tkinter import ttk

from account_client import PASSWORD_RANGE, valid_account_password


def _card(app, parent, title, detail):
    card = app.frame(parent, "field")
    app.label(card, title, bg="field", bold=True, size=13).pack(fill="x", padx=14, pady=(12, 4))
    subtitle = app.label(card, detail, bg="field", muted=True, size=9)
    subtitle.configure(wraplength=285)
    subtitle.pack(fill="x", padx=14, pady=(0, 8))
    form = app.frame(card, "field")
    form.pack(fill="x", padx=14, pady=(0, 12))
    form.columnconfigure(0, weight=1)
    return card, form


def _secret_field(app, parent, row, label):
    app.label(parent, label, bg="field").grid(row=row, column=0, sticky="w", pady=(2, 3))
    value = tk.StringVar(master=parent)
    entry = ttk.Entry(parent, textvariable=value, show="•", width=24)
    entry.grid(row=row + 1, column=0, sticky="ew", pady=(0, 4))
    return value


def _feedback(app, parent):
    value = tk.StringVar(master=parent)
    label = app.label(parent, variable=value, bg="field", color="heading", size=9)
    label.configure(wraplength=285)
    return value, label


def _valid_code(value):
    return len(value) == 6 and value.isascii() and value.isdigit()


def build_password_settings(app, parent):
    if not app.account_client:
        return

    client = app.account_client
    contents = app.frame(parent)
    contents.pack(fill="x")
    for column in range(2):
        contents.columnconfigure(column, weight=1, uniform="credentials")

    def allowed(*, admin=False):
        return (contents.winfo_exists() and (not admin or app.is_admin) and app.account_client is client
                and not app.busy and not app.closing)

    def request(title, action, feedback, completed, controls, secrets=(), *, admin=False):
        if not allowed(admin=admin):
            return
        feedback.set("正在处理…")
        for widget, _state in controls:
            widget.configure(state="disabled")

        def release():
            if not contents.winfo_exists() or app.account_client is not client:
                return False
            for widget, state in controls:
                widget.configure(state=state)
            return True

        def done(reply):
            if release():
                completed(reply)

        def failed(error):
            if not release():
                return
            if getattr(error, "status", 0) == 401:
                app.require_account_login("会话已失效，请重新登录。")
                return
            detail = str(error)
            for secret in secrets:
                detail = detail.replace(secret, "••••")
            feedback.set(detail[:360])

        app.worker(title, action, done, failed)

    personal, personal_form = _card(app, contents, "修改我的登录密码", "修改后，你的登录会话会失效，请重新登录。")
    personal.grid(row=0, column=0, columnspan=2, sticky="new", pady=(0, 16))
    current_password = _secret_field(app, personal_form, 0, "当前登录密码")
    new_password = _secret_field(app, personal_form, 2, f"新登录密码（{PASSWORD_RANGE}）")
    confirm_password = _secret_field(app, personal_form, 4, "确认新登录密码")
    personal_feedback, personal_label = _feedback(app, personal_form)

    def change_personal_password():
        if not allowed():
            return
        current, new, repeat = (field.get() for field in (current_password, new_password, confirm_password))
        if not valid_account_password(current):
            personal_feedback.set("请输入当前登录密码。")
            return
        if not valid_account_password(new):
            personal_feedback.set(f"新密码须为 {PASSWORD_RANGE}。")
            return
        if new != repeat or new == current:
            personal_feedback.set("两次新密码须一致，新密码须与当前密码不同。")
            return
        for field in (current_password, new_password, confirm_password):
            field.set("")
        request("修改我的登录密码", lambda: client.change_own_password(current, new, repeat),
                personal_feedback, lambda _reply: app.require_account_login("登录密码已修改，请重新登录。"),
                ((personal_button, "normal"),), (current, new))

    personal_button = app.button(personal_form, "修改登录密码", change_personal_password, "Accent.TButton")
    personal_button.grid(row=6, column=0, sticky="e", pady=(4, 3))
    personal_label.grid(row=7, column=0, sticky="ew")
    if not app.is_admin:
        return

    app.label(contents, "管理员安全管理", bold=True, size=15).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(3, 11))
    rotate, rotate_form = _card(app, contents, "管理员授权密码",
                                "修改后，当前管理员及其他管理员会话需要重新登录。")
    rotate.grid(row=2, column=0, sticky="new", padx=(0, 7))
    current_code = _secret_field(app, rotate_form, 0, "当前管理员授权密码")
    new_code = _secret_field(app, rotate_form, 2, "新管理员授权密码（6 位数字）")
    repeat_code = _secret_field(app, rotate_form, 4, "确认新管理员授权密码")
    rotate_feedback, rotate_label = _feedback(app, rotate_form)

    def change_code():
        if not allowed(admin=True):
            return
        current, new = current_code.get(), new_code.get()
        if not _valid_code(current) or not _valid_code(new):
            rotate_feedback.set("当前与新授权密码均须为 6 位数字。")
            return
        if new != repeat_code.get():
            rotate_feedback.set("两次输入的新授权密码不一致。")
            return
        if new == current:
            rotate_feedback.set("请输入不同的新授权密码。")
            return
        for field in (current_code, new_code, repeat_code):
            field.set("")
        request("修改管理员授权密码", lambda: client.change_authorization_code(current, new),
                rotate_feedback, lambda _reply: app.require_account_login("授权密码已修改，请重新登录。"),
                ((rotate_button, "normal"),), (current, new), admin=True)

    rotate_button = app.button(rotate_form, "修改授权密码", change_code, "Accent.TButton")
    rotate_button.grid(row=6, column=0, sticky="e", pady=(4, 3))
    rotate_label.grid(row=7, column=0, sticky="ew")

    reset, reset_form = _card(app, contents, "用户登录密码",
                              "重置后，该账号需使用新密码重新登录。")
    reset.grid(row=2, column=1, sticky="new", padx=(7, 0))
    requested_target = getattr(app, "password_reset_target", None)
    app.password_reset_target = None
    selector = app.frame(reset_form, "field")
    selector.grid(row=0, column=0, sticky="ew", pady=(2, 3))
    app.label(selector, "账号", bg="field").pack(side="left")
    target_name = tk.StringVar(master=reset_form)
    target_picker = ttk.Combobox(reset_form, textvariable=target_name, state="readonly", width=23)
    target_picker.grid(row=1, column=0, sticky="ew", pady=(0, 4))
    target_password = _secret_field(app, reset_form, 2, f"新登录密码（{PASSWORD_RANGE}）")
    repeat_password = _secret_field(app, reset_form, 4, "确认新登录密码")
    authorization = _secret_field(app, reset_form, 6, "管理员授权密码")
    reset_feedback, reset_label = _feedback(app, reset_form)
    targets = {}

    def clear_reset_fields(_event=None):
        for field in (target_password, repeat_password, authorization):
            field.set("")

    target_picker.bind("<<ComboboxSelected>>", clear_reset_fields)

    def load_users():
        def display(reply):
            nonlocal requested_target
            choices = {}
            rows = reply.get("users", [])
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                name, user_id = item.get("username"), item.get("id")
                if not isinstance(name, str) or type(user_id) is not int or user_id <= 0:
                    continue
                role = "管理员" if item.get("role") == "admin" else "普通用户"
                choices[f"{name} · {role}（{user_id}）"] = (user_id, name)
            targets.clear()
            targets.update(choices)
            values = tuple(targets)
            target_picker.configure(values=values)
            preferred = next((label for label, (user_id, _name) in targets.items()
                              if user_id == requested_target), "")
            requested_target = None
            if preferred or target_name.get() not in targets:
                target_name.set(preferred)
                clear_reset_fields()
            reset_button.configure(state="normal" if values else "disabled")
            reset_feedback.set("" if values else "暂无可管理的账号。")

        request("刷新账号列表", lambda: client.request("GET", "/api/admin/users"),
                reset_feedback, display, ((refresh_button, "normal"),), admin=True)

    refresh_button = app.button(selector, "刷新", load_users)
    refresh_button.pack(side="right")

    def reset_password():
        if not allowed(admin=True):
            return
        target = targets.get(target_name.get())
        password, code = target_password.get(), authorization.get()
        if not target:
            reset_feedback.set("请选择要重置密码的账号。")
            return
        if not valid_account_password(password):
            reset_feedback.set(f"登录密码须为 {PASSWORD_RANGE}。")
            return
        if password != repeat_password.get():
            reset_feedback.set("两次输入的新登录密码不一致。")
            return
        if not _valid_code(code):
            reset_feedback.set("管理员授权密码须为 6 位数字。")
            return
        user_id, username = target
        is_current = str(user_id) == str(app.account.get("id"))
        clear_reset_fields()

        def updated(_reply):
            if is_current:
                app.require_account_login("登录密码已修改，请重新登录。")
            else:
                reset_feedback.set(f"{username} 的登录密码已更新。")

        request("重置用户登录密码", lambda: client.reset_user_password(user_id, password, code),
                reset_feedback, updated, ((reset_button, "normal"), (target_picker, "readonly")),
                (password, code), admin=True)

    reset_button = app.button(reset_form, "重置登录密码", reset_password, "Accent.TButton")
    reset_button.configure(state="disabled")
    reset_button.grid(row=8, column=0, sticky="e", pady=(4, 3))
    reset_label.grid(row=9, column=0, sticky="ew")

    webui, webui_form = _card(app, contents, "Open WebUI 最高权限验证",
                              "验证后可修改入口密码。最高权限有效期为 10 分钟，修改后需重新验证。")
    webui.grid(row=3, column=0, columnspan=2, sticky="new", pady=(16, 0))
    current_access = _secret_field(app, webui_form, 0, "当前最高权限密码")
    access_password = _secret_field(app, webui_form, 4, f"新入口密码（{PASSWORD_RANGE}）")
    repeat_access = _secret_field(app, webui_form, 6, "确认新入口密码")
    webui_feedback, webui_label = _feedback(app, webui_form)

    def verify_webui_access():
        if not allowed(admin=True):
            return
        password = current_access.get()
        if not valid_account_password(password):
            webui_feedback.set(f"最高权限密码须为 {PASSWORD_RANGE}。")
            return
        current_access.set("")
        request("验证 Open WebUI 最高权限", lambda: client.verify_open_webui_access(password),
                webui_feedback, lambda _reply: webui_feedback.set("最高权限验证通过，可以修改入口密码。"),
                ((verify_button, "normal"),), (password,), admin=True)

    def change_webui_password():
        if not allowed(admin=True):
            return
        if not client.open_webui_expires_at:
            webui_feedback.set("请先验证当前最高权限密码。")
            return
        password = access_password.get()
        if not valid_account_password(password):
            webui_feedback.set(f"访问密码须为 {PASSWORD_RANGE}。")
            return
        if password != repeat_access.get():
            webui_feedback.set("两次输入的新访问密码不一致。")
            return
        access_password.set("")
        repeat_access.set("")
        request("修改 Open WebUI 访问密码", lambda: client.change_open_webui_access(password),
                webui_feedback, lambda _reply: webui_feedback.set("入口密码已更新，新进入者请使用新密码。"),
                ((webui_button, "normal"),), (password,), admin=True)

    verify_button = app.button(webui_form, "验证最高权限", verify_webui_access)
    verify_button.grid(row=2, column=0, sticky="e", pady=(4, 8))
    webui_button = app.button(webui_form, "修改 Open WebUI 访问密码", change_webui_password, "Accent.TButton")
    webui_button.grid(row=8, column=0, sticky="e", pady=(4, 3))
    webui_label.grid(row=9, column=0, sticky="ew")
    reveal_target = requested_target is not None
    load_users()
    return reset if reveal_target else None
