"""Native Tk checks for administrator login and role confirmation."""

from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
from time import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from access_view import build_access_page, prompt_role_switch
from account_client import AccountClient, AccountError, DEFAULT_SERVER, PASSWORD_RANGE


class AppStub:
    def __init__(self, root):
        self.root = root
        self.busy = self.running = self.closing = False
        self.prefs = {}
        self.account_connection = (DEFAULT_SERVER, "auto")
        self.get_account_client = Mock()
        self.gateway_status = tk.StringVar(master=root)
        self.gateway_error = tk.StringVar(master=root)
        self.gateway_report_status = tk.StringVar(master=root)
        self.check_account_connection = Mock()
        self.copy_account_diagnostics = Mock()
        self.show_account_diagnostics = Mock()
        self.signed_in = Mock()

    def frame(self, parent, _style=None):
        return ttk.Frame(parent)

    def label(self, parent, text="", variable=None, **_style):
        return ttk.Label(parent, text=text, textvariable=variable)

    def button(self, parent, text, command, _style=None):
        return ttk.Button(parent, text=text, command=command)

    def skin(self, *_args):
        pass

    def apply_theme(self):
        pass

    def worker(self, _label, request, done, failed):
        try:
            result = request()
        except RuntimeError as error:
            failed(error)
        else:
            done(result)


def widget(parent, widget_type, label=None):
    for child in parent.winfo_children():
        if isinstance(child, widget_type) and (label is None or child.cget("text") == label):
            return child
        found = widget(child, widget_type, label)
        if found is not None:
            return found
    return None


