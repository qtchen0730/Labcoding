"""Full Gateway quota integration, using real accounts and a scripted llama transport."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx

from .test_gateway import setup, admin_login, credentials, USER_PASSWORD
from .test_support import TEST_ROOT


class GatewayQuotaTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="quota-api-")
        self.addCleanup(self.scratch.cleanup)
        self.calls = []
        self.response = "json"
        self.prompt_count = 100
        def upstream(request):
            self.calls.append(request)
            if request.url.path == "/apply-template":
                self.assertIn("messages", json.loads(request.content))
                return httpx.Response(200, json={"prompt":"fixture-rendered-with-system-history-tools"})
            if request.url.path == "/tokenize":
                body = json.loads(request.content)
                self.assertTrue(body["add_special"])
                self.assertTrue(body["parse_special"])
                return httpx.Response(200, json={"tokens":[1]*self.prompt_count})
            self.assertEqual(request.url.path,"/v1/chat/completions")
            usage = {"prompt_tokens":100,"completion_tokens":25,"total_tokens":125,
                     "prompt_tokens_details":{"cached_tokens":75}}
            if self.response == "error":
                return httpx.Response(400,json={"error":{"message":"invalid temperature"}})
            if self.response == "missing":
                return httpx.Response(200,json={"choices":[]})
            if self.response == "stream":
                return httpx.Response(200,headers={"Content-Type":"text/event-stream"},content=(
                    'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n'+
                    'data: '+json.dumps({"choices":[],"usage":usage})+'\n\n'+
                    'data: [DONE]\n\n').encode())
            if self.response == "partial-stream":
                return httpx.Response(200,headers={"Content-Type":"text/event-stream"},content=(
                    'data: '+json.dumps({"choices":[{"delta":{"content":"partial"},
                        "finish_reason":None}],"usage":usage})+'\n\n').encode())
            return httpx.Response(200,json={"choices":[],"usage":usage})
        self.accounts,self.model,app = setup(Path(self.scratch.name),upstream)
        self.client = self.enterContext(TestClient(app,base_url="https://gateway.fixture"))
        self.admin = credentials(self.client.post("/api/login",json=admin_login()).json()["token"])
        user = self.client.post("/api/register",json={"username":"quota-member","password":USER_PASSWORD}).json()["user"]
        self.user_id = user["id"]
        self.client.post(f"/api/admin/users/{self.user_id}/status",headers=self.admin,json={"status":"active"})
        self.user = credentials(self.client.post("/api/login",json={"username":user["username"],"password":USER_PASSWORD}).json()["token"])
        self.path = f"/api/admin/users/{self.user_id}/quota"
        self.payload = {"model":"lab-general","messages":[{"role":"system","content":"system fixture"},
                         {"role":"user","content":"tool history fixture"}],"max_tokens":100}

    def grant(self, count):
        result = self.client.post(self.path,headers=self.admin,json={"mode":"limited" if count is not None else "unlimited","limit_tokens":count})
        self.assertEqual(result.status_code,200,result.text)

    def quota(self):
        return self.client.get("/api/me",headers=self.user).json()["user"]["quota"]

    def test_admin_only_updates_and_validation(self):
        body={"mode":"limited","limit_tokens":1000}
        self.assertEqual(self.client.post(self.path,headers=self.user,json=body).status_code,403)
        self.assertEqual(self.client.post(self.path,json=body).status_code,401)
        for invalid in (-1,True,1.5,10**12+1):
            self.assertEqual(self.client.post(self.path,headers=self.admin,json=body|{"limit_tokens":invalid}).status_code,400)
        self.grant(1000)
        self.assertEqual(self.quota()["remaining_tokens"],1000)
        users=self.client.get("/api/admin/users",headers=self.admin).json()["users"]
        self.assertEqual(next(user for user in users if user["id"]==self.user_id)["quota"],self.quota())

    def test_multiple_requests_charge_actual_usage_and_refill(self):
        self.grant(1000)
        for remaining in (875,750,625):
            result=self.client.post("/v1/chat/completions",headers=self.user,json=self.payload)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(self.quota()["remaining_tokens"],remaining)
            self.assertEqual(self.quota()["held_tokens"],0)
        self.grant(2000)
        self.assertEqual(self.quota()["remaining_tokens"],2000)
        self.assertEqual(self.quota()["used_tokens"],0)

    def test_zero_blocks_direct_api_and_low_balance_reserves_output(self):
        self.grant(0)
        result=self.client.post("/v1/chat/completions",headers=self.user,json=self.payload)
        self.assertEqual(result.status_code,402)
        self.assertIn("用户额度已经用完",result.text)
        self.assertEqual(self.calls,[])
        self.grant(199)
        self.assertEqual(self.client.post("/v1/chat/completions",headers=self.user,json=self.payload).status_code,402)
        self.assertFalse(any(request.url.path=="/v1/chat/completions" for request in self.calls))
        self.assertEqual(self.model.in_flight,0)

    def test_stream_usage_settles_and_rejected_requests_release(self):
        self.grant(1000)
        self.response="stream"
        result=self.client.post("/v1/chat/completions",headers=self.user,json=self.payload|{"stream":True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn("[DONE]",result.text)
        self.assertEqual(self.quota()["remaining_tokens"],875)
        self.response="error"
        self.assertEqual(self.client.post("/v1/chat/completions",headers=self.user,json=self.payload).status_code,400)
        self.assertEqual(self.quota()["remaining_tokens"],875)
        self.assertEqual(self.model.in_flight,0)

    def test_missing_statistics_hold_and_unlimited_stays_compatible(self):
        self.grant(1000)
        self.response="missing"
        self.assertEqual(self.client.post("/v1/chat/completions",headers=self.user,json=self.payload).status_code,200)
        self.assertEqual(self.quota()["used_tokens"],0)
        self.assertTrue(self.quota()["unconfirmed"])
        self.assertEqual(self.client.post("/v1/chat/completions",headers=self.user,json=self.payload).status_code,409)
        self.grant(None)
        self.calls.clear()
        self.assertEqual(self.client.post("/v1/chat/completions",headers=self.user,json=self.payload).status_code,200)
        self.assertEqual(len(self.calls),1)

    def test_full_hold_and_partial_stream_use_pending_error_not_exhaustion(self):
        self.grant(200)
        self.response="partial-stream"
        result=self.client.post("/v1/chat/completions",headers=self.user,json=self.payload|{"stream":True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.quota()["held_tokens"],200)
        self.assertEqual(self.quota()["used_tokens"],0)
        self.assertTrue(self.quota()["unconfirmed"])
        self.calls.clear()
        retry=self.client.post("/v1/chat/completions",headers=self.user,json=self.payload)
        self.assertEqual(retry.status_code,409,retry.text)
        self.assertEqual(retry.json()["error"]["code"],"token_quota_unconfirmed")
        self.assertFalse(self.calls)


if __name__=="__main__":
    unittest.main(verbosity=2)
