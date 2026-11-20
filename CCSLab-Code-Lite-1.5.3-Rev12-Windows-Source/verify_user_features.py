"""Rev9 native UI and Windows-encrypted login regression checks."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from verify_support import TEST_ROOT
sys.path.insert(0, str(Path(__file__).parent / "app"))
from app import App
from account_client import AccountError, DEFAULT_SERVER
from saved_login import SavedLogin
from quota_view import balance_text, parse_k, EXHAUSTED
from themes import THEMES
from admin_view import AdminPage
from account_client import AccountClient
from engine import _model_failure


USER = {"id": 5, "username": "ordinary-test", "role": "user", "assigned_role": "user", "status": "active"}


class UserFeatures(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="rev9-ui-")
        self.addCleanup(self.scratch.cleanup)
        self.root = tk.Tk()
        self.gui = App(self.root, Path(self.scratch.name), smoke=True)
        self.addCleanup(self.gui.close)
        self.root.update()

    def test_sun_cycles_persists_and_gear_opens_settings(self):
        app = self.gui
        self.assertEqual(app.theme.get(), "Dark")
        self.assertEqual(app.result_tabs.tab(app.result_tabs.select(), "text"), "执行记录")
        self.assertGreater(len(app.theme_icon.find_all()), 8)
        self.assertEqual(str(app.settings_button.cget("compound")), "right")
        self.assertTrue(app.settings_button.cget("image"))
        app.theme_icon.event_generate("<Button-1>")
        self.root.update()
        self.assertEqual(app.theme.get(), "Midnight")
        self.assertEqual(app.prefs["theme"], "herdr-midnight")
        app.settings_button.invoke()
        self.root.update()
        self.assertEqual(app.current_page, "settings")
        self.assertLess(app.theme_icon.winfo_x(), app.status_label.winfo_x())
        self.root.geometry("950x620")
        app.status.set("普通用户" * 22)
        self.root.update()
        self.assertGreaterEqual(app.theme_icon.winfo_width(),28)
        self.assertTrue(app.theme_icon.winfo_ismapped())

    def test_balance_zero_blocks_without_losing_draft_and_refill_recovers(self):
        app = self.gui
        app.account_client = Mock()
        app.account_online = True
        app.account = dict(USER, quota={"mode": "limited", "limit_tokens": 100000,
                                       "remaining_tokens": 25000})
        app.input.insert("1.0", "unfinished task and code")
        app.update_actions()
        self.assertEqual(app.quota_text.get(), "剩余额度 25%")
        self.assertTrue(app.can_use_agent)
        app.account["quota"]["remaining_tokens"] = 0
        app.update_actions()
        self.assertFalse(app.can_use_agent)
        self.assertEqual(app.quota_banner.cget("text"), EXHAUSTED)
        self.assertEqual(app.input.cget("state"), "disabled")
        self.assertEqual(app.input.get("1.0", "end-1c"), "unfinished task and code")
        app.account["quota"]["remaining_tokens"] = 100000
        app.update_actions()
        self.assertTrue(app.can_use_agent)
        self.assertEqual(app.input.cget("state"), "normal")
        self.assertEqual(app.input.get("1.0", "end-1c"), "unfinished task and code")
        app.account.pop("quota")
        app.update_actions()
        self.assertEqual(app.quota_text.get(), "额度不可用")
        self.assertTrue(app.can_use_agent)  # Old gateways keep existing access semantics.
        app.input.configure(state="disabled")
        app.update_actions()
        self.assertEqual(app.input.cget("state"), "disabled")  # Preserve task preparation's lock.

    def test_role_downgrade_skips_prompt_elevation_requires_it(self):
        app = self.gui
        app.account_client = Mock()
        app.account_online = True
        app.account = dict(USER, role="admin", assigned_role="admin")
        with patch.object(app, "switch_role") as switch, patch("app.prompt_role_switch") as prompt:
            app.change_role()
            switch.assert_called_once_with("user", "")
            prompt.assert_not_called()
            app.account["role"] = "user"
            app.change_role()
            self.assertEqual(prompt.call_args.args[1], "admin")

    def test_saved_login_dpapi_roundtrip_origin_binding_and_logout(self):
        store = self.gui.saved_login
        store.save(DEFAULT_SERVER, "ordinary-test", "account-test-password", "session-test-token")
        raw = store.path.read_bytes()
        self.assertNotIn(b"account-test-password", raw)
        self.assertNotIn(b"session-test-token", raw)
        self.assertEqual(SavedLogin(Path(self.scratch.name)).load(DEFAULT_SERVER)["password"], "account-test-password")
        self.assertIsNone(store.load("https://another.example"))
        store.forget()
        self.assertIsNone(store.load(DEFAULT_SERVER))

    def test_restart_restores_session_and_expiry_relogs_as_user(self):
        app = self.gui
        store = app.saved_login
        store.save(DEFAULT_SERVER, "ordinary-test", "account-test-password", "expired-token")
        app.page_controller.reset()
        client = Mock(base=DEFAULT_SERVER, token="")
        client.me.side_effect = AccountError("Expired", 401)
        def login(name, password):
            client.token = "renewed-token"
            return USER
        client.login.side_effect = login
        def worker(_label, request, done, failed):
            try:
                done(request())
            except Exception as error:
                failed(error)
        with patch.object(app, "get_account_client", return_value=client), patch.object(app, "worker", side_effect=worker), patch.object(app, "signed_in") as signed:
            app.restore_saved_login()
            signed.assert_called_once_with(client, USER)
        client.login.assert_called_once_with("ordinary-test", "account-test-password")
        self.assertEqual(store.load(DEFAULT_SERVER)["token"], "renewed-token")

    def test_login_consent_is_optional_and_password_remains_masked(self):
        page = self.gui.page_controller
        self.assertFalse(page.remember.get())
        self.assertTrue(page.password_entry[1].cget("show"))
        self.assertEqual(page.remember_checkbox.winfo_manager(), "grid")
        page.select("user")
        self.assertEqual(page.remember_checkbox.winfo_manager(), "")
        page.select("login")
        self.gui.saved_login.save(DEFAULT_SERVER, "ordinary-test", "account-test-password")
        page.remember.set(True)
        page.remember_checkbox.invoke()
        self.assertFalse(self.gui.saved_login.path.exists())

    def test_successful_login_and_logout_preserve_remembered_password(self):
        app = self.gui
        page = app.page_controller
        client = Mock(base=DEFAULT_SERVER,token="session-test-token")
        client.login.return_value = USER
        def worker(_label,request,done,failed=None):
            done(request())
        page.username.set(USER["username"])
        page.password.set("account-test-password")
        page.remember.set(True)
        with patch.object(app,"get_account_client",return_value=client),patch.object(app,"signed_in"),patch.object(app,"worker",side_effect=worker):
            page.action.invoke()
        self.assertEqual(app.saved_login.load(DEFAULT_SERVER)["username"],USER["username"])
        self.assertNotIn("password",app.prefs)
        self.assertEqual(page.password.get(),"")
        app.account_client=client
        app.account=dict(USER)
        app.account_online=True
        with patch.object(app,"worker",side_effect=worker),patch.object(app,"check_account_connection"):
            app.account_action()
        self.assertTrue(app.saved_login.path.exists())
        self.assertEqual(app.saved_login.load(DEFAULT_SERVER)["token"], "")
        self.assertEqual(app.page_controller.username.get(), USER["username"])
        self.assertEqual(app.page_controller.password.get(), "account-test-password")
        self.assertTrue(app.page_controller.password_entry[1].cget("show"))
        self.assertTrue(app.saved_login_suppressed)
        client.logout.assert_called_once()

    def test_quota_format_validation_and_authorization_preflight(self):
        self.assertEqual(parse_k("1.125"), 1125)
        for invalid in ("NaN", "Infinity", "-1", "1.0001", "1e99", "oops"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_k(invalid)
        user = dict(USER, quota={"mode": "limited", "limit_tokens": 100000, "remaining_tokens": 1})
        self.assertEqual(balance_text(user), "剩余额度 <0.1%")
        user["quota"]["remaining_tokens"] = 0
        client = Mock()
        client.me.return_value = user
        with self.assertRaisesRegex(AccountError, "额度已经用完"):
            App.require_active(client,check_quota=True)
        App.require_active(client)  # Already-issued local tools may finish without more inference.

    def test_admin_edits_k_quota_and_legacy_gateway_upgrade_message(self):
        app = self.gui
        app.account_client = client = Mock()
        app.account_online = True
        app.account = dict(USER, role="admin", assigned_role="admin")
        parent = app.frame(self.root)
        page = AdminPage(app, parent)
        page.users = {5: USER}
        page.edit_quota(5)
        self.root.update()
        popup = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        controls = list(descendants(popup))
        next(w for w in controls if isinstance(w, ttk.Radiobutton) and w.cget("text") == "限额").invoke()
        entry = next(w for w in controls if isinstance(w, ttk.Entry))
        entry.delete(0,"end")
        entry.insert(0,"25.125")
        save = next(w for w in controls if isinstance(w, ttk.Button) and w.cget("text") == "保存额度")
        with patch.object(app,"worker") as worker:
            save.invoke()
            worker.call_args.args[1]()
            client.set_user_quota.assert_called_once_with(5,"limited",25125)
        popup.destroy()
        parent.destroy()
        api = AccountClient(DEFAULT_SERVER,mode="direct")
        with patch.object(api,"request",side_effect=AccountError("Not Found",404)):
            with self.assertRaisesRegex(AccountError,"配套"):
                api.set_user_quota(5,"limited",1000)

    def test_quota_errors_are_distinct_from_context_failure(self):
        for code, status in (("token_quota_exhausted",402),("token_quota_insufficient",402),
                             ("token_quota_unconfirmed",409),("token_quota_changed",409),
                             ("token_quota_reserved",409),
                             ("用户额度已经用完，请充值。",402),("有请求用量待核对，请联系管理员。",409),
                             ("额度设置已更新，请重试本次任务。",409)):
            message, actual_status, kind = _model_failure({"data":{"statusCode":status,"message":code}})
            self.assertEqual((actual_status,kind),(status,"quota"))
            self.assertNotIn("上下文",message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
