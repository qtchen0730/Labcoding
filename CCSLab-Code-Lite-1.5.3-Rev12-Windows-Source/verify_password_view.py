"""Exercise the Tk administrator password forms without touching real credentials."""

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from account_client import AccountError
from password_view import build_password_settings


class AccountFixture:
    def __init__(self):
        self.calls = []
        self.reset_error = None
        self.webui_error = None
        self.open_webui_expires_at = 0

    def change_own_password(self, current, new, confirmation):
        self.calls.append(("change_own_password", current, new, confirmation))

    def verify_open_webui_access(self, password):
        self.calls.append(("verify_open_webui_access", password))
        self.open_webui_expires_at = 1

    def request(self, method, path):
        self.calls.append((method, path))
        return {"users": [
            {"id": 4, "username": "测试管理员", "role": "admin"},
            {"id": 7, "username": "项目成员", "role": "user"},
        ]}

    def change_authorization_code(self, current_code, new_code):
        self.calls.append(("change_code", current_code, new_code))

    def reset_user_password(self, user_id, password, authorization_code):
        self.calls.append(("reset_password", user_id, password, authorization_code))
        if self.reset_error:
            raise self.reset_error

    def change_open_webui_access(self, password):
        self.calls.append(("change_open_webui_access", password))
        if self.webui_error:
            raise self.webui_error
        self.open_webui_expires_at = 0


class AppFixture:
    @staticmethod
    def confirm_task_transition(_label, _resume):
        return False  # Form fixture has no background execution; integration covers active tasks.

    def __init__(self):
        self.is_admin = True
        self.account = {"id": 4}
        self.account_client = AccountFixture()
        self.busy = False
        self.closing = False
        self.login_messages = []

    @staticmethod
    def frame(parent, _style=None):
        return ttk.Frame(parent)

    @staticmethod
    def label(parent, text="", **options):
        return tk.Label(parent, text=text, textvariable=options.get("variable"))

    @staticmethod
    def button(parent, text, command, _style=None):
        return ttk.Button(parent, text=text, command=command)

    @staticmethod
    def worker(_caption, action, done, failed):
        try:
            result = action()
        except AccountError as error:
            failed(error)
        else:
            done(result)

    def require_account_login(self, message):
        self.login_messages.append(message)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def fill(entries, values):
    for entry, value in zip(entries, values, strict=True):
        entry.delete(0, "end")
        entry.insert(0, value)


class PasswordSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = AppFixture()

    def mount(self):
        build_password_settings(self.app, self.root)
        self.rotate_button = self.button(self.root, "修改授权密码")
        self.reset_button = self.button(self.root, "重置登录密码")
        self.webui_button = self.button(self.root, "修改 Open WebUI 访问密码")
        self.verify_button = self.button(self.root, "验证最高权限")
        self.rotate_card, self.reset_card, self.webui_card = (
            self.rotate_button.master, self.reset_button.master, self.webui_button.master,
        )
        self.rotate_entries = self.entries(self.rotate_card)
        self.reset_entries = self.entries(self.reset_card)
        self.webui_entries = self.entries(self.webui_card)
        self.picker = next(w for w in descendants(self.reset_card) if isinstance(w, ttk.Combobox))

    @staticmethod
    def entries(card):
        return [widget for widget in descendants(card) if type(widget) is ttk.Entry]

    @staticmethod
    def button(card, text):
        return next(w for w in descendants(card) if isinstance(w, ttk.Button) and w.cget("text") == text)

    def test_ordinary_users_see_only_their_own_password_form(self):
        self.app.is_admin = False
        build_password_settings(self.app, self.root)
        buttons = [w.cget("text") for w in descendants(self.root) if isinstance(w, ttk.Button)]
        self.assertEqual(buttons, ["修改登录密码"])
        self.assertEqual(self.app.account_client.calls, [])
        submit = self.button(self.root, "修改登录密码")
        entries = self.entries(submit.master)
        self.assertTrue(all(entry.cget("show") for entry in entries))
        fill(entries, ("123456", "abcde", "abcde"))
        submit.invoke()
        self.assertEqual(self.app.account_client.calls, [])
        fill(entries, ("123456", "abcdef", "abcdef"))
        submit.invoke()
        self.assertEqual(self.app.account_client.calls, [("change_own_password", "123456", "abcdef", "abcdef")])
        self.assertTrue(self.app.login_messages)
        self.assertEqual([entry.get() for entry in entries], ["", "", ""])

    def test_code_confirmation_and_relogin(self):
        self.mount()
        self.assertTrue(all(entry.cget("show") for entry in [*self.rotate_entries, *self.reset_entries]))
        fill(self.rotate_entries, ("098765", "012345", "654321"))
        self.rotate_button.invoke()
        self.assertEqual(self.app.account_client.calls, [("GET", "/api/admin/users")])
        fill(self.rotate_entries, ("098765", "012345", "012345"))
        self.rotate_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("change_code", "098765", "012345"))
        self.assertTrue(self.app.login_messages)
        self.assertEqual([entry.get() for entry in self.rotate_entries], ["", "", ""])

    def test_reset_target_and_redact_credentials_on_error(self):
        self.app.password_reset_target = 7
        self.mount()
        self.assertEqual(self.picker.current(), 1)
        password = "012345"
        self.app.account_client.reset_error = AccountError(f"error: {password} / 098765", 403)
        fill(self.reset_entries, (password, password, "098765"))
        self.reset_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("reset_password", 7, password, "098765"))
        self.assertEqual([entry.get() for entry in self.reset_entries], ["", "", ""])
        rendered = "\n".join(w.cget("text") for w in descendants(self.reset_card) if isinstance(w, tk.Label))
        self.assertIn("error: •••• / ••••", rendered)
        self.assertNotIn(password, rendered)
        self.assertFalse(self.app.login_messages)
        self.app.account_client.reset_error = None
        self.picker.current(0)
        fill(self.reset_entries, (password, password, "012345"))
        self.reset_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("reset_password", 4, password, "012345"))
        self.assertTrue(self.app.login_messages)

    def test_open_webui_password_confirmation_and_separate_update(self):
        self.mount()
        self.assertTrue(all(entry.cget("show") for entry in self.webui_entries))
        fill(self.webui_entries, ("", "123456", "123456"))
        self.webui_button.invoke()
        self.assertEqual(self.app.account_client.calls, [("GET", "/api/admin/users")])
        fill(self.webui_entries, ("gate-fixture", "123456", "654321"))
        self.verify_button.invoke()
        self.webui_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("verify_open_webui_access", "gate-fixture"))

        self.app.account_client.webui_error = AccountError("invalid 123456", 403)
        fill(self.webui_entries, ("", "123456", "123456"))
        self.webui_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("change_open_webui_access", "123456"))
        self.assertEqual([entry.get() for entry in self.webui_entries], ["", "", ""])
        rendered = "\n".join(w.cget("text") for w in descendants(self.webui_card) if isinstance(w, tk.Label))
        self.assertIn("invalid ••••", rendered)
        self.assertNotIn("123456", rendered)

        self.app.account_client.webui_error = None
        fill(self.webui_entries, ("", "654321", "654321"))
        self.webui_button.invoke()
        self.assertEqual(self.app.account_client.calls[-1], ("change_open_webui_access", "654321"))
        self.assertFalse(self.app.login_messages)


if __name__ == "__main__":
    unittest.main(verbosity=2)
