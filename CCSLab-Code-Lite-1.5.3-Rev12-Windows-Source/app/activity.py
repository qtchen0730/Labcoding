"""Per-session record of workspace tool calls and their results."""

from dataclasses import dataclass
import json
import ntpath
import re
import subprocess
import tkinter as tk
from tkinter import ttk


TOOL_NAMES = {
    "list": "列出文件",
    "search": "搜索文件",
    "read": "读取文件",
    "replace": "编辑文件",
    "create": "创建文件",
    "delete": "删除文件",
    "run": "执行命令",
}

STATUS_NAMES = {
    "running": "执行中",
    "completed": "完成",
    "error": "失败",
    "stopped": "已停止",
    "incomplete": "未完成",
}


def format_value(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def command_text(arguments):
    program = arguments.get("program")
    args = arguments.get("args", [])
    if isinstance(program, str) and isinstance(args, list):
        return subprocess.list2cmdline([program, *map(str, args)])
    return ""


def duration_text(timing):
    if isinstance(timing, dict):
        started, ended = timing.get("start"), timing.get("end")
        if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
            seconds = (ended - started) / 1000
            if 0 <= seconds < 86400:
                return "< 0.1 秒" if seconds < 0.1 else f"{seconds:.1f} 秒"
    return ""


@dataclass
class Activity:
    tool: str
    status: str
    arguments: dict
    output: str
    duration: str

    @classmethod
    def from_event(cls, event, previous=None):
        tool = str(event.get("name") or (previous.tool if previous else "工具"))
        status = str(event.get("status") or "running")
        arguments = event.get("input")
        if not isinstance(arguments, dict) or not arguments:
            arguments = previous.arguments if previous else {}
        output = event.get("output") or event.get("error") or ""
        output = format_value(output)
        duration = duration_text(event.get("time"))
        if status in {"error", "failed"} or output.startswith("操作失败："):
            status = "error"
        if tool.removeprefix("workspace_") == "run":
            code = re.match(r"\s*退出码[：:]\s*(-?\d+)", output)
            if "命令已取消。" in output[:90]:
                status = "stopped"
            elif code and int(code.group(1)) != 0:
                status = "error"
        return cls(tool, status, arguments, output, duration)

    @property
    def kind(self):
        return self.tool.removeprefix("workspace_")

    @property
    def title(self):
        return TOOL_NAMES.get(self.kind, self.tool)

    @property
    def target(self):
        command = command_text(self.arguments)
        if command:
            return command
        path = str(self.arguments.get("path", ""))
        query = str(self.arguments.get("query", ""))
        return query or ntpath.basename(path.rstrip("\\/")) or path

    @property
    def caption(self):
        return self.title + ("  " + self.target if self.target else "")

    def details(self):
        details = []
        program = command_text(self.arguments)
        if program:
            details.append(("命令", program))
        path = self.arguments.get("path")
        if path:
            details.append(("项目内路径", str(path)))
        fields = {name: value for name, value in self.arguments.items()
                  if name not in {"program", "args", "path", "text", "old_text", "new_text"}}
        if fields:
            details.append(("参数", format_value(fields)[:2500]))
        if self.output:
            details.append(("输出", self.output))
        return details


class ActivityPanel:
    """Show a single row per tool call, with a selectable result below."""

    def __init__(self, app, parent):
        self.app = app
        self.rows = {}
        self.keys = {}
        self.last_id = ""
        self.displayed = None
        self.summary = tk.StringVar(master=parent, value="暂无执行记录")
        self.container = app.frame(parent, "result_panel")
        self.container.pack(fill="both", expand=True, pady=(7, 0))
        bar = app.frame(self.container, "result_panel")
        bar.pack(fill="x", pady=(0, 6))
        app.label(bar, variable=self.summary, muted=True, size=9).pack(side="left")
        self.copy_button = app.button(bar, "复制输出", self.copy_output)
        self.copy_button.pack(side="right")
        self.copy_button.configure(state="disabled")

        table = app.frame(self.container, "result_panel")
        table.pack(fill="x")
        self.tree = ttk.Treeview(table, columns=("activity", "status"), show="headings",
                                 height=6, selectmode="browse", style="Activity.Treeview")
        self.tree.heading("activity", text="操作", anchor="w")
        self.tree.heading("status", text="状态", anchor="w")
        self.tree.column("activity", minwidth=105, width=165, stretch=True)
        self.tree.column("status", width=73, minwidth=73, stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        app.overlay_scrollbar(table, self.tree)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)

        detail, self.output_view = app.text_panel(self.container, mono=True, height=5)
        detail.pack(fill="both", expand=True, pady=(8, 0))
        self.clear()

    def theme(self, colors):
        self.app.style.configure("Activity.Treeview",
                                 background=colors["field"], fieldbackground=colors["field"],
                                 foreground=colors["text"], borderwidth=0, rowheight=26)
        self.app.style.map("Activity.Treeview",
                           background=[("selected", colors["selection"])],
                           foreground=[("selected", colors["heading"])])
        self.app.style.configure("Activity.Treeview.Heading",
                                 background=colors["panel"], foreground=colors["muted"],
                                 padding=(6, 4), borderwidth=0)
        for status, color in (("error", "remove"), ("running", "heading"),
                              ("stopped", "muted"), ("incomplete", "muted")):
            self.tree.tag_configure(status, foreground=colors[color])
        self.output_view.tag_configure("result_heading", foreground=colors["heading"],
                                       spacing1=5, spacing3=2)
        self.output_view.tag_configure("result_status", foreground=colors["muted"], spacing3=6)

    def clear(self):
        self.rows.clear()
        self.keys.clear()
        self.last_id = ""
        self.tree.delete(*self.tree.get_children())
        self.displayed = None
        self.refresh_summary()
        self.show_selected()

    def record(self, event):
        if event.get("status") == "pending":
            return
        key = event.get("key")
        row_id = self.keys.get(key) if key else None
        previous = self.rows.get(row_id)
        activity = Activity.from_event(event, previous)
        if activity == previous:
            return
        is_new = row_id is None
        selection = self.tree.selection()
        follow = not selection or selection[0] == self.last_id
        if is_new:
            row_id = f"entry-{len(self.rows)}"
            if key:
                self.keys[key] = row_id
            self.last_id = row_id
        self.rows[row_id] = activity
        values = (activity.caption, STATUS_NAMES.get(activity.status, activity.status))
        if is_new:
            self.tree.insert("", "end", iid=row_id, values=values, tags=(activity.status,))
        else:
            self.tree.item(row_id, values=values, tags=(activity.status,))
        if follow and is_new:
            self.tree.selection_set(row_id)
            self.tree.see(row_id)
        self.refresh_summary()
        self.show_selected()

    def finish(self, interrupted=False):
        for row_id, activity in self.rows.items():
            if activity.status != "running":
                continue
            activity.status = "stopped" if interrupted else "incomplete"
            self.tree.item(row_id, values=(activity.caption, STATUS_NAMES[activity.status]),
                           tags=(activity.status,))
            if self.displayed and self.displayed[0] == row_id:
                self.displayed = None
        self.refresh_summary()
        self.show_selected()

    def refresh_summary(self):
        size = len(self.rows)
        if not size:
            self.summary.set("暂无执行记录")
            return
        segments = [f"{size} 项操作"]
        for state in ("running", "error"):
            count = sum(activity.status == state for activity in self.rows.values())
            if count:
                segments.append(f"{count} {STATUS_NAMES[state]}")
        self.summary.set("  ·  ".join(segments))

    def selected(self):
        selection = self.tree.selection()
        row_id = selection[0] if selection else ""
        return row_id, self.rows.get(row_id)

    def show_selected(self, _event=None):
        row_id, activity = self.selected()
        value = (row_id, activity)
        if value == self.displayed:
            return
        self.displayed = value
        self.copy_button.configure(state="normal" if activity and activity.output else "disabled")
        self.output_view.configure(state="normal")
        self.output_view.delete("1.0", "end")
        if activity:
            label = STATUS_NAMES.get(activity.status, activity.status)
            if activity.duration:
                label += f"  ·  {activity.duration}"
            self.output_view.insert("end", activity.title + "\n", "result_heading")
            self.output_view.insert("end", label + "\n", "result_status")
            for title, content in activity.details():
                self.output_view.insert("end", title + "\n", "result_heading")
                self.output_view.insert("end", content + "\n")
        else:
            self.output_view.insert("end", "选择一项操作以查看详情。", "result_status")
        self.output_view.configure(state="disabled")
        self.output_view.see("1.0")

    def copy_output(self):
        _, activity = self.selected()
        if activity and activity.output:
            self.container.clipboard_clear()
            self.container.clipboard_append(activity.output)

    def transcript(self):
        return "\n".join(f"{entry.caption}\n{entry.output}" for entry in self.rows.values())
