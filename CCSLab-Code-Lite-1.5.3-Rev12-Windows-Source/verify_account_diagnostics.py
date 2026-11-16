"""Exercise the account diagnostics state machine with deterministic read-only probes."""

import platform
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from account_client import AccountError
from account_diagnostics import run_diagnostics


SECRET = "fixture-account-session-token"
PROXY_SECRET = "fixture-proxy-password"
PASSWORD = "fixture-server-password"
KEY = "fixture-upstream-key"


def issue(*, category="response", status=0, stage="health", api_error=False,
          route="direct", technical=""):
    error = AccountError(f"untrusted server error password={PASSWORD} X-API-Key: {KEY} Bearer {SECRET}", status)
    error.category = category
    error.stage = stage
    error.api_error = api_error
    error.route = route
    error.technical = technical or f"https://proxy:{PROXY_SECRET}@example.test:8080"
    return error


class ClientFixture:
    base = "https://gateway.fixture.test"

    def __init__(self, *, token="", account="active", route="direct", fallback=False,
                 model=None, health_error=None, account_error=None, session_error=None, model_error=None):
        self.token = token
        self.mode = "auto"
        self.route = route
        self.fallback = fallback
        self.account = account
        self.health_error = health_error
        self.account_error = account_error
        self.session_error = session_error
        self.model_error = model_error
        self.telemetry = model if model is not None else {
            "model_name": "Qwen3.6-27B Q4_K_M",
            "model_id": "lab-general",
            "status": "running",
            "total_context": 131072,
            "slots": 5,
            "effective_context_limit": 26368,
            "gpu": {"used_mib": 15360, "total_mib": 32768},
        }
        self.requests = []

    def health(self, *, observe):
        self.requests.append(("GET", "/health", False))
        observe("route", "warning" if self.fallback else "normal", self.route)
        network_stages = ("dns", "tcp", "tls")
        for stage in network_stages:
            if self.health_error and self.health_error.stage == stage:
                raise self.health_error
            observe(stage, "normal", f"must not be copied {PROXY_SECRET}")
        if self.health_error:
            raise self.health_error
        return {"ok": True, "secret": KEY}

    def request(self, method, path, *, auth):
        self.requests.append((method, path, auth))
        assert method == "GET" and path == "/api/me" and auth is False
        if self.account_error:
            raise self.account_error
        raise issue(category="unauthorized", status=401, stage="http", api_error=True)

    def me(self):
        self.requests.append(("GET", "/api/me", True))
        if self.session_error:
            raise self.session_error
        return {"username": "fixture-user-name", "role": "user", "status": self.account}

    def model(self):
        self.requests.append(("GET", "/api/model", True))
        if self.model_error:
            raise self.model_error
        return self.telemetry


def pick(report, stage):
    return next(row for row in report.rows if row.stage == stage)


