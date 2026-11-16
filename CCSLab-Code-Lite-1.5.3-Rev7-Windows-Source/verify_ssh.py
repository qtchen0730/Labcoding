"""Local integration fixture: native Windows SSH client and deterministic test peer."""

from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import paramiko

sys.path.insert(0, str(Path(__file__).parent / "app"))
from connection import Connection, ConnectionError


class Fixture(paramiko.ServerInterface):
    def __init__(self):
        self.destinations = []

    def check_auth_none(self, username):
        assert username == "tunnel_test"
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destinations.append(destination)
        return (paramiko.OPEN_SUCCEEDED if destination == ("172.17.0.1", 8000)
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)


def run_client_tests(state, server_port, transport, listener):
    profile = Connection(state)
    assert profile.load()["host"] == "192.168.1.107"
    try:
        profile.connect("127.0.0.1", "tunnel_test", "incorrect", server_port)
    except ConnectionError as error:
        assert "拒绝访问" in str(error)
        assert not (state / "connection.dpapi").exists()
    else:
        raise AssertionError("Unauthorized verification was accepted")

    url = profile.connect("127.0.0.1", "tunnel_test", "fixture_key", server_port)
    assert url.startswith("http://127.0.0.1:") and url.endswith("/v1")
    assert profile.alive()
    assert b"fixture_key" not in (state / "connection.dpapi").read_bytes()
    restarted = Connection(state)
    assert restarted.load() == {
        "host": "127.0.0.1", "user": "tunnel_test", "port": server_port, "token": "fixture_key",
    }
    assert restarted.connect(**restarted.load()).endswith("/v1")
    restarted.close()
    assert not restarted.alive()

    for client in transport:
        client.close()
    listener.close()
    deadline = time.monotonic() + 4
    while profile.alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not profile.alive(), "SSH exit must eventually be detected"
    profile.close()


def main():
    key = paramiko.ECDSAKey.generate()
    active_transports = []
    fixture = Fixture()

    def serve(sock):
        transport = paramiko.Transport(sock)
        active_transports.append(transport)
        transport.add_server_key(key)
        transport.start_server(server=fixture)
        while transport.is_active():
            channel = transport.accept(timeout=0.5)
            if not channel:
                continue
            with channel:
                channel.settimeout(3)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                if b"GET /v1/models HTTP/1.1" in request:
                    status = b"200 OK"
                    body = b'{"data":[{"id":"lab-general"}]}'
                else:
                    assert b"GET /props HTTP/1.1" in request
                    authorized = b"Authorization: Bearer fixture_key\r\n" in request
                    status = b"200 OK" if authorized else b"401 Unauthorized"
                    body = b'{}'
                channel.sendall(
                    b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n" + body
                )

    def accept(listener):
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                break
            threading.Thread(target=serve, args=(sock,), daemon=True).start()

    base = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(dir=base, prefix="SSH fixture ") as temporary, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        state = Path(temporary)
        port = listener.getsockname()[1]
        (state / "ssh_known_hosts").write_text(
            f"[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n", encoding="ascii"
        )
        threading.Thread(target=accept, args=(listener,), daemon=True).start()
        run_client_tests(state, port, active_transports, listener)
        assert fixture.destinations == [("172.17.0.1", 8000)] * 5
    print("PASS: native OpenSSH forwarding, wrong token rejection, model verification, DPAPI profile, automatic reconnect and disconnection.")


if __name__ == "__main__":
    main()
