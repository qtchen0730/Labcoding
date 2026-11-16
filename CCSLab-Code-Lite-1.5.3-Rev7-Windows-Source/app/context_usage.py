"""Per-request context readings for the pinned OpenCode session API."""

from dataclasses import dataclass, field
import tkinter as tk
from tkinter import ttk


def positive_tokens(value):
    return value if type(value) is int and 0 < value <= 2_147_483_647 else None


def step_tokens(tokens):
    """Get the measured context of one model step, including generated tokens."""
    if not isinstance(tokens, dict):
        return None
    reported = positive_tokens(tokens.get("total"))
    if reported is not None:
        return reported
    # OpenCode normalizes input and output to exclude cache and reasoning,
    # respectively. Recombine those components exactly once when no total exists.
    cache = tokens.get("cache") or {}
    if not isinstance(cache, dict):
        return None
    prompt = sum(positive_tokens(item) or 0 for item in (
        tokens.get("input"), cache.get("read"), cache.get("write"),
    ))
    if not prompt:
        return None
    return prompt + (positive_tokens(tokens.get("output")) or 0) + (positive_tokens(tokens.get("reasoning")) or 0)


def _event(state, **details):
    return {"type": "context", "state": state, **details}


def context_event(event, seen):
    """Adapt one session-filtered OpenCode event. The seen set is scoped to a task."""
    kind = event.get("type")
    props = event.get("properties") or {}
    if kind == "session.compacted":
        return _event("compacted")
    if kind == "session.error":
        return _event("request_failed")
    if kind == "message.updated":
        info = props.get("info") or {}
        if info.get("role") == "assistant" and info.get("summary") is True:
            seen.add(("summary", info.get("id")))
            if info.get("error") or info.get("finish") == "error":
                return _event("failed")
    if kind != "message.part.updated":
        return None
    part = props.get("part") or {}
    if part.get("type") == "step-start":
        return _event("requesting")
    if part.get("type") == "compaction":
        key = ("compaction", part.get("id"))
        if key not in seen:
            seen.add(key)
            return _event("compacting")
    if part.get("type") == "step-finish" and ("summary", part.get("messageID")) not in seen:
        return _event("measured", used_tokens=step_tokens(part.get("tokens")))
    return None


@dataclass
class ContextReading:
    used_tokens: int | None = None
    compaction: str = ""
    last_confirmed: int | None = field(default=None, compare=False)
    estimated_tokens: int | None = field(default=None, compare=False)
    request_state: str = field(default="", compare=False)
    request_id: str = field(default="", compare=False)

    def record(self, event):
        state = event.get("state")
        if state == "measured":
            self.used_tokens = positive_tokens(event.get("used_tokens"))
            self.last_confirmed = self.used_tokens or self.last_confirmed
            self.estimated_tokens = None
            self.request_state = "confirmed" if self.used_tokens else "unavailable"
            if self.compaction == "compacting":
                self.compaction = "compacted"
        elif state in {"task_started", "request", "requesting", "request_failed"}:
            self.used_tokens = None
            self.request_state = state
            if state == "task_started":
                self.estimated_tokens = None
                self.request_id = ""
            if state == "request":
                self.estimated_tokens = positive_tokens(event.get("estimated_input_tokens"))
                self.request_id = event.get("request_id", "")
        elif state in {"compacting", "compacted", "failed", "interrupted"}:
            self.used_tokens = None
            self.estimated_tokens = None
            self.compaction = state


def historical_reading(messages):
    """Recover the most recent ordinary step from native OpenCode history."""
    result = ContextReading()
    for message in messages:
        info = message.get("info") or {}
        parts = message.get("parts") or []
        if info.get("role") == "user" and any(part.get("type") == "compaction" for part in parts):
            result.record(_event("compacting"))
        elif info.get("role") == "user":
            result.record(_event("task_started"))
        elif info.get("role") != "assistant":
            continue
        elif info.get("summary") is True:
            if info.get("error") or info.get("finish") == "error":
                result.record(_event("failed"))
            elif info.get("finish"):
                result.record(_event("compacted"))
        else:
            if info.get("error"):
                result.record(_event("request_failed"))
                continue
            for part in parts:
                if part.get("type") == "step-finish":
                    result.record(_event("measured", used_tokens=step_tokens(part.get("tokens"))))
    if result.compaction == "compacting":
        result.record(_event("interrupted"))
    return result


