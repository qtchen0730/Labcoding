"""Certificate management dialog for the CCSLab Labcoding desktop application.

Provides a Toplevel window showing certificate status and configuration controls,
integrated into the login / direct connection page.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from certificate_manager import (
    configure_certificate, detect, status_label, classify_access,
    ACCESS_PUBLIC, ACCESS_PRIVATE,
    STATUS_VALID, STATUS_EXPIRING_SOON, STATUS_EXPIRED,
    STATUS_DOMAIN_MISMATCH, STATUS_UNTRUSTED, STATUS_CONFIG_FAILED,
    STATUS_NOT_CONFIGURED, STATUS_CA_MISSING, STATUS_CA_IMPORTED,
    STATUS_NEEDS_SUDO,
    import_ca_certificate,
)


_STATUS_COLORS = {
    STATUS_VALID: "add",
    STATUS_EXPIRING_SOON: "heading",
    STATUS_EXPIRED: "remove",
    STATUS_DOMAIN_MISMATCH: "remove",
    STATUS_UNTRUSTED: "remove",
    STATUS_CONFIG_FAILED: "remove",
    STATUS_CA_MISSING: "remove",
    STATUS_CA_IMPORTED: "add",
    STATUS_NEEDS_SUDO: "heading",
    STATUS_NOT_CONFIGURED: "muted",
}


def _set_status_color(label_widget, color_name):
    """Set foreground color of a status label widget.

    *color_name* is a semantic color key (e.g. "muted", "heading").
    We use the label's own ``fg`` / ``foreground`` configure option.
    If the widget was skinned via ``app.label()``, its foreground is
    already a resolved hex value, so we try setting ``fg`` directly.
    As a fallback we fall back to common CSS-like color names.
    """
    _SEMANTIC_COLOR_FALLBACKS = {
        "muted": "#6c7086",
        "heading": "#89b4fa",
        "text": "#cdd6f4",
        "add": "#a6e3a1",
        "remove": "#f38ba8",
    }
    hex_color = _SEMANTIC_COLOR_FALLBACKS.get(color_name, color_name)
    try:
        label_widget.config(fg=hex_color)
    except tk.TclError:
        pass


def build_certificate_dialog(app, parent, server_url_var: tk.StringVar,
                             address_var: tk.StringVar | None = None,
                             data_dir=None):
    """Show a certificate management Toplevel window.

    Args:
        app: The main application instance.
        parent: The parent widget (for placement).
        server_url_var: Tk StringVar containing the server URL.
        address_var: Optional Tk StringVar for the current address input field.
        data_dir: Application data directory (fallback to app.data_root).
    """
    if data_dir is None:
        data_dir = getattr(app, "data_root", None)
        if not data_dir:
            import os
            data_dir = Path(os.path.expanduser("~/.labcoding"))

    window = tk.Toplevel(app.root)
    window.title("安全证书管理")
    window.transient(app.root)
    window.resizable(True, True)
    app.skin(window, "panel", None)
    window.protocol("WM_DELETE_WINDOW", lambda: _safe_destroy(window))

    # Track whether this window is still alive (for safe callback invocation)
    window._alive = True

    # Main body: two sections, top (info) and bottom (steps + buttons).
    # The top section shrinks to its natural size; the bottom section
    # fills remaining space, but buttons are anchored at the very bottom.
    body = app.frame(window)
    body.pack(fill="both", expand=True, padx=24, pady=20)
    body.columnconfigure(0, weight=1)
    # Only row 1 (bottom section) expands; row 0 (info section) is fixed.
    body.rowconfigure(1, weight=1)

    # ── Top section: all certificate information ──────────────────────
    top = app.frame(body)
    top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    top.columnconfigure(0, weight=1)

    # Title
    app.label(top, "安全证书管理", size=18, bold=True).grid(
        row=0, column=0, sticky="w", pady=(0, 10))

    # Server address display — reads live from address_var if available
    server_row = app.frame(top)
    server_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
    app.label(server_row, "服务器地址：").pack(side="left")
    addr_var = tk.StringVar()

    def _sync_addr():
        addr_var.set(_get_server_value(app, server_url_var, address_var))

    _sync_addr()
    # Trace the address_var so we see live edits
    if address_var:
        address_var.trace_add("write", lambda *_: _sync_addr())
    app.label(server_row, variable=addr_var, muted=True, size=9).pack(side="left")

    # Status badge
    status_var = tk.StringVar(value="点击「重新检测」开始")
    status_label_widget = app.label(top, variable=status_var, color="muted",
                                     bold=True, size=10)
    status_label_widget.grid(row=2, column=0, sticky="w", pady=(0, 8))

    # Certificate details frame
    details_frame = app.frame(top, "soft")
    details_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    details_frame.columnconfigure(1, weight=1)

    detail_vars = {}
    detail_rows = [
        ("连接状态", "status"),
        ("证书状态", "cert_status"),
        ("颁发机构", "issuer"),
        ("有效期", "validity"),
        ("匹配域名", "domains"),
        ("信任状态", "trust"),
    ]
    for i, (label_key, var_key) in enumerate(detail_rows):
        app.label(details_frame, label_key, bg="soft", muted=True, size=9).grid(
            row=i, column=0, sticky="w", padx=12, pady=2)
        var = tk.StringVar(value="—")
        detail_vars[var_key] = var
        app.label(details_frame, variable=var, bg="soft", size=9).grid(
            row=i, column=1, sticky="ew", padx=(0, 12), pady=2)

    # ── CA certificate path display and browse button ────────────────
    ca_path_frame = app.frame(top, "soft")
    ca_path_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
    ca_path_frame.columnconfigure(0, weight=1)
    app.label(ca_path_frame, "CA 证书路径：", bg="soft", muted=True, size=9).grid(
        row=0, column=0, sticky="w", padx=12, pady=2)
    ca_file_var = tk.StringVar(value="—")
    ca_path_label = app.label(ca_path_frame, variable=ca_file_var,
                              bg="soft", size=9)
    ca_path_label.configure(wraplength=440)
    ca_path_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 2))

    browse_frame = app.frame(ca_path_frame)
    browse_frame.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 12))
    browse_btn = app.button(browse_frame, "浏览…", lambda: _browse_ca_file())
    browse_btn.pack(side="right")

    # ── Bottom section: steps (expandable) + buttons (fixed) ─────────
    bottom = app.frame(body)
    bottom.grid(row=1, column=0, sticky="nsew")
    bottom.columnconfigure(0, weight=1)
    # Row 0 (steps) expands; row 1 (buttons) stays fixed at bottom
    bottom.rowconfigure(0, weight=1)

    # Progress / steps display — expandable, fills available space
    steps_text = tk.Text(bottom, wrap="word", height=3, borderwidth=0,
                         padx=8, pady=6, undo=False, state="disabled",
                         takefocus=False)
    app.skin(steps_text, "field", "text")
    steps_text.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

    # Action buttons — always visible at the bottom
    actions = app.frame(bottom)
    actions.grid(row=1, column=0, sticky="ew")

    detecting = tk.BooleanVar(value=False)

    def set_detecting(val):
        detecting.set(val)
        _update_button_states()

    def _update_button_states():
        is_det = detecting.get()
        configure_btn.config(state="disabled" if is_det else "normal")
        recheck_btn.config(state="disabled" if is_det else "normal")
        import_btn.config(state="disabled" if is_det else "normal")

    def _update_details(status):
        info = status.info
        detail_vars["status"].set(
            "公网" if status.access_type == ACCESS_PUBLIC else "局域网")

        if info:
            detail_vars["cert_status"].set(status.label)
            detail_vars["issuer"].set(
                info.issuer.split(",")[0].split("=")[-1].strip()
                if info.issuer else "—")
            validity_parts = []
            if info.not_before:
                validity_parts.append(f"起始 {info.not_before.strftime('%Y-%m-%d')}")
            if info.not_after:
                validity_parts.append(f"到期 {info.not_after.strftime('%Y-%m-%d')}")
            detail_vars["validity"].set(
                " 至 ".join(validity_parts) if validity_parts else "—")
            if info.subject_alt_names:
                detail_vars["domains"].set(", ".join(info.subject_alt_names[:5]))
            else:
                import re
                cn_match = re.search(r"CN\s*=\s*([^/,]+)", info.subject)
                detail_vars["domains"].set(
                    cn_match.group(1).strip() if cn_match else "—")
            detail_vars["trust"].set(
                "已信任" if status.status in (STATUS_VALID, STATUS_EXPIRING_SOON)
                else "未信任" if status.status == STATUS_UNTRUSTED
                else "未知")
        else:
            detail_vars["cert_status"].set(status.label)
            detail_vars["issuer"].set("—")
            detail_vars["validity"].set("—")
            detail_vars["domains"].set("—")
            detail_vars["trust"].set("—")

        status_var.set(status.label)
        color = _STATUS_COLORS.get(status.status, "muted")
        _set_status_color(status_label_widget, color)

    def _update_steps(text):
        steps_text.config(state="normal")
        steps_text.delete("1.0", "end")
        steps_text.insert("1.0", text)
        steps_text.config(state="disabled")

    def _recheck():
        if detecting.get():
            return
        set_detecting(True)
        status_var.set("检测中…")
        _set_status_color(status_label_widget, "heading")
        _update_steps("正在检测证书状态…")
        server_url = _get_server_value(app, server_url_var, address_var)

        def do_detect():
            result = detect(server_url)
            if getattr(window, "_alive", False):
                app.root.after(0, lambda: _on_detect_done(result))

        _run_in_thread(do_detect)

    def _on_detect_done(result):
        if not getattr(window, "_alive", False):
            return
        _update_details(result)
        _update_steps("\n".join(result.steps))
        set_detecting(False)

    def _configure():
        if detecting.get():
            return
        set_detecting(True)
        status_var.set("配置中…")
        _set_status_color(status_label_widget, "heading")
        _update_steps("正在配置安全证书…")
        server_url = _get_server_value(app, server_url_var, address_var)

        def do_configure():
            steps = []
            def observer(stage, msg):
                steps.append(msg)
                if getattr(window, "_alive", False):
                    app.root.after(0, lambda m=msg: _update_steps("\n".join(steps)))
            result = configure_certificate(
                server_url, str(data_dir),
                observer=observer,
                auto_install_ca=False,
            )
            if getattr(window, "_alive", False):
                app.root.after(0, lambda: _on_config_done(result))

        _run_in_thread(do_configure)

    def _on_config_done(result):
        if not getattr(window, "_alive", False):
            return
        _update_details(result)
        _update_steps("\n".join(result.steps))
        set_detecting(False)

    def _browse_ca_file():
        """Open file chooser to select a CA certificate file.

        Temporarily releases the modal grab so the file dialog can
        interact properly on Ubuntu / GNOME.
        """
        try:
            window.grab_release()
        except tk.TclError:
            pass
        window.update()

        file_path = filedialog.askopenfilename(
            parent=window,
            title="选择 CA 公共证书文件",
            filetypes=[
                ("证书文件", "*.pem *.crt *.cer *.cert"),
                ("所有文件", "*.*"),
            ],
        )
        try:
            window.grab_set()
        except tk.TclError:
            pass

        if not file_path:
            return  # User cancelled
        ca_file_var.set(file_path)
        _import_ca_from_path(file_path)

    def _import_ca_from_path(file_path):
        """Import the CA certificate at *file_path*."""
        if detecting.get():
            return
        set_detecting(True)
        status_var.set("导入中…")
        _set_status_color(status_label_widget, "heading")
        _update_steps("正在导入 CA 证书…")

        def do_import():
            result = import_ca_certificate(file_path, str(data_dir))
            if getattr(window, "_alive", False):
                app.root.after(0, lambda: _on_import_done(result))

        _run_in_thread(do_import)

    def _import_ca():
        """Let the user select and import a CA public certificate file.

        Delegates to _browse_ca_file for the dialog, then imports.
        """
        _browse_ca_file()

    def _on_import_done(result):
        if not getattr(window, "_alive", False):
            return
        _update_details(result)
        _update_steps("\n".join(result.steps))
        set_detecting(False)
        if result.status == STATUS_CA_IMPORTED:
            messagebox.showinfo(
                window, "CA 导入成功",
                message="CA 公共证书已导入。\n"
                        "请点「配置安全证书」验证服务器证书。",
            )
            # Re-detect to reflect the new state
            _recheck()

    def _run_in_thread(fn):
        import threading
        t = threading.Thread(target=fn, daemon=True)
        t.start()

    configure_btn = app.button(actions, "配置安全证书", _configure, "Accent.TButton")
    configure_btn.pack(side="right")

    import_btn = app.button(actions, "导入 CA 证书", _import_ca)
    import_btn.pack(side="left", padx=(0, 8))

    recheck_btn = app.button(actions, "重新检测", _recheck)
    recheck_btn.pack(side="left", padx=(0, 8))

    close_btn = app.button(actions, "关闭", lambda: _safe_destroy(window))
    close_btn.pack(side="left")

    # ── Finalize window geometry ──────────────────────────────────────
    # Compute required size after ALL widgets are created.
    # Do this synchronously before grab_set so Tk can layout correctly.
    window.update_idletasks()
    req_w = window.winfo_reqwidth()
    req_h = window.winfo_reqheight()
    w = max(req_w, 620)
    h = max(req_h, 420)
    window.minsize(w, h)
    window.geometry(f"{w}x{h}")

    # Center on parent
    try:
        px = app.root.winfo_rootx() + app.root.winfo_width() // 2 - w // 2
        py = app.root.winfo_rooty() + app.root.winfo_height() // 2 - h // 2
        if px < 0:
            px = 20
        if py < 0:
            py = 20
        window.geometry(f"{w}x{h}+{px}+{py}")
    except tk.TclError:
        pass

    # Set grab last, after geometry is finalized
    window.grab_set()

    # Auto-detect on open (after window is visible)
    app.root.after(200, _recheck)


def _get_server_value(app, server_url_var, address_var):
    """Get the actual server URL value, preferring address_var (live edits)."""
    # address_var may be a StringVar or a plain string
    if address_var:
        val = address_var.get().strip() if hasattr(address_var, "get") else str(address_var).strip()
        if val:
            return val
    # server_url_var may be a StringVar or a plain string (caller bug workaround)
    val = server_url_var.get().strip() if hasattr(server_url_var, "get") else str(server_url_var).strip()
    if val:
        return val
    from account_client import DEFAULT_SERVER
    return DEFAULT_SERVER


def _safe_destroy(window):
    """Destroy the certificate dialog safely, cancelling any pending callbacks."""
    window._alive = False
    try:
        if window.winfo_exists():
            window.grab_release()
    except tk.TclError:
        pass
    try:
        window.destroy()
    except tk.TclError:
        pass
