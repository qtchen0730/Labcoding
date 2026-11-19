#!/usr/bin/python3 -I
"""Optional root-owned helper for a single, operator-enrolled llama.cpp systemd unit."""

import json
import os
from pathlib import Path
import re
import signal
import stat
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


def settings(total_context, slots):
    return (f"LLAMA_ARG_CTX_SIZE={total_context}\n"
            f"LLAMA_ARG_N_PARALLEL={slots}\n").encode("ascii")


def api_key(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 8192:
            raise ValueError("untrusted API key file")
        raw = stream.read(8193)
    secret = raw.decode("ascii").strip()
    if not secret or len(secret) > 8192 or not all(33 <= ord(ch) <= 126 for ch in secret):
        raise ValueError("invalid API key")
    return secret


class Host:
    def __init__(self, policy, token):
        self.policy = policy
        self.token = token

    def current(self):
        p = self.policy
        return control.read_runtime(p.llama_url, self.token, p.model_id, control.MODEL_UNIT, p.gpu_index,
                                    verify_environment=True)

    def snapshot(self):
        return control._root_file(control.CONTEXT_ENV)

    def remember(self, snapshot, payload):
        atomic_write(BACKUP, snapshot, 0o600)
        atomic_write(control.ROLLBACK_PENDING, json.dumps(payload).encode("utf-8"))

    def apply_settings(self, contents):
        atomic_write(control.CONTEXT_ENV, contents)

    def restart(self):
        control._fixed_command([control.SYSTEMCTL, "restart", "--", control.MODEL_UNIT], timeout=30)

    def wait_verified(self, total_context, slots, old_pid):
        deadline = monotonic() + 43
        while monotonic() < deadline:
            runtime = self.current()
            config = {"total_context": total_context, "slots": slots}
            if (runtime["status"] == "running" and runtime["_pid"] not in (None, old_pid)
                    and runtime["_managed_environment"] == config and runtime["_config"] == config
                    and runtime["total_context"] == total_context and runtime["slots"] == slots
                    and runtime["_uniform_context"]
                    and runtime["_observed_slot_context"] == control.slot_context(total_context, slots)):
                return True
            sleep(1)
        return False

    def clear_pending(self):
        control.ROLLBACK_PENDING.unlink()
        _sync_directory(control.ROLLBACK_PENDING.parent)


def run_change(host, policy, payload):
    """Return a sanitized outcome. Keep pending state unless apply or rollback is verified."""
    try:
        runtime = host.current()
        current, _ = control.validate_change(runtime, payload["new_context"], payload["new_slots"],
                                             require_idle=True)
        snapshot = host.snapshot()
        if (payload["policy"] != policy.digest
                or (payload["old_context"], payload["old_slots"])
                != (current["total_context"], current["slots"])
                or (current["total_context"], current["slots"]) == (payload["new_context"], payload["new_slots"])
                or runtime["_managed_environment"] != runtime["_config"]):
            raise ValueError("operation does not match policy")
        if control.context_env(snapshot) != current:
            raise ValueError("working configuration differs from environment snapshot")
        host.remember(snapshot, payload)
    except (OSError, control.ModelControlError, KeyError, TypeError, ValueError, Interrupted):
        return {"status": "refused", "reason": "policy_or_runtime_refused"}

    failure = ""
    try:
        host.apply_settings(settings(payload["new_context"], payload["new_slots"]))
        host.restart()
        if host.wait_verified(payload["new_context"], payload["new_slots"], runtime["_pid"]):
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
        if host.wait_verified(current["total_context"], current["slots"], current_pid):
            host.clear_pending()
            return {"status": "rolled_back", "reason": failure}
    except (OSError, subprocess.SubprocessError, ValueError, Interrupted):
        pass
    return {"status": "rollback_failed", "reason": failure or "restore_failed"}


def execute_change(payload):
    import fcntl
    # sudoers grants this fixed helper without arguments; only this validated payload is accepted.
    descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        policy = control.Policy.read(control.MODEL_SPEC)
        if control.ROLLBACK_PENDING.exists():
            return {"status": "refused", "reason": "previous_transaction_pending"}
        secret = api_key(policy.api_key_file)
        return run_change(Host(policy, secret), policy, payload)


def parse_request(envelope):
    if len(envelope) > 1024:
        raise ValueError("oversized model configuration request")
    payload = json.loads(envelope)
    if (not isinstance(payload, dict)
            or set(payload) != {"old_context", "old_slots", "new_context", "new_slots", "policy"}
            or any(type(payload[key]) is not int for key in ("old_context", "old_slots", "new_context", "new_slots"))
            or payload["old_context"] not in control.CONTEXT_OPTIONS
            or payload["new_context"] not in control.CONTEXT_OPTIONS
            or not control.MIN_SLOTS <= payload["old_slots"] <= control.MAX_SLOTS
            or not control.MIN_SLOTS <= payload["new_slots"] <= control.MAX_SLOTS
            or not isinstance(payload["policy"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", payload["policy"])):
        raise ValueError("invalid model configuration request")
    return payload


def main():
    import syslog
    result = {"status": "refused", "reason": "policy_or_runtime_refused"}
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise ValueError("requires root and no command arguments")
        payload = parse_request(sys.stdin.buffer.read(1025))
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        result = execute_change(payload)
    except (OSError, ValueError, TypeError, control.ModelControlError, Interrupted, RecursionError):
        pass
    syslog.syslog(syslog.LOG_NOTICE, f"ccslab model control: {result['status']} {result['reason']}")
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "applied" else 1


if __name__ == "__main__":
    sys.exit(main())
