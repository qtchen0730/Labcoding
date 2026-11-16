"""Account API, gateway authorization and proxy behavior tests."""

import asyncio
import json
from pathlib import Path
from .test_support import TEST_ROOT
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx

from .accounts import AccountError, Accounts, read_admin_code, token_hash, write_admin_code
from .gateway import Settings, create_app


class ControllerFixture:
    def __init__(self):
        self.in_flight = 0
        self.maintenance = False

    def status(self):
        return {"status": "running", "total_context": 131072, "slots": 5, "effective_context_limit": 26368}

    def begin_inference(self):
        if self.maintenance:
            return False
        self.in_flight += 1
        return True

    def end_inference(self):
        self.in_flight -= 1


class PausedStream(httpx.AsyncByteStream):
    def __init__(self, entered, closed):
        self.entered = entered
        self.closed = closed

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"prefix"}}]}\n\n'
        self.entered.set()
        await asyncio.sleep(15)
        yield b'data: {"choices":[{"delta":{"content":"should not arrive"}}]}\n\n'

    async def aclose(self):
        self.closed.set()


UPSTREAM_KEY = "fixture-shared-upstream-key"
ADMIN_PASSWORD = "fixture-admin-password"
USER_PASSWORD = "fixture-user-password"
ADMIN_CODE = "310742"


def credentials(token):
    return {"Authorization": f"Bearer {token}"}


def admin_login(code=ADMIN_CODE, username="admin-test"):
    return {"username": username, "password": ADMIN_PASSWORD, "role": "admin", "authorization_code": code}


def setup(root, upstream, *, admin_code_file=None, bootstrap=True):
    db = root / "accounts.sqlite3"
    if admin_code_file is None:
        admin_code_file = root / "administrator.verifier"
        write_admin_code(admin_code_file, ADMIN_CODE)
    accounts = Accounts(db, admin_code_file=admin_code_file)
    accounts.migrate()
    if bootstrap:
        accounts.create("admin-test", ADMIN_PASSWORD, bootstrap=True)
    controller = ControllerFixture()
    app = create_app(
        Settings(db, "http://llama.fixture", "lab-general", UPSTREAM_KEY,
                 admin_registration_code_file=admin_code_file),
        model_control=controller,
        upstream_transport=httpx.MockTransport(upstream),
    )
    return accounts, controller, app