class AccessUI(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.app = AppStub(self.root)
        self.page = build_access_page(self.app, self.root)
        self.root.update_idletasks()

    def test_admin_checkbox_defaults_off_and_clears_hidden_code(self):
        self.assertFalse(self.page.admin_login.get())
        self.assertEqual(self.page.role_checkbox.winfo_manager(), "grid")
        self.assertEqual(self.page.admin_entry[1].winfo_manager(), "")
        self.page.role_checkbox.invoke()
        self.assertEqual(self.page.admin_entry[1].winfo_manager(), "grid")
        self.assertTrue(self.page.admin_entry[1].cget("show"))
        self.page.authorization.set("admin-fixture")
        self.page.role_checkbox.invoke()
        self.assertEqual(self.page.authorization.get(), "")
        self.page.select("admin")
        self.assertEqual(self.page.role_checkbox.winfo_manager(), "")
        self.assertEqual(self.page.admin_entry[1].winfo_manager(), "grid")
        self.page.select("login")
        self.assertFalse(self.page.admin_login.get())
        self.assertEqual(self.page.admin_entry[1].winfo_manager(), "")

    def test_login_sends_code_only_when_requested(self):
        client = self.app.get_account_client.return_value
        client.login.return_value = {"username": "fixture-user"}
        self.page.username.set("fixture-user")
        self.page.password.set("fixture-password")
        self.page.action.invoke()
        self.app.get_account_client.assert_called_with(self.app.account_connection)
        client.login.assert_called_once_with("fixture-user", "fixture-password")
        self.app.signed_in.assert_called_once_with(client, client.login.return_value)
        client.login.reset_mock()
        self.page.password.set("fixture-password")
        self.page.role_checkbox.invoke()
        self.page.action.invoke()
        client.login.assert_not_called()
        self.assertTrue(self.page.feedback.get())
        self.page.authorization.set("admin-fixture")
        self.page.action.invoke()
        client.login.assert_called_once_with(
            "fixture-user", "fixture-password", authorization_code="admin-fixture"
        )
        self.assertEqual(self.page.authorization.get(), "")
        self.assertEqual(self.page.password.get(), "")

    def test_failed_login_scrubs_authorization_from_feedback(self):
        self.app.get_account_client.return_value.login.side_effect = RuntimeError("admin-fixture fixture-password")
        self.page.username.set("fixture-user")
        self.page.password.set("fixture-password")
        self.page.role_checkbox.invoke()
        self.page.authorization.set("admin-fixture")
        self.page.action.invoke()
        self.assertNotIn("admin-fixture", self.page.feedback.get())
        self.assertNotIn("fixture-password", self.page.feedback.get())
        self.assertEqual(self.page.authorization.get(), "")

    def test_registration_minimum_and_login_hint_match(self):
        client = self.app.get_account_client.return_value
        replies = {"user": {"username": "fixture-user"}}
        client.register.return_value = client.register_admin.return_value = replies
        self.assertIn(PASSWORD_RANGE, self.page.password_entry[0].cget("text"))
        self.page.username.set("fixture-user")
        for role, register in (("user", client.register), ("admin", client.register_admin)):
            with self.subTest(role=role):
                self.page.select(role)
                self.page.authorization.set("123456")
                self.page.password.set("12345")
                self.page.confirmation.set("12345")
                self.page.action.invoke()
                register.assert_not_called()
                self.assertIn(PASSWORD_RANGE, self.page.feedback.get())
                self.page.password.set("123456")
                self.page.confirmation.set("123456")
                self.page.action.invoke()
                fields = ("fixture-user", "123456")
                if role == "admin":
                    fields += ("123456",)
                register.assert_called_once_with(*fields)
        self.assertIn(PASSWORD_RANGE, self.page.password_entry[0].cget("text"))

    def test_switch_prompts_for_both_directions_and_requires_nonempty_code(self):
        for target in ("user", "admin"):
            submitted = Mock()
            window = prompt_role_switch(self.app, target, submitted)
            entry = widget(window, ttk.Entry)
            confirm = widget(window, ttk.Button, "确认切换")
            self.assertTrue(entry.cget("show"))
            confirm.invoke()
            submitted.assert_not_called()
            self.assertTrue(window.winfo_exists())
            entry.insert(0, "switch-fixture")
            confirm.invoke()
            submitted.assert_called_once_with("switch-fixture")
            self.assertFalse(window.winfo_exists())


class OpenWebUIAccessRequests(unittest.TestCase):
    def setUp(self):
        self.client = AccountClient("https://account-fixture.example", mode="direct")
        self.client.token = "fixture-admin-session"
        self.grant = "g" * 64
        self.expiry = int(time()) + 600
        self.client.request = Mock(return_value={"grant": self.grant, "expires_at": self.expiry})

    def test_verification_and_update_are_session_bound(self):
        self.assertEqual(self.client.verify_open_webui_access("fixture-password"), {"expires_at": self.expiry})
        self.client.request.assert_called_once_with(
            "POST", "/api/open-webui/access", {"password": "fixture-password"}, expected_status=200
        )
        self.client.request.reset_mock()
        self.client.check_open_webui_access()
        self.client.request.assert_called_once_with(
            "GET", "/api/open-webui/access", expected_status=204, webui_grant=self.grant
        )
        self.client.request.reset_mock()
        self.client.change_open_webui_access("012345")
        self.client.request.assert_called_once_with(
            "POST", "/api/admin/open-webui/password", {"password": "012345"}, expected_status=204,
            webui_grant=self.grant,
        )
        self.assertEqual(self.client.open_webui_expires_at, 0)

    def test_different_session_cannot_reuse_cached_grant(self):
        self.client.verify_open_webui_access("fixture-password")
        self.client.request.reset_mock()
        self.client.token = "another-admin-session"
        with self.assertRaises(AccountError):
            self.client.check_open_webui_access()
        self.client.request.assert_not_called()

    def test_legacy_204_gateway_requires_paired_upgrade(self):
        self.client.request.side_effect = AccountError("Unexpected HTTP 204", 204)
        with self.assertRaisesRegex(AccountError, "配套的 1.5.3 修订版"):
            self.client.verify_open_webui_access("fixture-password")
        self.assertEqual(self.client.open_webui_expires_at, 0)

    def test_upgrade_and_secret_redaction(self):
        for submit in (self.client.verify_open_webui_access, self.client.change_open_webui_access):
            with self.subTest(endpoint=submit.__name__):
                self.client.request.side_effect = None
                self.client.verify_open_webui_access("fixture-password")
                self.client.request.side_effect = AccountError("fixture-password", 404, route="direct")
                with self.assertRaises(AccountError) as raised:
                    submit("fixture-password")
                self.assertIn("需要更新", str(raised.exception))
                self.assertEqual(raised.exception.status, 404)
                self.assertEqual(raised.exception.route, "direct")
                self.client.request.side_effect = AccountError("bad secret fixture-password", 403)
                with self.assertRaises(AccountError) as raised:
                    submit("fixture-password")
                self.assertNotIn("fixture-password", str(raised.exception))

    def test_validation_uses_six_to_128_characters(self):
        for password in ("12345", "x" * 129, "12345\0", "12345\ud800"):
            with self.subTest(password=repr(password)), self.assertRaises(AccountError):
                self.client.verify_open_webui_access(password)
        self.client.request.assert_not_called()
        for password in ("密" * 6, "x" * 128):
            self.client.verify_open_webui_access(password)
        self.assertEqual(self.client.request.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
