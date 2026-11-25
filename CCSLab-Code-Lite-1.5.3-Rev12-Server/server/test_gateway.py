"""Account API, gateway authorization and proxy behavior tests."""

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
from .test_support import TEST_ROOT
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from .accounts import AccountError, Accounts, read_admin_code, token_hash, write_admin_code
from .gateway import Settings, create_app


class ControllerFixture:
    def __init__(self):
        self.in_flight = 0
        self.maintenance = False

    def status(self):
        return {"status": "running", "total_context": 131072, "slots": 5, "effective_context_limit": 26368,
                "context_sync": {"backend": {"effective_context_limit": 26368}}}

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


def setup(root, upstream, *, admin_code_file=None, bootstrap=True, opencode_config=None, managed_opencode_config=None):
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
                 admin_registration_code_file=admin_code_file, opencode_config=opencode_config,
                 managed_opencode_config=managed_opencode_config),
        model_control=controller,
        upstream_transport=httpx.MockTransport(upstream),
    )
    return accounts, controller, app


class AccountGatewayTests(unittest.TestCase):
    def test_context_enrollment_is_explicit_and_reaches_controller(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            key = root / "key"
            key.write_text("fixture-key", encoding="ascii")
            environment = {"CCSLAB_LLAMA_API_KEY_FILE": str(key), "CCSLAB_DATABASE": str(root / "accounts.db")}
            with patch.dict(os.environ, environment, clear=True):
                self.assertIsNone(Settings.from_environment().managed_opencode_config)
                os.environ["CCSLAB_MANAGED_OPENCODE_CONFIG"] = str(root / "opencode.json")
                settings = Settings.from_environment()
                self.assertEqual(settings.opencode_config, settings.managed_opencode_config)
                from . import gateway
                with patch.object(gateway, "ModelControl", wraps=gateway.ModelControl) as factory:
                    create_app(settings)
                    self.assertEqual(factory.call_args.kwargs["context_store"].path, root / "opencode.json")
                os.environ["CCSLAB_MANAGED_OPENCODE_CONFIG"] = "relative.json"
                with self.assertRaises(ValueError):
                    Settings.from_environment()

    def test_opencode_output_setting_read_write_retry_and_authorization(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as scratch:
            root = Path(scratch)
            config_path = root / ".config" / "opencode" / "opencode.json"
            _, _, gateway = setup(root, lambda _: None, managed_opencode_config=config_path,
                                  opencode_config=root / "must-not-write.json")
            with TestClient(gateway, base_url="https://gateway.fixture") as client:
                admin = credentials(client.post("/api/login", json=admin_login()).json()["token"])
                ordinary_result = client.post("/api/register", json={
                    "username": "output-user", "password": USER_PASSWORD,
                })
                self.assertEqual(ordinary_result.status_code, 201)
                user_id = ordinary_result.json()["user"]["id"]
                self.assertEqual(client.post(f"/api/admin/users/{user_id}/status", json={
                    "status": "active",
                }, headers=admin).status_code, 200)
                user = credentials(client.post("/api/login", json={
                    "username": "output-user", "password": USER_PASSWORD,
                }).json()["token"])
                self.assertEqual(client.put("/api/admin/opencode/output-limit", json={
                    "output_limit": 8192,
                }, headers=user).status_code, 403)
                self.assertEqual(client.put("/api/admin/opencode/output-limit", json={
                    "output_limit": 8192,
                }, headers=admin).status_code, 503)
                self.assertEqual(client.get("/api/opencode/output-limit", headers=user).status_code, 503)
                config_path.parent.mkdir(parents=True)
                original = {"model": "ccslab/lab-general", "other": {"keep": True}, "provider": {
                    "ccslab": {"models": {"lab-general": {"limit": {"context": 131072, "output": 4096}}}}
                }}
                config_path.write_text(json.dumps(original), encoding="utf-8")
                self.assertEqual(client.get("/api/opencode/output-limit", headers=user).json(),
                                 {"output_limit": 4096})
                for value in (8192, 4096):
                    result = client.put("/api/admin/opencode/output-limit", json={"output_limit": value},
                                        headers=admin)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.json(), {"output_limit": value})
                    written = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(written["provider"]["ccslab"]["models"]["lab-general"]
                                     ["limit"]["output"], value)
                    self.assertEqual(written["other"], original["other"])
                    self.assertEqual(client.get("/api/opencode/output-limit", headers=user).json(),
                                     {"output_limit": value})
                invalid = client.put("/api/admin/opencode/output-limit", json={"output_limit": True},
                                     headers=admin)
                self.assertEqual(invalid.status_code, 400)

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
                if request.url.path == "/apply-template":
                    return httpx.Response(200, json={"prompt": "fixture"})
                if request.url.path == "/tokenize":
                    return httpx.Response(200, json={"tokens": [1, 2, 3]})
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
                if _request.url.path == "/apply-template":
                    return httpx.Response(200, json={"prompt": "fixture"})
                if _request.url.path == "/tokenize":
                    return httpx.Response(200, json={"tokens": [1, 2, 3]})
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


class ModelApplyIntegrationTests(unittest.TestCase):
    """Real API/SQLite/policy/transaction files; mock fixed sudo boundary and model host.

    POSIX ownership and directory fsync require a separate Linux acceptance run.
    All dimensions and GPU budgets here are synthetic test fixtures.
    """

    def setUp(self):
        from . import model_control as m, model_host as host
        from .test_model_control import A, B, snapshot
        self.m, self.host, self.A, self.B = m, host, A, B
        temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temp.cleanup)
        root = self.root = Path(temp.name)
        paths = {"MODEL_SPEC": root / "policy.json", "CONTEXT_ENV": root / "context.env",
                 "ROLLBACK_PENDING": root / "pending",
                 "CONTROLLER": str(root / "controller")}
        for key, value in paths.items():
            self.enterContext(patch.object(m, key, value))
        self.enterContext(patch.object(host, "BACKUP", root / "previous"))
        Path(m.CONTROLLER).write_text("isolated helper boundary", encoding="ascii")
        m.MODEL_SPEC.write_text(json.dumps({
            "enabled": True, "gpu_index": 0, "llama_url": "http://127.0.0.1:8000",
            "model_id": "lab-general", "api_key_file": str(root / "key"),
        }), encoding="ascii")
        (root / "key").write_text("fixture-key", encoding="ascii")
        m.CONTEXT_ENV.write_bytes(host.settings(A.total_context, A.slots))

        def read_fixture(path):
            path = Path(path).resolve()
            self.assertTrue(path.is_relative_to(root.resolve()))
            return path.read_bytes()
        self.enterContext(patch.object(m, "_root_file", side_effect=read_fixture))
        if os.name == "nt":
            self.enterContext(patch.object(host, "_sync_directory"))
        self.profile, self.pid, self.bad_dimensions = A, 1000, False
        self.runtime_failure = ""
        self.explicit_override = False
        self.restarts, self.restart_modes = 0, []

        def telemetry(*_args, **_kwargs):
            result = snapshot(self.profile, used=27000 if self.profile == A else 21000,
                              train=getattr(self, "train_context", 131072), busy=getattr(self, "active_requests", 0))
            result["_pid"] = self.pid
            if self.bad_dimensions:
                result["_observed_slot_context"] = 1
            if self.runtime_failure == "health_timeout":
                result["status"] = "unavailable"
            elif self.runtime_failure == "wrong_slots":
                result["slots"] = A.slots
            if self.explicit_override:
                result["_managed_environment"] = {}
            return result
        self.enterContext(patch.object(m, "read_runtime", side_effect=telemetry))

        def systemctl(args, **_kwargs):
            self.assertEqual(args, [m.SYSTEMCTL, "restart", "--", m.MODEL_UNIT])
            self.restarts += 1
            mode = self.restart_modes.pop(0) if self.restart_modes else "ok"
            if mode == "restart_failed":
                raise subprocess.SubprocessError("isolated restart failure")
            values = m.context_env(m.CONTEXT_ENV.read_bytes())
            from .test_model_control import Parameters
            self.profile = { (p.total_context, p.slots): p for p in
                             (A, B, Parameters(163840, 1), Parameters(131072, 1)) }[
                values["total_context"], values["slots"]]
            self.pid += 1
            self.bad_dimensions = mode == "wrong_dimensions"
            self.runtime_failure = mode
            return ""
        self.enterContext(patch.object(m, "_fixed_command", side_effect=systemctl))
        self.clock = 0
        def tick():
            self.clock += 10
            return self.clock
        self.enterContext(patch.object(host, "monotonic", side_effect=tick))
        self.enterContext(patch.object(host, "sleep"))

        def sudo_run(args, **kwargs):
            self.assertEqual(args, ["/usr/bin/sudo", "-n", "--", m.CONTROLLER])
            self.assertEqual(kwargs["timeout"], 250)
            self.assertFalse(self.control.begin_inference())
            policy = m.Policy.read(m.MODEL_SPEC)
            payload = json.loads(kwargs["input"])
            self.assertEqual(set(payload), {"old_context", "old_slots", "new_context", "new_slots", "policy"})
            self.assertTrue(all(type(payload[key]) is int for key in
                                ("old_context", "old_slots", "new_context", "new_slots")))
            result = host.run_change(host.Host(policy, "fixture-key"), policy, payload)
            return subprocess.CompletedProcess(args, 0 if result["status"] == "applied" else 1,
                                               json.dumps(result), "")
        self.sudo = self.enterContext(patch.object(m.subprocess, "run", side_effect=sudo_run))
        accounts, _, _ = setup(root, lambda _request: None)
        self.control = m.ModelControl(accounts.database, "http://127.0.0.1:8000", UPSTREAM_KEY,
                                      config_path=m.MODEL_SPEC)
        app = create_app(Settings(accounts.database, self.control.base, "lab-general", UPSTREAM_KEY,
                                  admin_registration_code_file=root / "administrator.verifier"),
                         model_control=self.control, upstream_transport=httpx.MockTransport(lambda _: None))
        self.client = self.enterContext(TestClient(app, base_url="https://gateway.fixture"))
        reply = self.client.post("/api/login", json=admin_login())
        self.assertEqual(reply.status_code, 200)
        self.admin = credentials(reply.json()["token"])

    def preview(self):
        reply = self.client.post("/api/admin/model/preview", headers=self.admin,
                                 json={"total_context": self.B.total_context, "slots": self.B.slots})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertEqual(self.restarts, 0)
        return reply.json()["confirmation"]

    def apply(self, ticket):
        return self.client.post("/api/admin/model/apply", headers=self.admin, json={"confirmation": ticket})

    def test_apply_reads_environment_and_reports_verified_dimensions(self):
        user = self.client.post("/api/register", json={"username": "quota-fixture", "password": USER_PASSWORD})
        self.assertEqual(user.status_code, 201)
        identity = user.json()["user"]["id"]
        quota = self.client.post(f"/api/admin/users/{identity}/quota", headers=self.admin,
                                 json={"mode": "limited", "limit_tokens": 50000})
        self.assertEqual(quota.status_code, 200)
        ticket = self.preview()
        tables = ("users", "sessions", "token_quotas", "token_requests", "token_quota_audit")
        with closing(sqlite3.connect(self.control.db)) as db:
            unchanged = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        reply = self.apply(ticket)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["status"], "applied")
        live = self.client.get("/api/model", headers=self.admin).json()
        self.assertEqual((live["total_context"], live["slots"]), (65536, 2))
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.B.total_context, self.B.slots))
        self.assertEqual(self.host.BACKUP.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.apply(ticket).status_code, 409)
        self.assertEqual(self.client.get("/api/me", headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get("/v1/models", headers=self.admin).json()["data"][0]["id"], "lab-general")
        with closing(sqlite3.connect(self.control.db)) as db:
            for table in tables:
                self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), unchanged[table], table)
            self.assertEqual(db.execute("SELECT result FROM model_configuration_audit ORDER BY id").fetchall(),
                             [("requested",), ("applied",)])

    def test_context_sync_api_partial_retry_status_gpu_and_auth(self):
        from . import output_settings
        config = self.root / "opencode.json"
        config.write_text(json.dumps({"keep": "unchanged", "provider": {"ccslab": {"models": {
            "lab-general": {"limit": {"context": 24000, "input": 22000, "output": 4096}}}}}}), encoding="utf-8")
        self.control.context_store = output_settings.OpenCodeContext(config)
        original = config.read_bytes()
        self.assertEqual(self.client.post("/api/admin/model/preview", json={
            "total_context": 65536, "slots": 2}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/model/preview", headers=self.admin, json={
            "total_context": 65536, "slots": 2, "path": str(config)}).status_code, 400)
        token = self.preview()
        with patch.object(output_settings, "_write_config", side_effect=PermissionError()):
            reply = self.apply(token)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["status"], "partially_applied")
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(self.restarts, 1)
        retry = self.client.post("/api/admin/model/preview", headers=self.admin,
                                 json={"total_context": 65536, "slots": 2}).json()
        self.assertFalse(retry["restart_required"])
        self.assertEqual(self.apply(retry["confirmation"]).json()["status"], "applied")
        self.assertEqual(self.restarts, 1)
        live = self.client.get("/api/model", headers=self.admin).json()
        self.assertEqual(live["gpu"]["used_mib"], 21000)
        sync = live["context_sync"]
        self.assertEqual(sync["saved_configuration"], {"total_context": 65536, "slots": 2})
        self.assertEqual(sync["backend"]["status"], "synced")
        self.assertEqual(sync["builtin"]["context_limit"], 32768)
        self.assertEqual(sync["opencode"]["configured_context_limit"], 32768)
        self.assertIsNone(sync["opencode"]["effective_context_limit"])
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["keep"], "unchanged")
        self.assertEqual(saved["provider"]["ccslab"]["models"]["lab-general"]["limit"],
                         {"context": 32768, "input": 22000, "output": 4096})

    def setup_decrease(self):
        from .test_model_control import Parameters
        from .output_settings import OpenCodeContext
        self.A, self.B = Parameters(163840, 1), Parameters(131072, 1)
        self.profile, self.train_context = self.A, 196608
        self.control.base = "http://172.17.0.1:8000"
        self.m.CONTEXT_ENV.write_bytes(self.host.settings(163840, 1))
        self.config = self.root / "opencode.json"
        self.config.write_text(json.dumps({"keep": True, "provider": {"ccslab": {"options": {"apiKey": "fixture"},
            "models": {"lab-general": {"limit": {"context": 163840, "output": 8192}}}}}}), encoding="utf-8")
        self.config.chmod(0o600)
        self.control.context_store = OpenCodeContext(self.config)
        self.enterContext(patch.object(self.m, "MANAGED_OPENCODE", self.config))
        self.enterContext(patch.object(self.m, "MODEL_MAINTENANCE", self.root / "maintenance"))
        policy = json.loads(self.m.MODEL_SPEC.read_text())
        policy.update(llama_url=self.control.base, managed_opencode_config=str(self.config))
        self.m.MODEL_SPEC.write_text(json.dumps(policy))
        self.opencode_running = False
        self.guard_failure = False
        self.release_failures = 0
        self.maintenance_events = []
        owner = self

        class Guard:
            def start(self):
                owner.m.MODEL_MAINTENANCE.touch()
                owner.maintenance_events.append("fenced")
                owner.assertFalse(owner.control.begin_inference())
                if getattr(owner, "busy_after_fence", False): owner.active_requests = 1
                if owner.guard_failure:
                    raise owner.host.MaintenanceError("direct_guard_failed")
            def stop(self):
                if owner.release_failures:
                    owner.release_failures -= 1
                    raise owner.host.MaintenanceError("direct_guard_release_failed")
                owner.maintenance_events.append("unfenced")
                owner.m.MODEL_MAINTENANCE.unlink(missing_ok=True)

        def closed(_host):
            if self.opencode_running:
                raise self.host.MaintenanceError("independent_running")
        def managed(_host, context=None, allowed=None):
            store = self.control.context_store
            _, limits = store._read()
            if context is not None:
                self.assertTrue(self.m.MODEL_MAINTENANCE.exists(), "file update must remain fenced")
                if limits["context"] not in allowed:
                    raise self.host.MaintenanceError("managed_config_failed")
                try:
                    store.sync(context)
                except (OSError, ValueError) as error:
                    raise self.host.MaintenanceError("managed_config_failed") from error
                self.maintenance_events.append(("file", context))
            return {"context": context or limits["context"], "output": limits["output"]}
        self.enterContext(patch.object(self.host, "DirectRequestGuard", Guard))
        self.enterContext(patch.object(self.host.Host, "check_closed", closed))
        self.enterContext(patch.object(self.host.Host, "managed_config", managed))

    def test_160_to_128_to_160_and_same_target_sync_preserves_shared_data(self):
        self.setup_decrease()
        with closing(sqlite3.connect(self.control.db)) as db:
            before = {n: db.execute("SELECT * FROM " + n).fetchall() for n in
                      ("users", "sessions", "token_quotas", "token_requests")}
        ticket = self.preview()
        reply = self.apply(ticket)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["model"]["effective_context_limit"], 131072)
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.maintenance_events, ["fenced", ("file", 131072), "unfenced"])
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())
        for context in (131072, 163840, 163840):
            expected_restarts = self.restarts + (context != self.profile.total_context)
            preview = self.client.post("/api/admin/model/preview", headers=self.admin,
                                      json={"total_context": context, "slots": 1}).json()
            reply = self.apply(preview["confirmation"])
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(self.restarts, expected_restarts)
            limits = json.loads(self.config.read_text())["provider"]["ccslab"]["models"]["lab-general"]["limit"]
            self.assertEqual(limits, {"context": context, "output": 8192})
        self.assertEqual(self.restarts, 2)
        with closing(sqlite3.connect(self.control.db)) as db:
            for name, rows in before.items():
                self.assertEqual(db.execute("SELECT * FROM " + name).fetchall(), rows)

    def test_increase_uses_fence_and_keeps_existing_partial_sync_retry(self):
        from . import output_settings
        self.setup_decrease()
        self.profile = self.B
        self.m.CONTEXT_ENV.write_bytes(self.host.settings(131072, 1))
        self.control.context_store.sync(131072)
        self.opencode_running = True  # the smaller running budget is safe on increase
        preview = self.client.post("/api/admin/model/preview", headers=self.admin,
                                  json={"total_context": 163840, "slots": 1}).json()
        with patch.object(output_settings, "_write_config", side_effect=PermissionError()):
            reply = self.apply(preview["confirmation"])
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["status"], "partially_applied")
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.maintenance_events, ["fenced", "unfenced"])
        preview = self.client.post("/api/admin/model/preview", headers=self.admin,
                                  json={"total_context": 163840, "slots": 1}).json()
        self.assertEqual(self.apply(preview["confirmation"]).json()["status"], "applied")
        self.assertEqual(self.restarts, 1)
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())

    def test_decrease_requires_normal_exit_and_idle_task_leases(self):
        self.setup_decrease()
        ticket = self.preview()
        key = "c" * 32
        self.assertTrue(self.client.post(f"/api/tasks/{key}/acquire", headers=self.admin).json()["acquired"])
        self.assertEqual(self.apply(ticket).status_code, 409)
        self.client.delete(f"/api/tasks/{key}", headers=self.admin)
        self.opencode_running = True
        reply = self.apply(ticket)
        self.assertEqual(reply.status_code, 503)
        self.assertIn("正常退出", reply.json()["detail"])
        self.assertEqual(self.restarts, 0)
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())

    def test_decrease_mismatched_file_can_be_repaired_without_restarting_model(self):
        from .output_settings import OpenCodeContext
        self.setup_decrease()
        OpenCodeContext(self.config).sync(131072)
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("重新同步", reply.json()["detail"])
        self.assertEqual(self.restarts, 0)
        same = self.client.post("/api/admin/model/preview", headers=self.admin,
                               json={"total_context": 163840, "slots": 1}).json()
        self.assertEqual(self.apply(same["confirmation"]).status_code, 200)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.apply(self.preview()).status_code, 200)
        self.assertEqual(self.restarts, 1)

    def test_decrease_guard_failure_never_changes_backend(self):
        self.setup_decrease()
        self.guard_failure = True
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("nftables", reply.json()["detail"])
        self.assertEqual(self.restarts, 0)
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())

    def test_decrease_file_failure_restores_and_verifies_160k(self):
        from . import output_settings
        self.setup_decrease()
        original = self.config.read_bytes()
        with patch.object(output_settings, "_write_config", side_effect=PermissionError()):
            reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("恢复", reply.json()["detail"])
        self.assertEqual(self.profile.total_context, 163840)
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())

    def test_decrease_backend_failure_and_restore_failure_keep_truthful_state(self):
        self.setup_decrease()
        self.restart_modes = ["wrong_dimensions", "restart_failed"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("恢复校验也未通过", reply.json()["detail"])
        self.assertTrue(self.m.ROLLBACK_PENDING.exists())
        self.assertTrue(self.m.MODEL_MAINTENANCE.exists())
        self.assertFalse(self.control.begin_inference())

    def test_decrease_rechecks_actual_requests_after_fencing(self):
        self.setup_decrease()
        self.busy_after_fence = True
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("实际请求", reply.json()["detail"])
        self.assertEqual(self.restarts, 0)
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())

    def test_decrease_startup_failure_restores_backend_and_file(self):
        self.setup_decrease()
        self.restart_modes = ["restart_failed", "ok"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("恢复并验证", reply.json()["detail"])
        self.assertEqual(self.profile.total_context, 163840)
        self.assertEqual(self.restarts, 2)
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())

    def test_decrease_file_restore_failure_still_restores_backend_and_keeps_fence(self):
        self.setup_decrease()
        self.release_failures = 1
        with patch.object(self.host.Host, "restore_decrease", side_effect=self.host.MaintenanceError("managed_config_permission")):
            reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.profile.total_context, 163840)
        self.assertEqual(self.restarts, 2)
        self.assertTrue(self.m.ROLLBACK_PENDING.exists())
        self.assertTrue(self.m.MODEL_MAINTENANCE.exists())
        self.assertFalse(self.control.begin_inference())

    def test_decrease_guard_release_failure_rolls_back_before_reopening(self):
        self.setup_decrease()
        self.release_failures = 1
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.profile.total_context, 163840)
        self.assertEqual(self.restarts, 2)
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.m.MODEL_MAINTENANCE.exists())

    def test_context_sync_does_not_bypass_task_lease(self):
        ticket = self.preview()
        key = "f" * 32
        self.assertTrue(self.client.post(f"/api/tasks/{key}/acquire", headers=self.admin).json()["acquired"])
        self.assertEqual(self.apply(ticket).status_code, 409)
        self.assertEqual(self.restarts, 0)
        self.client.delete(f"/api/tasks/{key}", headers=self.admin)
        self.assertEqual(self.apply(ticket).status_code, 200)

    def test_failed_dimensions_restore_original_file(self):
        # Each test instance starts from A; rollback must verify a new PID too.
        self.restart_modes = ["wrong_dimensions", "ok"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertIn("恢复", reply.json()["detail"])
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.restarts, 2)

    def test_restart_error_rolls_back_without_success_response(self):
        self.restart_modes = ["restart_failed", "ok"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())

    def test_unverified_recovery_preserves_pending_and_blocks_requests(self):
        self.restart_modes = ["wrong_dimensions", "wrong_dimensions"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertTrue(self.m.ROLLBACK_PENDING.exists())
        self.assertFalse(self.control.begin_inference())
        self.assertEqual(self.apply("expired-or-invalid").status_code, 409)
        with closing(sqlite3.connect(self.control.db)) as db:
            self.assertEqual(db.execute("SELECT result FROM model_configuration_audit ORDER BY id DESC LIMIT 1").fetchone()[0], "rollback_failed")

    def test_explicit_launch_flags_are_refused_before_writing(self):
        self.explicit_override = True
        reply = self.client.post("/api/admin/model/preview", headers=self.admin,
                                 json={"total_context": self.B.total_context, "slots": self.B.slots})
        self.assertEqual(reply.status_code, 409)
        self.sudo.assert_not_called()
        self.assertEqual(self.restarts, 0)
        self.assertFalse(self.host.BACKUP.exists())
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))

    def test_unavailable_runtime_prevents_privileged_invocation(self):
        ticket = self.preview()
        self.runtime_failure = "health_timeout"
        self.assertEqual(self.apply(ticket).status_code, 409)
        self.sudo.assert_not_called()
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))

    def audit_tail(self):
        with closing(sqlite3.connect(self.control.db)) as db:
            return db.execute("SELECT result, reason FROM model_configuration_audit ORDER BY id DESC LIMIT 1").fetchone()

    def test_configuration_write_failure_restores_original_configuration(self):
        original = self.host.atomic_write
        def failing_write(path, contents, mode=0o644):
            if path == self.m.CONTEXT_ENV and contents == self.host.settings(self.B.total_context, self.B.slots):
                raise PermissionError("isolated configuration write denied")
            return original(path, contents, mode)
        with patch.object(self.host, "atomic_write", side_effect=failing_write):
            reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.audit_tail()[0], "rolled_back")

    def test_backup_write_failure_refuses_before_changing_configuration(self):
        original = self.host.atomic_write
        def failing_write(path, contents, mode=0o644):
            if path == self.host.BACKUP:
                raise PermissionError("isolated backup denied")
            return original(path, contents, mode)
        with patch.object(self.host, "atomic_write", side_effect=failing_write):
            reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.audit_tail()[0], "refused")

    def test_helper_permission_failure_is_audited_without_success(self):
        self.sudo.side_effect = PermissionError("isolated sudo authorization denied")
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertEqual(self.audit_tail(), ("unknown", "unknown"))

    def test_health_check_timeout_restores_original_configuration(self):
        self.restart_modes = ["health_timeout", "ok"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.audit_tail(), ("rolled_back", "health_or_dimensions_failed"))

    def test_actual_slots_mismatch_restores_original_configuration(self):
        self.restart_modes = ["wrong_slots", "ok"]
        reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertFalse(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.audit_tail(), ("rolled_back", "health_or_dimensions_failed"))

    def test_rollback_write_failure_retains_pending_and_blocks_inference(self):
        original = self.host.atomic_write
        def failing_write(path, contents, mode=0o644):
            if path == self.m.CONTEXT_ENV and contents == self.host.settings(self.A.total_context, self.A.slots):
                raise PermissionError("isolated rollback write denied")
            return original(path, contents, mode)
        self.restart_modes = ["wrong_dimensions"]
        with patch.object(self.host, "atomic_write", side_effect=failing_write):
            reply = self.apply(self.preview())
        self.assertEqual(reply.status_code, 503)
        self.assertTrue(self.m.ROLLBACK_PENDING.exists())
        self.assertEqual(self.host.BACKUP.read_bytes(), self.host.settings(self.A.total_context, self.A.slots))
        self.assertEqual(self.m.CONTEXT_ENV.read_bytes(), self.host.settings(self.B.total_context, self.B.slots))
        self.assertFalse(self.control.begin_inference())
        self.assertEqual(self.audit_tail()[0], "rollback_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
