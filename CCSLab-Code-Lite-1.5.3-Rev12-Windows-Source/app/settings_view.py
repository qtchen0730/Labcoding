"""Settings sections rendered inside the desktop window."""

import tkinter as tk
from tkinter import ttk

from account_client import ACCOUNT_STATUS, DEFAULT_SERVER, AccountError
from direct_connection import MODEL_ID
from themes import THEMES


PROXY_LABELS = {"auto": "自动", "system": "系统代理", "direct": "直连"}


def _value(value):
    return f"{value:,}" if type(value) is int and value > 0 else "暂不可用"


def model_fields(model):
    status = {"running": "运行中", "sleeping": "休眠", "unavailable": "暂不可用"}
    gpu = model.get("gpu")
    used, total = (gpu.get("used_mib"), gpu.get("total_mib")) if isinstance(gpu, dict) else (None, None)
    memory = f"{used:,} / {total:,} MiB" if type(used) is int and type(total) is int else "暂不可用"
    return (
        ("模型", model.get("model_name") or model.get("model_id") or "暂不可用"),
        ("模型 ID", model.get("model_id") or "暂不可用"),
        ("运行状态", status.get(model.get("status"), "暂不可用")),
        ("总上下文 Token", _value(model.get("total_context"))),
        ("并发槽位", _value(model.get("slots"))),
        ("每槽实测 Token", _value(model.get("effective_context_limit"))),
        ("单次输出 Token 上限", _value(model.get("output_token_limit"))),
        ("GPU 显存", memory),
    )