class TestAccountDiagnostics(unittest.TestCase):
    def assert_sanitized(self, report):
        rendered = report.to_text()
        for secret in (SECRET, PROXY_SECRET, PASSWORD, KEY, "fixture-user-name", "X-API-Key:"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, repr(report))

    def test_order_and_active_model_diagnostics(self):
        client = ClientFixture(token=SECRET, route="system")
        report = run_diagnostics(client)
        self.assertTrue(report.healthy)
        self.assertEqual(report.version, "1.5.3")
        self.assertIn(platform.system(), report.to_text())
        self.assertEqual([row.stage for row in report.rows], [
            "address", "dns", "tcp", "tls", "health", "account", "auth", "model",
        ])
        self.assertTrue(all(row.state == "normal" for row in report.rows), report.to_text())
        self.assertIn("Windows 系统 DNS 已解析代理主机", pick(report, "dns").detail)
        self.assertIn("系统代理", report.summary)
        self.assertIn("26,368 Token", report.to_text())
        self.assertIn("15,360 / 32,768 MiB", report.to_text())
        self.assertEqual(client.requests, [
            ("GET", "/health", False), ("GET", "/api/me", False),
            ("GET", "/api/me", True), ("GET", "/api/model", True),
        ])
        self.assert_sanitized(report)

    def test_quick_startup_and_fallback(self):
        client = ClientFixture(token=SECRET, fallback=True)
        report = run_diagnostics(client, full=False)
        self.assertEqual(client.requests, [("GET", "/health", False)])
        self.assertEqual(pick(report, "account").state, "unchecked")
        self.assertEqual(pick(report, "model").state, "unchecked")
        self.assertIn("回退到直连", report.summary)
        self.assertIn("回退到直连", report.to_text())

    def test_pending_disabled_rejected_expired_permission_and_logged_out(self):
        samples = (
            (ClientFixture(), "未登录", "warning"),
            (ClientFixture(token=SECRET, account="pending"), "待审批", "warning"),
            (ClientFixture(token=SECRET, account="disabled"), "已禁用", "warning"),
            (ClientFixture(token=SECRET, account="rejected"), "已拒绝", "warning"),
            (ClientFixture(token=SECRET, session_error=issue(status=401, api_error=True)), "已过期", "failure"),
            (ClientFixture(token=SECRET, session_error=issue(status=403, api_error=True)), "没有访问权限", "failure"),
        )
        for client, expected, state in samples:
            with self.subTest(auth=expected):
                report = run_diagnostics(client)
                self.assertEqual(pick(report, "auth").state, state)
                self.assertIn(expected, pick(report, "auth").detail)
                self.assertEqual(pick(report, "model").state, "na")
                self.assertNotIn(("GET", "/api/model", True), client.requests)
                self.assert_sanitized(report)

    def test_api_waf_rejection_is_not_a_valid_account_endpoint(self):
        client = ClientFixture(token=SECRET, account_error=issue(status=401, api_error=False))
        report = run_diagnostics(client)
        self.assertTrue(report.healthy)
        self.assertEqual(pick(report, "account").state, "failure")
        self.assertEqual(report.error, "http_401")
        self.assertEqual(client.requests, [("GET", "/health", False), ("GET", "/api/me", False)])

    def test_transport_failures_are_reported_at_the_correct_layer(self):
        samples = (
            ("dns", "dns", "系统 DNS"),
            ("tcp", "tcp", "TCP"),
            ("tcp", "proxy", "网络代理"),
            ("tls", "certificate", "域名与服务器地址不匹配"),
            ("tls", "tls", "HTTPS 握手"),
            ("health", "timeout", "超时"),
        )
        for stage, category, expected in samples:
            with self.subTest(stage=stage, category=category):
                failure = issue(stage=stage, category=category, technical=f"verify_code=62 {PASSWORD}")
                report = run_diagnostics(ClientFixture(health_error=failure))
                self.assertFalse(report.healthy)
                self.assertEqual(pick(report, stage).state, "failure")
                self.assertIn(expected, report.connection_error)
                self.assertEqual(report.error, category)
                self.assert_sanitized(report)

    def test_http_health_failure_stops_before_account_probes(self):
        for status in (302, 401, 403, 404, 405, 429, 502, 503, 525, 526, 530):
            with self.subTest(status=status):
                client = ClientFixture(health_error=issue(status=status))
                report = run_diagnostics(client)
                self.assertEqual(report.error, f"http_{status}")
                self.assertIn(f"HTTP {status}", report.connection_error)
                self.assertEqual(pick(report, "tls").state, "normal")
                self.assertEqual(client.requests, [("GET", "/health", False)])
                self.assert_sanitized(report)

    def test_report_sanitizes_invalid_urls_server_payloads_and_error_details(self):
        bad_url = f"https://user:{PASSWORD}@gateway.fixture.test/path?token={SECRET}"
        invalid = run_diagnostics(server=bad_url)
        self.assertEqual(pick(invalid, "address").state, "failure")
        self.assert_sanitized(invalid)
        client = ClientFixture(token=SECRET, model={
            "model_name": f"Bearer {SECRET}",
            "model_id": f"https://user:{PROXY_SECRET}@proxy.fixture.test",
            "status": f"password={PASSWORD}",
            "effective_context_limit": KEY,
            "gpu": {"total_mib": SECRET, "used_mib": 2},
            "raw": KEY,
        })
        report = run_diagnostics(client)
        self.assertEqual(pick(report, "model").state, "warning")
        self.assertIn("暂不可用", pick(report, "model").detail)
        self.assert_sanitized(report)
        denied = run_diagnostics(ClientFixture(token=SECRET, model_error=issue(status=403)))
        self.assertEqual(pick(denied, "model").state, "na")
        self.assert_sanitized(denied)


if __name__ == "__main__":
    unittest.main(verbosity=2)
