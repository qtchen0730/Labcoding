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
NFT = "/usr/sbin/nft"
NFT_TABLE = "ccslab_context_maintenance"


class MaintenanceError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def opencode_processes(uid, proc=Path("/proc")):
    """PID-only evidence; no command lines, keys, session data or process signals."""
    try:
        result = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == uid:
                    executable = str((entry / "exe").readlink()).removesuffix(" (deleted)")
                    if Path(executable).name.startswith("opencode"):
                        result.append(int(entry.name))
            except FileNotFoundError:  # exited / zombie, cannot submit a request
                continue
        return result
    except OSError as error:
        raise MaintenanceError("process_check_failed") from error


class DirectRequestGuard:
    """Only the host model's fixed TCP port; an independent ephemeral nft table."""
    def __init__(self):
        self.installed = self.marked = False

    @staticmethod
    def command(script):
        try:
            subprocess.run([NFT, "-f", "-"], input=script, encoding="ascii", check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            raise MaintenanceError("direct_guard_failed") from error

    def start(self):
        if self.installed:
            return
        if not self.marked:
            if control.MODEL_MAINTENANCE.exists():
                raise MaintenanceError("direct_guard_failed")
            self.marker = control.MODEL_MAINTENANCE
            atomic_write(self.marker, b"ccslab fixed model maintenance\n", 0o600)
            self.marked = True
        # inet covers IPv4 and IPv6. Root-local probes remain possible; all
        # unprivileged local and all remote traffic to the host port is rejected.
        # "create" refuses an existing table; never edit somebody else's rules.
        self.command(f"""create table inet {NFT_TABLE}
add chain inet {NFT_TABLE} output {{ type filter hook output priority -150; policy accept; }}
add rule inet {NFT_TABLE} output fib daddr type local meta skuid != 0 tcp dport 8000 reject with tcp reset
add chain inet {NFT_TABLE} input {{ type filter hook input priority -150; policy accept; }}
add rule inet {NFT_TABLE} input iifname != "lo" tcp dport 8000 reject with tcp reset
""")
        self.installed = True

    def stop(self):
        if self.installed:
            try:
                self.command(f"delete table inet {NFT_TABLE}\n")
            except MaintenanceError as error:
                raise MaintenanceError("direct_guard_release_failed") from error
            self.installed = False
        if self.marked:
            self.marker.unlink()
            self.marked = False


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
        self.guard = None
        self.managed_before = None
        self.managed_target = None

    def managed_config(self, context=None, allowed=None):
        """Drop privileges before touching the server-owned JSON or its parents."""
        import pwd
        account = pwd.getpwnam("server")
        path = self.policy.managed_opencode_config
        if path is None:
            raise MaintenanceError("maintenance_not_enrolled")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                if os.geteuid() == 0:
                    os.setgroups([])
                    os.setgid(account.pw_gid)
                    os.setuid(account.pw_uid)
                if os.geteuid() != account.pw_uid:
                    raise PermissionError("unexpected managed uid")
                os.umask(0o077)
                if __package__:
                    from .output_settings import OpenCodeContext
                else:
                    from output_settings import OpenCodeContext
                store = OpenCodeContext(path)
                _, limits = store._read()
                info = path.stat()
                if info.st_uid != account.pw_uid or info.st_mode & 0o777 != 0o600:
                    raise PermissionError("managed config ownership/mode")
                before = limits.get("context")
                output = limits.get("output")
                if type(before) is not int or before < 1024 or type(output) is not int or output <= 0:
                    raise ValueError("invalid managed limits")
                if context is not None:
                    if before not in allowed:
                        raise ValueError("independent context changed concurrently")
                    store.sync(context)
                    if store.status(context)["status"] != "pending_reload":
                        raise ValueError("managed readback mismatch")
                    before = context
                result = {"ok": True, "context": before, "output": output}
            except BaseException as error:
                reason = ("managed_config_permission" if isinstance(error, PermissionError) else
                          "managed_config_missing" if isinstance(error, FileNotFoundError) else
                          "managed_config_io" if isinstance(error, OSError) else "managed_config_failed")
                result = {"ok": False, "reason": reason}
            try:
                os.write(write_fd, json.dumps(result).encode("ascii"))
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            with os.fdopen(read_fd, "rb") as stream:
                result = json.loads(stream.read(4096))
        except (OSError, ValueError) as error:
            raise MaintenanceError("managed_config_failed") from error
        finally:
            _, status = os.waitpid(pid, 0)
        if status != 0 or result.get("ok") is not True:
            reason = result.get("reason")
            raise MaintenanceError(reason if reason in control.REASONS else "managed_config_failed")
        return result

    def check_closed(self):
        import pwd
        if opencode_processes(pwd.getpwnam("server").pw_uid):
            raise MaintenanceError("independent_running")

    def prepare_guard(self):
        # This guard implementation is deliberately fixed, never client-selected.
        if control.normalized_llama_url(self.policy.llama_url) != "http://172.17.0.1:8000":
            raise MaintenanceError("direct_guard_failed")
        self.guard = DirectRequestGuard()

    def prepare_decrease(self, target, current_context):
        self.check_closed()
        values = self.managed_config()
        if values["context"] != current_context:
            raise MaintenanceError("managed_config_mismatch")
        if values["output"] >= target:
            raise MaintenanceError("managed_config_failed")
        self.managed_before, self.managed_target = values["context"], target
        self.prepare_guard()

    def begin_maintenance(self, runtime, require_closed):
        self.guard.start()
        if require_closed:
            self.check_closed()
        live = self.current()
        if live.get("_active_slots") != 0:
            raise MaintenanceError("model_busy")
        control.validate_change(live, live["total_context"], live["slots"], require_idle=True)
        if live["_pid"] != runtime["_pid"] or live["_config"] != runtime["_config"]:
            raise MaintenanceError("policy_or_runtime_refused")

    def sync_decrease(self):
        self.managed_config(self.managed_target, {self.managed_before})
        self.check_closed()

    def restore_decrease(self):
        self.managed_config(self.managed_before, {self.managed_before, self.managed_target})

    def end_maintenance(self):
        self.guard.stop()

    def current(self):
        p = self.policy
        return control.read_runtime(p.llama_url, self.token, p.model_id, control.MODEL_UNIT, p.gpu_index,
                                    verify_environment=True)

    def snapshot(self):
        return control._root_file(control.CONTEXT_ENV)

    def remember(self, snapshot, payload):
        atomic_write(BACKUP, snapshot, 0o600)
        journal = dict(payload)
        if self.managed_before is not None:
            journal["independent_context_before"] = self.managed_before
        atomic_write(control.ROLLBACK_PENDING, json.dumps(journal).encode("utf-8"))

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
    decreasing = False
    guarded = False
    try:
        runtime = host.current()
        if runtime.get("_active_slots") != 0:
            raise MaintenanceError("model_busy")
        current, target = control.validate_change(runtime, payload["new_context"], payload["new_slots"],
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
        decreasing = target < runtime["_observed_slot_context"]
        guarded = decreasing or policy.managed_opencode_config is not None
        if decreasing:
            host.prepare_decrease(target, runtime["_observed_slot_context"])
        elif guarded:
            host.prepare_guard()
        host.remember(snapshot, payload)
    except (OSError, control.ModelControlError, KeyError, TypeError, ValueError, Interrupted) as error:
        return {"status": "refused", "reason": getattr(error, "reason", "policy_or_runtime_refused")}

    if guarded:
        try:
            host.begin_maintenance(runtime, require_closed=decreasing)
        except (OSError, control.ModelControlError, ValueError, Interrupted) as error:
            reason = getattr(error, "reason", "policy_or_runtime_refused")
            try:
                host.end_maintenance()
                host.clear_pending()
                return {"status": "refused", "reason": reason}
            except (OSError, ValueError, Interrupted):
                return {"status": "rollback_failed", "reason": "direct_guard_release_failed"}

    failure = ""
    try:
        host.apply_settings(settings(payload["new_context"], payload["new_slots"]))
        host.restart()
        if host.wait_verified(payload["new_context"], payload["new_slots"], runtime["_pid"]):
            if decreasing:
                host.sync_decrease()
            host.clear_pending()
            if guarded:
                try:
                    host.end_maintenance()
                except (OSError, ValueError, Interrupted):
                    host.remember(snapshot, payload)
                    raise MaintenanceError("direct_guard_release_failed")
            return {"status": "applied", "reason": ""}
        failure = "health_or_dimensions_failed"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        failure = getattr(error, "reason", "restart_failed")
    except Interrupted:
        failure = "interrupted"

    backend_restored = False
    protected = not guarded
    try:
        host.remember(snapshot, payload)
        if guarded:
            host.guard.start()
            protected = True
        current_pid = host.current()["_pid"]
        host.apply_settings(snapshot)
        host.restart()
        backend_restored = host.wait_verified(current["total_context"], current["slots"], current_pid)
    except (OSError, subprocess.SubprocessError, ValueError, Interrupted):
        pass
    file_restored = not decreasing
    if decreasing and protected:
        try:
            host.restore_decrease()
            file_restored = True
        except (OSError, ValueError, Interrupted):
            pass
    if backend_restored and file_restored:
        try:
            host.clear_pending()
            if guarded:
                try:
                    host.end_maintenance()
                except (OSError, ValueError, Interrupted):
                    host.remember(snapshot, payload)
                    raise
            return {"status": "rolled_back", "reason": failure}
        except (OSError, ValueError, Interrupted):
            pass
    return {"status": "rollback_failed", "reason": failure or "restore_failed"}


def execute_change(payload):
    import fcntl
    # sudoers grants this fixed helper without arguments; only this validated payload is accepted.
    descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        policy = control.Policy.read(control.MODEL_SPEC)
        if control.ROLLBACK_PENDING.exists() or control.MODEL_MAINTENANCE.exists():
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
