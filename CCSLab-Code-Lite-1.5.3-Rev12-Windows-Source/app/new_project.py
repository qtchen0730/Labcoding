"""Create and open a local project in the desktop application."""

import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import tempfile
import tkinter as tk
from tkinter import filedialog, ttk


def _git_executable(project):
    filename = "git.exe" if os.name == "nt" else "git"
    for entry in os.get_exec_path():
        directory = Path(entry.strip('"'))
        if not directory.is_absolute():
            continue
        try:
            candidate = (directory / filename).resolve(strict=True)
        except OSError:
            continue
        if not candidate.is_relative_to(project) and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _initialize_git(git, directory):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            [git, "init", "--quiet", "--template=", "--initial-branch=main", str(directory)],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Git 初始化超时，请重试。") from error
    if result.returncode:
        detail = result.stdout.decode("utf-8", errors="replace").strip()[:1200]
        raise RuntimeError(f"Git 初始化失败。\n{detail}")


def create_project(parent, name, initialize_git=False):
    """Create one new directory and return its absolute path."""
    if (not name or name.casefold() == ".git" or name != name.strip() or name.endswith(".") or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or PureWindowsPath(name).is_reserved()):
        raise ValueError("请输入有效的项目名称，名称不能包含路径、保留字符或首尾空格。")
    location = Path(parent).expanduser()
    if not location.is_absolute() or not location.is_dir():
        raise ValueError("请选择一个已存在的上级文件夹。")
    project = location.resolve(strict=True) / name
    if os.path.lexists(project):
        raise FileExistsError("该位置已有同名文件或文件夹。请选择其他名称。")

    def make_directory():
        try:
            project.mkdir()
        except FileExistsError as error:
            raise FileExistsError("该位置已有同名文件或文件夹。请选择其他名称。") from error

    if initialize_git:
        git = _git_executable(project)
        if not git:
            raise RuntimeError("未找到 Git。请安装 Git，或取消勾选「初始化 Git 仓库」。")
        with tempfile.TemporaryDirectory(prefix=".ccslab-project-", dir=project.parent) as temporary:
            staging = Path(temporary)
            _initialize_git(git, staging)
            # Reserve the target atomically before moving the completed Git metadata.
            make_directory()
            try:
                (staging / ".git").rename(project / ".git")
            except OSError:
                try:
                    project.rmdir()
                except OSError:
                    pass
                raise
    else:
        make_directory()
    return project


def show_new_project(app):
    if app.busy or app.running or app.closing:
        return
    window = tk.Toplevel(app.root)
    window.title("新建项目")
    window.transient(app.root)
    window.resizable(False, False)
    app.skin(window, "panel", None)
    frame = app.frame(window)
    frame.pack(fill="both", expand=True, padx=22, pady=18)
    frame.columnconfigure(0, weight=1)
    app.label(frame, "新建项目", size=15, bold=True).grid(row=0, column=0, columnspan=2, sticky="w")

    initial = Path(app.project).parent if app.project else Path.home() / "Documents"
    if not initial.is_dir():
        initial = Path.home()
    name = tk.StringVar(master=window)
    parent = tk.StringVar(master=window, value=str(initial))
    use_git = tk.BooleanVar(master=window, value=False)
    preview = tk.StringVar(master=window)
    notice = tk.StringVar(master=window)
    app.label(frame, "项目名称").grid(row=1, column=0, sticky="w", pady=(15, 4))
    name_entry = ttk.Entry(frame, textvariable=name, width=47)
    name_entry.grid(row=2, column=0, columnspan=2, sticky="ew")
    app.label(frame, "上级文件夹").grid(row=3, column=0, sticky="w", pady=(13, 4))
    path_entry = ttk.Entry(frame, textvariable=parent, width=43)
    path_entry.grid(row=4, column=0, sticky="ew")

    def browse():
        location = filedialog.askdirectory(
            parent=window, initialdir=parent.get(), title="选择项目的上级文件夹", mustexist=True
        )
        if location:
            parent.set(location)

    browse_button = app.button(frame, "浏览…", browse)
    browse_button.grid(row=4, column=1, padx=(8, 0))
    path_preview = app.label(frame, variable=preview, muted=True)
    path_preview.configure(wraplength=505)
    path_preview.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 3))
    git_option = ttk.Checkbutton(frame, text="初始化 Git 仓库", variable=use_git)
    git_option.grid(row=6, column=0, sticky="w", pady=(6, 5))
    status = app.label(frame, variable=notice, color="remove")
    status.configure(wraplength=505)
    status.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 4))
    actions = app.frame(frame)
    actions.grid(row=8, column=0, columnspan=2, sticky="e")
    creating = False

    def update(*_args):
        preview.set(f"将创建于 {Path(parent.get()) / name.get()}" if name.get() and parent.get() else "")
        if not creating:
            notice.set("")
            create_button.configure(state="normal" if name.get().strip() and parent.get() else "disabled")

    def cancel():
        if not creating:
            window.destroy()

    def submit():
        nonlocal creating
        if creating or app.busy or not name.get().strip() or not parent.get():
            return
        values = parent.get().strip(), name.get(), use_git.get()
        creating = True
        notice.set("正在创建项目…")
        controls = (name_entry, path_entry, browse_button, git_option, cancel_button, create_button)
        for control in controls:
            control.configure(state="disabled")

        def done(project):
            window.destroy()
            app.open_project(str(project))

        def failed(error):
            nonlocal creating
            creating = False
            for control in controls:
                control.configure(state="normal")
            notice.set(str(error)[:1450])

        app.worker("新建项目", lambda: create_project(*values), done, failed)

    cancel_button = app.button(actions, "取消", cancel)
    cancel_button.pack(side="left", padx=(0, 8))
    create_button = app.button(actions, "创建并打开", submit, "Accent.TButton")
    create_button.pack(side="left")
    name.trace_add("write", update)
    parent.trace_add("write", update)
    window.protocol("WM_DELETE_WINDOW", cancel)
    window.bind("<Escape>", lambda _event: cancel())
    window.bind("<Return>", lambda _event: submit())
    update()
    app.apply_theme()
    window.grab_set()
    name_entry.focus_set()
