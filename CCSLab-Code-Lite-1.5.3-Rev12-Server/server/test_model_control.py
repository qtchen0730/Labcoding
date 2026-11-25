"""Isolated model telemetry, permission, journaling and rollback tests."""

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from .test_support import TEST_ROOT
    from . import model_control as m
    from . import model_host as host
else:
    from test_support import TEST_ROOT
    import model_control as m
    import model_host as host


@dataclass(frozen=True)
class Parameters:
    total_context: int
    slots: int


A = Parameters(total_context=131072, slots=5)
B = Parameters(total_context=65536, slots=2)
POLICY = m.Policy("a" * 64, 0, "http://127.0.0.1:8000", "lab-general", Path("/tmp/api-key"))
CHANGE = {"old_context": A.total_context, "old_slots": A.slots,
          "new_context": B.total_context, "new_slots": B.slots, "policy": POLICY.digest}
TEST_DIR = Path(__file__).resolve().parent


def snapshot(profile=A, *, busy=0, used=27000, current_model=25000, total=32000, train=131072):
    config = {"total_context": profile.total_context, "slots": profile.slots}
    return {
        "model_id": "lab-general", "model_name": "Model", "status": "running",
        "total_context": profile.total_context, "slots": profile.slots,
        "effective_context_limit": m.slot_context(profile.total_context, profile.slots),
        "output_limit": 4096,
        "gpu": {"index": 0, "used_mib": used, "total_mib": total, "_uuid": "GPU-fixture",
                "_model_mib": current_model},
        "_train_context": train, "_observed_slot_context": m.slot_context(profile.total_context, profile.slots),
        "_uniform_context": True,
        "_active_slots": busy, "_pid": 1234,
        "_config": config, "_managed_environment": config,
    }


