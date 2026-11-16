"""Run against loopback mock origins and CONNECT proxies. No public credentials."""

from verify_support import TEST_ROOT

from base64 import b64encode
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import sys
import tempfile
from threading import Thread
from time import sleep
import unittest
from unittest.mock import MagicMock, patch

from verify_tls import tls_files

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from account_client import AccountClient, AccountError, normalize_server
from account_transport import _tcp_connection


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
        code, result = self.server.replies.get(self.path, (200, {"ok": True}))
        if self.server.delay:
            sleep(self.server.delay)
        if isinstance(result, dict):
            data = json.dumps(result).encode("utf-8")
        else:
            data = result
        try:
            self.send_response(code)
            if code == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    do_GET = do_POST = handle_request


class Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        first = self.rfile.readline()
        headers = []
        while field := self.rfile.readline().strip():
            headers.append(field)
        self.server.requests.append((first, headers))
        if self.server.status != 200:
            self.request.sendall(f"HTTP/1.1 {self.server.status} Proxy decision\r\nContent-Length: 0\r\n\r\n".encode())
            return
        with socket.create_connection(self.server.target, timeout=4) as origin:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            links = (self.request, origin)
            try:
                while readers := select.select(links, (), (), 6)[0]:
                    for client in readers:
                        data = client.recv(32768)
                        if not data:
                            return
                        destination = origin if client is self.request else self.request
                        destination.sendall(data)
            except OSError:
                return