def build_settings_page(app, parent, section="general"):
    panel = app.frame(parent)
    panel.pack(fill="both", expand=True)
    side = app.frame(panel, "side", width=204)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    app.label(side, "设置", bg="side", bold=True, size=20).pack(fill="x", padx=21, pady=(25, 18))
    body = app.frame(panel)
    body.pack(side="left", fill="both", expand=True, padx=(29, 28), pady=(25, 18))
    routes = {}
    chosen = None

    def select(key):
        nonlocal chosen
        if chosen == key:
            return
        chosen = key
        if app.settings_update_cleanup:
            app.settings_update_cleanup()
            app.settings_update_cleanup = None
        for widget in body.winfo_children():
            widget.destroy()
        app.update_settings_status_label = None
        app.update_settings_version_label = None
        app.update_settings_release_section = None
        app.update_settings_notes_frame = None
        app.update_settings_notes_text = None
        app.update_settings_security_label = None
        app.update_settings_download_status_label = None
        app.update_settings_download_actions = None
        for item, (button, _name, _build) in routes.items():
            button.configure(style="Selected.Nav.TButton" if item == key else "Nav.TButton")
        _button, name, build = routes[key]
        app.label(body, name, bold=True, size=20).pack(fill="x", pady=(0, 14))
        build(body)
        app.apply_theme()

    def card(parent, heading, detail=None):
        shelf = app.frame(parent, "field")
        shelf.pack(fill="x", pady=(0, 10))
        title = app.label(shelf, heading, bg="field", bold=True, size=11)
        title.pack(fill="x", padx=15, pady=(11, 4 if detail else 10))
        if detail:
            label = app.label(shelf, detail, bg="field", muted=True, size=9)
            label.configure(wraplength=580)
            label.pack(fill="x", padx=15, pady=(0, 7))
        return shelf

    def scrollable(parent, build):
        scroller = app.frame(parent)
        scroller.pack(fill="both", expand=True)
        viewport = tk.Canvas(scroller, borderwidth=0, highlightthickness=0, takefocus=False)
        app.skin(viewport, "panel", None)
        viewport.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(scroller, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        inner = app.frame(viewport)
        content = viewport.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: viewport.configure(scrollregion=viewport.bbox(content)))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(content, width=event.width))
        reveal = build(inner)
        if reveal is not None:
            def scroll_to_target():
                if reveal.winfo_exists():
                    offset = reveal.winfo_rooty() - inner.winfo_rooty()
                    viewport.yview_moveto(offset / max(1, inner.winfo_height()))

            viewport.after_idle(scroll_to_target)

        def scroll_page(event):
            viewport.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        def bind_wheel(widget):
            widget.bind("<MouseWheel>", scroll_page)
            for child in widget.winfo_children():
                bind_wheel(child)

        bind_wheel(viewport)

    def general(parent):
        updates = card(parent, "软件更新", f"当前版本：{app.version_info['version']}（{app.version_info['channel']} 通道）")
        update_row = app.frame(updates, "field")
        update_row.pack(fill="x", padx=15, pady=(0, 10))
        app.update_check_button = app.button(update_row, "检查更新", lambda: app.check_for_updates(manual=True))
        app.update_check_button.pack(side="left")
        app.label(update_row, "仅连接 GitHub Releases；检查不使用 CCSLab 账号凭据。",
                  bg="field", muted=True, size=9).pack(side="left", padx=(10, 0))
        status = app.label(updates, variable=app.update_status, bg="field", muted=True, size=9)
        status.configure(wraplength=580, justify="left")
        status.pack(fill="x", padx=15, pady=(0, 10))
        app.update_settings_status_label = status

        release_section = app.frame(updates, "field")
        app.update_settings_release_section = release_section
        version_label = app.label(release_section, variable=app.update_found_version,
                                  bg="field", bold=True, color="heading")
        version_label.pack(anchor="w", padx=10, pady=(8, 3))
        app.update_settings_version_label = version_label
        app.label(release_section, "更新说明", bg="field", muted=True, size=9).pack(
            anchor="w", padx=10, pady=(0, 4))
        notes_frame = app.frame(release_section, "field", height=190)
        notes_frame.pack(fill="x", padx=10, pady=(0, 6))
        notes_frame.pack_propagate(False)
        notes = tk.Text(notes_frame, wrap="word", height=8, borderwidth=0,
                        padx=7, pady=5, undo=False, takefocus=True)
        app.skin(notes, "field", "text")
        notes_scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=notes.yview)
        notes.configure(yscrollcommand=notes_scroll.set, state="disabled")
        notes.pack(side="left", fill="both", expand=True)
        notes_scroll.pack(side="right", fill="y")
        app.update_settings_notes_frame = notes_frame
        app.update_settings_notes_text = notes

        def scroll_notes(event):
            delta = getattr(event, "delta", 0)
            if delta:
                units = max(1, abs(delta) // 120)
                notes.yview_scroll(-units if delta > 0 else units, "units")
                return "break"
            number = getattr(event, "num", None)
            if number in (4, 5):
                notes.yview_scroll(-1 if number == 4 else 1, "units")
                return "break"
            return None

        for widget in (notes, notes_frame, notes_scroll):
            widget.bind("<MouseWheel>", scroll_notes)
            widget.bind("<Button-4>", scroll_notes)
            widget.bind("<Button-5>", scroll_notes)

        security = app.label(release_section, variable=app.update_security_notice,
                             bg="field", muted=True, size=9)
        security.configure(wraplength=580, justify="left")
        security.pack(fill="x", padx=10, pady=(0, 8))
        app.update_settings_security_label = security

        app.update_settings_download_status_label = app.label(
            updates, variable=app.update_download_status, bg="field", muted=True, size=9)
        app.update_settings_download_status_label.configure(wraplength=580, justify="left")
        app.update_settings_download_status_label.pack(fill="x", padx=15, pady=(0, 8))
        update_actions = app.frame(updates, "field")
        app.update_settings_download_actions = update_actions
        update_actions.pack(fill="x", padx=15, pady=(0, 10))
        app.update_settings_download_button = app.button(
            update_actions, "\u4e0b\u8f7d\u66f4\u65b0", app.download_update, "Accent.TButton")
        app.update_settings_download_button.pack(side="left")
        app.update_settings_later_button = app.button(
            update_actions, "\u7a0d\u540e\u63d0\u9192", app.snooze_update)
        app.update_settings_later_button.pack(side="left", padx=(8, 0))
        app.update_settings_open_folder_button = app.button(
            update_actions, "\u6253\u5f00\u5b89\u88c5\u5305\u6240\u5728\u6587\u4ef6\u5939", app.open_update_folder)
        app.update_settings_open_folder_button.pack(side="left", padx=(8, 0))

        def resize_notes(_event=None):
            try:
                if notes_frame.winfo_exists():
                    dpi_height = int(app.root.winfo_fpixels("1i") * 2.0)
                    window_height = max(120, int(app.root.winfo_height() * 0.32))
                    notes_frame.configure(height=max(120, min(220, dpi_height, window_height)))
            except (tk.TclError, ValueError):
                return

        resize_binding = app.root.bind("<Configure>", resize_notes, add="+")
        resize_notes()

        def cleanup_update_settings():
            try:
                if resize_binding:
                    app.root.unbind("<Configure>", resize_binding)
            except tk.TclError:
                pass

        app.settings_update_cleanup = cleanup_update_settings
        app.refresh_update_settings_view()

        appearance = card(parent, "外观")
        row = app.frame(appearance, "field")
        row.pack(fill="x", padx=15, pady=(0, 10))
        app.label(row, "主题", bg="field").pack(side="left")
        app.theme_picker = ttk.Combobox(row, textvariable=app.theme,
                                       values=tuple(label for label, _colors in THEMES.values()),
                                       state="readonly", width=16)
        app.theme_picker.pack(side="left", padx=(17, 0))
        app.theme_picker.bind("<<ComboboxSelected>>", lambda _event: app.apply_preferences())
        app.label(row, "自动保存", bg="field", muted=True, size=9).pack(side="left", padx=(10, 0))
        row = app.frame(appearance, "field")
        row.pack(fill="x", padx=15, pady=(0, 10))
        app.label(row, "对话字号", bg="field").pack(side="left", padx=(0, 12))
        size = ttk.Combobox(row, textvariable=app.chat_size, values=("标准", "大"), state="readonly", width=8)
        size.pack(side="left")
        size.bind("<<ComboboxSelected>>", lambda _event: app.apply_preferences())

        behavior = card(parent, "任务与权限")
        row = app.frame(behavior, "field")
        row.pack(fill="x", padx=15, pady=(0, 9))
        app.label(row, "默认任务模式", bg="field").pack(side="left", padx=(0, 12))
        mode = ttk.Combobox(row, textvariable=app.default_mode, values=("方案", "执行"),
                            state="readonly", width=8)
        mode.pack(side="left")
        mode.bind("<<ComboboxSelected>>", lambda _event: app.apply_preferences(mode_changed=True))
        ttk.Checkbutton(behavior, text="严格审批每次文件写入", variable=app.strict,
                        command=app.apply_preferences).pack(anchor="w", padx=15, pady=(0, 5))
        ttk.Checkbutton(behavior, text="启动时展示结果面板", variable=app.results_preference,
                        command=lambda: app.apply_preferences(results_changed=True)).pack(
                            anchor="w", padx=15, pady=(0, 11))

    def connections(parent):
        if app.connection_mode == "direct":
            shelf = card(parent, "Open WebUI 模型", f"模型 ID：{MODEL_ID}")
            endpoint = app.label(shelf, variable=app.direct_address, muted=True, bg="field")
            endpoint.configure(wraplength=560)
            endpoint.pack(fill="x", padx=15, pady=(0, 6))
            app.label(shelf, variable=app.direct_status, bg="field", color="heading", bold=True).pack(
                anchor="w", padx=15, pady=(0, 5))
            row = app.frame(shelf, "field")
            row.pack(fill="x", padx=15, pady=(0, 11))
            app.button(row, "编辑模型连接", lambda: app.show_page("direct")).pack(side="left")
            app.button(row, "使用账号网关", lambda: app.select_connection("account")).pack(side="left", padx=(8, 0))
            return
        scrollable(parent, account_connections)

    def account_connections(parent):
        account = app.account
        text = ("账号激活后可使用模型" if not account else
                f"{account['username']}  ·  {'管理员' if account['role'] == 'admin' else '普通用户'}"
                f"  ·  {ACCOUNT_STATUS.get(account['status'], '暂不可用')}")
        shelf = card(parent, "账号与服务器", text)
        row = app.frame(shelf, "field")
        row.pack(fill="x", padx=15, pady=(0, 6))
        address = tk.StringVar(master=parent, value=app.account_connection[0])
        entry = ttk.Entry(row, textvariable=address)
        entry.pack(side="left", fill="x", expand=True)
        proxy_row = app.frame(shelf, "field")
        proxy_row.pack(fill="x", padx=15, pady=(0, 8))
        app.label(proxy_row, "连接模式", bg="field").pack(side="left", padx=(0, 8))
        proxy = tk.StringVar(master=parent, value=PROXY_LABELS[app.account_connection[1]])
        choices = {label: key for key, label in PROXY_LABELS.items()}
        proxy_selector = ttk.Combobox(proxy_row, textvariable=proxy, values=tuple(choices),
                                      state="readonly", width=10)
        proxy_selector.pack(side="left")
        save_message = tk.StringVar(master=parent)

        def save_server(value, manual=True):
            try:
                saved = app.save_account_connection(value, choices[proxy.get()], manual=manual)
            except AccountError as error:
                save_message.set(str(error))
                return
            if saved:
                address.set(app.account_connection[0])
                save_message.set("连接设置已保存。" if manual else "已恢复公网默认地址。")

        if app.account_client:
            entry.configure(state="readonly")
            proxy_selector.configure(state="disabled")
            app.label(shelf, "修改服务器地址或连接模式请先退出当前账号。", bg="field", muted=True, size=9).pack(
                fill="x", padx=15, pady=(0, 9))
        else:
            app.button(proxy_row, "保存并测试", lambda: save_server(address.get()), "Accent.TButton").pack(
                side="left", padx=(8, 0))
            app.button(proxy_row, "恢复公网默认", lambda: save_server(DEFAULT_SERVER, False)).pack(
                side="left", padx=(8, 0))
            app.label(shelf, variable=save_message, bg="field", color="heading", size=9).pack(
                fill="x", padx=15, pady=(0, 4))

        diagnostic = card(parent, "服务器连接诊断", "诊断使用已保存的服务器地址和连接模式。")
        summary = app.frame(diagnostic, "field")
        summary.pack(fill="x", padx=15)
        for title, variable in (("连接状态", app.gateway_status), ("上次成功", app.gateway_last_success),
                                ("最近错误", app.gateway_recent_error)):
            row = app.frame(summary, "field")
            row.pack(fill="x", pady=(0, 3))
            label = app.label(row, title, bg="field", muted=True, size=9)
            label.configure(width=9)
            label.pack(side="left", anchor="n")
            value = app.label(row, variable=variable, bg="field", size=9)
            value.configure(wraplength=470)
            value.pack(side="left", fill="x", expand=True)
        tools = app.frame(diagnostic, "field")
        tools.pack(fill="x", padx=15, pady=(4, 5))
        for title, action in (
            ("测试连接", app.check_account_connection),
            ("运行完整诊断", lambda: app.check_account_connection(full=True, details=True)),
            ("复制诊断报告", app.copy_account_diagnostics),
            ("查看技术详情", app.show_account_diagnostics),
        ):
            app.button(tools, title, action).pack(side="left", padx=(0, 6))
        app.label(diagnostic, variable=app.gateway_report_status, bg="field", color="heading", size=9).pack(
            fill="x", padx=15, pady=(0, 5))

        model = card(parent, "模型与资源")
        message = tk.StringVar(master=parent)
        grid = app.frame(model, "field")
        grid.pack(fill="x", padx=15, pady=(0, 9))
        grid.columnconfigure(0, weight=1, uniform="model")
        grid.columnconfigure(1, weight=1, uniform="model")
        fields = []
        for i, (key, _) in enumerate(model_fields({})):
            cell = app.frame(grid, "field")
            cell.grid(row=i // 2, column=i % 2, sticky="ew", pady=(0, 6), padx=(0, 8))
            app.label(cell, key, bg="field", muted=True, size=9).pack(fill="x")
            value = tk.StringVar(master=parent)
            field = app.label(cell, variable=value, bg="field", bold=True)
            field.configure(wraplength=225)
            field.pack(fill="x")
            fields.append(value)

        def display(info):
            if grid.winfo_exists():
                for var, (_, value) in zip(fields, model_fields(info)):
                    var.set(value)

        def refresh():
            if not app.can_use_agent or app.busy:
                return
            refresh_button.configure(state="disabled")

            def done(info):
                app.update_model(info)
                if refresh_button.winfo_exists():
                    refresh_button.configure(state="normal")
                    display(info)
                    message.set("已刷新。")

            def failed(error):
                if refresh_button.winfo_exists():
                    refresh_button.configure(state="normal")
                    message.set(str(error))

            app.worker("读取模型信息", app.account_client.model, done, failed)

        display(app.model_info)
        refresh_button = app.button(model, "刷新模型信息", refresh)
        refresh_button.pack(anchor="w", padx=15, pady=(0, 8))
        refresh_button.configure(state="normal" if app.can_use_agent else "disabled")
        feedback = app.label(model, variable=message, bg="field", color="heading", size=9)
        feedback.configure(wraplength=560)
        feedback.pack(fill="x", padx=15, pady=(0, 5))
        app.button(parent, "使用 Open WebUI 模型", lambda: app.select_connection("direct")).pack(anchor="w")

    def shortcuts(parent):
        shelf = card(parent, "键盘快捷键")
        for label, key in (("打开项目", "Ctrl+O"), ("新建项目", "Ctrl+Shift+N"),
                           ("新建会话", "Ctrl+N"), ("打开设置", "Ctrl+,"),
                           ("发送任务", "Ctrl+Enter")):
            row = app.frame(shelf, "field")
            row.pack(fill="x", padx=15, pady=(0, 9))
            app.label(row, label, bg="field").pack(side="left")
            app.label(row, key, bg="field", color="heading").pack(side="right")

    categories = [("general", "外观与任务", general), ("connection", "账号与模型", connections),
                  ("shortcuts", "快捷键", shortcuts)]
    if app.account_client and app.account:
        from password_view import build_password_settings

        categories.insert(2, ("passwords", "密码管理",
                              lambda parent: scrollable(parent, lambda inner: build_password_settings(app, inner))))
    for key, name, build in categories:
        button = app.button(side, name, lambda key=key: select(key), "Nav.TButton")
        button.pack(fill="x", padx=14, pady=(0, 5))
        routes[key] = (button, name, build)

    select(section if section in routes else "general")
