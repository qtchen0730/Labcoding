"""Public model connection settings inside the desktop workspace."""

import tkinter as tk
from tkinter import ttk

from direct_connection import BASE_URL, MODEL_ID, supports_webui_sign_in


def build_direct_page(app, parent):
    panel = app.frame(parent, "bg")
    panel.pack(fill="both", expand=True)
    panel.columnconfigure((0, 2), weight=1)
    panel.rowconfigure((0, 2), weight=1)
    card = app.frame(panel)
    card.grid(row=1, column=1, padx=22, pady=18)
    card.columnconfigure(0, weight=1)
    if not app.direct_unlocked:
        build_direct_gate(app, card)
        return
    app.label(card, "模型连接", size=21, bold=True).grid(row=0, column=0, sticky="ew", padx=25, pady=(23, 7))
    app.label(card, "公网 API 地址", bold=True).grid(row=1, column=0, sticky="w", padx=25)
    saved_address = app.direct_address.get()
    address = tk.StringVar(master=card, value=saved_address)
    location = app.frame(card)
    location.grid(row=2, column=0, sticky="ew", padx=25, pady=(5, 4))
    address_field = ttk.Entry(location, textvariable=address, width=43)
    address_field.pack(side="left", fill="x", expand=True)

    def sync_address(*_args):
        nonlocal saved_address
        canonical = app.direct_address.get()
        if address.get() == saved_address:
            address.set(canonical)
        saved_address = canonical

    trace = app.direct_address.trace_add("write", sync_address)

    def release_address(event):
        if event.widget is card:
            app.direct_address.trace_remove("write", trace)

    card.bind("<Destroy>", release_address, add="+")
    app.label(card, f"使用 HTTPS 模型服务。模型 {MODEL_ID}", muted=True, size=9).grid(
        row=3, column=0, sticky="w", padx=25, pady=(0, 10))

    badge = app.label(card, variable=app.direct_status, color="heading", bg="soft", bold=True)
    badge.configure(padx=10, pady=5, wraplength=440)
    badge.grid(row=4, column=0, sticky="ew", padx=25, pady=(0, 10))

    def use_default():
        address.set(BASE_URL)
        address_field.focus_set()

    app.button(location, "使用默认地址", use_default).pack(side="left", padx=(8, 0))

    actions = app.frame(card)
    actions.grid(row=5, column=0, sticky="ew", padx=25, pady=(0, 5))

    def connect(_event=None):
        app.check_direct_connection(address=address.get(), save=True)
        return "break"

    def test():
        app.check_direct_connection(address=address.get())

    connect_button = app.button(actions, "连接", connect, "Accent.TButton")
    connect_button.pack(side="right")
    app.button(actions, "连接测试", test).pack(side="left")
    app.button(actions, "验证工具调用", app.check_direct_tools).pack(side="left", padx=(6, 0))

    auth = app.frame(card, "soft")
    auth.grid(row=6, column=0, sticky="ew", padx=25, pady=(6, 3))
    auth.columnconfigure(1, weight=1)
    app.label(auth, "Open WebUI 账号登录", bg="soft", bold=True).grid(
        row=0, column=0, columnspan=2, sticky="w", padx=11, pady=(9, 0))
    hint = app.label(auth, "需要认证时，使用登录该网站的邮箱与密码。", bg="soft", muted=True, size=9)
    hint.configure(wraplength=428)
    hint.grid(row=1, column=0, columnspan=2, sticky="w", padx=11, pady=(1, 6))
    email = tk.StringVar(master=auth)
    password = tk.StringVar(master=auth)
    app.label(auth, "登录邮箱", bg="soft").grid(row=2, column=0, sticky="w", padx=(11, 9), pady=(0, 4))
    email_field = ttk.Entry(auth, textvariable=email, width=30)
    email_field.grid(row=2, column=1, sticky="ew", padx=(0, 11), pady=(0, 4))
    app.label(auth, "登录密码", bg="soft").grid(row=3, column=0, sticky="w", padx=(11, 9), pady=(0, 4))
    password_field = ttk.Entry(auth, textvariable=password, show="•", width=30)
    password_field.grid(row=3, column=1, sticky="ew", padx=(0, 11), pady=(0, 4))

    def sign_in(_event=None):
        if app.check_direct_connection(address=address.get(), save=True, email=email.get().strip(),
                                       password=password.get()):
            password.set("")
        return "break"

    app.button(auth, "登录并连接", sign_in, "Accent.TButton").grid(
        row=4, column=1, sticky="e", padx=(0, 11), pady=(2, 9))
    if app.can_change_webui_password:
        app.button(card, "修改当前 Open WebUI 登录密码",
                   lambda: build_webui_password_form(app, panel, card), "Nav.TButton").grid(
            row=9, column=0, sticky="w", padx=25, pady=(0, 12))

    def show_auth(*_args):
        email.set("")
        password.set("")
        webui = supports_webui_sign_in(address.get())
        if webui:
            auth.grid()
        else:
            auth.grid_remove()
        connect_button.configure(style="TButton" if webui else "Accent.TButton")

    address.trace_add("write", show_auth)
    show_auth()
    app.button(card, "CCSLab 登录与管理", lambda: app.select_connection("account"), "Nav.TButton").grid(
        row=8, column=0, sticky="w", padx=25, pady=(0, 18))

    # Certificate configuration button (v2.0.4 original)
    try:
        from certificate_view import build_certificate_dialog as _build_cert_dialog
    except ImportError:
        _build_cert_dialog = None
    if _build_cert_dialog is not None:
        app.button(card, "证书配置",
                   lambda: _build_cert_dialog(app, card, app.direct_address, address), "Nav.TButton").grid(
            row=9, column=0, sticky="w", padx=25, pady=(0, 12))
    feedback = app.label(card, variable=app.direct_feedback, muted=True, size=9)
    feedback.configure(wraplength=452)
    feedback.grid(row=7, column=0, sticky="ew", padx=25, pady=(4, 10))
    address_field.bind("<Return>", connect)
    email_field.bind("<Return>", lambda _event: password_field.focus_set())
    password_field.bind("<Return>", sign_in)
    address_field.focus_set()