class ContextMeter:
    """Inline context usage display. A blank value means unmeasured, not zero."""

    def __init__(self, app, parent):
        self.app = app
        self.reading = ContextReading()
        self.limit = None
        self.output_limit = None
        self.value = tk.DoubleVar(master=parent, value=0)
        self.title = tk.StringVar(master=parent)
        self.detail = tk.StringVar(master=parent)
        panel = self.panel = app.frame(parent)
        panel.pack(fill="x", padx=18, pady=(0, 8))
        headline = app.frame(panel)
        headline.pack(fill="x")
        label = app.label(headline, variable=self.title, bold=True, size=10)
        label.pack(side="left")
        headline.bind("<Configure>", lambda event: label.configure(wraplength=max(120, event.width - 95)))
        self.bar = ttk.Progressbar(
            headline, orient="horizontal", mode="determinate", length=74,
            maximum=100, variable=self.value, style="Context.Horizontal.TProgressbar",
        )
        self.bar.pack(side="right", padx=(7, 0))
        self.secondary = app.label(panel, variable=self.detail, muted=True, size=9)
        self.secondary.pack(fill="x")
        panel.bind("<Configure>", lambda event: self.secondary.configure(wraplength=event.width))
        self.refresh()

    def theme(self, colors):
        for style, color in (("Context", "accent"), ("ContextHigh", "remove")):
            self.app.style.configure(
                f"{style}.Horizontal.TProgressbar", background=colors[color],
                troughcolor=colors["field"], bordercolor=colors["border"],
                lightcolor=colors[color], darkcolor=colors[color], thickness=6,
            )

    def set_model(self, metadata=None, output_limit=None):
        data = metadata or {}
        self.limit = positive_tokens(data.get("effective_context_limit"))
        caps = map(positive_tokens, (output_limit, data.get("output_limit"), data.get("output_token_limit")))
        self.output_limit = min((cap for cap in caps if cap is not None), default=None)
        self.refresh()

    def record(self, event):
        self.reading.record(event)
        self.refresh()

    def reset(self):
        self.reading = ContextReading()
        self.refresh()

    def restore(self, messages):
        self.reading = historical_reading(messages)
        self.refresh()

    def finish(self):
        if self.reading.compaction == "compacting":
            self.record(_event("interrupted"))

    def refresh(self):
        used = self.reading.used_tokens
        limit = self.limit
        output = self.output_limit
        estimate = self.reading.estimated_tokens
        displayed = used if used is not None else estimate
        ratio = 100 * displayed / limit if displayed is not None and limit is not None else None
        if ratio is None:
            display = f"{used:,}" if used is not None else "—"
            maximum = f"{limit:,}" if limit is not None else "—"
            self.title.set(f"上下文 {display} / {maximum} Token · 暂不可用")
        else:
            label = "已确认" if used is not None else "估算输入"
            self.title.set(f"上下文 {displayed:,} / {limit:,} Token · {ratio:.1f}% · {label}")
        self.value.set(min(ratio, 100) if ratio is not None else 0)
        close = ratio is not None and (ratio >= 85 or (output is not None and limit - displayed <= output))
        self.bar.configure(style=("ContextHigh" if close else "Context") + ".Horizontal.TProgressbar")
        compaction = {
            "compacting": "压缩中", "compacted": "已压缩", "failed": "压缩失败",
            "interrupted": "压缩未完成",
        }.get(self.reading.compaction, "尚无压缩")
        parts = ["最近成功请求（含输出）" if used is not None else "当前请求实测统计不可用", compaction]
        if self.reading.request_state == "request_failed":
            parts.insert(0, "请求未成功，历史统计不能代表失败输入")
        if used is None and self.reading.last_confirmed:
            parts.append(f"上次成功 {self.reading.last_confirmed:,}")
        if estimate is not None:
            parts.append("估算含系统提示、历史、工具及文件；非模型分词实测")
        if close:
            parts.insert(0, "接近上限")
        if output is not None:
            parts.append(f"输出预留上限 {output:,}")
        self.detail.set(" · ".join(parts))
