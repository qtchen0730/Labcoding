"""Local isolation checks for the fixed-scope deployment entry."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import model_control_deploy as deploy


EXEC_START = ("/home/server/llm-service/llama.cpp/build/bin/llama-server "
              "-m /home/server/llm-service/models/model.gguf -ngl all -c 131072 -np 5 "
              "--host 172.17.0.1 --port 8000 --alias lab-general --api-key-file /etc/ccslab/key")
ARGV = EXEC_START.split()


class DeployEntryTests(unittest.TestCase):
    @staticmethod
    def _gateway_preflight_fixture(root, token="fixture-token"):
        key_file = root / "llama-api-key"
        key_file.write_text(token, encoding="utf-8")
        key_file.chmod(0o600)
        database = root / "gateway.sqlite"
        tables = ("users", "sessions", "auth_limits", "user_status_audit", "credential_audit",
                  "self_password_audit", "token_quotas", "token_requests", "token_quota_audit",
                  "model_confirmations", "model_configuration_audit")
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA user_version=2")
            for name in tables:
                db.execute(f'CREATE TABLE "{name}" (id INTEGER)')
        database.with_name(database.name + ".open-webui.argon2").write_bytes(b"fixture")
        env = {
            "CCSLAB_LLAMA_URL": deploy.LLAMA_URL,
            "CCSLAB_MODEL_ID": deploy.MODEL_ID,
            "CCSLAB_LLAMA_API_KEY_FILE": str(key_file),
            "CCSLAB_DATABASE": str(database),
            "CCSLAB_MODEL_GPU_INDEX": "0",
        }
        return env, database

    @unittest.skipUnless(sys.platform.startswith("linux")
                         and os.environ.get("CCSLAB_DEPLOY_LINUX_TEST") == "1",
                         "runs only in the isolated Linux VM")
    def test_linux_live_process_table_and_ssh_ancestry(self):
        # Uses the VM's real /usr/bin/ps and /proc; it never invokes deployment.
        deploy._assert_no_concurrent_maintenance()

    @staticmethod
    def _process_rows(extra=""):
        rows = {
            100: {"ppid": 1, "user": 0, "tty": "?", "state": "S", "command": "systemd"},
            200: {"ppid": 100, "user": 1002, "tty": "?", "state": "S", "command": "sshd"},
            300: {"ppid": 200, "user": 1002, "tty": "pts/4", "state": "S", "command": "bash"},
            400: {"ppid": 300, "user": 0, "tty": "pts/4", "state": "S", "command": "sudo"},
            500: {"ppid": 400, "user": 0, "tty": "pts/4", "state": "S", "command": "python3"},
            600: {"ppid": 200, "user": 1002, "tty": "pts/7", "state": "S", "command": "bash"},
        }
        if extra:
            pid, ppid, user, tty, state, command = extra.split()
            rows[int(pid)] = {"ppid": int(ppid), "user": user, "tty": tty,
                              "state": state, "command": command}
        return rows

    def test_session_check_allows_invoking_ssh_and_idle_other_sessions(self):
        with patch.object(deploy, "_read_process_table", return_value=self._process_rows()), \
                patch.object(deploy.os, "getpid", return_value=500):
            deploy._assert_no_concurrent_maintenance()

    def test_session_check_rejects_other_maintenance_process(self):
        rows = self._process_rows("700 600 1002 pts/7 S systemctl")
        with patch.object(deploy, "_read_process_table", return_value=rows), \
                patch.object(deploy.os, "getpid", return_value=500), \
                self.assertRaisesRegex(deploy.DeployError, "maintenance operation"):
            deploy._assert_no_concurrent_maintenance()

    def test_session_check_rejects_unclassifiable_other_tty_work(self):
        rows = self._process_rows("700 600 1002 pts/7 S opencode")
        with patch.object(deploy, "_read_process_table", return_value=rows), \
                patch.object(deploy.os, "getpid", return_value=500), \
                self.assertRaisesRegex(deploy.DeployError, "unclassifiable work"):
            deploy._assert_no_concurrent_maintenance()

    def test_session_check_does_not_ignore_other_work_on_invoking_tty(self):
        rows = self._process_rows("700 300 1002 pts/4 S python3")
        with patch.object(deploy, "_read_process_table", return_value=rows), \
                patch.object(deploy.os, "getpid", return_value=500), \
                self.assertRaisesRegex(deploy.DeployError, "unclassifiable work"):
            deploy._assert_no_concurrent_maintenance()

    def test_session_check_fails_closed_when_current_session_is_unknown(self):
        with patch.object(deploy, "_read_process_table", return_value=self._process_rows() | {500: None}), \
                patch.object(deploy.os, "getpid", return_value=500), \
                self.assertRaisesRegex(deploy.DeployError, "could not be identified"):
            deploy._assert_no_concurrent_maintenance()

    def test_failure_report_includes_stage_type_and_source_without_exception_text(self):
        def trigger():
            raise TypeError("secret-value-must-not-be-logged")

        try:
            trigger()
        except TypeError as error:
            message = deploy._failure_report("unit test phase", error)
        self.assertIn("stage=unit test phase", message)
        self.assertIn("exception=TypeError", message)
        self.assertIn("test_model_control_deploy.py:trigger:", message)
        self.assertNotIn("secret-value-must-not-be-logged", message)

    def test_malformed_model_list_is_a_clean_health_failure_not_typeerror(self):
        # Reproduce the former generator/filter ordering that iterated None first.
        malformed = {"data": None}
        with self.assertRaises(TypeError):
            any(isinstance(item, dict) and item.get("id") == "lab-general"
                for item in malformed.get("data", [])
                if isinstance(malformed.get("data"), list))
        responses = {
            "/health": {"status": "ok"},
            "/v1/models": {"data": None},
            "/props": {},
            "/slots": [],
        }
        with patch.object(deploy, "_api_get", side_effect=lambda _base, _token, path: responses[path]):
            self.assertIsNone(deploy._model_snapshot("http://localhost", "unused", "lab-general"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires POSIX file permission semantics")
    def test_gateway_route_preflight_accepts_string_api_key_and_sqlite_types(self):
        with tempfile.TemporaryDirectory() as temp:
            env, _database = self._gateway_preflight_fixture(Path(temp))
            with patch.object(deploy, "_proc_env", return_value=env):
                settings = deploy._read_gateway_settings(4493)
            self.assertEqual(settings[0:2], (deploy.LLAMA_URL, deploy.MODEL_ID))
            self.assertEqual(settings[3], "fixture-token")
            self.assertEqual(settings[4], 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires POSIX file permission semantics")
    def test_gateway_route_rejects_invalid_key_and_sqlite_result_types(self):
        class Cursor:
            def __init__(self, one=None, many=None):
                self.one, self.many = one, many
            def fetchone(self):
                return self.one
            def fetchall(self):
                return self.many

        class ReadOnlyDb:
            def __init__(self, version, journal, tables):
                self.rows = {"PRAGMA query_only=ON": Cursor(),
                             "PRAGMA user_version": Cursor(version),
                             "PRAGMA journal_mode": Cursor(journal),
                             "SELECT name FROM sqlite_master WHERE type='table'": Cursor(many=tables)}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def execute(self, query):
                return self.rows[query]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env, database = self._gateway_preflight_fixture(root)
            for token in ("snowman-☃", "bad\x01key"):
                env["CCSLAB_LLAMA_API_KEY_FILE"] = str(root / "bad-key")
                bad_key = root / "bad-key"
                bad_key.write_text(token, encoding="utf-8")
                bad_key.chmod(0o600)
                with patch.object(deploy, "_proc_env", return_value=env), \
                        self.assertRaises(deploy.DeployError):
                    deploy._read_gateway_settings(4493)

            env["CCSLAB_LLAMA_API_KEY_FILE"] = str(root / "llama-api-key")
            invalid_results = (
                (None, ("wal",), [("users",)]),
                ((2,), (None,), [("users",)]),
                ((2,), ("wal",), [("users",), None]),
            )
            for version, journal, tables in invalid_results:
                with self.subTest(version=version, journal=journal, tables=tables), \
                        patch.object(deploy, "_proc_env", return_value=env), \
                        patch.object(deploy.sqlite3, "connect",
                                     return_value=ReadOnlyDb(version, journal, tables)), \
                        self.assertRaises(deploy.DeployError):
                    deploy._read_gateway_settings(4493)

    def test_gateway_health_rejects_non_object_json(self):
        response = SimpleNamespace(status=200, read=lambda _limit: b"[]")
        connection = SimpleNamespace(request=lambda *_args, **_kwargs: None,
                                     getresponse=lambda: response, close=lambda: None)
        with patch.object(deploy.http.client, "HTTPConnection", return_value=connection):
            self.assertFalse(deploy._gateway_health())

    def test_restore_backup_rejects_malformed_manifest_types(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp)
            (backup / "manifest.json").write_text(json.dumps({"files": None}), encoding="utf-8")
            with self.assertRaises(deploy.DeployError):
                deploy._restore_backup(backup)

    def test_cleanup_typeerror_does_not_mask_primary_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rule = root / "sudoers"
            rule.write_text("temporary rule", encoding="ascii")
            lock_path = root / "lock"
            descriptor_path = root / "fd"
            descriptor = deploy.os.open(descriptor_path, deploy.os.O_CREAT | deploy.os.O_RDWR, 0o600)
            primary = ValueError("primary failure")
            caught_primary = False
            with patch.object(deploy, "TEMP_RULE", rule), patch.object(deploy, "LOCK_PATH", lock_path), \
                    patch.object(deploy, "_revoke_temp_rule", side_effect=TypeError("cleanup failure")):
                try:
                    raise primary
                except ValueError as caught:
                    failures = deploy._cleanup_after_run(descriptor)
                    caught_primary = caught is primary
                self.assertTrue(caught_primary)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0][1], TypeError)
            self.assertFalse(lock_path.exists())

    def test_fixed_launch_edit_removes_only_context_flags(self):
        result = deploy.strip_managed_flags(EXEC_START)
        args = result.split()
        self.assertEqual(args, [arg for arg in ARGV if arg not in ("-c", "131072", "-np", "5")])
        self.assertIn("--api-key-file /etc/ccslab/key", result)
        self.assertEqual(deploy._dimensions(args), {})

    def test_launch_mismatch_and_duplicate_flags_fail_closed(self):
        for launch in (
            EXEC_START.replace("131072", "65536", 1),
            EXEC_START + " -np 5",
            EXEC_START.replace("-np 5", "-np 4"),
            EXEC_START.replace("/home/server/llm-service/llama.cpp/build/bin/llama-server", "/tmp/llama-server"),
            EXEC_START + " --extra=unsafe;token",
        ):
            with self.subTest(launch=launch), self.assertRaises(deploy.DeployError):
                deploy.strip_managed_flags(launch)

    def test_payload_is_exact_name_and_pinned_sha256(self):
        contents = {name: (name + " pinned bytes").encode("ascii") for name in deploy.PAYLOAD_SHA256}
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
        with patch.object(deploy, "PAYLOAD_SHA256", hashes):
            self.assertEqual(deploy._validate_payload_bytes(contents), contents)
            with self.assertRaises(deploy.DeployError):
                deploy._validate_payload_bytes(dict(contents, arbitrary=b"command"))
            corrupt = dict(contents, **{"model_host.py": b"modified"})
            with self.assertRaises(deploy.DeployError):
                deploy._validate_payload_bytes(corrupt)
            wrong_type = dict(contents, **{"model_host.py": "not bytes"})
            with self.assertRaises(deploy.DeployError):
                deploy._validate_payload_bytes(wrong_type)

    def test_pinned_payload_matches_deployed_gateway_sources(self):
        source_root = Path(__file__).resolve().parent
        for name, expected in deploy.PAYLOAD_SHA256.items():
            self.assertEqual(hashlib.sha256((source_root / name).read_bytes()).hexdigest(), expected)

    def test_user_payload_permissions_and_root_stage_ownership_are_enforced(self):
        valid_user_file = SimpleNamespace(st_mode=0o100644, st_uid=1002, st_size=100)
        self.assertTrue(deploy._payload_file_metadata_trusted(valid_user_file))
        for invalid in (
            SimpleNamespace(st_mode=0o100664, st_uid=1002, st_size=100),
            SimpleNamespace(st_mode=0o100644, st_uid=1001, st_size=100),
            SimpleNamespace(st_mode=0o100644, st_uid=1002, st_size=deploy.MAX_PAYLOAD + 1),
            SimpleNamespace(st_mode=0o040755, st_uid=1002, st_size=100),
        ):
            self.assertFalse(deploy._payload_file_metadata_trusted(invalid))
        self.assertTrue(deploy._root_staged_file_metadata_trusted(
            SimpleNamespace(st_mode=0o100400, st_uid=0)))
        self.assertFalse(deploy._root_staged_file_metadata_trusted(
            SimpleNamespace(st_mode=0o100600, st_uid=0)))
        self.assertFalse(deploy._root_staged_file_metadata_trusted(
            SimpleNamespace(st_mode=0o100400, st_uid=1002)))

    def test_backend_validation_does_not_import_from_gateway_tree(self):
        source = Path(deploy.__file__).read_text(encoding="utf-8")
        self.assertIn('spec_from_file_location("_ccslab_root_model_control", root_module_path)', source)
        self.assertNotIn('importlib.import_module("server.model_control")', source)

    def test_temporary_grant_revocation_is_exact_and_removes_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rule, entry = root / "sudoers", root / "deploy"
            rule.write_text(deploy.TEMP_RULE_TEXT, encoding="ascii")
            rule.chmod(0o440)
            entry.write_text("fixed entry", encoding="ascii")
            with patch.object(deploy, "TEMP_RULE", rule), patch.object(deploy, "DEPLOY_PATH", entry), \
                    patch.object(deploy, "_verify_sudo_rule"):
                # TemporaryDirectory files are owned by this test user, so emulate root metadata.
                real_lstat = Path.lstat

                def root_owned(path):
                    result = real_lstat(path)
                    if path in (rule, entry):
                        mode = 0o100440 if path == rule else 0o100755
                        return type("Stat", (), {"st_uid": 0, "st_gid": 0, "st_mode": mode})()
                    return result

                real_unlink = Path.unlink

                def root_unlink(path, *args, **kwargs):
                    if path in (rule, entry):
                        path.chmod(0o666)
                    return real_unlink(path, *args, **kwargs)

                with patch.object(Path, "lstat", root_owned), patch.object(Path, "unlink", root_unlink):
                    self.assertTrue(deploy._revoke_temp_rule())
            self.assertFalse(rule.exists())
            self.assertFalse(entry.exists())

    def test_temporary_grant_refuses_unexpected_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rule, entry = root / "sudoers", root / "deploy"
            rule.write_text("server ALL=(root) NOPASSWD: ALL\n", encoding="ascii")
            rule.chmod(0o440)
            entry.write_text("fixed entry", encoding="ascii")
            with patch.object(deploy, "TEMP_RULE", rule), patch.object(deploy, "DEPLOY_PATH", entry):
                real_lstat = Path.lstat

                def root_owned(path):
                    result = real_lstat(path)
                    if path in (rule, entry):
                        mode = 0o100440 if path == rule else 0o100755
                        return type("Stat", (), {"st_uid": 0, "st_gid": 0, "st_mode": mode})()
                    return result

                with patch.object(Path, "lstat", root_owned):
                    self.assertFalse(deploy._revoke_temp_rule())
            self.assertTrue(rule.exists())
            self.assertTrue(entry.exists())

    def test_dropins_keep_existing_service_arguments_except_managed_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            unit = Path(temp) / "llm.service"
            unit.write_text("[Service]\nEnvironmentFile=/etc/ccslab/llm.env\nExecStart=" + EXEC_START + "\n",
                            encoding="utf-8")
            with patch.object(deploy, "MODEL_UNIT_FILE", unit):
                model, gateway, spec = deploy._prepare_configs(
                    EXEC_START, deploy.LLAMA_URL, deploy.MODEL_ID, "/etc/ccslab/llama-api-key", 0,
                )
            self.assertIn(b"EnvironmentFile=/etc/ccslab/model-context.env", model)
            self.assertEqual(deploy._context_environment(),
                             b"LLAMA_ARG_CTX_SIZE=131072\nLLAMA_ARG_N_PARALLEL=5\n")
            self.assertNotIn(b"-c 131072", model)
            self.assertNotIn(b"-np 5", model)
            for argument in (b"-m /home/server/llm-service/models/model.gguf", b"-ngl all",
                             b"--host 172.17.0.1", b"--port 8000", b"--alias lab-general",
                             b"--api-key-file /etc/ccslab/key"):
                self.assertIn(argument, model)
            self.assertIn(b"NoNewPrivileges=no", gateway)
            spec_data = json.loads(spec)
            self.assertFalse(spec_data["enabled"])
            self.assertEqual((spec_data["llama_url"], spec_data["model_id"], spec_data["gpu_index"]),
                             (deploy.LLAMA_URL, deploy.MODEL_ID, 0))
            self.assertNotIn(b"fixture-token", spec)

    def test_backup_restore_recovers_files_and_removes_new_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "unit.service"
            original.write_bytes(b"original unit")
            created = root / "new-control.json"
            created.write_bytes(b"new config")
            backup = root / "backup"
            (backup / "files").mkdir(parents=True)
            saved = b"original unit"
            (backup / "files/0").write_bytes(saved)
            metadata = {
                "baseline": deploy.BASELINE,
                "runtime": {},
                "files": [{"path": str(original), "backup": "0", "mode": 0o600,
                           "uid": 0, "gid": 0, "sha256": hashlib.sha256(saved).hexdigest()}],
                "absent_dirs": [],
            }
            (backup / "manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
            with patch.multiple(deploy, NEW_FILES=(created,), NEW_DIRS=(), BACKUP_FILES=(original,)), \
                    patch.object(deploy, "_atomic_write",
                                 side_effect=lambda path, body, _mode, *_owner: Path(path).write_bytes(body)), \
                    patch.object(deploy, "_run"):
                deploy._restore_backup(backup)
            self.assertEqual(original.read_bytes(), b"original unit")
            self.assertFalse(created.exists())

    def test_fixed_privilege_and_target_boundaries(self):
        self.assertEqual(deploy.MODEL_UNIT, "ccslab-llm.service")
        self.assertEqual(deploy.GATEWAY_UNIT, "ccslab-gateway.service")
        self.assertEqual(deploy.BASELINE, {"total_context": 131072, "slots": 5, "slot_context": 26368})
        self.assertIn('""', deploy.TEMP_RULE_TEXT)
        self.assertNotIn("ALL", deploy.TEMP_RULE_TEXT.split("NOPASSWD:", 1)[1].split(":", 1)[0])
        for arg in ("bash", "cp", "tee", "python", "systemctl"):
            self.assertNotIn("NOPASSWD: " + arg, deploy.TEMP_RULE_TEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
