"""Controlled interleaved streams and real Tk checks; never contact production services."""
from copy import deepcopy
from pathlib import Path
from queue import Queue, Empty
from threading import Event, Lock, get_ident
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from verify_support import TEST_ROOT
from app import App, read_json, write_json
from approval_policy import session_scope
from account_client import AccountError
from new_project import create_project
from tasks import ACTIVE, ProjectContext, SessionTask, TaskScheduler


USER = {"id": 31, "username": "isolated-fixture", "status": "active", "role": "admin",
        "assigned_role": "admin", "quota_mode": "unlimited"}
WRITE = {"kind": "write", "path": "marker.txt", "detail": "marker.txt", "operation": "write"}


class FixtureClient:
    def __init__(self):
        self.token = "isolated-token"
        self.user = dict(USER)
        self.base = self.model_base = "http://127.0.0.1:1"
        self.logout_count = 0
        self.requests = []
        self.password_changes = []
        self.slot_lock = Lock()
        self.slots = set()
        self.slot_limit = 2

    def acquire_task(self, task_id):
        with self.slot_lock:
            if len(self.slots) >= self.slot_limit:
                return False
            self.slots.add(task_id)
            return True

    def heartbeat_task(self, task_id):
        assert task_id in self.slots

    def release_task(self, task_id):
        with self.slot_lock:
            self.slots.discard(task_id)

    def me(self):
        return dict(self.user)

    def model(self):
        return {"effective_context_limit": 32768, "output_token_limit": 4096}

    def engine_proxy_url(self):
        return None

    def clear_open_webui_access(self):
        pass

    def request(self, *args, **_kwargs):
        self.requests.append(args[:2])
        if args[:2] == ("GET", "/api/tasks"):
            return {"running_tasks": len(self.slots), "available_slots": self.slot_limit-len(self.slots),
                    "user_running_tasks": len(self.slots)}
        return {"users": []}

    def logout(self):
        self.logout_count += 1

    def switch_role(self, role, _code):
        self.user = {**self.user, "role": role}
        self.token = "replacement-role-token"
        return dict(self.user)

    def change_own_password(self, current, new, repeat):
        self.password_changes.append((current, new, repeat))


class MetadataEngine:
    def __init__(self, project, store):
        self.project, self.store = project, store
        self.closed = False

    def new_session(self):
        sid = "ses_" + str(len(self.store) + 1)
        self.store[sid] = {"id": sid, "title": "新会话"}
        return sid

    def sessions(self):
        return list(self.store.values())

    def history(self, _session):
        return []

    def delete_session(self, session):
        self.store.pop(session, None)

    def close(self):
        self.closed = True


