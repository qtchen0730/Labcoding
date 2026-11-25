"""Targeted POSIX and opt-in QEMU/network-namespace checks; never run on production."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

if __package__:
    from . import model_control as control, model_host as host
    from .test_support import TEST_ROOT
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server import model_control as control, model_host as host
    from server.test_support import TEST_ROOT


class ManagedMaintenanceTests(unittest.TestCase):
    def test_policy_enrollment_accepts_only_the_fixed_operator_path(self):
        data = {"enabled": True, "gpu_index": 0, "llama_url": "http://172.17.0.1:8000",
                "model_id": "lab-general", "api_key_file": "/etc/ccslab/key"}
        with patch.object(control, "_root_file", side_effect=lambda _: json.dumps(data).encode()):
            self.assertIsNone(control.Policy.read(Path("fixture")).managed_opencode_config)
            data["managed_opencode_config"] = str(control.MANAGED_OPENCODE)
            self.assertEqual(control.Policy.read(Path("fixture")).managed_opencode_config, control.MANAGED_OPENCODE)
            data["managed_opencode_config"] = "/etc/passwd"
            with self.assertRaises(ValueError): control.Policy.read(Path("fixture"))

    @unittest.skipUnless(os.name == "posix", "requires POSIX fork and service UID")
    def test_managed_file_is_updated_as_server_and_preserves_other_fields(self):
        import pwd
        account = pwd.getpwnam("server")
        if os.geteuid() == 0 and "QEMU" not in Path("/sys/class/dmi/id/sys_vendor").read_text():
            self.skipTest("privilege-drop root test is restricted to the isolated VM")
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            if os.geteuid() == 0:
                os.chown(root, account.pw_uid, account.pw_gid)
                # The service UID needs traversal, not write permission, on test parents.
                for parent in root.parents:
                    if not parent.is_relative_to(TEST_ROOT.parent): break
                    if parent.stat().st_uid == 0: parent.chmod(parent.stat().st_mode | 0o001)
            path = root / "opencode.json"
            document = {"provider": {"ccslab": {"options": {"apiKey": "fixture-only"}, "models": {
                "lab-general": {"limit": {"context": 163840, "output": 8192}}}}}, "compaction": {"auto": True}}
            path.write_text(json.dumps(document)); path.chmod(0o600)
            if os.geteuid() == 0: os.chown(path, account.pw_uid, account.pw_gid)
            policy = control.Policy("a" * 64, 0, "http://172.17.0.1:8000", "lab-general", root / "key", path)
            worker = host.Host(policy, "fixture")
            self.assertEqual(worker.managed_config()["context"], 163840)
            self.assertEqual(worker.managed_config(131072, {163840})["context"], 131072)
            document["provider"]["ccslab"]["models"]["lab-general"]["limit"]["context"] = 131072
            self.assertEqual(json.loads(path.read_text()), document)
            self.assertEqual((path.stat().st_uid, path.stat().st_mode & 0o777), (account.pw_uid, 0o600))
            self.assertEqual(len(list(root.glob(".opencode-context-backup-*"))), 1)
            with self.assertRaises(host.MaintenanceError):
                worker.managed_config(163840, {196608})
            self.assertEqual(worker.managed_config(163840, {131072})["context"], 163840)
            path.chmod(0o644)
            with self.assertRaisesRegex(host.MaintenanceError, "managed_config_permission"):
                worker.managed_config()
            path.chmod(0o600)

    @unittest.skipUnless(os.name == "posix", "requires /proc-style symlinks")
    def test_process_scan_detects_opencode_and_fails_closed_on_permission_errors(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            for pid, executable in ((123, "/opt/opencode"), (124, "/usr/bin/python3")):
                p = root / str(pid); p.mkdir(); (p / "exe").symlink_to(executable)
            self.assertEqual(host.opencode_processes(root.stat().st_uid, root), [123])
            with patch.object(Path, "readlink", side_effect=PermissionError()):
                with self.assertRaisesRegex(host.MaintenanceError, "process_check_failed"):
                    host.opencode_processes(root.stat().st_uid, root)

    @unittest.skipUnless(os.environ.get("CCSLAB_MAINTENANCE_NET_TEST") == "1", "requires isolated QEMU root VM")
    def test_real_nft_local_remote_ipv6_and_established_connections(self):
        self.assertEqual(os.geteuid(), 0)
        self.assertIn("QEMU", Path("/sys/class/dmi/id/sys_vendor").read_text())
        result = subprocess.run(["unshare", "-n", sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--network-probe"], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS real nft", result.stdout)
        print(result.stdout, end="", flush=True)


def network_probe():
    assert os.geteuid() == 0 and "QEMU" in Path("/sys/class/dmi/id/sys_vendor").read_text()
    def command(*args): subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    command("ip", "link", "set", "lo", "up")
    peer = "ccslab-test-" + str(os.getpid())
    guard = host.DirectRequestGuard()
    sockets = []
    established = None
    def listen(address, family=socket.AF_INET):
        server = socket.socket(family); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6: server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        server.bind(address); server.listen(); sockets.append(server)
        def accept():
            def echo(conn):
                with conn:
                    while conn.recv(1): conn.sendall(b"y")
            while True:
                conn, _ = server.accept(); Thread(target=echo, args=(conn,), daemon=True).start()
        Thread(target=accept, daemon=True).start()
    listen(("0.0.0.0", 8000)); listen(("::", 8000), socket.AF_INET6); listen(("0.0.0.0", 8001))
    code = "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=1); s.sendall(b\"x\"); assert s.recv(1)==b\"y\""
    def probe(address="127.0.0.1", port=8000, *, unprivileged=False, remote=False):
        args = [sys.executable, "-I", "-c", code, address, str(port)]
        if unprivileged: args = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", *args]
        if remote: args = ["ip", "netns", "exec", peer, *args]
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode
    try:
        command("ip", "netns", "add", peer)
        command("ip", "link", "add", "testhost", "type", "veth", "peer", "name", "testpeer")
        command("ip", "link", "set", "testpeer", "netns", peer)
        command("ip", "addr", "add", "192.0.2.1/30", "dev", "testhost")
        command("ip", "link", "set", "testhost", "up")
        command("ip", "-n", peer, "addr", "add", "192.0.2.2/30", "dev", "testpeer")
        command("ip", "-n", peer, "link", "set", "testpeer", "up")
        assert probe(unprivileged=True) == 0 and probe("::1", unprivileged=True) == 0
        assert probe("192.0.2.1", remote=True) == 0
        established_code = "import socket,sys; s=socket.create_connection((\"127.0.0.1\",8000),timeout=2); s.sendall(b\"x\"); assert s.recv(1)==b\"y\"; print(\"ready\",flush=True); input(); s.sendall(b\"x\"); assert s.recv(1)==b\"y\""
        established = subprocess.Popen(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
            sys.executable, "-I", "-c", established_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        assert established.stdout.readline().strip() == "ready"
        with tempfile.TemporaryDirectory() as folder, patch.object(control, "MODEL_MAINTENANCE", Path(folder) / "hold"):
            guard.start()
            assert control.MODEL_MAINTENANCE.exists()
            assert probe() == 0, "root-local helper health probe blocked"
            assert probe(unprivileged=True) != 0 and probe("::1", unprivileged=True) != 0
            assert probe("192.0.2.1", remote=True) != 0
            assert probe(port=8001, unprivileged=True) == 0, "unrelated port blocked"
            established.communicate("go\n", timeout=5)
            assert established.returncode != 0, "pre-existing unprivileged connection bypassed fence"
            guard.stop()
            assert not control.MODEL_MAINTENANCE.exists()
            assert probe(unprivileged=True) == 0 and probe("::1", unprivileged=True) == 0
            assert probe("192.0.2.1", remote=True) == 0
        print("PASS real nft: IPv4/IPv6 local, external direct and established requests blocked; root probes and port8001 preserved; release verified")
    finally:
        if established is not None and established.poll() is None: established.terminate(); established.wait()
        if guard.installed: guard.stop()
        subprocess.run(["ip", "netns", "delete", peer], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for server in sockets: server.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--network-probe"]: network_probe()
    else: unittest.main(verbosity=2)
