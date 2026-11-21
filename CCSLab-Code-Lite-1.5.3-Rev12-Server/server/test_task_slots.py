"""Global admission, ownership and expiry with isolated accounts and model streams."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx

from .account_errors import AccountError
from .task_slots import TaskSlots
from .test_gateway import setup, credentials, admin_login, USER_PASSWORD, ControllerFixture
from .test_support import TEST_ROOT


class SlotTests(unittest.TestCase):
    def test_parallel_title_and_chat_requests_wait_inside_one_task_slot(self):
        async def exercise(root):
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def upstream(request):
                calls.append(request)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={"choices": []})
            accounts, model, app = setup(root, upstream)
            from .test_gateway import ADMIN_PASSWORD
            token = accounts.login("admin-test", ADMIN_PASSWORD, "fixture")["token"]
            headers, key = credentials(token), uuid4().hex
            payload = {"model": "lab-general", "messages": [{"role": "user", "content": "fixture"}]}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://gateway.fixture") as client:
                    self.assertTrue((await client.post(f"/api/tasks/{key}/acquire", headers=headers)).json()["acquired"])
                    headers = dict(headers, **{"X-CCSLab-Task": key})
                    first = asyncio.create_task(client.post("/v1/chat/completions", json=payload, headers=headers))
                    await asyncio.wait_for(entered.wait(), 3)
                    second = asyncio.create_task(client.post("/v1/chat/completions", json=payload, headers=headers))
                    try:
                        await asyncio.sleep(.2)
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(model.in_flight, 1)
                    finally:
                        release.set()
                    replies = await asyncio.wait_for(asyncio.gather(first, second), 3)
                    self.assertEqual([reply.status_code for reply in replies], [200, 200])
                    self.assertEqual(len(calls), 2)
                    await client.delete(f"/api/tasks/{key}", headers=headers)
                    self.assertEqual(model.in_flight, 0)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            asyncio.run(exercise(Path(directory)))

    def test_atomic_admission_across_users_and_completion_reuse(self):
        model = ControllerFixture()
        slots = TaskSlots(model)
        def enter(user):
            return slots.acquire(str(user), user, str(user).ljust(43, "_"), 4)
        with ThreadPoolExecutor(16) as pool:
            admitted = list(pool.map(enter, range(20)))
        self.assertEqual(sum(admitted), 4)
        self.assertEqual(model.in_flight, 4)
        user = admitted.index(True)
        slots.release(str(user), user, str(user).ljust(43, "_"))
        self.assertTrue(slots.acquire("replacement", 100, "new".ljust(43, "_"), 4))
        self.assertEqual(slots.snapshot()["available_slots"], 0)

    def test_heartbeat_ownership_expiry_and_running_request_drain(self):
        clock = [0]
        model = ControllerFixture()
        slots = TaskSlots(model, lambda: clock[0])
        self.assertTrue(slots.acquire("a", 1, "one".ljust(43, "_"), 1))
        with self.assertRaises(AccountError):
            slots.heartbeat("a", 2, "two".ljust(43, "_"))
        self.assertTrue(slots.begin_request("a", 1, "one".ljust(43, "_")))
        self.assertFalse(slots.begin_request("a", 1, "one".ljust(43, "_")))
        clock[0] = 100
        self.assertFalse(slots.acquire("b", 2, "two".ljust(43, "_"), 1))
        slots.end_request("a")
        self.assertEqual(model.in_flight, 0)
        self.assertTrue(slots.acquire("b", 2, "two".ljust(43, "_"), 1))
        clock[0] = 120
        slots.heartbeat("b", 2, "two".ljust(43, "_"))
        clock[0] = 150
        self.assertEqual(slots.snapshot(2)["user_running_tasks"], 1)
        slots.release("b", 2, "two".ljust(43, "_"))
        slots.release("b", 2, "two".ljust(43, "_"))
        self.assertEqual(model.in_flight, 0)

    def test_release_does_not_reuse_slot_until_stream_closed(self):
        slots = TaskSlots(ControllerFixture())
        slots.acquire("a", 1, "token".ljust(43, "_"), 1)
        slots.begin_request("a", 1, "token".ljust(43, "_"))
        slots.release("a", 1, "token".ljust(43, "_"))
        self.assertFalse(slots.acquire("b", 2, "other".ljust(43, "_"), 1))
        slots.end_request("a")
        self.assertTrue(slots.acquire("b", 2, "other".ljust(43, "_"), 1))

    def test_http_two_users_statistics_legacy_requests_and_task_headers(self):
        seen = []
        def upstream(request):
            seen.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "fixture"}}]})
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            accounts, model, app = setup(Path(directory), upstream)
            with TestClient(app, base_url="https://gateway.fixture") as client:
                admin = client.post("/api/login", json=admin_login()).json()["token"]
                users = []
                for i in range(2):
                    name = f"slot-user-{i}"
                    user = accounts.create(name, USER_PASSWORD, bootstrap=False)
                    accounts.change_status(admin, user["id"], "active")
                    token = accounts.login(name, USER_PASSWORD, "fixture")["token"]
                    users.append((user["id"], credentials(token)))
                keys = [uuid4().hex for _ in range(6)]
                for i, key in enumerate(keys):
                    reply = client.post(f"/api/tasks/{key}/acquire", headers=users[i % 2][1])
                    self.assertEqual(reply.status_code, 200, reply.text)
                    self.assertEqual(reply.json()["acquired"], i < 5)
                first = client.get("/api/tasks", headers=users[0][1]).json()
                second = client.get("/api/tasks", headers=users[1][1]).json()
                self.assertEqual((first["running_tasks"], first["available_slots"], first["user_running_tasks"]), (5, 0, 3))
                self.assertEqual(second["user_running_tasks"], 2)
                self.assertNotIn("users", first)
                listed = client.get("/api/admin/users", headers=credentials(admin)).json()["users"]
                self.assertEqual({u["id"]: u["running_tasks"] for u in listed}[users[0][0]], 3)
                payload = {"model": "lab-general", "messages": [{"role": "user", "content": "fixture"}]}
                # Old clients cannot bypass leases by omitting the task header.
                self.assertEqual(client.post("/v1/chat/completions", json=payload, headers=users[1][1]).status_code, 429)
                own = dict(users[0][1], **{"X-CCSLab-Task": keys[0]})
                other = dict(users[1][1], **{"X-CCSLab-Task": keys[0]})
                self.assertEqual(client.post("/v1/chat/completions", json=payload, headers=other).status_code, 409)
                self.assertEqual(client.post("/v1/chat/completions", json=payload, headers=own).status_code, 200)
                self.assertEqual(len(seen), 1)
                self.assertNotIn("X-CCSLab-Task", seen[0].headers)
                self.assertEqual(model.in_flight, 5)  # Agent retains its slot between model calls/approvals.
                self.assertEqual(client.delete(f"/api/tasks/{keys[0]}", headers=users[0][1]).status_code, 204)
                self.assertTrue(client.post(f"/api/tasks/{keys[5]}/acquire", headers=users[1][1]).json()["acquired"])
                self.assertEqual(client.get("/api/model", headers=users[1][1]).json()["available_slots"], 0)