class AccountGatewayTests(unittest.TestCase):
    def test_administrator_registration_secret_and_role(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            secret_file = root / "register-admin.secret"
            secret = "310742"
            accounts, _, gateway = setup(root, lambda _: None, admin_code_file=secret_file, bootstrap=False)
            self.assertFalse(accounts.has_administrator())
            payload = {"username": "new-admin", "password": ADMIN_PASSWORD, "authorization_code": secret}
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                self.assertEqual(client.post("/api/register/admin", json=payload).status_code, 503)
                secret_file.write_text(secret, encoding="ascii")
                self.assertIsNone(read_admin_code(secret_file))
                self.assertEqual(client.post("/api/register/admin", json=payload).status_code, 503)

                write_admin_code(secret_file, secret)
                self.assertTrue(secret_file.read_text(encoding="ascii").startswith("$argon2id$"))
                self.assertNotIn(secret, secret_file.read_text(encoding="ascii"))
                self.assertEqual(client.post("/api/register/admin", json={
                    "username": "new-admin", "password": ADMIN_PASSWORD,
                }).status_code, 400)
                self.assertEqual(client.post("/api/register/admin", json=payload | {
                    "authorization_code": "875964",
                }).status_code, 403)
                self.assertEqual(client.post("/api/register/admin", json=payload | {
                    "authorization_code": int(secret),
                }).status_code, 403)
                self.assertFalse(accounts.has_administrator())
                self.assertEqual(client.post("/api/register", json=payload).status_code, 400)
                ordinary = client.post("/api/register", json={"username": "ordinary-1", "password": USER_PASSWORD})
                self.assertEqual(ordinary.status_code, 201)
                self.assertEqual(ordinary.json()["user"]["status"], "pending")

                response = client.post("/api/register/admin", json=payload)
                self.assertEqual(response.status_code, 201, response.text)
                admin = response.json()["user"]
                self.assertEqual((admin["role"], admin["status"]), ("admin", "active"))
                login = client.post("/api/login", json=admin_login(secret, admin["username"]))
                self.assertEqual(login.status_code, 200)
                bearer = credentials(login.json()["token"])
                self.assertEqual(client.get("/api/admin/users", headers=bearer).status_code, 200)
                self.assertEqual(client.get("/v1/models", headers=bearer).status_code, 200)
                with self.assertRaises(AccountError) as failure:
                    accounts.create("second-bootstrap", ADMIN_PASSWORD, bootstrap=True)
                self.assertEqual(failure.exception.status_code, 409)

    def test_administrator_registration_source_and_global_limits(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            secret_file = root / "register-admin.secret"
            secret = "310742"
            write_admin_code(secret_file, secret)
            _, _, gateway = setup(root, lambda _: None, admin_code_file=secret_file)
            payload = {"username": "second-admin", "password": ADMIN_PASSWORD, "authorization_code": "875964"}
            with TestClient(gateway, base_url="https://gateway.fixture", client=("192.0.2.10", 5000)) as client:
                for _ in range(3):
                    self.assertEqual(client.post("/api/register/admin", json=payload).status_code, 403)
                self.assertEqual(client.post("/api/register/admin", json=payload | {
                    "authorization_code": secret,
                }).status_code, 429)

            _, _, reloaded = setup(root, lambda _: None, admin_code_file=secret_file, bootstrap=False)
            with TestClient(reloaded, base_url="https://gateway.fixture", client=("192.0.2.11", 5010)) as client:
                for _ in range(2):
                    self.assertEqual(client.post("/api/register/admin", json=payload).status_code, 403)
            with TestClient(reloaded, base_url="https://gateway.fixture", client=("192.0.2.12", 5020)) as client:
                self.assertEqual(client.post("/api/register/admin", json=payload | {
                    "authorization_code": secret,
                }).status_code, 429)
                self.assertEqual(client.post("/api/register", json={
                    "username": "still-register", "password": USER_PASSWORD,
                }).status_code, 201)

    def test_accounts_auth_and_proxy(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            requests = []

            def upstream(request):
                requests.append(request)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream", "Set-Cookie": "test=redacted"},
                    text='data: {"choices":[],"usage":{"prompt_tokens":500,"completion_tokens":30}}\n\ndata: [DONE]\n\n',
                )

            accounts, controller, gateway = setup(Path(scratch), upstream)
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                model = {"model": "lab-general", "messages": [{"role": "user", "content": "测试"}],
                         "stream": True, "max_tokens": 9000}
                self.assertEqual(client.post("/v1/chat/completions", json=model).status_code, 401)
                self.assertEqual(client.get("/v1/models").status_code, 401)
                self.assertEqual(client.get("/openapi.json").status_code, 404)

                register = client.post("/api/register", json={"username": "测试开发者", "password": USER_PASSWORD})
                self.assertEqual(register.status_code, 201, register.text)
                target = register.json()["user"]
                self.assertEqual((target["status"], target["role"]), ("pending", "user"))
                user_login = client.post("/api/login", json={"username": target["username"], "password": USER_PASSWORD})
                self.assertEqual(user_login.status_code, 200)
                token = user_login.json()["token"]
                bearer = credentials(token)
                self.assertEqual(client.get("/api/me", headers=bearer).json()["user"]["status"], "pending")
                self.assertEqual(client.post("/v1/chat/completions", json=model, headers=bearer).status_code, 403)
                self.assertFalse(requests)

                wrong = client.post("/api/login", json={"username": "admin-test", "password": "incorrect"})
                self.assertEqual(wrong.status_code, 401)
                signed_admin = client.post("/api/login", json=admin_login())
                self.assertEqual(signed_admin.status_code, 200)
                admin = credentials(signed_admin.json()["token"])
                self.assertEqual(client.get("/api/admin/users", headers=bearer).status_code, 403)
                self.assertEqual(client.get("/api/admin/users", headers=admin).json()["users"][0]["status"], "pending")
                status_url = f"/api/admin/users/{target['id']}/status"
                self.assertEqual(client.post(status_url, json={"status": "active"}, headers=bearer).status_code, 403)
                self.assertEqual(client.post(status_url, json={"status": "active"}, headers=admin).status_code, 200)
                self.assertEqual(client.get("/api/me", headers=bearer).json()["user"]["status"], "active")
                self.assertEqual(client.post("/api/admin/model/apply", json={"confirmation": "fake"}, headers=bearer).status_code, 403)
                self.assertEqual(client.get("/api/model", headers=bearer).json()["effective_context_limit"], 26368)
                self.assertEqual(client.get("/api/model", headers=bearer).json()["output_limit"], 4096)
                self.assertEqual(client.get("/v1/models", headers=bearer).status_code, 200)

                controller.maintenance = True
                self.assertEqual(client.post("/v1/chat/completions", json=model, headers=bearer).status_code, 503)
                controller.maintenance = False
                self.assertEqual(client.post("/v1/chat/completions", json=model | {"id_slot": 0}, headers=bearer)
                                 .status_code, 400)
                response = client.post("/v1/chat/completions", json=model, headers=bearer | {"Cookie": "client=cookie"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('"prompt_tokens":500', response.text)
                self.assertIn("data: [DONE]", response.text)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(controller.in_flight, 0)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.headers["Authorization"], f"Bearer {UPSTREAM_KEY}")
                self.assertNotIn("cookie", request.headers)
                forwarded = json.loads(request.content)
                self.assertEqual(forwarded["max_tokens"], 4096)
                self.assertEqual(forwarded["stream_options"], {"include_usage": True})

                self.assertEqual(client.post(status_url, json={"status": "disabled"}, headers=admin).status_code, 200)
                self.assertEqual(client.get("/api/me", headers=bearer).json()["user"]["status"], "disabled")
                self.assertEqual(client.post("/v1/chat/completions", json=model, headers=bearer).status_code, 403)
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.post("/api/logout", headers=bearer).status_code, 204)
                self.assertEqual(client.get("/api/me", headers=bearer).status_code, 401)
                next_login = client.post("/api/login", json={"username": target["username"], "password": USER_PASSWORD}).json()
                self.assertEqual(next_login["user"]["status"], "disabled")
                with accounts.connect() as db:
                    db.execute("UPDATE sessions SET expires_at = 0 WHERE token_hash = ?", (token_hash(next_login["token"]),))
                    self.assertEqual(db.execute("SELECT count(*) FROM user_status_audit").fetchone()[0], 2)
                    self.assertTrue(all(row[0].startswith("$argon2id$") for row in db.execute("SELECT password_hash FROM users")))
                self.assertEqual(client.get("/api/me", headers=credentials(next_login["token"])).status_code, 401)

            with TestClient(gateway, base_url="http://gateway.fixture") as insecure:
                refused = insecure.post("/api/login", headers={"X-Forwarded-Proto": "https"},
                                        json={"username": "admin-test", "password": ADMIN_PASSWORD})
                self.assertEqual(refused.status_code, 400)
                self.assertIn("HTTPS", refused.text)
            for file in Path(scratch).iterdir():
                for secret in (UPSTREAM_KEY, ADMIN_PASSWORD, USER_PASSWORD, token):
                    self.assertNotIn(secret.encode("ascii"), file.read_bytes())

    def test_session_roles_default_and_switch(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            _, _, gateway = setup(Path(scratch), lambda _: None)
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                result = client.post("/api/login", json={"username": "admin-test", "password": ADMIN_PASSWORD})
                self.assertEqual(result.status_code, 200)
                original = result.json()
                self.assertEqual((original["user"]["role"], original["user"]["assigned_role"]), ("user", "admin"))
                ordinary = credentials(original["token"])
                self.assertEqual(client.get("/api/admin/users", headers=ordinary).status_code, 403)
                self.assertEqual(client.get("/v1/models", headers=ordinary).status_code, 200)
                self.assertEqual(client.post("/api/session/role", json={"role": "admin"}, headers=ordinary).status_code, 400)
                elevation = {"role": "admin", "authorization_code": ADMIN_CODE}
                self.assertEqual(client.post("/api/session/role", json=elevation | {
                    "authorization_code": "999999",
                }, headers=ordinary).status_code, 403)
                result = client.post("/api/session/role", json=elevation, headers=ordinary)
                self.assertEqual(result.status_code, 200, result.text)
                elevated = result.json()
                self.assertEqual(elevated["user"]["role"], "admin")
                self.assertEqual(elevated["expires_at"], original["expires_at"])
                admin = credentials(elevated["token"])
                self.assertEqual(client.get("/api/me", headers=ordinary).status_code, 401)
                self.assertEqual(client.get("/api/admin/users", headers=admin).status_code, 200)
                result = client.post("/api/session/role", json={
                    "role": "user", "authorization_code": "",
                }, headers=admin)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["user"]["role"], "user")
                self.assertEqual(client.get("/api/me", headers=admin).status_code, 401)
                self.assertEqual(client.get("/api/admin/users", headers=credentials(result.json()["token"])).status_code, 403)

                pending = client.post("/api/register", json={"username": "pending-one", "password": USER_PASSWORD})
                self.assertEqual(pending.status_code, 201)
                body = {"username": "pending-one", "password": USER_PASSWORD}
                self.assertEqual(client.post("/api/login", json=body | elevation).status_code, 403)
                ordinary_result = client.post("/api/login", json=body).json()
                self.assertEqual(ordinary_result["user"]["status"], "pending")
                user = credentials(ordinary_result["token"])
                self.assertEqual(client.post("/api/session/role", json=elevation, headers=user).status_code, 403)
                self.assertEqual(client.get("/v1/models", headers=user).status_code, 403)

    def test_password_reset_and_code_rotation(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            accounts, _, gateway = setup(root, lambda _: None)
            new_password = "different-user-password"
            new_code = "602159"
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                admin = credentials(client.post("/api/login", json=admin_login()).json()["token"])
                other_admin = credentials(client.post("/api/login", json=admin_login()).json()["token"])
                result = client.post("/api/register", json={"username": "reset-user", "password": USER_PASSWORD})
                target = result.json()["user"]["id"]
                user_login = {"username": "reset-user", "password": USER_PASSWORD}
                first_token = credentials(client.post("/api/login", json=user_login).json()["token"])
                second_token = credentials(client.post("/api/login", json=user_login).json()["token"])
                regular_admin = credentials(client.post("/api/login", json={
                    "username": "admin-test", "password": ADMIN_PASSWORD,
                }).json()["token"])
                password_path = f"/api/admin/users/{target}/password"
                reset = {"password": new_password, "authorization_code": ADMIN_CODE}
                self.assertEqual(client.post(password_path, json=reset, headers=regular_admin).status_code, 403)
                self.assertEqual(client.post(password_path, json=reset | {"password": "short"}, headers=admin).status_code, 400)
                self.assertEqual(client.post(password_path, json=reset | {
                    "authorization_code": "999999",
                }, headers=admin).status_code, 403)
                self.assertEqual(client.post(password_path, json=reset, headers=admin).status_code, 204)
                self.assertEqual(client.get("/api/me", headers=first_token).status_code, 401)
                self.assertEqual(client.get("/api/me", headers=second_token).status_code, 401)
                self.assertEqual(client.post("/api/login", json=user_login).status_code, 401)
                current = client.post("/api/login", json=user_login | {"password": new_password})
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["user"]["status"], "pending")

                code_path = "/api/admin/authorization-code"
                change = {"current_code": ADMIN_CODE, "new_code": new_code}
                self.assertEqual(client.post(code_path, json=change, headers=regular_admin).status_code, 403)
                self.assertEqual(client.post(code_path, json=change, headers=admin).status_code, 204)
                self.assertEqual(client.get("/api/me", headers=admin).status_code, 401)
                self.assertEqual(client.get("/api/me", headers=other_admin).status_code, 401)
                self.assertEqual(client.get("/api/me", headers=regular_admin).status_code, 200)
                self.assertEqual(client.post("/api/login", json=admin_login(ADMIN_CODE)).status_code, 403)
                signed_admin = client.post("/api/login", json=admin_login(new_code))
                self.assertEqual(signed_admin.status_code, 200, signed_admin.text)
                current_admin = credentials(signed_admin.json()["token"])
                # An operator-side verifier replacement must invalidate elevated tokens too.
                write_admin_code(accounts.admin_code_file, "386028")
                self.assertEqual(client.get("/api/me", headers=current_admin).status_code, 401)

            with accounts.connect() as db:
                records = [tuple(row) for row in db.execute(
                    "SELECT target_id, action FROM credential_audit ORDER BY id"
                )]
                self.assertEqual(records, [(target, "password_reset"), (None, "authorization_code_changed")])
            for file in root.iterdir():
                for secret in (new_password, new_code, USER_PASSWORD, ADMIN_CODE):
                    self.assertNotIn(secret.encode("ascii"), file.read_bytes())

    def test_guess_limit_is_shared_between_admin_endpoints(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            _, _, gateway = setup(Path(scratch), lambda _: None)
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                wrong = "123456"
                ordinary = credentials(client.post("/api/login", json={
                    "username": "admin-test", "password": ADMIN_PASSWORD,
                }).json()["token"])
                self.assertEqual(client.post("/api/register/admin", json={
                    "username": "admin-new", "password": ADMIN_PASSWORD, "authorization_code": wrong,
                }).status_code, 403)
                self.assertEqual(client.post("/api/login", json=admin_login(wrong)).status_code, 403)
                self.assertEqual(client.post("/api/session/role", json={
                    "role": "admin", "authorization_code": wrong,
                }, headers=ordinary).status_code, 403)
                self.assertEqual(client.post("/api/login", json=admin_login()).status_code, 429)
                self.assertEqual(client.get("/api/me", headers=ordinary).status_code, 200)

    def test_prior_sessions_migrate_without_granting_admin(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            schema = Path(__file__).with_name("migrations") / "001_accounts.sql"
            database = root / "accounts.sqlite3"
            code_file = root / "admin.verifier"
            write_admin_code(code_file, ADMIN_CODE)
            legacy = "a" * 43
            db = sqlite3.connect(database)
            try:
                db.executescript(schema.read_text(encoding="utf-8") + "PRAGMA user_version = 1;")
                db.execute("INSERT INTO users VALUES (1, 'admin-test', 'admin-test', 'placeholder', 'admin', 'active', 1)")
                db.execute("INSERT INTO sessions VALUES (?, 1, 9999999999)", (token_hash(legacy),))
                db.commit()
            finally:
                db.close()
            accounts = Accounts(database, admin_code_file=code_file)
            accounts.migrate()
            accounts.migrate()
            profile = accounts.session(legacy)
            self.assertEqual((profile["role"], profile["assigned_role"]), ("user", "admin"))
            with self.assertRaises(AccountError) as denied:
                accounts.session(legacy, admin=True)
            self.assertEqual(denied.exception.status_code, 403)
            self.assertEqual(accounts.switch_role(legacy, "admin", ADMIN_CODE, "fixture")["user"]["role"], "admin")


class RevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_interrupts_stalled_stream(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            entered, closed = asyncio.Event(), asyncio.Event()

            async def upstream(_request):
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                      stream=PausedStream(entered, closed))

            _, controller, gateway = setup(Path(scratch), upstream)
            async with gateway.router.lifespan_context(gateway), httpx.AsyncClient(
                base_url="https://gateway.fixture", transport=httpx.ASGITransport(app=gateway)
            ) as client:
                result = await client.post("/api/register", json={"username": "user-test", "password": USER_PASSWORD})
                target = result.json()["user"]["id"]
                login = await client.post("/api/login", json=admin_login())
                admin = credentials(login.json()["token"])
                login = await client.post("/api/login", json={"username": "user-test", "password": USER_PASSWORD})
                user = credentials(login.json()["token"])
                status_url = f"/api/admin/users/{target}/status"
                await client.post(status_url, json={"status": "active"}, headers=admin)
                stream = asyncio.create_task(client.post("/v1/chat/completions", headers=user, json={
                    "messages": [{"role": "user", "content": "start"}], "stream": True,
                }))
                await asyncio.wait_for(entered.wait(), timeout=2)
                self.assertEqual(controller.in_flight, 1)
                disabled = await client.post(status_url, json={"status": "disabled"}, headers=admin)
                self.assertEqual(disabled.status_code, 200)
                response = await asyncio.wait_for(stream, timeout=3)
                self.assertIn("permission_error", response.text)
                self.assertNotIn("should not arrive", response.text)
                self.assertTrue(closed.is_set())
                self.assertEqual(controller.in_flight, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
