"""Sidebar deletion checks using isolated Tk data and the bundled OpenCode."""

from verify_support import TEST_ROOT

from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "app"))
from app import App, read_json, write_json
from engine import Engine, EngineError, _Job
from verify_engine import Handler


class SessionFixture:
    def __init__(self):
        self.deleted = []

    def delete_session(self, session_id):
        self.deleted.append(session_id)

    def close(self):
        pass


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="history-",
                                                    ignore_cleanup_errors=True)
        self.base = Path(self.scratch.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.project_file = self.project / "keep.txt"
        self.project_file.write_text("project content", encoding="utf-8")
        self.errors = []
        self.start_app()
        self.gui.engine = SessionFixture()
        self.seed([("ses_keep", "Keep conversation"), ("ses_remove", "Remove conversation")])

    def start_app(self):
        self.root = tk.Tk()
        self.gui = App(self.root, self.base / "state", smoke=True)
        self.gui.account = {"id": 1, "role": "user", "status": "active"}
        self.gui.account_online = True
        self.gui.account_client = SimpleNamespace(clear_open_webui_access=lambda: None)
        self.gui.show_error = lambda title, error: self.errors.append((title, str(error)))
        self.gui.project = str(self.project)
        self.gui.history_path = self.gui.state / "history.json"
        self.gui.load_session_titles()
        self.gui.show_page("workspace")

    def seed(self, rows):
        self.gui.history = {sid: {"title": title, "events": [{"type": "delta", "text": title}]}
                            for sid, title in rows}
        write_json(self.gui.history_path, self.gui.history)
        self.gui.session = rows[0][0]
        self.gui.update_sessions([{"id": sid, "title": title} for sid, title in rows])
        self.gui.update_actions()
        self.root.update()

    def wait(self):
        deadline = time.monotonic() + 20
        while self.gui.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.gui.busy)

    def tearDown(self):
        self.gui.close()
        self.assertEqual(self.project_file.read_text(encoding="utf-8"), "project content")
        self.scratch.cleanup()

    def test_recent_remove_cancel_persist_and_keep_open_project(self):
        other = str(self.base / "other-project")
        write_json(self.gui.state / "recents.json", [str(self.project), other])
        self.gui.refresh_recents()
        self.assertTrue(self.gui.remove_recent_button.instate(["disabled"]))
        self.gui.recent_list.selection_set(0)
        self.gui.update_delete_actions()
        self.assertFalse(self.gui.remove_recent_button.instate(["disabled"]))
        with patch("app.messagebox.askyesno", return_value=False):
            self.gui.remove_recent_button.invoke()
        self.assertEqual(self.gui.recents, [str(self.project), other])
        with patch("app.messagebox.askyesno", return_value=True), patch("app.write_json", side_effect=OSError("disk failure")):
            self.gui.remove_recent()
        self.assertEqual(self.gui.recents, [str(self.project), other])
        self.assertTrue(self.errors)
        with patch("app.messagebox.askyesno", return_value=True) as confirm:
            self.gui.remove_recent_button.invoke()
        self.assertEqual(confirm.call_args.kwargs["default"], "no")
        self.assertEqual(read_json(self.gui.state / "recents.json", []), [other])
        self.gui.refresh_recents()
        self.assertEqual(self.gui.recents, [other])
        self.assertEqual(self.gui.project, str(self.project))
        self.assertEqual(set(self.gui.history), {"ses_keep", "ses_remove"})

    def test_filtered_delete_targets_id_and_preserves_other_session(self):
        self.gui.drafts[(str(self.project), "ses_keep")] = "keep draft"
        self.gui.drafts[(str(self.project), "ses_remove")] = "remove draft"
        self.gui.session_search.set("Remove")
        self.gui.session_list.selection_set(0)
        self.gui.update_delete_actions()
        with patch("app.messagebox.askyesno", return_value=True) as confirm:
            self.gui.delete_session_button.invoke()
        self.wait()
        self.assertIn("Remove conversation", confirm.call_args.args[1])
        self.assertEqual(self.gui.engine.deleted, ["ses_remove"])
        self.assertEqual(self.gui.session, "ses_keep")
        self.assertEqual(set(read_json(self.gui.history_path, {})), {"ses_keep"})
        self.assertEqual(self.gui.drafts, {(str(self.project), "ses_keep"): "keep draft"})
        self.gui.session_search.set("")
        self.assertEqual([row["id"] for row in self.gui.sessions], ["ses_keep"])
        self.assertFalse(self.errors)

    def test_cancel_failure_and_running_task_preserve_records(self):
        with patch("app.messagebox.askyesno", return_value=False):
            self.gui.delete_session_button.invoke()
        self.assertFalse(self.gui.engine.deleted)
        self.gui.running = True
        self.gui.update_actions()
        self.assertTrue(self.gui.delete_session_button.instate(["disabled"]))
        with patch("app.messagebox.askyesno") as confirm:
            self.gui.delete_session()
            confirm.assert_not_called()
        self.gui.running = False
        self.gui.update_actions()
        with patch("app.messagebox.askyesno", return_value=True), patch.object(
                self.gui.engine, "delete_session", side_effect=EngineError("transport failure")):
            self.gui.delete_session_button.invoke()
            self.wait()
        self.assertEqual(set(self.gui.history), {"ses_keep", "ses_remove"})
        self.assertEqual(set(read_json(self.gui.history_path, {})), {"ses_keep", "ses_remove"})
        self.assertEqual(self.gui.session, "ses_keep")
        self.assertEqual(len(self.errors), 1)

    def test_local_save_failure_can_retry_and_header_is_below_search(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        header = next(w for w in descendants(self.root) if isinstance(w, tk.Label) and w.cget("text") == "历史会话")
        search = next(w for w in descendants(self.root) if isinstance(w, tk.ttk.Entry)
                      and str(w.cget("textvariable")) == str(self.gui.session_search))
        self.assertLess(search.winfo_rooty() + search.winfo_height(), header.winfo_rooty())
        self.assertLess(header.winfo_rooty(), self.gui.session_list.winfo_rooty())
        self.root.geometry("950x580")
        for dark in (False, True):
            self.gui.theme.set("经典深色" if dark else "经典浅色")
            self.gui.apply_theme()
            self.root.update()
            self.assertTrue(header.winfo_ismapped())
            self.assertGreaterEqual(self.gui.session_list.winfo_height(), 20)
            self.assertLessEqual(header.winfo_rooty() + header.winfo_height(),
                                 self.gui.session_list.winfo_rooty())
            self.assertGreaterEqual(self.gui.delete_session_button.winfo_width(), 40)
        with patch("app.messagebox.askyesno", return_value=True), patch("app.write_json", side_effect=OSError("disk failure")):
            self.gui.delete_session_button.invoke()
            self.wait()
        self.assertIn("ses_keep", self.gui.history)
        self.assertEqual(self.gui.session, "ses_keep")
        with patch("app.messagebox.askyesno", return_value=True):
            self.gui.delete_session_button.invoke()
            self.wait()
        self.assertIsNone(self.gui.session)
        self.assertNotIn("ses_keep", read_json(self.gui.history_path, {}))
        self.assertEqual(self.gui.input.get("1.0", "end-1c"), "")

    def test_native_session_delete_and_app_restart(self):
        model = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        model.calls, model.slow, model.down_until = [], Event(), 0
        runner = Thread(target=model.serve_forever, daemon=True)
        runner.start()
        engine = Engine(self.base / "engine-state", f"http://127.0.0.1:{model.server_port}/v1", "fixture-token")
        try:
            engine.open_project(str(self.project))
            keep, target = engine.new_session(), engine.new_session()
            engine.run(target, "PLAN-TURN", lambda _event: None, lambda _request: False, read_only=True)
            self.assertTrue(engine.history(target))
            engine.job = _Job(target, lambda _: False, False)
            with self.assertRaisesRegex(EngineError, "任务"):
                engine.delete_session(target)
            engine.job = None
            self.gui.engine = engine
            self.seed([(target, "Delete current"), (keep, "Keep native")])
            transcript = engine.history(target)
            model_calls = len(model.calls)
            with patch("app.simpledialog.askstring", return_value="Renamed native conversation"):
                self.gui.rename_session(target)
            self.assertEqual(engine.history(target), transcript)
            self.assertEqual(len(model.calls), model_calls)
            self.gui.update_sessions(engine.sessions())
            self.assertEqual(self.gui.session_list.get(0), "Renamed native conversation")
            self.gui.open_session(target)
            self.gui.input.insert("1.0", "remove unsent draft")
            self.gui.context_meter.record({"state": "measured", "used_tokens": 1000})
            with patch("app.messagebox.askyesno", return_value=True):
                self.gui.delete_session_button.invoke()
                self.wait()
            self.assertIsNone(self.gui.session)
            self.assertIsNone(self.gui.context_meter.reading.used_tokens)
            self.assertNotIn(target, {row["id"] for row in engine.sessions()})
            with self.assertRaises(EngineError):
                engine.history(target)
            engine.delete_session(target)  # Already-absent sessions can still clean up local records.
            self.gui.new_button.invoke()
            self.wait()
            self.assertNotEqual(self.gui.session, target)
            self.gui.close()
            self.start_app()
            engine.open_project(str(self.project))
            self.gui.engine = engine
            self.gui.history = read_json(self.gui.history_path, {})
            self.gui.update_sessions(engine.sessions())
            self.assertNotIn(target, {row["id"] for row in self.gui.sessions})
            self.assertIn(keep, {row["id"] for row in self.gui.sessions})
            self.assertFalse(self.errors)
        finally:
            engine.close()
            model.shutdown()
            model.server_close()
            runner.join(3)

    def test_right_click_renames_clicked_filtered_session_and_preserves_content(self):
        original_history = self.gui.history_path.read_bytes()
        original_drafts = {(str(self.project),"ses_keep"):"unfinished draft"}
        self.gui.drafts.update(original_drafts)
        self.gui.input.insert("1.0","current input")
        self.gui.session_search.set("Remove")
        self.root.update()
        bounds = self.gui.session_list.bbox(0)
        with patch.object(self.gui.session_menu,"tk_popup") as popup, patch("app.simpledialog.askstring",return_value="  用户命名的会话  "):
            self.gui.session_list.event_generate("<Button-3>",x=8,y=bounds[1]+bounds[3]//2)
            self.root.update()
            popup.assert_called_once()
            self.gui.session_menu.invoke(0)
        self.assertEqual(self.gui.session_titles,{"ses_remove":"用户命名的会话"})
        self.assertEqual(self.gui.history_path.read_bytes(),original_history)
        self.assertEqual(self.gui.session,"ses_keep")
        self.assertEqual(self.gui.drafts,original_drafts)
        self.assertEqual(self.gui.input.get("1.0","end-1c"),"current input")
        self.assertEqual(self.gui.session_search.get(),"")
        self.assertEqual(self.gui.session_list.get(1),"用户命名的会话")
        self.gui.update_sessions([{"id":"ses_keep","title":"Engine keep"},{"id":"ses_remove","title":"Engine old title"}])
        self.assertEqual(self.gui.session_list.get(1),"用户命名的会话")
        self.gui.session_search.set("用户命名")
        self.assertEqual([row['id'] for row in self.gui.visible_sessions],["ses_remove"])
        self.assertFalse(self.errors)

    def test_rename_restart_native_only_and_project_isolation(self):
        self.gui.history = {}
        self.gui.update_sessions([{"id":"ses_native","title":"Native history"}])
        with patch("app.simpledialog.askstring",return_value="Renamed native"):
            self.gui.rename_session("ses_native")
        self.assertEqual(self.gui.history,{})  # A display name is not an empty conversation cache.
        with patch.object(self.gui,"worker") as worker:
            self.gui.open_session("ses_native")
            self.assertEqual(worker.call_args.args[0],"读取会话")
        self.gui.close()
        self.start_app()
        self.gui.history=read_json(self.gui.history_path,{})
        self.gui.update_sessions([{"id":"ses_native","title":"Native history"}])
        self.assertEqual(self.gui.session_list.get(0),"Renamed native")
        self.gui.history_path=self.gui.state/"another-project.json"
        self.gui.load_session_titles()
        self.gui.update_sessions([{"id":"ses_native","title":"Other project native title"}])
        self.assertEqual(self.gui.session_list.get(0),"Other project native title")

    def test_rename_cancel_invalid_storage_failure_and_running_task(self):
        for value in (None,"  ","x"*101,"bad\nname"):
            with self.subTest(value=value),patch("app.simpledialog.askstring",return_value=value):
                self.gui.rename_session("ses_keep")
        self.assertFalse(self.gui.session_titles)
        with patch("app.simpledialog.askstring",return_value="New name"),patch("app.write_json",side_effect=OSError("disk failure")):
            self.gui.rename_session("ses_keep")
        self.assertEqual(self.gui.session_list.get(0),"Keep conversation")
        self.assertFalse(self.gui.session_titles)
        self.gui.running=True
        with patch("app.simpledialog.askstring") as prompt:
            self.gui.rename_session("ses_keep")
            prompt.assert_not_called()
        self.gui.running=False
        with patch.object(self.gui.session_menu,"tk_popup") as popup:
            self.gui.show_session_menu(SimpleNamespace(y=self.gui.session_list.winfo_height()-1,x_root=0,y_root=0))
            popup.assert_not_called()

    def test_delete_renamed_session_cleans_display_name_and_can_retry(self):
        with patch("app.simpledialog.askstring",return_value="Custom name"):
            self.gui.rename_session("ses_keep")
        self.gui.session_list.selection_set(0)
        real_write=write_json
        def fail_titles(path,value):
            if path.name.endswith('.titles.json'):
                raise OSError('name storage unavailable')
            real_write(path,value)
        with patch("app.messagebox.askyesno",return_value=True),patch("app.write_json",side_effect=fail_titles):
            self.gui.delete_session()
            self.wait()
        self.assertIn('ses_keep',self.gui.history)
        self.assertIn('ses_keep',self.gui.session_titles)
        with patch("app.messagebox.askyesno",return_value=True):
            self.gui.delete_session()
            self.wait()
        self.assertNotIn('ses_keep',self.gui.session_titles)
        self.assertNotIn('ses_keep',read_json(self.gui.history_path.with_suffix('.titles.json'),{}))

    def test_rename_dialog_does_not_write_to_changed_project(self):
        original_path=self.gui.history_path
        def switch_scope(*_args,**_kwargs):
            self.gui.project=str(self.base/'different-project')
            self.gui.history_path=self.gui.state/'changed-project.json'
            self.gui.session_titles={}
            return 'Wrong destination'
        with patch('app.simpledialog.askstring',side_effect=switch_scope):
            self.gui.rename_session('ses_keep')
        self.assertFalse(original_path.with_suffix('.titles.json').exists())
        self.assertFalse(self.gui.history_path.with_suffix('.titles.json').exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
