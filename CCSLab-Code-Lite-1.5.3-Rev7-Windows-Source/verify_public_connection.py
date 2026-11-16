"""Unit-level request handling and session safety for public inference."""

from http.client import HTTPException
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from direct_connection import BASE_URL, MODEL_ID, DirectConnection, DirectError, normalize_base_url


TOKEN = "test-session-never-print-this"


class PublicConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="direct-probe-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.connection = DirectConnection(self.path)

    def fake_response(self, http, status=200, data=b"", retry_after=""):
        connection = http.return_value
        reply = connection.getresponse.return_value
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        reply.status = status
        reply.getheader.return_value = retry_after
        reply.read.return_value = data
        return connection, reply

    @patch("direct_connection.HTTPSConnection")
    def test_generation_probe(self, http):
        data = {"model": MODEL_ID, "choices": [{"message": {"role": "assistant", "content": "Ready."}}]}
        transport, _reply = self.fake_response(http, data=json.dumps(data).encode())
        metadata = self.connection.probe()
        self.assertEqual(metadata, {"model_name": MODEL_ID, "model_id": MODEL_ID, "status": "running"})
        self.assertEqual(self.connection.model_base, BASE_URL)
        self.assertEqual(http.call_args.args[:2], ("ccslab-llm.qtchen.cn", 443))
        (method, path, body, headers), _kwargs = transport.request.call_args
        self.assertEqual((method, path), ("POST", "/api/chat/completions"))
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("CF-Access-Client-Secret", headers)
        record = json.loads(body)
        self.assertEqual(record["model"], MODEL_ID)
        self.assertFalse(record["stream"])
        self.assertLessEqual(record["max_tokens"], 256)
        transport.close.assert_called_once()

    def test_custom_endpoint_normalization(self):
        cases = {
            " https://Example.test/ ": "https://example.test/v1",
            "HTTPS://Example.test:443/v1/": "https://example.test/v1",
            "https://example.test:8443/Proxy/openai/v1/": "https://example.test:8443/Proxy/openai/v1",
            "https://Example.test/api/v1/chat/completions": "https://example.test/api/v1",
            "https://[2001:0db8::1]:9443/v1": "https://[2001:db8::1]:9443/v1",
            "https://模型.example.test/v1": "https://xn--xgs754b.example.test/v1",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(normalize_base_url(address), expected)
                self.assertEqual(normalize_base_url(expected), expected)

    @patch("direct_connection.HTTPSConnection")
    def test_custom_base_route_and_standard_request(self, http):
        data = {"model": MODEL_ID, "choices": [{"message": {"role": "assistant", "content": "Ready."}}]}
        transport, _reply = self.fake_response(http, data=json.dumps(data).encode())
        alternate = self.connection.for_address("https://API.Example.test:8443/gateway/openai/v1/")
        alternate.probe(TOKEN)
        self.assertIs(http.call_args.kwargs["context"], self.connection._tls)
        self.assertEqual(http.call_args.args[:2], ("api.example.test", 8443))
        (method, path, body, headers), _kwargs = transport.request.call_args
        self.assertEqual((method, path), ("POST", "/gateway/openai/v1/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("chat_template_kwargs", json.loads(body))
        self.assertEqual(self.connection.model_base, BASE_URL)

    def test_handled_statuses_without_reading_untrusted_error_payload(self):
        for status, category in [(401, "unauthorized"), (403, "forbidden"), (429, "rate_limit"),
                                 (502, "unavailable"), (503, "unavailable"), (307, "redirect")]:
            with self.subTest(status=status), patch("direct_connection.HTTPSConnection") as http:
                transport, reply = self.fake_response(http, status, TOKEN.encode(), retry_after="12")
                with self.assertRaises(DirectError) as caught:
                    self.connection.probe(TOKEN)
                self.assertEqual((caught.exception.status, caught.exception.category), (status, category))
                if status == 429:
                    self.assertIn("12", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
                reply.read.assert_not_called()
                transport.request.assert_called_once()
                transport.close.assert_called_once()

    def test_connection_failures_hide_exception_detail(self):
        for error, category in [(TimeoutError(TOKEN), "timeout"),
                                (ssl.SSLCertVerificationError(TOKEN), "certificate"),
                                (HTTPException(TOKEN), "network"), (OSError(TOKEN), "network")]:
            with self.subTest(category=category), patch("direct_connection.HTTPSConnection") as http:
                http.return_value.getresponse.side_effect = error
                with self.assertRaises(DirectError) as caught:
                    self.connection.probe(TOKEN)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.status, 0)
                self.assertNotIn(TOKEN, str(caught.exception))
                http.return_value.close.assert_called_once()

    def test_valid_https_does_not_make_model_list_or_empty_completion_a_success(self):
        cases = [b"<html>Cloudflare error page</html>",
                 json.dumps({"data": [{"id": MODEL_ID}]}).encode(),
                 json.dumps({"model": MODEL_ID, "choices": [{"message": {"role": "assistant", "content": ""}}]}).encode(),
                 json.dumps({"model": "wrong-model", "choices": [{"message": {"role": "assistant", "content": "Hi"}}]}).encode()]
        for reply in cases:
            with self.subTest(reply=reply), patch("direct_connection.HTTPSConnection") as http:
                self.fake_response(http, data=reply)
                with self.assertRaises(DirectError) as caught:
                    self.connection.probe(TOKEN)
                self.assertEqual((caught.exception.status, caught.exception.category), (200, "response"))

    def test_insecure_transport_and_header_injection_refused_before_request(self):
        for url in ("http://example.test/v1", "https://user:pass@example.test/v1",
                    "https://example.test/v1?secret=true", "https://example.test/v1#",
                    "https://example.test/v1/../other", "https://example.test/%2e%2e/v1",
                    "https://example.test//other", "https://example.test:/v1", "https://example.test:0/v1",
                    "https://[2001:db8::1]bad/v1", "https://example.test\\other",
                    "https://example.test/v1/chat/completions/chat/completions",
                    "https://example.test/v1\r\nX-Test:", None, b"https://example.test"):
            with self.subTest(url=url), self.assertRaises(DirectError):
                DirectConnection(self.path, base_url=url)
        insecure = ssl._create_unverified_context()
        with self.assertRaises(DirectError):
            DirectConnection(self.path, tls=insecure)
        with patch("direct_connection.HTTPSConnection") as http:
            with self.assertRaises(DirectError):
                self.connection.probe("abc\r\nX-Test: spoof")
            http.assert_not_called()

    @patch("direct_connection.HTTPSConnection")
    def test_sign_in_returns_session_without_persisting_credentials(self, http):
        transport, _reply = self.fake_response(http, data=json.dumps({"token": TOKEN, "token_type": "Bearer"}).encode())
        password = "fixture-password"
        self.assertEqual(self.connection.sign_in("example@example.test", password), TOKEN)
        method, path, body, headers = transport.request.call_args.args
        self.assertEqual((method, path), ("POST", "/api/v1/auths/signin"))
        self.assertNotIn("Authorization", headers)
        self.assertEqual(json.loads(body), {"email": "example@example.test", "password": password})
        self.assertEqual(list(self.path.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