def build_direct_gate(app, card):
    app.label(card, "Open WebUI 模型连接", size=21, bold=True).grid(
        row=0, column=0, sticky="ew", padx=25, pady=(23, 7))
    hint = app.label(card, "通过 CCSLab 账号网关验证访问密码后，可在授权有效期内连接模型。", muted=True)
    hint.configure(wraplength=415)
    hint.grid(row=1, column=0, sticky="w", padx=25, pady=(0, 14))
    app.label(card, "Open WebUI 访问密码", bold=True).grid(row=2, column=0, sticky="w", padx=25)
    password = tk.StringVar(master=card)
    field = ttk.Entry(card, textvariable=password, show="•", width=40)
    field.grid(row=3, column=0, sticky="ew", padx=25, pady=(5, 8))

    def unlock(_event=None):
        if app.unlock_direct_access(password.get()):
            password.set("")
        return "break"

    status = app.label(card, variable=app.direct_status, color="heading", bold=True, size=9)
    status.grid(row=4, column=0, sticky="w", padx=25)
    feedback = app.label(card, variable=app.direct_feedback, muted=True, size=9)
    feedback.configure(wraplength=415)
    feedback.grid(row=5, column=0, sticky="ew", padx=25, pady=(2, 7))
    actions = app.frame(card)
    actions.grid(row=6, column=0, sticky="ew", padx=25, pady=(5, 18))
    app.button(actions, "CCSLab 登录与管理", lambda: app.select_connection("account"), "Nav.TButton").pack(
        side="left")
    app.button(actions, "验证并继续", unlock, "Accent.TButton").pack(side="right")
    field.bind("<Return>", unlock)
    field.focus_set()


def build_webui_password_form(app, panel, connection_card):
    if not app.can_change_webui_password or app.busy or app.running:
        return
    connection_card.grid_remove()
    form = app.frame(panel)
    form.grid(row=1, column=1, padx=22, pady=18)
    form.columnconfigure(1, weight=1)
    app.label(form, "修改 Open WebUI 登录密码", size=19, bold=True).grid(
        row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(20, 8))
    hint = app.label(form, app.api_base, muted=True, size=9)
    hint.configure(wraplength=390)
    hint.grid(row=1, column=0, columnspan=2, sticky="ew", padx=24, pady=(0, 12))
    fields = []
    for index, label in enumerate(("当前登录密码", "新登录密码", "确认新登录密码"), start=2):
        app.label(form, label).grid(row=index, column=0, sticky="w", padx=(24, 12), pady=5)
        field = ttk.Entry(form, show="•", width=28)
        field.grid(row=index, column=1, sticky="ew", padx=(0, 24), pady=5)
        fields.append(field)

    def submit(_event=None):
        if app.change_webui_password(*(field.get() for field in fields)):
            for field in fields:
                field.delete(0, "end")
        return "break"

    def cancel():
        if not app.busy:
            form.destroy()
            connection_card.grid()
            app.direct_feedback.set("")

    app.direct_feedback.set("")
    feedback = app.label(form, variable=app.direct_feedback, muted=True, size=9)
    feedback.configure(wraplength=390)
    feedback.grid(row=5, column=0, columnspan=2, sticky="ew", padx=24, pady=(4, 8))
    app.button(form, "返回", cancel).grid(row=6, column=0, sticky="w", padx=24, pady=(0, 17))
    app.button(form, "修改登录密码", submit, "Accent.TButton").grid(
        row=6, column=1, sticky="e", padx=24, pady=(0, 17))
    fields[-1].bind("<Return>", submit)
    fields[0].focus_set()
    app.apply_theme()
