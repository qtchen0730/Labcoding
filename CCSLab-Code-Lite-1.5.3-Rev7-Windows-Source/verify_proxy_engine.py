"""Exercise the bundled OpenCode over an HTTP CONNECT proxy with real TLS."""

from base64 import b64encode
from http.server import ThreadingHTTPServer
from pathlib import Path
import select
import socket
from socketserver import StreamRequestHandler, ThreadingTCPServer
import ssl
import tempfile
from threading import Thread
import unittest

from verify_engine import Engine, Handler
from verify_tls import tls_files


GATEWAY = "gateway.fixture.invalid"
PROXY_LOGIN = "proxy-fixture:test-only-proxy-credential"


class ConnectFixture(StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(4096).decode("ascii").split()
        if len(line) != 3:
            return
        headers = {}
        while (line_header := self.rfile.readline(4096)) not in {b"\r\n", b"\n", b""}:
            name, _, value = line_header.partition(b":")
            headers[name.lower()] = value.strip()
        self.server.tunnels.append((line[0], line[1], headers))
        if line[0] != "CONNECT" or line[1] != f"{GATEWAY}:{self.server.target_port}":
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        if headers.get(b"proxy-authorization") != self.server.authorization:
            self.wfile.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            return
        with socket.create_connection(("127.0.0.1", self.server.target_port), timeout=3) as upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            peers = {self.connection: upstream, upstream: self.connection}
            while True:
                readable = [sock for sock in peers if isinstance(sock, ssl.SSLSocket) and sock.pending()]
                if not readable:
                    readable, _, _ = select.select(peers, [], [], 10)
                if not readable:
                    return
                for source in readable:
                    try:
                        data = source.recv(65536)
                        if not data:
                            return
                        peers[source].sendall(data)
                    except OSError:
                        return


class ProxyFixture(ThreadingTCPServer):
    daemon_threads = True


class ProxyEngineTests(unittest.TestCase):
    def test_proxy_carries_model_traffic_but_not_local_tool_bridge(self):
        self.verify_proxy("http")

    def test_tls_proxy_carries_model_traffic_but_not_local_tool_bridge(self):
        self.verify_proxy("https")

    def verify_proxy(self, scheme):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="proxy-engine-") as folder:
            state = Path(folder)
            project = state / "project"
            project.mkdir()
            cert, key, _ = tls_files(state, hostname=GATEWAY)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)

            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as origin:
                origin.socket = context.wrap_socket(origin.socket, server_side=True)
                origin.calls, origin.down_until = [], 0
                Thread(target=origin.serve_forever, daemon=True).start()
                with ProxyFixture(("127.0.0.1", 0), ConnectFixture) as proxy:
                    proxy.target_port, proxy.tunnels = origin.server_port, []
                    proxy.authorization = b"Basic " + b64encode(PROXY_LOGIN.encode("ascii"))
                    ca_bundle = state / "ca.pem"
                    if scheme == "https":
                        proxy_dir = state / "proxy"
                        proxy_dir.mkdir()
                        proxy_cert, proxy_key, _ = tls_files(proxy_dir)
                        proxy_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        proxy_tls.load_cert_chain(proxy_cert, proxy_key)
                        proxy.socket = proxy_tls.wrap_socket(proxy.socket, server_side=True)
                        with ca_bundle.open("ab") as file:
                            file.write((proxy_dir / "ca.pem").read_bytes())
                    Thread(target=proxy.serve_forever, daemon=True).start()
                    engine = Engine(state / "agent", f"https://{GATEWAY}:{origin.server_port}/v1", "fixture-token",
                                    ca_bundle=ca_bundle,
                                    proxy_url=f"{scheme}://{PROXY_LOGIN}@127.0.0.1:{proxy.server_address[1]}")
                    try:
                        engine.open_project(str(project))
                        session = engine.new_session()
                        events = []
                        engine.run(session, "PLAN-TURN 请检查项目", events.append, lambda _what: False, read_only=True)
                        self.assertTrue(any("实施方案" in event.get("text", "") for event in events), events)
                        self.assertTrue(origin.calls)
                        targets = {(method, target) for method, target, _ in proxy.tunnels}
                        self.assertIn(("CONNECT", f"{GATEWAY}:{origin.server_port}"), targets)
                        for _, target, headers in proxy.tunnels:
                            self.assertFalse(target.startswith(("127.0.0.1:", "localhost:", "[::1]:")))
                            self.assertNotIn(b"authorization", headers)
                        for log in (state / "agent").rglob("*.log"):
                            self.assertTrue(b"test-only-proxy-credential" not in log.read_bytes(), log.name)
                    finally:
                        engine.close()
                        proxy.shutdown()
                        origin.shutdown()
