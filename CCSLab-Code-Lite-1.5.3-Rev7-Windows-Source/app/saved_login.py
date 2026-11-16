"""Opt-in login storage encrypted for the current Windows user with DPAPI."""
import json
from account_client import normalize_server, valid_account_password
from connection import _dpapi


class SavedLogin:
    def __init__(self, data_root):
        self.path = data_root / "saved-login.dpapi"

    def forget(self):
        self.path.unlink(missing_ok=True)

    def save(self, server, username, password, token=""):
        record = {"server": normalize_server(server), "username": username,
                  "password": password, "token": token}
        if not isinstance(username, str) or not username or not valid_account_password(password):
            raise ValueError("Invalid saved account")
        encrypted = _dpapi(json.dumps(record, ensure_ascii=False).encode("utf-8"), encrypt=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_bytes(encrypted)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, server):
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 65536:
            raise ValueError("Invalid saved account")
        value = json.loads(_dpapi(self.path.read_bytes(), encrypt=False).decode("utf-8"))
        if (not isinstance(value, dict) or not isinstance(value.get("username"), str)
                or not valid_account_password(value.get("password"))
                or not isinstance(value.get("token", ""), str)):
            raise ValueError("Invalid saved account")
        # Never send saved credentials to a different configured server.
        if normalize_server(value.get("server")) != normalize_server(server):
            return None
        return value

    def update_session(self, server, token):
        record = self.load(server)
        if record:
            self.save(server, record["username"], record["password"], token)