@contextmanager
def serving(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.025}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="transport-", dir=TEST_ROOT)
        cls.addClassCleanup(cls.scratch.cleanup)
        cert, key, cls.tls = tls_files(Path(cls.scratch.name))
        cls.tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls_server.load_cert_chain(cert, key)

    @contextmanager
    def endpoint(self, *, name="127.0.0.1"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        server.socket = self.tls_server.wrap_socket(server.socket, server_side=True)
        server.requests, server.replies, server.delay = [], {}, 0
        with serving(server):
            yield server, f"https://{name}:{server.server_port}"

    @contextmanager
    def proxy(self, origin, *, secure=False, status=200):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Tunnel)
        server.daemon_threads = True
        server.status, server.target, server.requests = status, origin.server_address, []
        if secure:
            server.socket = self.tls_server.wrap_socket(server.socket, server_side=True)
        with serving(server):
            protocol = "https" if secure else "http"
            yield server, f"{protocol}://127.0.0.1:{server.server_address[1]}"

    def test_address_and_modes(self):
        self.assertEqual(normalize_server(" HTTPS://Ccslab-code.qtchen.cn./ "), "https://ccslab-code.qtchen.cn")
        self.assertEqual(normalize_server("https://[::1]:8443"), "https://[::1]:8443")
        for invalid in ("https://user:secret@localhost", "https://ccs\t.lab", "https://bad%2e.test",
                        "https://-bad.test", "http://example.com", "https://localhost/v1",
                        "https://localhost:", "https://localhost?", "https://localhost#",
                        "https://[::1]junk", "https://[::1]:"):
            with self.assertRaises(AccountError):
                normalize_server(invalid)
        with self.endpoint() as (origin, address):
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1"}, clear=True), \
                    patch("account_transport.windows_proxy") as system:
                for mode in ("auto", "direct", "system"):
                    client = AccountClient(address, tls=self.tls, mode=mode)
                    client.token = "a-fixture-token"
                    self.assertEqual(client.health(), {"ok": True})
                    self.assertEqual(client.route, "direct")
                    self.assertIsNone(client.engine_proxy_url())
            system.assert_not_called()
            self.assertEqual(origin.requests, [("GET", "/health", b"", None)] * 3)

    def test_dns_and_tcp_observers(self):
        with self.endpoint(name="localhost") as (_, address):
            client = AccountClient(address, tls=self.tls, mode="direct")
            rows = []
            with patch("account_transport.socket.getaddrinfo", side_effect=socket.gaierror(11001, "secret")):
                with self.assertRaises(AccountError) as caught:
                    client.health(observe=lambda *row: rows.append(row))
            self.assertEqual(caught.exception.category, "dns")
            self.assertEqual(caught.exception.stage, "dns")
            self.assertIn("11001", caught.exception.technical)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(rows[-1][:2], ("dns", "failure"))
            with patch("account_transport._tcp_connection", side_effect=ConnectionRefusedError(10061, "rejected")):
                with self.assertRaises(AccountError) as refused:
                    client.health(observe=lambda *row: rows.append(row))
            self.assertEqual((refused.exception.category, refused.exception.stage), ("tcp", "tcp"))
            self.assertIn(("dns", "normal"), [row[:2] for row in rows])
            self.assertEqual(rows[-1][:2], ("tcp", "failure"))

    def test_connect_timeout_is_shared_between_addresses(self):
        first, second = MagicMock(), MagicMock()
        first.connect.side_effect = TimeoutError
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]
        with (
            patch("account_transport.socket.socket", side_effect=[first, second]),
            patch("account_transport.monotonic", side_effect=[0, 3]),
        ):
            self.assertIs(_tcp_connection(addresses, 6), second)
        first.settimeout.assert_called_once_with(3)
        first.close.assert_called_once()
        second.settimeout.assert_called_once_with(3)

    def test_certificate_unknown_ca_and_wrong_hostname(self):
        with self.endpoint() as (_, address):
            untrusted = AccountClient(address, mode="direct")
            with self.assertRaises(AccountError) as error:
                untrusted.health()
            self.assertEqual((error.exception.category, error.exception.stage), ("certificate", "tls"))
            mismatched = AccountClient(address.replace("127.0.0.1", "localhost"), tls=self.tls, mode="direct")
            with self.assertRaises(AccountError) as wrong_name:
                mismatched.health()
            self.assertEqual((wrong_name.exception.category, wrong_name.exception.stage), ("certificate", "tls"))
            self.assertRegex(wrong_name.exception.technical, r"verify_code=\d+")

    def test_environment_http_and_https_proxy(self):
        for secure in (False, True):
            with self.subTest(secure=secure), self.endpoint() as (origin, address), \
                    self.proxy(origin, secure=secure) as (proxy, proxy_url):
                protocol, remaining = proxy_url.split("://")
                proxy_secret = "fixture-proxy-password"
                authenticated_url = f"{protocol}://user:{proxy_secret}@{remaining}"
                with patch.dict(os.environ, {"HTTPS_PROXY": authenticated_url}, clear=True), \
                        patch("account_transport._is_loopback", return_value=False):
                    client = AccountClient(address, tls=self.tls)
                    self.assertEqual(client.health(), {"ok": True})
                    self.assertEqual(client.route, "environment")
                    self.assertEqual(client.engine_proxy_url(), authenticated_url)
                    self.assertFalse(client.fallback)
                    client.token = "fixture-session-token"
                    encoded = b64encode(f"user:{proxy_secret}".encode()).decode()
                    origin.replies["/api/register"] = (403, {"detail": f"{encoded} {proxy_secret} fixture-form-password"})
                    with self.assertRaises(AccountError) as error:
                        client.register("test-user", "fixture-form-password")
                    self.assertNotIn(proxy_secret, str(error.exception))
                    self.assertNotIn(encoded, str(error.exception))
                    self.assertNotIn("fixture-form-password", str(error.exception))
                    self.assertTrue(error.exception.api_error)
                self.assertEqual(len(proxy.requests), 2)
                self.assertIn(b"Proxy-Authorization: Basic " + encoded.encode(), proxy.requests[0][1])
                self.assertEqual(origin.requests[-1][-1], "Bearer fixture-session-token")

    def test_windows_system_mode_and_bypass(self):
        with self.endpoint(name="localhost") as (origin, address), self.proxy(origin) as (proxy, url):
            server = f"http=127.0.0.1:1;https={url}"
            with patch("account_transport.windows_proxy", return_value=(server, "")) as resolve, \
                    patch.dict(os.environ, {"HTTPS_PROXY": "http://localhost:1"}, clear=True), \
                    patch("account_transport._is_loopback", return_value=False):
                # A mock IP hostname is needed to avoid the TLS SAN mismatch.
                client = AccountClient(address.replace("localhost", "127.0.0.1"), tls=self.tls, mode="system")
                self.assertEqual(client.health(), {"ok": True})
                self.assertEqual(client.route, "system")
                self.assertEqual(client.engine_proxy_url(), url)
                self.assertEqual(resolve.call_count, 1)
            self.assertEqual(len(proxy.requests), 1)
            with patch("account_transport.windows_proxy", return_value=(server, "127.*")), \
                    patch("account_transport._is_loopback", return_value=False):
                client = AccountClient(address.replace("localhost", "127.0.0.1"), tls=self.tls, proxy_mode="system")
                client.health()
                self.assertEqual(client.route, "direct")
            self.assertEqual(len(proxy.requests), 1)

    def test_auto_fallback_never_bypasses_proxy_denials_or_replays(self):
        with self.endpoint() as (origin, address), patch("account_transport._is_loopback", return_value=False):
            for status in (403, 407, 502):
                with self.subTest(status=status), self.proxy(origin, status=status) as (proxy, url), \
                        patch.dict(os.environ, {"HTTPS_PROXY": url}, clear=True):
                    client = AccountClient(address, tls=self.tls)
                    initial = len(origin.requests)
                    if status == 502:
                        client.register("fixture", "fixture-data")
                        self.assertTrue(client.fallback)
                        self.assertIsNone(client.engine_proxy_url())
                        self.assertEqual(len(origin.requests), initial + 1)
                    else:
                        with self.assertRaises(AccountError) as blocked:
                            client.register("fixture", "fixture-data")
                        self.assertEqual(blocked.exception.category, "proxy")
                        self.assertEqual(len(origin.requests), initial)
                    self.assertEqual(len(proxy.requests), 1)
                    self.assertTrue(proxy.requests[0][0].startswith(b"CONNECT "))
            with self.proxy(origin) as (_proxy, url), patch.dict(os.environ, {"HTTPS_PROXY": url}, clear=True):
                client = AccountClient(address, tls=self.tls)
                previous = len(origin.requests)
                origin.delay = 0.5
                with self.assertRaises(AccountError) as timeout:
                    client.request("POST", "/api/register", {"password":"not-a-real-key"}, timeout=0.2)
                self.assertEqual((timeout.exception.category, timeout.exception.stage), ("timeout", "http"))
                self.assertFalse(client.fallback)
                self.assertEqual(len(origin.requests), previous + 1)

    def test_http_classification_and_health(self):
        with self.endpoint() as (origin, address):
            client = AccountClient(address, tls=self.tls, mode="direct")
            for status, category in ((401, "unauthorized"), (403, "forbidden"), (404, "not_found"),
                                     (405, "method"), (429, "rate_limit"), (502, "unavailable"),
                                     (503, "unavailable")):
                with self.subTest(status=status):
                    origin.replies["/api/me"] = (status, {"detail":"mock account error"})
                    with self.assertRaises(AccountError) as error:
                        client.request("GET", "/api/me", auth=False)
                    self.assertEqual((error.exception.status, error.exception.category), (status, category))
                    self.assertTrue(error.exception.api_error)
            origin.replies["/health"] = (200, {"ok": False})
            with self.assertRaises(AccountError) as invalid:
                client.health()
            self.assertEqual(invalid.exception.technical, "unexpected_health_json")
            origin.replies["/health"] = (200, b"<html>wrong server</html>")
            with self.assertRaises(AccountError) as invalid:
                client.health()
            self.assertEqual(invalid.exception.technical, "non_json_response")


if __name__ == "__main__":
    unittest.main(verbosity=2)
