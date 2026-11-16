"""Shared Open WebUI entry password and account password policy tests."""

import os
from pathlib import Path
import tempfile
from time import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from .accounts import _ARGON2, _INITIAL_OPEN_WEBUI_VERIFIER, _write_verifier
from .test_gateway import ADMIN_CODE, ADMIN_PASSWORD, admin_login, credentials, setup


ACCESS = "/api/open-webui/access"
REPLACE = "/api/admin/open-webui/password"
ORIGIN = "https://gateway.fixture"
GRANT_HEADER = "X-CCSLab-WebUI-Grant"
ENTRY_PASSWORD = "fixture-webui-entry"


class OpenWebUIAccessTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.accounts, _, self.gateway = setup(self.root, lambda _: None)

    def configure_test_password(self):
        _write_verifier(self.accounts.open_webui_password_file, _ARGON2.hash(ENTRY_PASSWORD))

    def login(self, client, payload=None):
        response = client.post("/api/login", json=payload or admin_login())
        self.assertEqual(response.status_code, 200, response.text)
        return credentials(response.json()["token"])

    def authorize(self, client, headers, password=ENTRY_PASSWORD):
        response = client.post(ACCESS, json={"password": password}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn(password, response.text)
        payload = response.json()
        self.assertEqual(set(payload), {"expires_at", "grant"})
        return headers | {GRANT_HEADER: payload["grant"]}, payload["expires_at"]

    def test_setup_and_account_password_limits(self):
        self.assertEqual(self.accounts.open_webui_password_file.read_text(encoding="ascii").strip(),
                         _INITIAL_OPEN_WEBUI_VERIFIER)
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            regular = {"username": "new-user", "password": "新用户密码六"}
            admin = {"username": "new-admin", "password": "secret", "authorization_code": ADMIN_CODE}
            for invalid in ("short", "x" * 129):
                self.assertEqual(client.post("/api/register", json=regular | {"password": invalid}).status_code, 400)
                self.assertEqual(client.post("/api/register/admin", json=admin | {"password": invalid}).status_code, 400)
            user = client.post("/api/register", json=regular)
            self.assertEqual(user.status_code, 201, user.text)
            self.assertEqual(user.json()["user"]["status"], "pending")
            self.assertEqual(client.post("/api/register/admin", json=admin).status_code, 201)
            role_login = client.post("/api/login", json=admin | {"role": "admin"})
            self.assertEqual(role_login.status_code, 200)
            admin_headers = credentials(role_login.json()["token"])
            self.assertEqual(client.post("/api/login", json=regular).status_code, 200)
            target_id = user.json()["user"]["id"]
            reset = client.post(f"/api/admin/users/{target_id}/password", headers=admin_headers,
                                json={"password": "reset!", "authorization_code": ADMIN_CODE})
            self.assertEqual(reset.status_code, 204, reset.text)
            self.assertEqual(client.post("/api/login", json=regular | {"password": "reset!"}).status_code, 200)
            self.assertEqual(client.post("/api/login", json=regular).status_code, 401)

    def test_change_requires_active_admin_and_preserves_other_credentials(self):
        self.configure_test_password()
        verifier = self.accounts.open_webui_password_file
        admin_verifier = self.accounts.admin_code_file.read_bytes()
        initial_verifier = verifier.read_bytes()
        replacement = {"password": "入口测试新密钥"}
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            self.assertEqual(client.post(ACCESS, json={"password": ENTRY_PASSWORD}).status_code, 401)
            self.assertEqual(client.post(REPLACE, json=replacement).status_code, 401)
            ordinary_headers = self.login(client, {"username": "admin-test", "password": ADMIN_PASSWORD})
            self.assertEqual(client.post(ACCESS, json={"password": ENTRY_PASSWORD},
                                         headers=ordinary_headers).status_code, 403)
            self.assertEqual(client.post(REPLACE, json=replacement, headers=ordinary_headers).status_code, 403)
            admin_headers = self.login(client)
            self.assertEqual(client.post(REPLACE, json=replacement, headers=admin_headers).status_code, 403)
            self.assertEqual(client.get(ACCESS, headers=admin_headers).status_code, 403)
            grant_headers, expires = self.authorize(client, admin_headers)
            self.assertGreaterEqual(expires, int(time()) + 595)
            self.assertLessEqual(expires, int(time()) + 600)
            self.assertEqual(client.get(ACCESS, headers=grant_headers).status_code, 204)
            self.assertEqual(client.post(REPLACE, json={"password": "short"}, headers=grant_headers).status_code, 400)
            self.assertEqual(client.post(REPLACE, json=replacement | {"extra": "field"},
                                         headers=grant_headers).status_code, 400)
            self.assertEqual(verifier.read_bytes(), initial_verifier)
            response = client.post(REPLACE, json=replacement, headers=grant_headers)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
            self.assertEqual(client.get(ACCESS, headers=grant_headers).status_code, 403)
            self.assertEqual(client.post(REPLACE, json=replacement, headers=grant_headers).status_code, 403)
            self.assertEqual(client.get("/api/me", headers=ordinary_headers).status_code, 200)
            self.assertEqual(client.get("/api/admin/users", headers=admin_headers).status_code, 200)
            self.assertEqual(self.accounts.admin_code_file.read_bytes(), admin_verifier)
            for wrong in (ADMIN_CODE, ENTRY_PASSWORD):
                self.assertEqual(client.post(ACCESS, json={"password": wrong}, headers=admin_headers).status_code, 403)
            replacement_headers, _ = self.authorize(client, admin_headers, replacement["password"])

        updated_verifier = verifier.read_bytes()
        self.assertNotEqual(updated_verifier, initial_verifier)
        self.assertNotIn(replacement["password"].encode(), updated_verifier)
        if os.name != "nt":
            self.assertEqual(verifier.stat().st_mode & 0o777, 0o600)
        _, _, restarted = setup(self.root, lambda _: None, admin_code_file=self.accounts.admin_code_file,
                                bootstrap=False)
        self.assertEqual(verifier.read_bytes(), updated_verifier)
        with TestClient(restarted, base_url=ORIGIN) as client:
            self.assertEqual(client.get(ACCESS, headers=replacement_headers).status_code, 403)
            self.authorize(client, admin_headers, replacement["password"])
            self.assertEqual(client.get("/api/admin/users", headers=admin_headers).status_code, 200)

    def test_guess_budget_is_bounded_and_independent(self):
        self.configure_test_password()
        wrong = {"password": "invalid"}
        good = {"password": ENTRY_PASSWORD}
        with TestClient(self.gateway, base_url=ORIGIN, client=("192.0.2.10", 1000)) as client:
            admin_headers = self.login(client)
            for _ in range(6):
                self.authorize(client, admin_headers)
            self.assertEqual(client.post(ACCESS, json={"password": 123456}, headers=admin_headers).status_code, 403)
            for _ in range(3):
                response = client.post(ACCESS, json=wrong, headers=admin_headers)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(wrong["password"], response.text)
            self.assertEqual(client.post(ACCESS, json=good, headers=admin_headers).status_code, 429)
            self.assertEqual(client.post("/api/login", json=admin_login()).status_code, 200)
        with TestClient(self.gateway, base_url=ORIGIN, client=("192.0.2.11", 1000)) as client:
            for _ in range(2):
                self.assertEqual(client.post(ACCESS, json=wrong, headers=admin_headers).status_code, 403)
        with TestClient(self.gateway, base_url=ORIGIN, client=("192.0.2.12", 1000)) as client:
            self.assertEqual(client.post(ACCESS, json=good, headers=admin_headers).status_code, 429)

    def test_existing_invalid_verifier_fails_closed(self):
        verifier = self.accounts.open_webui_password_file
        verifier.write_text("invalid verifier", encoding="ascii")
        _, _, restarted = setup(self.root, lambda _: None, admin_code_file=self.accounts.admin_code_file,
                                bootstrap=False)
        with TestClient(restarted, base_url=ORIGIN) as client:
            admin_headers = self.login(client)
            self.assertEqual(client.post(ACCESS, json={"password": "guess!"}, headers=admin_headers).status_code, 503)
            self.assertEqual(verifier.read_text(encoding="ascii"), "invalid verifier")
            self.assertEqual(client.post(REPLACE, json={"password": "recoveR"}, headers=admin_headers).status_code, 403)

    def test_grant_is_bound_to_user_session_and_expiry(self):
        self.configure_test_password()
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            admin = self.login(client)
            grant, expires = self.authorize(client, admin)
            other = self.login(client)
            self.assertEqual(client.get(ACCESS, headers=other | {GRANT_HEADER: grant[GRANT_HEADER]}).status_code, 403)
            registration = {"username": "other-admin", "password": ADMIN_PASSWORD, "authorization_code": ADMIN_CODE}
            self.assertEqual(client.post("/api/register/admin", json=registration).status_code, 201)
            other_user = self.login(client, admin_login(username="other-admin"))
            self.assertEqual(client.get(ACCESS, headers=other_user | {GRANT_HEADER: grant[GRANT_HEADER]}).status_code, 403)
            forged = grant | {GRANT_HEADER: f"{expires + 10}.{grant[GRANT_HEADER].split('.')[1]}"}
            self.assertEqual(client.get(ACCESS, headers=forged).status_code, 403)
            with patch(f"{__package__}.accounts.time", return_value=expires):
                self.assertEqual(client.get(ACCESS, headers=grant).status_code, 403)
            self.assertEqual(client.post("/api/logout", headers=admin).status_code, 204)
            self.assertEqual(client.get(ACCESS, headers=grant).status_code, 401)

    def test_role_switch_and_own_password_change_invalidate_grants(self):
        self.configure_test_password()
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            admin = self.login(client)
            grant, _ = self.authorize(client, admin)
            switched = client.post("/api/session/role", headers=admin,
                                   json={"role": "user", "authorization_code": ADMIN_CODE})
            self.assertEqual(switched.status_code, 200)
            self.assertEqual(client.get(ACCESS, headers=grant).status_code, 401)
            downgraded = credentials(switched.json()["token"]) | {GRANT_HEADER: grant[GRANT_HEADER]}
            self.assertEqual(client.get(ACCESS, headers=downgraded).status_code, 403)
            admin = self.login(client)
            grant, _ = self.authorize(client, admin)
            new_password = "new-admin-login-password"
            response = client.post("/api/me/password", headers=admin, json={"current_password": ADMIN_PASSWORD,
                                   "new_password": new_password, "confirm_password": new_password})
            self.assertEqual(response.status_code, 204)
            self.assertEqual(client.get(ACCESS, headers=grant).status_code, 401)
            fresh = self.login(client, admin_login() | {"password": new_password})
            self.assertEqual(client.get(ACCESS, headers=fresh | {GRANT_HEADER: grant[GRANT_HEADER]}).status_code, 403)
