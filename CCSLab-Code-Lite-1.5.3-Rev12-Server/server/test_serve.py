"""Check the Gateway CLI with real loopback HTTP and HTTPS listeners."""

from http.client import HTTPConnection, HTTPSConnection
import json
import os
from pathlib import Path
from .test_support import TEST_ROOT
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from .accounts import Accounts


PROJECT = Path(__file__).resolve().parent.parent


@unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "The Gateway CLI must run as a non-root user.")
class ServeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "accounts.sqlite3"
        token_file = self.root / "llama-api-key"
        token_file.write_text("fixture-only-key\n", encoding="ascii")
        self.command = [sys.executable, "-B", "-m", "server", "--database", str(self.database), "serve"]
        self.process_options = {
            "cwd": PROJECT, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True,
            "env": {key: value for key, value in os.environ.items() if not key.startswith("CCSLAB_")} | {
                "CCSLAB_LLAMA_URL": "http://127.0.0.1:1", "CCSLAB_LLAMA_API_KEY_FILE": str(token_file),
            },
        }

    def test_http_starts_on_default_loopback(self):
        self.assert_serves(HTTPConnection)

    @unittest.skipUnless(shutil.which("openssl"), "Install OpenSSL to run the HTTPS startup test.")
    def test_https_starts_with_both_tls_files(self):
        certificate = self.root / "localhost.crt"
        key = self.root / "localhost.key"
        config = self.root / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name = dn\n[dn]\n", encoding="ascii")
        generated = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-config", str(config),
            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        context = ssl.create_default_context(cafile=str(certificate))

        def connect(host, port, *, timeout):
            return HTTPSConnection(host, port, timeout=timeout, context=context)

        self.assert_serves(connect, "--bind", "127.0.0.1", "--tls-cert", str(certificate), "--tls-key", str(key))

    def test_rejects_each_single_tls_parameter(self):
        for parameter in ("--tls-cert", "--tls-key"):
            with self.subTest(parameter=parameter):
                self.assert_rejected("Provide both --tls-cert and --tls-key", parameter, str(self.root / "unused"))

    def test_rejects_non_loopback_http(self):
        for host in ("0.0.0.0", "192.0.2.1"):
            with self.subTest(host=host):
                self.assert_rejected("Plain HTTP is only allowed on 127.0.0.1", "--bind", host)

    def assert_rejected(self, reason, *flags):
        result = subprocess.run(self.command + list(flags), timeout=10, **self.process_options)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(reason, result.stdout)
        self.assertFalse(self.database.exists())

    def assert_serves(self, connect, *flags):
        accounts = Accounts(self.database)
        accounts.migrate()
        accounts.create("serve-admin", "fixture-admin-password", bootstrap=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        command = self.command + ["--port", str(port), *flags]
        with subprocess.Popen(command, **self.process_options) as process:
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        self.fail(f"Gateway exited before listening: {process.communicate()[0]}")
                    connection = connect("127.0.0.1", port, timeout=2)
                    try:
                        connection.request("GET", "/health")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read()), {"ok": True})
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        connection.request("GET", "/v1/models")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 401)
                        response.read()
                        break
                    except (ConnectionRefusedError, TimeoutError):
                        if time.monotonic() >= deadline:
                            self.fail("Gateway did not start listening within 10 seconds.")
                        time.sleep(0.05)
                    finally:
                        connection.close()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
