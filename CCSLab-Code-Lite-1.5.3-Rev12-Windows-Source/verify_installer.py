"""Native Windows installer transactions with a small, explicitly synthetic payload.

Run separately from UI tests: python -B -m unittest -v verify_installer.
Final full-size frozen payload is also checked by packaging/Test-Installer.ps1.
No registry, shortcuts, installed software or real user data are changed.
"""
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile

from verify_support import TEST_ROOT


class InstallerTransactions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="rev12-native-installer-")
        cls.base = Path(cls.scratch.name).resolve()
        cls.addClassCleanup(cls.scratch.cleanup)
        compiler = Path(os.environ["WINDIR"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        source = Path(__file__).parent/"packaging/Setup.cs"
        cls.installers = {}
        for version in ("old", "new"):
            payload = cls.base/(version+".zip")
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("CCSLab Code Lite.exe", version.encode())
                archive.writestr("_internal/fixture.txt", version.encode())
            exe = cls.base/(version+"-setup.exe")
            result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/codepage:65001",
                "/r:System.dll", "/r:System.Core.dll", "/r:System.Drawing.dll",
                "/r:System.Windows.Forms.dll", "/r:System.IO.Compression.dll",
                "/resource:"+str(payload)+",ccslab-payload", "/out:"+str(exe), str(source)],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError(result.stdout+result.stderr)
            cls.installers[version] = exe

    def setUp(self):
        self.case = self.base/self._testMethodName
        self.case.mkdir()
        self.destination = self.case/"program 安装"
        self.data = self.case/"user-data.json"
        self.data.write_text('{"draft":"unfinished","history":["tool"],"git_diff":"keep"}')

    def start(self, version="new", point=None):
        env = dict(os.environ)
        env.pop("CCSLAB_INSTALL_TEST_POINT", None)
        if point:
            env["CCSLAB_INSTALL_TEST_POINT"] = point
        return subprocess.Popen([str(self.installers[version]), "--smoke-install", str(self.destination)],
            env=env, creationflags=subprocess.CREATE_NO_WINDOW)

    def run_install(self, version="new", point=None, expected=0):
        with self.start(version, point) as process:
            self.assertEqual(process.wait(timeout=55), expected,
                (Path(str(self.destination)+".error.txt").read_text(errors="replace")
                 if Path(str(self.destination)+".error.txt").exists() else "no installer diagnostic"))

    def installed(self, version):
        self.assertEqual((self.destination/"CCSLab Code Lite.exe").read_text(), version)
        self.assertEqual((self.destination/"_internal/fixture.txt").read_text(), version)
        self.assertIn('"draft":"unfinished"', self.data.read_text())

    def wait_for_point(self, process, point):
        marker = Path(str(self.destination)+".test-point")
        deadline = time.monotonic()+35
        while not marker.exists() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertTrue(marker.exists(), "installer did not reach pause: "+point)
        self.assertEqual(marker.read_text(), point)
        marker.unlink()

    def test_first_install_upgrade_and_unrelated_files(self):
        self.run_install("old")
        self.installed("old")
        self.run_install()
        self.installed("new")
        extra = self.destination/"user-file.txt"
        extra.write_text("preserve")
        self.run_install("old", expected=1)
        self.installed("new")
        self.assertEqual(extra.read_text(), "preserve")

    def test_upgrade_exceptions_restore_previous_version(self):
        self.run_install("old")
        for point in ("after-backup", "after-swap"):
            with self.subTest(point=point):
                self.run_install(point="throw:"+point, expected=1)
                self.installed("old")
        self.run_install()
        self.installed("new")

    def test_first_install_termination_recovers_without_partial_program(self):
        with self.start(point="pause:after-backup") as process:
            try:
                self.wait_for_point(process, "after-backup")
                self.assertFalse(self.destination.exists())
            finally:
                process.kill()
                process.wait(timeout=10)
        self.run_install(point="throw:after-backup", expected=1)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.case.glob(".ccslab-setup-*")))
        self.run_install()
        self.installed("new")

    def test_process_termination_recovers_before_next_install(self):
        self.run_install("old")
        for point in ("after-backup", "after-swap"):
            with self.subTest(point=point), self.start(point="pause:"+point) as process:
                try:
                    self.wait_for_point(process, point)
                finally:
                    process.kill()
                    process.wait(timeout=10)
            # Recovery must first restore old; this deliberately fails the new attempt.
            self.run_install(point="throw:after-backup", expected=1)
            self.installed("old")
        with self.start(point="pause:after-commit") as process:
            try:
                self.wait_for_point(process, "after-commit")
            finally:
                process.kill()
                process.wait(timeout=10)
        self.run_install()
        self.installed("new")
        self.assertFalse(list(self.case.glob("*.transaction")))
        self.assertFalse(list(self.case.glob(".ccslab-setup-*")))

    def test_concurrent_installer_is_refused_without_losing_progress(self):
        self.run_install("old")
        with self.start(point="pause:after-backup") as first:
            try:
                self.wait_for_point(first, "after-backup")
                self.run_install(expected=1)
            finally:
                first.kill()
                first.wait(timeout=10)
        self.run_install()
        self.installed("new")

    def locked_handle(self):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        api.CreateFileW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.CreateFileW(str(self.destination/"CCSLab Code Lite.exe"), 0x80000000,
                                 1, None, 3, 0, None)  # Read sharing, no delete sharing.
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return api, handle

    def test_transient_file_lock_retries_then_upgrades(self):
        self.run_install("old")
        api, handle = self.locked_handle()
        release = threading.Timer(1.5, lambda: api.CloseHandle(handle))
        release.start()
        try:
            self.run_install()
        finally:
            release.join()
        self.installed("new")

    def test_permanent_file_lock_fails_safely_then_recovers(self):
        self.run_install("old")
        api, handle = self.locked_handle()
        try:
            self.run_install(expected=1)
            self.installed("old")
        finally:
            api.CloseHandle(handle)
        self.run_install()
        self.installed("new")

    def test_untrusted_recovery_path_is_refused(self):
        self.run_install("old")
        name = hashlib.sha256(str(self.destination).upper().encode()).hexdigest().upper()
        journal = self.case/(".ccslab-install-"+name+".transaction")
        journal.write_text(str(self.destination)+"\n"+str(self.case/"outside")+"\nprepared")
        self.run_install(expected=1)
        self.installed("old")
        self.assertTrue(journal.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
