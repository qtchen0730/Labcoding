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
            self.assertEqual(host.api_key(key_file), "fixture-key")

            key_file.write_text("invalid key", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "invalid API key"):
                host.api_key(key_file)

    def test_helper_request_accepts_only_fixed_integer_fields(self):
        self.assertEqual(host.parse_request(json.dumps(CHANGE).encode("ascii")), CHANGE)
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
