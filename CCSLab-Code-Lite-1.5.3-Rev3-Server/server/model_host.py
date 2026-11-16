#!/usr/bin/python3 -I
"""Optional root-owned helper for a single, operator-enrolled llama.cpp systemd unit."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from time import monotonic, sleep

if not __package__ and str(Path(__file__)) == "/usr/local/sbin/ccslab-model-controller":
    sys.path.insert(0, "/usr/local/lib/ccslab-model-control")

if __package__:
    from . import model_control as control
else:
    import model_control as control


BACKUP = Path("/etc/ccslab/model-context.previous")
LOCK_FILE = Path("/etc/ccslab/model-change.lock")


class Interrupted(Exception):
    pass


def interrupt(_number, _frame):
    raise Interrupted()


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, contents, mode=0o644):
    descriptor, name = tempfile.mkstemp(prefix=".model-change-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def settings(profile):
    return (f"LLAMA_ARG_CTX_SIZE={profile.total_context}\n"
            f"LLAMA_ARG_N_PARALLEL={profile.slots}\n").encode("ascii")


class Host:
    def __init__(self, policy, token):
        self.policy = policy
        self.token = token

    def current(self):
        p = self.policy
        return control.read_runtime(p.llama_url, self.token, p.model_id, p.unit, p.gpu_index,
                                    verify_environment=True)

    def snapshot(self):
        return control._root_file(control.CONTEXT_ENV)

    def remember(self, snapshot, payload):
        atomic_write(BACKUP, snapshot, 0o600)
        atomic_write(control.ROLLBACK_PENDING, json.dumps(payload).encode("utf-8"))

    def apply_settings(self, contents):
        atomic_write(control.CONTEXT_ENV, contents)

    def restart(self):
        control._fixed_command([control.SYSTEMCTL, "restart", "--", self.policy.unit], timeout=30)

    def wait_verified(self, profile, old_pid):
        deadline = monotonic() + 43
        while monotonic() < deadline:
            runtime = self.current()
            config = {"total_context": profile.total_context, "slots": profile.slots}
            gpu = runtime["gpu"] or {}
            model_mib = gpu.get("_model_mib")
            if (runtime["status"] == "running" and runtime["_pid"] not in (None, old_pid)
                    and runtime["_managed_environment"] == config and runtime["_config"] == config
                    and runtime["slots"] == profile.slots
                    and runtime["_uniform_context"] and runtime["_observed_slot_context"] == profile.slot_context
                    and gpu.get("_uuid") == self.policy.gpu_uuid and type(model_mib) is int
                    and 0 < model_mib <= profile.peak_model_mib
                    and gpu["used_mib"] + self.policy.reserve_mib <= gpu["total_mib"]):
                return True
            sleep(1)
        return False

    def clear_pending(self):
        control.ROLLBACK_PENDING.unlink()
        _sync_directory(control.ROLLBACK_PENDING.parent)


def run_change(host, policy, payload):
    """Return a sanitized outcome. Keep pending state unless apply or rollback is verified."""
    try:
        current = policy.profiles[payload["old_context"], payload["old_slots"]]
        requested = policy.profiles[payload["new_context"], payload["new_slots"]]
        runtime = host.current()
        verified, _ = control.validate_change(policy, runtime, requested, require_idle=True)
        snapshot = host.snapshot()
        if (payload["policy"] != policy.digest or current == requested or verified != current
                or runtime["_managed_environment"] != runtime["_config"]):
            raise ValueError("operation does not match policy")
        if control.context_env(snapshot) != {"total_context": current.total_context, "slots": current.slots}:
            raise ValueError("working configuration differs from environment snapshot")
        host.remember(snapshot, payload)
    except (OSError, control.ModelControlError, KeyError, TypeError, ValueError, Interrupted):
        return {"status": "refused", "reason": "policy_or_runtime_refused"}

    failure = ""
    try:
        host.apply_settings(settings(requested))
        host.restart()
        if host.wait_verified(requested, runtime["_pid"]):
            host.clear_pending()
            return {"status": "applied", "reason": ""}
        failure = "health_or_dimensions_failed"
    except (OSError, subprocess.SubprocessError, ValueError):
        failure = "restart_failed"
    except Interrupted:
        failure = "interrupted"

    try:
        current_pid = host.current()["_pid"]
        host.apply_settings(snapshot)
        host.restart()
        if host.wait_verified(current, current_pid):
            host.clear_pending()
            return {"status": "rolled_back", "reason": failure}
    except (OSError, subprocess.SubprocessError, ValueError, Interrupted):
        pass
    return {"status": "rollback_failed", "reason": failure or "restore_failed"}


def main():
    import fcntl
    import syslog

    result = {"status": "refused", "reason": "policy_or_runtime_refused"}
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise ValueError("requires root and no command arguments")
        envelope = sys.stdin.buffer.read(1025)
        payload = json.loads(envelope)
        if (len(envelope) > 1024 or not isinstance(payload, dict)
                or set(payload) != {"old_context", "old_slots", "new_context", "new_slots", "policy"}
                or not all(type(payload[key]) is int for key in ("old_context", "old_slots", "new_context", "new_slots"))
                or not isinstance(payload["policy"], str) or len(payload["policy"]) != 64):
            raise ValueError("invalid request")
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if control.ROLLBACK_PENDING.exists():
                result = {"status": "refused", "reason": "previous_transaction_pending"}
            elif control.maintenance_open():
                policy = control.Policy.read(control.MODEL_SPEC)
                secret = control._root_file(control.LLAMA_API_KEY).decode("ascii").strip()
                if not secret or len(secret) > 8192 or not all(33 <= ord(ch) <= 126 for ch in secret):
                    raise ValueError("invalid llama credential")
                result = run_change(Host(policy, secret), policy, payload)
    except (OSError, ValueError, TypeError, control.ModelControlError, Interrupted, RecursionError):
        pass
    syslog.syslog(syslog.LOG_NOTICE, f"ccslab model control: {result['status']} {result['reason']}")
    print(json.dumps(result))
    return 0 if result["status"] == "applied" else 1


if __name__ == "__main__":
    sys.exit(main())