class StreamRuntime:
    def __init__(self, task):
        self.task = task
        self.commands = Queue()
        self.started, self.stopped, self.closed = Event(), Event(), Event()
        self.cancel_release = Event()
        self.cancel_release.set()
        self.approvals = []

    def run(self, session, prompt, on_event, approve, **options):
        self.thread = get_ident()
        self.options = options
        assert session == self.task.session_id and prompt == self.task.prompt
        self.started.set()
        while True:
            if options["cancel_event"].is_set():
                self.cancel_release.wait(5)
                return
            try:
                event = self.commands.get(timeout=.015)
            except Empty:
                continue
            if event == "end":
                return
            if isinstance(event, Exception):
                raise event
            if event.get("type") == "approval":
                self.approvals.append(approve(event["request"]))
            elif event.get("type") == "write":
                Path(self.task.context.project, "marker.txt").write_text(event["text"], encoding="utf-8")
            else:
                on_event(event)

    def stop(self, session):
        assert session == self.task.session_id
        self.stopped.set()

    def close(self):
        self.closed.set()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="schedule-")
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.scheduler = TaskScheduler(2)
        self.release = Event()
        self.addCleanup(self.release.set)
        self.jobs = []

    def task(self, project="a", session=None, mode="执行", owner="owner"):
        context = ProjectContext(owner, str(self.base / project), self.base / "history.json", {})
        task = SessionTask(context, session or str(len(self.jobs)), "prompt", USER, mode=mode)
        self.scheduler.submit(task)
        self.jobs.append(task)
        return task

    def start(self):
        return self.scheduler.start_pending(lambda _task: self.release.wait(5))

    def test_two_projects_run_and_third_queues(self):
        a, b, c = self.task(), self.task("b"), self.task("c")
        self.assertEqual(self.start(), [a, b])
        self.assertEqual(c.status, "queued")
        self.scheduler.finish(a)
        self.assertEqual(self.start(), [c])

    def test_same_workspace_readers_parallel(self):
        a, b = self.task(mode="方案"), self.task(mode="方案")
        self.assertEqual(self.start(), [a, b])

    def test_same_project_writers_use_available_slots(self):
        a, b, c = self.task(), self.task(), self.task("b")
        self.assertEqual(self.start(), [a, b])
        self.assertEqual(c.status, "queued")

    def test_overlapping_projects_use_available_slots(self):
        a, b = self.task("a"), self.task("a/child")
        self.assertEqual(self.start(), [a, b])

    def test_slot_limit_respects_submission_order(self):
        a = self.task(mode="方案")
        b = self.task()
        c = self.task(mode="方案")
        self.assertEqual(self.start(), [a, b])
        self.scheduler.finish(a)
        self.assertEqual(self.start(), [c])

    def test_duplicate_session_blocked_but_other_owner_isolated(self):
        a = self.task(session="same")
        with self.assertRaises(ValueError):
            self.task(session="same")
        b = self.task(session="same", owner="other")
        self.assertNotEqual(a.key, b.key)

    def test_cancel_queue_never_starts_and_is_idempotent(self):
        a = self.task()
        self.assertTrue(self.scheduler.cancel_task(a))
        self.assertFalse(self.scheduler.cancel_task(a))
        self.assertEqual(self.start(), [])
        self.assertEqual(a.status, "cancelled")

    def test_cancel_running_retains_slot_until_finished(self):
        a, b = self.task(), self.task()
        self.scheduler.limit = 1
        self.start()
        self.scheduler.cancel_task(a)
        self.assertEqual(a.status, "cancelling")
        self.assertFalse(self.start())
        self.scheduler.finish(a)
        self.assertEqual(a.status, "cancelled")
        self.assertEqual(self.start(), [b])

    def test_recovery_marks_interrupted_and_preserves_finished_and_legacy(self):
        context = ProjectContext("owner", str(self.base), self.base / "h.json", {
            "live": {"task": {"status": "running"}, "tasks": [{"status": "queued"}], "events": []},
            "done": {"task": {"status": "completed"}, "events": []}, "legacy": {"events": []}})
        self.assertTrue(context.recover())
        self.assertEqual(context.history["live"]["task"]["status"], "failed")
        self.assertTrue(context.history["live"]["task"]["interrupted"])
        self.assertEqual(context.history["live"]["tasks"][0]["status"], "failed")
        self.assertFalse(context.recover())
        self.assertEqual(len(context.history["live"]["events"]), 1)
        self.assertEqual(context.history["done"]["task"]["status"], "completed")

    def test_metadata_excludes_secrets_and_callbacks(self):
        task = self.task()
        task.token = "secret-token"
        self.assertNotIn("token", task.metadata())
        self.assertEqual(task.redact({"text": "x secret-token y"}), {"text": "x •••• y"})
        self.assertEqual(task.scope, session_scope(USER, task.context.project, task.session_id))


class MultiSessionTkTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="multisession-ui-", ignore_cleanup_errors=True)
        self.base = Path(self.scratch.name)
        self.projects = [self.base / name for name in ("project-a", "project-b", "project-c")]
        for project in self.projects:
            project.mkdir()
            (project / "marker.txt").write_text(project.name, encoding="utf-8")
        self.root = tk.Tk()
        self.tk_errors = []
        self.root.report_callback_exception = lambda *error: self.tk_errors.append(error)
        self.gui = App(self.root, self.base / "state", smoke=True)
        self.gui.account = dict(USER)
        self.gui.account_online = True
        self.client = self.gui.account_client = FixtureClient()
        self.gui.api_base = self.client.model_base
        self.gui.token = self.client.token
        self.gui.model_info = self.client.model()
        self.gui.poll_account = lambda: None
        self.errors = []
        self.gui.show_error = lambda *error: self.errors.append(error)
        self.stores, self.runtimes = {}, {}
        self.gui.create_engine = self.metadata
        self.gui.create_task_engine = self.runtime
        self.gui.open_project(str(self.projects[0]))
        self.wait(lambda: self.gui.engine and not self.gui.busy)
        self.addCleanup(self.cleanup)

    def metadata(self, path, *_args, **_kwargs):
        engine = MetadataEngine(path, self.stores.setdefault(path, {}))
        return engine, engine.sessions()

    def runtime(self, task, _limits):
        engine = StreamRuntime(task)
        self.runtimes[task.task_id] = engine
        return engine

    def wait(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.root.update()
        self.assertTrue(condition(), f"timeout; UI errors: {self.tk_errors}, errors: {self.errors}")

    def tick(self, seconds=.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)

    def new(self):
        previous = self.gui.session
        self.gui.new_session()
        self.wait(lambda: self.gui.session != previous and not self.gui.busy)
        return self.gui.session

    def start(self, project=None, mode="方案"):
        if project is not None:
            self.gui.open_project(str(self.projects[project]))
            self.wait(lambda: self.gui.engine and not self.gui.busy)
        self.new()
        self.gui.mode.set(mode)
        self.gui.input.insert("1.0", "prompt " + self.gui.session)
        self.gui.submit()
        return self.gui.current_task()

    def running(self, task):
        self.wait(lambda: task.status == "running" and task.task_id in self.runtimes and self.runtimes[task.task_id].started.is_set())
        return self.runtimes[task.task_id]

    def complete(self, task):
        self.running(task).commands.put("end")
        self.wait(lambda: task.status == "completed")

    def text(self, task):
        return "".join(event.get("text", "") for event in task.context.history[task.session_id]["events"]
                       if event["type"] == "delta")

    def cleanup(self):
        if self.gui.closing:
            self.scratch.cleanup()
            self.assertFalse(self.tk_errors)
            return
        for runtime in self.runtimes.values():
            runtime.cancel_release.set()
        for task in self.gui.tasks.active():
            self.gui.cancel_task(task)
        self.wait(lambda: not self.gui.tasks.active())
        self.gui.running = False
        self.gui.close()
        self.scratch.cleanup()
        self.assertFalse(self.tk_errors)

    def test_interleaved_streams_same_project_and_two_rotating_icons(self):
        a = self.start()
        ra = self.running(a)
        b = self.start()
        rb = self.running(b)
        self.assertNotEqual(ra.thread, get_ident())
        self.assertNotEqual(rb.thread, get_ident())
        self.assertIsNot(ra, rb)
        for index in range(20):
            ra.commands.put({"type": "delta", "text": f"A{index}|"})
            rb.commands.put({"type": "delta", "text": f"B{index}|"})
            self.tick(.008)
        self.wait(lambda: "A19|" in self.text(a) and "B19|" in self.text(b))
        self.assertNotIn("B0|", self.text(a))
        self.assertNotIn("A0|", self.text(b))
        canvas = self.gui.session_indicators.canvas
        self.assertEqual(sum(canvas.type(item) == "arc" for item in canvas.find_all()), 2)
        phase = self.gui.session_indicators.phase
        self.tick(.2)
        self.assertNotEqual(phase, self.gui.session_indicators.phase)
        self.complete(a)
        self.assertEqual(sum(canvas.type(item) == "arc" for item in canvas.find_all()), 1)
        self.assertEqual(b.status, "running")
        self.gui.open_session(a.session_id)
        self.assertIn("A19|", self.gui.chat.get("1.0", "end"))
        self.assertNotIn("B19|", self.gui.chat.get("1.0", "end"))
        self.complete(b)

    def test_navigation_projects_new_project_admin_settings_and_theme(self):
        a = self.start()
        ra = self.running(a)
        for button in (self.gui.new_project_button, self.gui.open_button,
                       self.gui.settings_button, self.gui.admin_button, self.gui.workspace_button):
            self.assertFalse(button.instate(["disabled"]))
        self.gui.new_project_button.invoke()
        self.root.update()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        dialog.destroy()
        created = create_project(self.base, "new-during-task")
        self.assertTrue(created.is_dir())
        self.gui.show_page("admin")
        self.wait(lambda: not self.gui.busy)
        self.assertEqual(self.gui.current_page, "admin")
        self.assertIn(("GET", "/api/admin/users"), self.client.requests)
        self.gui.show_page("workspace")
        self.assertEqual(self.gui.current_page, "workspace")
        self.gui.show_page("settings")
        self.assertEqual(self.gui.current_page, "settings")
        self.gui.cycle_theme()
        self.assertEqual(a.status, "running")
        self.gui.show_page("workspace")
        b = self.start(1)
        self.running(b)
        ra.commands.put({"type": "delta", "text": "background-original-project"})
        self.wait(lambda: "background-original-project" in self.text(a))
        self.assertNotIn("background-original-project", self.gui.chat.get("1.0", "end"))
        self.gui.open_project(str(self.projects[0]))
        self.wait(lambda: self.gui.engine and not self.gui.busy)
        self.gui.open_session(a.session_id)
        self.assertIn("background-original-project", self.gui.chat.get("1.0", "end"))
        self.assertEqual(a.status, "running")

    def test_stop_selected_session_keeps_other_running_and_waits_for_tool_exit(self):
        a = self.start()
        ra = self.running(a)
        b = self.start()
        rb = self.running(b)
        self.gui.open_session(a.session_id)
        ra.cancel_release.clear()
        self.gui.stop()
        self.wait(ra.stopped.is_set)
        self.assertEqual(a.status, "cancelling")
        self.assertEqual(b.status, "running")
        self.assertFalse(rb.stopped.is_set())
        self.gui.stop()
        rb.commands.put({"type": "delta", "text": "B still live"})
        self.wait(lambda: self.text(b) == "B still live")
        ra.cancel_release.set()
        self.wait(lambda: a.status == "cancelled")
        self.assertTrue(ra.closed.is_set())
        self.complete(b)

    def test_limit_queue_and_queue_cancellation(self):
        a, b, c = self.start(0), self.start(1), self.start(2)
        self.running(a)
        self.running(b)
        self.assertEqual(c.status, "queued")
        self.assertNotIn(c.task_id, self.runtimes)
        self.assertTrue(self.gui.session_indicators.timer)
        self.gui.stop()
        self.wait(lambda: c.status == "cancelled")
        self.assertNotIn(c.task_id, self.runtimes)

    def test_queue_starts_after_other_runtime_closed(self):
        a, b, c = self.start(0), self.start(1), self.start(2)
        self.assertEqual(c.status, "queued")
        self.complete(a)
        self.running(c)
        self.assertTrue(self.runtimes[a.task_id].closed.is_set())
        self.assertEqual(b.status, "running")

    def test_project_b_completion_frees_slot_for_second_writer_in_project_a(self):
        a = self.start(mode="执行")
        ra = self.running(a)
        b = self.start(1, mode="执行")
        rb = self.running(b)
        rb.commands.put({"type": "write", "text": "B output"})
        self.complete(b)
        c = self.start(0, mode="执行")
        self.running(c)
        self.assertEqual(a.status, "running")
        self.assertEqual(c.status, "running")
        ra.commands.put({"type": "write", "text": "A output"})
        self.wait(lambda: (self.projects[0] / "marker.txt").read_text() == "A output")
        self.assertEqual((self.projects[1] / "marker.txt").read_text(), "B output")
        self.complete(a)
        self.complete(c)

    def test_draft_and_completed_task_persist_and_recover_after_restart(self):
        a = self.start()
        self.running(a).commands.put({"type": "delta", "text": "durable result"})
        self.complete(a)
        sid = self.new()
        self.gui.input.insert("1.0", "draft B")
        self.gui.open_session(a.session_id)
        self.gui.open_session(sid)
        self.assertEqual(self.gui.input.get("1.0", "end-1c"), "draft B")
        self.gui.save_history()
        path = self.gui.history_path
        recovered = ProjectContext(str(self.gui.state), self.gui.project, path, read_json(path, {}))
        self.assertFalse(recovered.recover())
        self.assertEqual(recovered.history[a.session_id]["task"]["status"], "completed")
        self.assertEqual(recovered.history[sid]["draft"], "draft B")
        self.assertEqual(recovered.history[a.session_id]["events"][1]["text"], "durable result")
        self.assertNotIn(self.client.token, path.read_text(encoding="utf-8"))

    def test_failure_icon_stops_animation_and_error_is_redacted(self):
        a = self.start()
        self.running(a).commands.put(RuntimeError("fixture failed " + self.client.token))
        self.wait(lambda: a.status == "failed")
        canvas = self.gui.session_indicators.canvas
        self.assertFalse(any(canvas.type(item) == "arc" for item in canvas.find_all()))
        self.assertFalse(self.gui.session_indicators.timer)
        self.assertNotIn(self.client.token, a.error)
        self.assertIn("••••", a.error)

    def test_real_tk_restart_restores_completed_and_marks_crashed_task_interrupted(self):
        a = self.start()
        self.running(a).commands.put({"type": "delta", "text": "completed across restart"})
        self.complete(a)
        self.gui.history["ses_crashed"] = {"title": "Interrupted", "events": [],
                                           "task": {"task_id": "crash", "status": "running"}}
        self.gui.save_history()
        self.gui.close()
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *error: self.tk_errors.append(error)
        self.gui = App(self.root, self.base / "state", smoke=True)
        self.gui.account, self.gui.account_online = dict(USER), True
        self.gui.account_client = self.client
        self.gui.poll_account = lambda: None
        self.gui.create_engine = self.metadata
        self.gui.create_task_engine = self.runtime
        self.gui.open_project(str(self.projects[0]))
        self.wait(lambda: self.gui.engine and not self.gui.busy)
        self.assertEqual(self.gui.session_task_state(a.session_id)["status"], "completed")
        self.assertEqual(self.gui.session_task_state("ses_crashed")["status"], "failed")
        self.assertTrue(self.gui.session_task_state("ses_crashed")["interrupted"])
        self.gui.open_session(a.session_id)
        self.assertIn("completed across restart", self.gui.chat.get("1.0", "end"))
        self.assertFalse(self.gui.tasks.active())
        self.gui.open_session("ses_crashed")
        self.assertIn("任务已中断", self.gui.chat.get("1.0", "end"))

    def test_native_only_history_is_imported_before_new_turn_and_owner_checked(self):
        sid = self.new()
        self.gui.history.pop(sid)
        messages = [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "native earlier question"}]},
                    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "native earlier answer"}]}]
        self.gui.engine.history = lambda _session: deepcopy(messages)
        self.gui.open_session(sid)
        self.wait(lambda: not self.gui.busy)
        self.assertIn("native earlier answer", self.gui.chat.get("1.0", "end"))
        self.gui.mode.set("方案")
        self.gui.run_task("new turn")
        a = self.gui.current_task()
        self.complete(a)
        self.gui.open_session(sid)
        self.assertIn("native earlier question", self.gui.chat.get("1.0", "end"))
        self.assertIn("native earlier answer", self.gui.chat.get("1.0", "end"))
        self.assertIn("new turn", self.gui.chat.get("1.0", "end"))
        old = ProjectContext("other-owner", str(self.projects[1]), self.base / "foreign.json", {})
        self.gui.render_history([{"parts": [{"type": "text", "text": "foreign-private"}]}], old, "ses_foreign")
        self.assertNotIn("foreign-private", self.gui.chat.get("1.0", "end"))

    def test_pending_approval_follows_session_and_does_not_cross_tasks(self):
        a = self.start()
        ra = self.running(a)
        b = self.start(1)
        rb = self.running(b)
        ra.commands.put({"type": "approval", "request": WRITE})
        self.wait(lambda: bool(self.gui.approval_tickets))
        self.assertIsNone(self.gui.active_approval)
        rb.commands.put({"type": "delta", "text": "unblocked B"})
        self.wait(lambda: self.text(b) == "unblocked B")
        self.gui.open_project(str(self.projects[0]))
        self.wait(lambda: self.gui.engine and not self.gui.busy)
        self.gui.open_session(a.session_id)
        self.wait(lambda: self.gui.active_approval is not None)
        self.assertEqual(self.gui.active_approval["task_id"], a.task_id)
        self.gui.approval_reject_button.invoke()
        self.wait(lambda: ra.approvals == [False])
        self.assertEqual(b.status, "running")

    def test_four_server_slots_allow_four_writers_in_one_project(self):
        self.client.slot_limit = 4
        tasks = [self.start(mode="执行") for _ in range(5)]
        for task in tasks[:4]:
            self.running(task)
        self.assertEqual(tasks[4].status, "queued")
        self.complete(tasks[1])
        self.running(tasks[4])
        self.assertEqual(sum(task.status == "running" for task in tasks), 4)

    def test_runtime_close_failure_still_releases_global_slot(self):
        task = self.start()
        runtime = self.running(task)
        def close():
            runtime.closed.set()
            raise RuntimeError("fixture close failure")
        runtime.close = close
        runtime.commands.put("end")
        self.wait(lambda: task.status == "failed")
        self.assertNotIn(task.task_id, self.client.slots)

    def test_multiple_approvals_have_independent_notices_and_center_marks(self):
        a, b = self.start(), self.start(1)
        ra, rb = self.running(a), self.running(b)
        ra.commands.put({"type": "approval", "request": WRITE})
        rb.commands.put({"type": "approval", "request": WRITE})
        self.wait(lambda: len(self.gui.approval_notice_rows) == 2)
        self.assertEqual(self.gui.active_approval["task_id"], b.task_id)
        self.gui.session_indicators.draw()
        self.assertEqual(len(self.gui.session_indicators.task_canvas.find_withtag("approval")), 2)
        notice = self.gui.approval_notices.get("1.0", "end")
        self.assertIn("project-a 需要审批！", notice)
        self.assertIn("project-b 需要审批！", notice)
        self.gui.show_page("settings")
        self.assertIsNone(self.gui.active_approval)
        self.gui.finish_approval(True)
        self.assertFalse(ra.approvals or rb.approvals)
        self.gui.show_page("workspace")
        self.wait(lambda: self.gui.active_approval is not None)
        self.gui.approval_once_button.invoke()
        self.wait(lambda: rb.approvals == [True])
        self.assertEqual(self.gui.approval_notice_rows, [a.task_id])
        self.gui.running_task_list.selection_set(0)
        self.gui.select_running_task()
        self.wait(lambda: self.gui.active_approval and self.gui.active_approval["task_id"] == a.task_id)
        self.assertEqual(self.gui.session, a.session_id)
        self.wait(lambda: not self.gui.approval_action_locked)
        self.gui.approval_reject_button.invoke()
        self.wait(lambda: ra.approvals == [False])
        self.assertFalse(self.gui.approval_notice_rows)
        self.gui.session_indicators.draw()
        self.assertFalse(self.gui.session_indicators.task_canvas.find_withtag("approval"))

    def test_project_tree_plus_search_running_jump_and_completion_removal(self):
        a = self.start()
        self.running(a)
        self.gui.navigate_session(str(self.projects[1]), create=True)
        self.wait(lambda: self.gui.project == str(self.projects[1]) and self.gui.session and not self.gui.busy)
        self.gui.input.insert("1.0", "searchable task")
        self.gui.submit()
        b = self.gui.current_task()
        self.running(b)
        self.assertEqual(len(self.gui.project_tree.get_children()), 2)
        for key in self.gui.project_tree.get_children():
            self.assertEqual(self.gui.project_tree.item(key, "values"), ("＋",))
        self.gui.session_search.set("searchable")
        self.assertEqual(len(self.gui.project_tree.get_children()), 1)
        row = self.gui.project_tree.get_children()[0]
        self.assertEqual(self.gui.tree_rows[row][0], str(self.projects[1]))
        self.gui.session_search.set("")
        self.gui.running_task_list.selection_set(0)
        self.gui.select_running_task()
        self.wait(lambda: self.gui.project == a.context.project and self.gui.session == a.session_id and not self.gui.busy)
        self.complete(b)
        self.assertNotIn(b, self.gui.running_task_rows)
        self.assertIn(a, self.gui.running_task_rows)
        self.gui.poll_task_stats()
        self.wait(lambda: self.gui.task_stats.get("running_tasks") == 1)
        self.assertIn("剩余槽位：1", self.gui.task_stats_text.get())

    def test_actual_identity_transition_confirms_and_waits_no_privilege_migration(self):
        a = self.start()
        ra = self.running(a)
        ra.cancel_release.clear()
        self.client.token = "new-token-not-used-by-task"
        self.gui.account["role"] = "user"
        self.assertEqual(a.identity["role"], "admin")
        self.assertEqual(a.client.token, "isolated-token")
        resumed = []
        with patch("app.messagebox.askyesno", return_value=False):
            self.assertTrue(self.gui.confirm_task_transition("退出登录", lambda: resumed.append(True)))
        self.assertEqual(a.status, "running")
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.confirm_task_transition("退出登录", lambda: resumed.append(True))
        self.assertFalse(resumed)
        self.assertEqual(a.status, "cancelling")
        self.assertFalse(self.gui.tasks.accepting)
        ra.cancel_release.set()
        self.wait(lambda: bool(resumed))
        self.assertEqual(a.status, "cancelled")
        self.assertTrue(self.gui.tasks.accepting)

    def test_old_owner_late_output_never_renders_after_account_cleared(self):
        a = self.start()
        ra = self.running(a)
        self.gui.clear_account()
        self.gui.state = self.base / "other-account"
        self.gui.state.mkdir()
        self.gui.account = {**USER, "id": 99, "role": "user"}
        self.gui.track_task(a, {"type": "delta", "text": "old-owner-private"})
        self.assertNotIn("old-owner-private", self.gui.chat.get("1.0", "end"))
        self.assertIn("old-owner-private", self.text(a))
        self.wait(lambda: a.status == "cancelled")

    def test_real_role_switch_cancels_before_revoking_task_credentials(self):
        self.gui.account_client.user = {**USER, "role": "user"}
        self.gui.account = dict(self.gui.account_client.user)
        a = self.start()
        self.running(a)
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.switch_role("admin", "fixture-code")
            self.wait(lambda: self.gui.account.get("role") == "admin" and not self.gui.busy)
        self.assertEqual(a.status, "cancelled")
        self.assertEqual(a.identity["role"], "user")
        self.assertEqual(self.gui.token, "replacement-role-token")

    def test_logout_confirmation_keeps_or_cancels_all_tasks(self):
        a, b = self.start(), self.start(1)
        self.running(a)
        self.running(b)
        self.gui.check_account_connection = lambda: None
        with patch("app.messagebox.askyesno", return_value=False):
            self.gui.account_action()
        self.assertEqual(self.client.logout_count, 0)
        self.assertEqual((a.status, b.status), ("running", "running"))
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.account_action()
            self.wait(lambda: self.gui.account is None and not self.gui.busy)
        self.assertEqual(self.client.logout_count, 1)
        self.assertEqual((a.status, b.status), ("cancelled", "cancelled"))

    def test_auth_revocation_stops_background_sessions_not_just_selected(self):
        a, b = self.start(), self.start(1)
        self.running(a)
        self.running(b)
        self.new()
        def expired():
            raise AccountError("expired fixture session", 401)
        self.client.me = expired
        self.gui.poll_account = App.poll_account.__get__(self.gui)
        self.gui.poll_account()
        self.wait(lambda: a.status == b.status == "cancelled" and not self.gui.account_polling)
        self.assertFalse(self.gui.account_online)

    def test_credential_revocation_form_waits_for_task_cancellation(self):
        self.gui.account = {**USER, "role": "user"}
        a = self.start()
        runtime = self.running(a)
        self.gui.show_page("settings", section="passwords")
        from tkinter import ttk
        def descendants(parent):
            for child in parent.winfo_children():
                yield child
                yield from descendants(child)
        button = next(widget for widget in descendants(self.gui.page_panel)
                      if isinstance(widget, ttk.Button) and widget.cget("text") == "修改登录密码")
        entries = [widget for widget in descendants(button.master) if isinstance(widget, ttk.Entry)]
        self.assertEqual(len(entries), 3)
        for widget, text in zip(entries, ("Old#42", "New#42", "New#42")):
            widget.insert(0, text)
        runtime.cancel_release.clear()
        messages = []
        self.gui.require_account_login = messages.append
        with patch("app.messagebox.askyesno", return_value=True):
            button.invoke()
        self.assertEqual(a.status, "cancelling")
        self.assertFalse(self.client.password_changes)
        runtime.cancel_release.set()
        self.wait(lambda: bool(self.client.password_changes) and not self.gui.busy and bool(messages))
        self.assertEqual(a.status, "cancelled")
        self.assertEqual(self.client.password_changes, [("Old#42", "New#42", "New#42")])
        self.assertTrue(messages)

    def test_high_frequency_tokens_batched_while_tk_heartbeat_runs(self):
        a, b = self.start(), self.start()
        ra, rb = self.running(a), self.running(b)
        beats = []
        def heartbeat():
            beats.append(time.monotonic())
            if a.status in ACTIVE or b.status in ACTIVE:
                self.root.after(8, heartbeat)
        heartbeat()
        for index in range(1500):
            ra.commands.put({"type": "delta", "text": "a"})
            rb.commands.put({"type": "delta", "text": "b"})
        self.wait(lambda: len(self.text(a)) == len(self.text(b)) == 1500)
        self.assertGreater(len(beats), 3)
        self.assertEqual(self.text(a), "a" * 1500)
        self.assertEqual(self.text(b), "b" * 1500)
        self.complete(a)
        self.complete(b)

    def test_delete_running_session_blocked_other_session_deletable(self):
        a = self.start()
        self.running(a)
        idle = self.new()
        self.gui.open_session(a.session_id)
        self.gui.render_session_list()
        self.gui.update_actions()
        with patch("app.messagebox.askyesno") as confirm:
            self.gui.delete_session()
            confirm.assert_not_called()
        self.gui.open_session(idle)
        self.gui.render_session_list()
        self.gui.update_actions()
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.delete_session()
        self.wait(lambda: not self.gui.busy)
        self.assertNotIn(idle, self.gui.history)
        self.assertEqual(a.status, "running")

    def test_geometry_scroll_and_theme_keep_icons_at_session_rows(self):
        a, b = self.start(), self.start()
        self.running(a)
        self.running(b)
        for index in range(30):
            self.gui.history[f"idle{index}"] = {"title": f"History {index}", "events": []}
        self.gui.update_sessions(self.gui.sessions)
        self.gui.show_page("workspace")
        for geometry in ("800x600", "1366x768", "1920x1080"):
            with self.subTest(geometry=geometry):
                self.root.geometry(geometry)
                self.tick(.75)
                self.gui.project_tree.yview_moveto(0)
                self.root.update()
                self.gui.session_indicators.draw()
                canvas = self.gui.session_indicators.canvas
                arcs = [item for item in canvas.find_all() if canvas.type(item) == "arc"]
                self.assertEqual(len(arcs), 2)
                self.assertGreaterEqual(canvas.winfo_rootx(), self.gui.project_tree.winfo_rootx())
                self.assertLessEqual(canvas.winfo_rootx() + canvas.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                self.gui.cycle_theme()
                self.root.update()
                for item in canvas.find_all():
                    if canvas.type(item) == "arc":
                        self.assertEqual(canvas.itemcget(item, "outline"), self.gui.colors["accent"])
                self.gui.project_tree.yview_moveto(1)
                self.root.update()
                self.gui.session_indicators.draw()
                self.assertFalse(any(canvas.type(item) == "arc" for item in canvas.find_all()))
                self.assertIsNotNone(self.gui.session_indicators.timer)  # Running list still animates.
        self.assertEqual(a.status, "running")
        self.assertEqual(b.status, "running")

    def test_close_waits_for_all_task_resources_then_saves(self):
        a, b = self.start(), self.start(1)
        ra, rb = self.running(a), self.running(b)
        ra.cancel_release.clear()
        rb.cancel_release.clear()
        history_paths = [a.context.history_path, b.context.history_path]
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.close()
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue(self.gui.closing)
        ra.cancel_release.set()
        rb.cancel_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if not self.root.winfo_exists():
                    break
                self.root.update()
                time.sleep(.01)
            except tk.TclError:
                break
        self.assertTrue(ra.closed.is_set() and rb.closed.is_set())
        for path in history_paths:
            self.assertTrue(all(record.get("task", {}).get("status") == "cancelled"
                                for record in read_json(path, {}).values()))

if __name__ == "__main__":
    unittest.main(verbosity=2)