class TelemetryTest(unittest.TestCase):
    def test_gpu_sample_has_actual_free_memory_and_failure_is_unknown(self):
        with patch.object(m, "_fixed_command", side_effect=[
                "0, GPU-abcd, NVIDIA GeForce RTX 5090, 29000, 32607, 3100",
                "123, GPU-abcd, 28000\n123, GPU-ffff, 20"]):
            gpu = m._read_gpu(0, 123)
            self.assertEqual(gpu["free_mib"], 3100)
            self.assertEqual(gpu["unaccounted_mib"], 507)
            self.assertEqual(gpu["model_process_mib"], 28000)
            self.assertEqual(gpu["name"], "NVIDIA GeForce RTX 5090")
            self.assertAlmostEqual(gpu["utilization_percent"], 88.9)
            self.assertGreater(gpu["sampled_at"], 0)
        with patch.object(m, "_fixed_command", side_effect=OSError()):
            self.assertIsNone(m._read_gpu(0, None))
        with patch.object(m, "_fixed_command", return_value="0, GPU-abcd, GPU, 0, 0, 0"):
            self.assertIsNone(m._read_gpu(0, None))

    def test_live_sources_no_inferred_context(self):
        replies = {
            "/health": {"status": "ok"},
            "/props": {"model_path": "/private/Model.gguf", "total_slots": 5,
                       "default_generation_settings": {"n_ctx": 26368, "params": {"n_predict": -1}}},
            "/v1/models": {"data": [{"id": "lab-general", "meta": {"n_ctx_train": 131072}}]},
            "/slots": [{"id": n, "n_ctx": 26368, "is_processing": not n} for n in range(5)],
        }
        with patch.object(m, "_llama_get", side_effect=lambda _a, _b, endpoint: replies.get(endpoint)), \
                patch.object(m, "llama_pid", return_value=1234), patch.object(m, "_read_gpu", return_value=None), \
                patch.object(m, "_proc_config", return_value={"total_context": 131072, "slots": 5}):
            live = m.read_runtime("http://unused", "unused", "lab-general")
            self.assertEqual((live["status"], live["model_name"], live["total_context"]),
                             ("running", "Model.gguf", 131072))
            self.assertEqual((live["effective_context_limit"], live["slots"], live["_active_slots"]), (26368, 5, 1))
            self.assertIsNone(live["output_limit"])
        replies.pop("/slots")
        with patch.object(m, "_llama_get", side_effect=lambda _a, _b, endpoint: replies.get(endpoint)), \
                patch.object(m, "llama_pid", return_value=None), patch.object(m, "_read_gpu", return_value=None):
            live = m.read_runtime("http://unused", "unused", "lab-general")
            self.assertEqual(live["effective_context_limit"], 26368)
            self.assertIsNone(live["total_context"])
            self.assertIsNone(live["_active_slots"])
            self.assertIsNone(live["_observed_slot_context"])


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temp.cleanup)
        self.control = m.ModelControl(Path(temp.name) / "model.db", "http://example.invalid", "fixture-token")
        patch.object(self.control, "_policy", return_value=POLICY).start()
        self.runtime = patch.object(self.control, "_runtime", return_value=snapshot()).start()

    def preview(self):
        return self.control.preview("owner", B.total_context, B.slots)

    def test_refuse_out_of_range_values_or_unverifiable_runtime(self):
        for total, slots in [(70000, 3), (65536, 0), (65536, True), (65536, 11)]:
            with self.assertRaises(m.ModelControlError):
                self.control.preview("owner", total, slots)
        for unsafe in (snapshot(train=30000), dict(snapshot(), status="unavailable"),
                       dict(snapshot(), _config={"total_context": 1, "slots": 5}),
                       dict(snapshot(), _managed_environment={})):
            self.runtime.return_value = unsafe
            with self.assertRaises(m.ModelControlError):
                self.preview()
        with closing(sqlite3.connect(self.control.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM model_confirmations").fetchone()[0], 0)

    def test_apply_only_after_single_use_actor_bound_confirmation(self):
        preview = self.preview()
        self.assertEqual(preview["estimated_slot_context"], m.slot_context(B.total_context, B.slots))
        self.assertTrue(preview["restart_required"])
        token = preview["confirmation"]
        with self.assertRaises(m.ModelControlError):
            self.control.apply("different-user", token)
        self.assertTrue(self.control.begin_inference())
        try:
            with self.assertRaises(m.ModelControlError):
                self.control.apply("owner", token)
        finally:
            self.control.end_inference()

        def invoke(payload):
            self.assertEqual(payload, CHANGE)
            self.assertFalse(self.control.begin_inference())
            self.runtime.return_value = snapshot(B)
            return {"status": "applied"}

        with patch.object(self.control, "_invoke", side_effect=invoke) as call:
            result = self.control.apply("owner", token)
            self.assertEqual(result["model"]["effective_context_limit"], 32768)
            with self.assertRaises(m.ModelControlError):
                self.control.apply("owner", token)
            call.assert_called_once()
        with closing(sqlite3.connect(self.control.db)) as db:
            self.assertEqual(db.execute(
                "SELECT result FROM model_configuration_audit ORDER BY id"
            ).fetchall(), [("requested",), ("applied",)])

    def test_deployment_hold_blocks_admission_and_apply_without_consuming_confirmation(self):
        ticket = self.preview()["confirmation"]
        marker = self.control.db.parent / "deployment.hold"
        with patch.object(m, "DEPLOYMENT_HOLD", marker):
            marker.touch()
            self.assertFalse(self.control.begin_inference())
            self.assertTrue(self.control.status()["deployment_hold"])
            self.assertFalse(self.control.status()["control_available"])
            with self.assertRaisesRegex(m.ModelControlError, "服务器忙碌"):
                self.control.apply("owner", ticket)
            with self.assertRaisesRegex(m.ModelControlError, "服务器忙碌"):
                self.preview()
            marker.unlink()
            self.assertTrue(self.control.begin_inference())
            self.control.end_inference()
            # The held operation did not consume the original confirmation.
            with patch.object(self.control, "_invoke", return_value={"status": "refused"}) as helper:
                with self.assertRaises(m.ModelControlError):
                    self.control.apply("owner", ticket)
                helper.assert_called_once()

    def test_expiry_and_slot_draining(self):
        token = self.preview()["confirmation"]
        with closing(sqlite3.connect(self.control.db)) as db, db:
            db.execute("UPDATE model_confirmations SET expires = 0")
        with patch.object(self.control, "_invoke") as call:
            with self.assertRaises(m.ModelControlError):
                self.control.apply("owner", token)
            token = self.preview()["confirmation"]
            self.runtime.return_value = snapshot(busy=1)
            with self.assertRaises(m.ModelControlError):
                self.control.apply("owner", token)
            call.assert_not_called()

    def test_failure_reason_audited(self):
        with patch.object(self.control, "_invoke", return_value={
            "status": "rolled_back", "reason": "health_or_dimensions_failed",
        }):
            with self.assertRaisesRegex(m.ModelControlError, "恢复"):
                self.control.apply("owner", self.preview()["confirmation"])
        with closing(sqlite3.connect(self.control.db)) as db:
            self.assertEqual(db.execute(
                "SELECT result, reason FROM model_configuration_audit ORDER BY id DESC LIMIT 1"
            ).fetchone(), ("rolled_back", "health_or_dimensions_failed"))

    def enroll_opencode(self):
        from .output_settings import OpenCodeContext
        self.config = self.control.db.parent / "opencode.json"
        self.document = {"model": "ccslab/lab-general", "other": {"keep": True}, "provider": {
            "ccslab": {"options": {"apiKey": "fixture-secret"}, "models": {
                "lab-general": {"limit": {"context": 24000, "input": 20000, "output": 4096}}}}}}
        self.config.write_text(json.dumps(self.document), encoding="utf-8")
        self.control.context_store = OpenCodeContext(self.config)

    def test_actual_context_sync_preserves_output_and_other_fields(self):
        self.enroll_opencode()
        def invoke(_payload):
            self.runtime.return_value = snapshot(B, used=21000)
            return {"status": "applied"}
        with patch.object(self.control, "_invoke", side_effect=invoke):
            result = self.control.apply("owner", self.preview()["confirmation"])
        expected = self.document
        backups = list(self.config.parent.glob(".opencode-context-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.document)
        expected["provider"]["ccslab"]["models"]["lab-general"]["limit"].update(context=32768)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), expected)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["model"]["gpu"]["used_mib"], 21000)
        sync = result["model"]["context_sync"]
        self.assertEqual(sync["builtin"]["context_limit"], 32768)
        self.assertEqual(sync["opencode"]["status"], "pending_reload")
        self.assertIsNone(sync["opencode"]["effective_context_limit"])
        # Status remains reconstructable after a later read, without a success cache.
        self.assertEqual(self.control.status()["context_sync"]["opencode"]["configured_context_limit"], 32768)

    def test_failed_file_sync_is_partial_and_same_target_retries_without_restart(self):
        from . import output_settings
        self.enroll_opencode()
        original = self.config.read_bytes()
        def invoke(_payload):
            self.runtime.return_value = snapshot(B)
            return {"status": "applied"}
        with patch.object(self.control, "_invoke", side_effect=invoke), \
                patch.object(output_settings, "_write_config", side_effect=PermissionError()):
            result = self.control.apply("owner", self.preview()["confirmation"])
        self.assertEqual(result["status"], "partially_applied")
        self.assertFalse(result["fully_synchronized"])
        self.assertEqual(result["configuration_errors"][0]["component"], "independent_opencode_file")
        self.assertIn("权限", result["configuration_errors"][0]["reason"])
        self.assertEqual(result["model"]["context_sync"]["opencode"]["status"], "error")
        self.assertEqual(self.config.read_bytes(), original)
        retry = self.preview()
        self.assertFalse(retry["restart_required"])
        with patch.object(self.control, "_invoke") as restart:
            self.assertEqual(self.control.apply("owner", retry["confirmation"])["status"], "applied")
            restart.assert_not_called()

    def test_context_only_sync_keeps_absent_input_owner_mode_and_avoids_noop_writes(self):
        self.enroll_opencode()
        limits = self.document["provider"]["ccslab"]["models"]["lab-general"]["limit"]
        limits.pop("input")
        original = json.dumps(self.document).encode()
        self.config.write_bytes(original)
        self.config.chmod(0o600)
        before = self.config.stat()
        self.control.context_store.sync(32768)
        after = self.config.stat()
        saved = json.loads(self.config.read_bytes())
        limits["context"] = 32768
        self.assertEqual(saved, self.document)
        if os.name == "posix":
            self.assertEqual((after.st_uid, after.st_gid, after.st_mode & 0o777),
                             (before.st_uid, before.st_gid, 0o600))
        backup = list(self.config.parent.glob(".opencode-context-backup-*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_bytes(), original)
        with patch.object(Path, "replace", side_effect=AssertionError("unnecessary write")):
            self.control.context_store.sync(32768)

    def test_unenrolled_decrease_and_more_slots_are_refused_before_any_change(self):
        from . import output_settings
        self.enroll_opencode()
        # Current actual capacity 32768. Both targets decrease per-slot capacity;
        # the second leaves total context unchanged and increases slot count.
        self.runtime.return_value = snapshot(B)
        original = self.config.read_bytes()
        for total, slots in ((32768, 2), (65536, 4)):
            for file_failure in (PermissionError(), None):
                with self.subTest(total=total, slots=slots, file_failure=type(file_failure).__name__), \
                        patch.object(self.control, "_invoke") as helper, \
                        patch.object(output_settings, "_write_config", side_effect=file_failure) as write:
                    with self.assertRaisesRegex(m.ModelControlError, "降容已阻止"):
                        self.control.preview("owner", total, slots)
                    helper.assert_not_called()
                    write.assert_not_called()
                    self.assertEqual(self.config.read_bytes(), original)
                    self.assertEqual(self.control.status()["total_context"], 65536)

    def test_same_target_repair_rechecks_health_slots_and_capacity(self):
        self.enroll_opencode()
        self.runtime.return_value = snapshot(B)
        for invalid in (dict(snapshot(B), status="unavailable"), dict(snapshot(B), slots=3),
                        dict(snapshot(B), _observed_slot_context=1)):
            token = self.preview()["confirmation"]
            self.runtime.return_value = invalid
            with patch.object(self.control, "_invoke") as helper:
                with self.assertRaises(m.ModelControlError):
                    self.control.apply("owner", token)
                helper.assert_not_called()
            self.runtime.return_value = snapshot(B)

    def test_other_admin_same_target_version_and_new_instance_invalidate_old_repair(self):
        self.enroll_opencode()
        self.runtime.return_value = snapshot(B)
        old_version = self.control.status()["configuration_version"]
        first = self.control.preview("first", B.total_context, B.slots, expected_version=old_version)
        second = self.control.preview("second", B.total_context, B.slots, expected_version=old_version)
        with patch.object(self.control, "_invoke") as helper:
            self.control.apply("second", second["confirmation"])
            with self.assertRaisesRegex(m.ModelControlError, "版本"):
                self.control.apply("first", first["confirmation"])
            with self.assertRaisesRegex(m.ModelControlError, "版本"):
                self.control.preview("first", B.total_context, B.slots, expected_version=old_version)
            token = self.preview()["confirmation"]
            self.runtime.return_value = dict(snapshot(B), _pid=5678)
            with self.assertRaisesRegex(m.ModelControlError, "实例"):
                self.control.apply("owner", token)
            helper.assert_not_called()

    def test_backend_rollback_or_unverified_success_never_changes_opencode(self):
        self.enroll_opencode()
        original = self.config.read_bytes()
        for result in ({"status": "rolled_back"}, {"status": "applied"}):
            with self.subTest(result=result), patch.object(self.control, "_invoke", return_value=result):
                with self.assertRaises(m.ModelControlError):
                    self.control.apply("owner", self.preview()["confirmation"])
                self.assertEqual(self.config.read_bytes(), original)

    def test_unmanaged_and_broken_config_are_truthful(self):
        from .output_settings import OpenCodeContext
        self.assertEqual(OpenCodeContext(None).status(32768)["status"], "unmanaged")
        self.enroll_opencode()
        for raw in ("{}", "invalid", '[]', '{"provider":null}'):
            self.config.write_text(raw, encoding="utf-8")
            self.assertEqual(self.control.status()["context_sync"]["opencode"]["status"], "error")
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.control.context_store.sync(32768)
            self.assertEqual(self.config.read_text(encoding="utf-8"), raw)
        self.config.unlink()
        self.assertEqual(self.control.status()["context_sync"]["opencode"]["status"], "error")

    def test_atomic_failure_and_overlapping_output_update_preserve_document(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from . import output_settings
        self.enroll_opencode()
        original = self.config.read_bytes()
        with patch.object(Path, "replace", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                self.control.context_store.sync(32768)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(list(self.config.parent.glob(".opencode-output-*")))
        entered, release, output_started = Event(), Event(), Event()
        write = output_settings._write_config
        def delayed_write(path, config):
            if config["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"] == 4096:
                entered.set()
                self.assertTrue(release.wait(5))
            return write(path, config)
        def output_update():
            output_started.set()
            return output_settings.write_output_limit(self.config, 8192)
        with patch.object(output_settings, "_write_config", side_effect=delayed_write), ThreadPoolExecutor(2) as pool:
            context = pool.submit(self.control.context_store.sync, 32768)
            self.assertTrue(entered.wait(5))
            output = pool.submit(output_update)
            try:
                self.assertTrue(output_started.wait(5))
            finally:
                release.set()
            context.result(5)
            self.assertEqual(output.result(5), 8192)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["provider"]["ccslab"]["models"]["lab-general"]["limit"],
                         {"context": 32768, "input": 20000, "output": 8192})


class FixtureHost:
    def __init__(self, directory, outcomes):
        self.config = Path(directory) / "current"
        self.backup = Path(directory) / "backup"
        self.journal = Path(directory) / "pending"
        self.config.write_bytes(host.settings(A.total_context, A.slots))
        self.restarts = 0
        self.outcomes = list(outcomes)

    def current(self):
        return snapshot()

    def snapshot(self):
        return self.config.read_bytes()

    def remember(self, saved, _payload):
        self.backup.write_bytes(saved)
        self.journal.touch()

    def apply_settings(self, contents):
        self.config.write_bytes(contents)

    def restart(self):
        self.restarts += 1
        if self.outcomes[0] == "restart_failure":
            self.outcomes.pop(0)
            raise subprocess.SubprocessError()

    def wait_verified(self, total_context, slots, old_pid):
        assert old_pid == 1234
        assert self.config.read_bytes() == host.settings(total_context, slots)
        return self.outcomes.pop(0)

    def clear_pending(self):
        self.journal.unlink()


class HostRecoveryTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "fixed helper file checks require POSIX")
    def test_helper_api_key_ascii_validation_does_not_compare_string_to_integer(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            key_file = Path(directory) / "api-key"
            key_file.write_text("fixture-key", encoding="ascii")
            key_file.chmod(0o600)
            self.assertEqual(host.api_key(key_file), "fixture-key")

            key_file.write_text("invalid key", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "invalid API key"):
                host.api_key(key_file)
            key_file.write_text("fixture-key", encoding="ascii")
            key_file.chmod(0o664)
            with self.assertRaisesRegex(ValueError, "untrusted API key"):
                host.api_key(key_file)

    def test_helper_request_accepts_only_fixed_integer_fields(self):
        self.assertEqual(host.parse_request(json.dumps(CHANGE).encode("ascii")), CHANGE)
        self.assertEqual(m.CONTEXT_OPTIONS, (32768, 65536, 131072, 147456, 163840, 196608))
        for context in m.CONTEXT_OPTIONS:
            self.assertEqual(host.parse_request(json.dumps(dict(CHANGE, new_context=context)).encode("ascii"))["new_context"], context)
        for invalid in (dict(CHANGE, path="/etc/passwd"), dict(CHANGE, new_slots=True),
                        dict(CHANGE, new_slots=11)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                host.parse_request(json.dumps(invalid).encode("ascii"))
        with self.assertRaises(ValueError):
            host.parse_request(b" " * 1025)

    def test_rejects_stale_baseline_before_writing(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            worker = FixtureHost(directory, [True])
            stale = dict(CHANGE, old_slots=4)
            self.assertEqual(host.run_change(worker, POLICY, stale)["status"], "refused")
            self.assertEqual(worker.config.read_bytes(), host.settings(A.total_context, A.slots))
            self.assertEqual(worker.restarts, 0)
            self.assertFalse(worker.journal.exists())

    def test_verified_new_context(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            worker = FixtureHost(directory, [True])
            self.assertEqual(host.run_change(worker, POLICY, CHANGE)["status"], "applied")
            self.assertEqual(worker.config.read_bytes(), host.settings(B.total_context, B.slots))
            self.assertEqual(worker.backup.read_bytes(), host.settings(A.total_context, A.slots))
            self.assertFalse(worker.journal.exists())
            self.assertEqual(worker.restarts, 1)

    def test_restore_on_failed_health_or_restart(self):
        for first in (False, "restart_failure"):
            with self.subTest(first=first), tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
                worker = FixtureHost(directory, [first, True])
                self.assertEqual(host.run_change(worker, POLICY, CHANGE)["status"], "rolled_back")
                self.assertEqual(worker.config.read_bytes(), host.settings(A.total_context, A.slots))
                self.assertFalse(worker.journal.exists())
                self.assertEqual(worker.restarts, 2)

    def test_preserve_pending_when_recovery_cannot_be_verified(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            worker = FixtureHost(directory, [False, False])
            self.assertEqual(host.run_change(worker, POLICY, CHANGE)["status"], "rollback_failed")
            self.assertEqual(worker.config.read_bytes(), host.settings(A.total_context, A.slots))
            self.assertTrue(worker.journal.exists())
            self.assertEqual(worker.restarts, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
