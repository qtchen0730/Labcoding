"""Gateway self-service password changes, authorization, and migration tests."""

from pathlib import Path
from .test_support import TEST_ROOT
import tempfile
import unittest

from fastapi.testclient import TestClient

from .accounts import _ARGON2
from .test_gateway import ADMIN_CODE, admin_login, credentials, setup


ORIGIN = "https://gateway.fixture"
CHANGE_PASSWORD = "/api/me/password"
USER_PASSWORD = "fixture-new-user-password"
NEW_PASSWORD = "fresh!"


class SelfPasswordTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(scratch.cleanup)
        self.accounts, _, self.gateway = setup(Path(scratch.name), lambda _: None)

    def register(self, client, username, password=USER_PASSWORD):
        login = {"username": username, "password": password}
        registered = client.post("/api/register", json=login)
        self.assertEqual(registered.status_code, 201)
        return registered.json()["user"]["id"], login

    def login_headers(self, client, payload):
        login = client.post("/api/login", json=payload)
        self.assertEqual(login.status_code, 200, login.text)
        return credentials(login.json()["token"])

    def test_password_rules_and_user_identity(self):
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            user_id, user_login = self.register(client, "regular-user")
            other_id, _ = self.register(client, "other-user")
            headers = self.login_headers(client, user_login)
            valid = {"current_password": USER_PASSWORD, "new_password": NEW_PASSWORD,
                     "confirm_password": NEW_PASSWORD}
            self.assertEqual(client.post(CHANGE_PASSWORD, json=valid).status_code, 401)
            response = client.post(CHANGE_PASSWORD, headers=headers, json=valid | {"current_password": "wrong"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(NEW_PASSWORD, response.text)
            self.assertNotIn("wrong", response.text)
            for too_short_or_long in ("", "x" * 5, "x" * 129):
                invalid = valid | {"new_password": too_short_or_long, "confirm_password": too_short_or_long}
                self.assertEqual(client.post(CHANGE_PASSWORD, headers=headers, json=invalid).status_code, 400)
            self.assertEqual(client.post(CHANGE_PASSWORD, headers=headers,
                                         json=valid | {"confirm_password": "mismatch"}).status_code, 400)
            same = valid | {"new_password": USER_PASSWORD, "confirm_password": USER_PASSWORD}
            self.assertEqual(client.post(CHANGE_PASSWORD, headers=headers, json=same).status_code, 400)
            for target in ({"username": "other-user"}, {"user_id": other_id}):
                self.assertEqual(client.post(CHANGE_PASSWORD, headers=headers, json=valid | target).status_code, 400)
            self.assertEqual(client.post(f"/api/admin/users/{other_id}/password", headers=headers,
                                         json={"password": NEW_PASSWORD, "authorization_code": ADMIN_CODE}).status_code,
                             403)
            self.assertEqual(client.post("/api/admin/authorization-code", headers=headers,
                                         json={"current_code": ADMIN_CODE, "new_code": "100849"}).status_code, 403)
            self.assertEqual(client.get("/api/me", headers=headers).json()["user"]["id"], user_id)

    def test_six_character_change_preserves_other_accounts_and_revokes_all_own_sessions(self):
        with TestClient(self.gateway, base_url=ORIGIN) as client:
            target_id, user_login = self.register(client, "legacy-short-user", "short!")
            _, other_login = self.register(client, "another-user")
            current = self.login_headers(client, user_login)
            earlier = self.login_headers(client, user_login)
            other = self.login_headers(client, other_login)
            admin = self.login_headers(client, admin_login())
            payload = {"current_password": user_login["password"], "new_password": NEW_PASSWORD,
                       "confirm_password": NEW_PASSWORD}
            response = client.post(CHANGE_PASSWORD, headers=current, json=payload)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
            for revoked in (current, earlier):
                self.assertEqual(client.get("/api/me", headers=revoked).status_code, 401)
            self.assertEqual(client.get("/api/me", headers=other).status_code, 200)
            self.assertEqual(client.get("/api/admin/users", headers=admin).status_code, 200)
            self.assertEqual(client.post("/api/login", json=user_login).status_code, 401)
            fresh = self.login_headers(client, user_login | {"password": NEW_PASSWORD})
            self.assertEqual(client.get("/api/me", headers=fresh).json()["user"]["status"], "pending")
            # Existing administrator-driven reset retains its separate audit and policy.
            self.assertEqual(client.post(f"/api/admin/users/{target_id}/password", headers=admin,
                                         json={"password": "reset!", "authorization_code": ADMIN_CODE}).status_code,
                             204)
            self.assertEqual(client.get("/api/me", headers=fresh).status_code, 401)
            self.login_headers(client, user_login | {"password": "reset!"})
        with self.accounts.connect() as db:
            audit = db.execute("SELECT user_id, changed_at FROM self_password_audit").fetchall()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["user_id"], target_id)
            self.assertGreater(audit[0]["changed_at"], 0)
            self.assertEqual(db.execute("SELECT action FROM credential_audit").fetchone()["action"], "password_reset")

    def test_additive_migration_preserves_schema_version_existing_hash_and_session(self):
        user = self.accounts.create("legacy-user", USER_PASSWORD, bootstrap=False)
        session = self.accounts.login("legacy-user", USER_PASSWORD, "fixture")["token"]
        with self.accounts.connect() as db:
            db.execute("DROP TABLE self_password_audit")
            original = db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.accounts.migrate()
        self.accounts.migrate()
        self.assertEqual(self.accounts.session(session)["id"], user["id"])
        self.accounts.change_own_password(session, USER_PASSWORD, NEW_PASSWORD, NEW_PASSWORD, "fixture")
        with self.accounts.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            updated = db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
            self.assertNotEqual(updated, original)
            self.assertTrue(updated.startswith("$argon2id$"))
            self.assertNotIn(NEW_PASSWORD, updated)
            self.assertTrue(_ARGON2.verify(updated, NEW_PASSWORD))
