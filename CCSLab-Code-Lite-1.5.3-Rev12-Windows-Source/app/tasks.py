"""Session-owned tasks with Gateway admission; UI state transitions run on Tk's thread."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import monotonic, time
from uuid import uuid4

from approval_policy import SessionGrants


ACTIVE = {"queued", "running", "cancelling"}
EXECUTING = {"running", "cancelling"}


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


@dataclass
class ProjectContext:
    owner: str
    project: str
    history_path: Path
    history: dict
    titles: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)
    changes: object = None

    @property
    def key(self):
        return self.owner, canonical(self.project)

    def recover(self):
        """A previous process cannot still own these local tasks; never silently replay them."""
        changed = False
        for record in self.history.values():
            if not isinstance(record, dict):
                continue
            states = [record.get("task"), *record.get("tasks", [])]
            for state in states:
                if isinstance(state, dict) and state.get("status") in ACTIVE:
                    state.update(status="failed", interrupted=True, ended_at=time(),
                                 error="客户端上次退出，任务已中断；未自动重试。")
                    changed = True
            if record.get("task", {}).get("interrupted") and not record.get("interruption_recorded"):
                record.setdefault("events", []).append({"type": "error", "text": "客户端上次退出，任务已中断；未自动重试。"})
                record["interruption_recorded"] = True
        return changed


@dataclass
class SessionTask:
    context: ProjectContext
    session_id: str
    prompt: str
    identity: dict
    client: object = None
    api_base: str = ""
    token: str = ""
    ca_path: object = None
    model: dict = field(default_factory=dict)
    mode: str = "执行"
    strict: bool = False
    direct: bool = False
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    queued_at: float = field(default_factory=time)
    started_at: float | None = None
    started_mono: float | None = None
    ended_at: float | None = None
    error: str = ""
    auth_failure: bool = False
    cancel: Event = field(default_factory=Event)
    events: Queue = field(default_factory=Queue)
    grants: SessionGrants = field(default_factory=SessionGrants)
    engine: object = None
    thread: object = None
    result: object = None
    admitted: bool = False

    @property
    def key(self):
        return (*self.context.key, self.session_id)

    @property
    def read_only(self):
        return self.mode == "方案"

    @property
    def scope(self):
        account = self.identity.get("id") or self.identity.get("username")
        return (str(account), canonical(self.context.project), self.session_id) if account else None

    def metadata(self):
        # Never persist credentials, transports, locks or OpenCode's local authentication.
        return {"task_id": self.task_id, "session_id": self.session_id,
                "opencode_session_id": self.session_id, "project_id": canonical(self.context.project),
                "account_id": self.identity.get("id"), "role": self.identity.get("role"),
                "status": self.status, "input": self.redact(self.prompt), "mode": self.mode,
                "queued_at": self.queued_at, "started_at": self.started_at, "ended_at": self.ended_at,
                "cancel_requested": self.cancel.is_set(), "error": self.error}

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.token, "••••") if self.token else value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value


class TaskScheduler:
    def __init__(self, limit=None):
        self.limit = limit
        self.tasks = {}
        self.sessions = {}
        self.accepting = True

    def selected(self, owner, project, session):
        return self.sessions.get((str(owner), canonical(project), session)) if project and session else None

    def submit(self, task):
        current = self.sessions.get(task.key)
        if not self.accepting:
            raise ValueError("正在处理身份切换或关闭，请稍后提交。")
        if current and current.status in ACTIVE:
            raise ValueError("此会话已有任务，请等待或停止后再提交。")
        self.tasks[task.task_id] = task
        self.sessions[task.key] = task

    def active(self):
        return [task for task in self.tasks.values() if task.status in ACTIVE]

    def start_pending(self, execute):
        active = [task for task in self.tasks.values() if task.status in EXECUTING]
        started = []
        if not self.accepting:
            return started
        for task in self.tasks.values():
            if task.status != "queued" or task.thread is not None:
                continue
            if self.limit is not None and len(active) >= self.limit:
                break
            # Gateway-backed tasks remain queued until the server grants a global slot.
            if task.direct or task.client is None:
                task.status = "running"
                task.started_at, task.started_mono = time(), monotonic()
            def run(current=task):
                error, code, category = "", 0, ""
                try:
                    if not current.cancel.is_set():
                        execute(current)
                except Exception as failure:
                    error = current.redact(str(failure))
                    code, category = getattr(failure, "status", 0), getattr(failure, "category", "")
                finally:
                    # execute must drain native tools and close its Engine before releasing this lease.
                    current.events.put({"type": "_finished", "error": error, "status_code": code,
                                        "category": category})
            task.thread = Thread(target=run, daemon=True, name="labcoding-task-" + task.task_id[:8])
            task.thread.start()
            active.append(task)
            started.append(task)
        return started

    def cancel_task(self, task):
        if task.status not in ACTIVE or task.cancel.is_set():
            return False
        task.cancel.set()
        if task.status == "queued" and task.thread is None:
            task.status, task.ended_at = "cancelled", time()
            task.prompt = task.redact(task.prompt)
            task.client, task.token = None, ""
        else:
            task.status = "cancelling"
        return True

    def finish(self, task, error=""):
        task.error = error if not task.cancel.is_set() else ""
        task.status = "cancelled" if task.cancel.is_set() else "failed" if error else "completed"
        task.ended_at = time()
        task.prompt = task.redact(task.prompt)
        task.client, task.token = None, ""
