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
    inspection = payload.get('operation') == 'inspect'
    try:
        descriptor = os.open(LOCK_FILE, os.O_RDONLY | os.O_NOFOLLOW) if inspection else os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError:
        if inspection:return dict(status='inspected', reason='sample_missing', sample_status='missing', estimate=None)
        raise
    with os.fdopen(descriptor, "rb" if inspection else "wb") as lock:
        fcntl.flock(lock, (fcntl.LOCK_SH if inspection else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        policy = control.Policy.read(control.MODEL_SPEC)
        if inspection:
            state = control.model_service.read_state(control._root_file)
            if payload['policy'] != policy.digest or payload['expected_generation'] != (state or {}).get('generation', 0):
                return dict(status='inspected', reason='state_changed', sample_status='unverified', estimate=None)
            worker = ServiceHost(policy, '')  # Inspect does not contact the model API or load its key.
            result = control.model_service.startup_assessment(worker, state, control.context_env(worker.snapshot()), policy)
            return dict(status='inspected', **{k:result[k] for k in ('estimate', 'sample_status', 'reason')})
        if 'operation' in payload:
            return run_service(ServiceHost(policy, api_key(policy.api_key_file)), policy, payload)
        if control.model_service.held(control.model_service.read_state(control._root_file)):
            return {'status': 'refused', 'reason': 'previous_transaction_pending'}
        if control.ROLLBACK_PENDING.exists() or control.MODEL_MAINTENANCE.exists():
            return {"status": "refused", "reason": "previous_transaction_pending"}
        secret = api_key(policy.api_key_file)
        return run_change(Host(policy, secret), policy, payload)


def parse_request(envelope):
    if len(envelope) > 1024:
        raise ValueError("oversized model configuration request")
    payload = json.loads(envelope)
    if isinstance(payload, dict) and 'operation' in payload:
        if (set(payload) != {'operation', 'policy', 'expected_generation', 'operation_id'}
                or not isinstance(payload['operation'], str) or payload['operation'] not in {'start', 'stop', 'restart', 'inspect'}
                or type(payload['expected_generation']) is not int or payload['expected_generation'] < 0
                or any(not isinstance(payload[k], str) or not re.fullmatch(r'[a-f0-9]{64}', payload[k])
                       for k in ('policy', 'operation_id'))):
            raise ValueError('invalid fixed service request')
        return payload
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


class ServiceHost(Host):
    """Uses the same fixed unit, helper flock and direct-request fence as configuration changes."""
    def service(self, operation):
        if operation not in {'start', 'stop'}:
            raise ValueError('invalid service verb')
        control._fixed_command([control.SYSTEMCTL, operation, '--', control.MODEL_UNIT], timeout=45)

    def unit(self):
        return control.model_service.unit_status(control)

    def unit_profile(self):
        return self.configuration_identity()['fingerprint']

    def configuration_identity(self, gpu=None):
        return control.model_service.configuration_identity(control, self.policy, self.snapshot(), gpu or self.gpu())

    def sample_record(self):
        try:
            value = json.loads(control._root_file(control.model_service.SAMPLE_RECORD))
            return value if isinstance(value, dict) else None
        except FileNotFoundError:return None

    def store_sample(self, sample):
        atomic_write(control.model_service.SAMPLE_RECORD, json.dumps(sample).encode(), 0o600)

    def save_state(self, state):
        atomic_write(control.model_service.STATE, json.dumps(state).encode(), 0o644)

    def gpu(self, pid=None):
        return control._read_gpu(self.policy.gpu_index, pid)

    def fence(self, previous):
        self.prepare_guard()
        if previous and previous.get('guard') and control.MODEL_MAINTENANCE.exists():
            # Only reattach the root-recorded fence; never adopt context recovery.
            if (control._root_file(control.MODEL_MAINTENANCE) != b'ccslab fixed model maintenance\n'
                    or control.ROLLBACK_PENDING.exists()):
                raise MaintenanceError('guard_failed')
            raw = control._fixed_command([NFT, '-j', 'list', 'table', 'inet', NFT_TABLE])
            entries = json.loads(raw)['nftables']
            chains = [e['chain'] for e in entries if 'chain' in e]
            rules = [e['rule'] for e in entries if 'rule' in e]
            def fixed_port(rule):
                return any(e.get('match') == {'op': '==', 'left': {'payload': {'protocol': 'tcp', 'field': 'dport'}},
                                             'right': 8000} for e in rule.get('expr', []))
            if ({c.get('name') for c in chains} != {'input', 'output'} or len(rules) != 2
                    or any(c.get('table') != NFT_TABLE or c.get('hook') != c.get('name')
                           or c.get('prio') != -150 or c.get('policy') != 'accept' for c in chains)
                    or any(r.get('table') != NFT_TABLE or not fixed_port(r)
                           or 'reject' not in json.dumps(r.get('expr')) for r in rules)):
                raise MaintenanceError('guard_failed')
            self.guard.installed = self.guard.marked = True
            self.guard.marker = control.MODEL_MAINTENANCE
        else:
            self.guard.start()

    def stopped(self, old_pid):
        deadline = monotonic() + 30
        while monotonic() < deadline:
            unit = self.unit()
            if (unit.get('ActiveState') in {'inactive', 'failed'} and unit.get('MainPID') == '0'
                    and (not old_pid or not Path(f'/proc/{old_pid}').exists())
                    and control._llama_get(self.policy.llama_url, self.token, '/health') is None):
                return True
            sleep(.25)
        return False


def run_service(host, policy, payload):
    service = control.model_service
    previous = service.read_state(control._root_file)
    generation = (previous or {}).get('generation', 0)
    operation = payload['operation']
    if (payload['policy'] != policy.digest or generation != payload['expected_generation']
            or control.ROLLBACK_PENDING.exists() or control.DEPLOYMENT_HOLD.exists()
            or (previous or {}).get('phase') in {'starting', 'stopping', 'unknown'}):
        return dict(status='refused', reason='state_changed')
    state = dict(previous or {}, generation=generation + 1, operation_id=payload['operation_id'],
                 reason='', guard=True, desired='stopped', phase='stopping')
    fenced = False
    changed = False
    starting = operation == 'start'
    intent_written = False
    recovered_sample = False
    try:
        # Persist admission intent before telemetry/preflight. A Gateway restarted
        # during this interval must not admit new leases or auto-start the model.
        host.save_state(dict(state, phase='starting' if starting else 'stopping',
                             guard=bool((previous or {}).get('guard'))))
        intent_written = True
        saved = control.context_env(host.snapshot())
        before = host.current()
        unit = host.unit()
        if operation in {'stop', 'restart'}:
            if service.held(previous) or before['status'] != 'running' or before.get('_active_slots') != 0:
                raise MaintenanceError('busy')
            control.validate_change(before, saved['total_context'], saved['slots'], require_idle=True)
            gpu = before.get('gpu') or {}
            usage = gpu.get('model_process_mib') or gpu.get('_model_mib')
            if type(usage) is not int or usage <= 0:raise MaintenanceError('sample_unavailable')
            identity = host.configuration_identity(gpu)
            execution = identity.get('_execution') or {}
            if (execution.get('pid') != before['_pid'] or execution.get('start_epoch') is None
                    or identity.get('_latest_change', 0) > execution['start_epoch'] + 1):
                raise MaintenanceError('profile_mismatch')
            state['sample'] = service.make_sample(usage, gpu.get('sampled_at', control.time()), saved, policy, identity)
            # Both root-owned copies must exist BEFORE stopping the model.
            host.store_sample(state['sample'])
            state.update(model_name=before.get('model_name'), last_started_at=unit.get('ExecMainStartTimestamp'))
            if operation == 'restart':
                assessment = service.startup_assessment(host, state, saved, policy, reclaim_mib=usage)
                if assessment['reason']:raise MaintenanceError(assessment['reason'])
        elif unit.get('ActiveState') not in {'inactive', 'failed'} or before.get('_pid'):
            raise MaintenanceError('state_changed')
        # Read-only startup preflight before changing a stopped service.
        if operation == 'start':
            assessment = check_start_memory(host, previous, saved, policy)
            recovered_sample = assessment['estimate'].get('source') != 'state'
            state['sample'] = assessment['sample']
            host.store_sample(state['sample'])
        try:
            host.fence(previous)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            raise MaintenanceError('guard_failed') from error
        fenced = True
        if operation in {'stop', 'restart'}:
            check = host.current()
            if check.get('_active_slots') != 0 or check.get('_pid') != before.get('_pid'):
                raise MaintenanceError('busy')
            host.save_state(state)
            changed = True
            host.service('stop')
            if not host.stopped(before['_pid']):
                raise MaintenanceError('stop_failed')
            after = host.gpu()
            state.update(phase='stopped', gpu_after_stop=public_gpu(after),
                         observed_released_mib=(gpu['used_mib'] - after['used_mib'] if after and 'used_mib' in gpu else None))
            host.save_state(state)
            if operation == 'stop':
                return dict(status='applied', reason='')
        starting = True
        assessment = check_start_memory(host, state, saved, policy)
        state['sample'] = assessment['sample']
        host.store_sample(state['sample'])
        state.update(phase='starting', desired='running')
        host.save_state(state)
        changed = True
        host.service('start')
        if not host.wait_verified(saved['total_context'], saved['slots'], None):
            raise MaintenanceError('start_failed')
        live = host.current()
        if live.get('_active_slots') != 0:
            raise MaintenanceError('start_failed')
        state.update(phase='running', desired='running', last_started_at=host.unit().get('ExecMainStartTimestamp'),
                     ready_checked_at=control.time(), reason='')
        # Keep the persistent gate until the direct fence is removed successfully.
        host.save_state(state)
        host.end_maintenance()
        state['guard'] = False
        host.save_state(state)
        return dict(status='applied', reason='sample_recovered' if recovered_sample else '')
    except (OSError, ValueError, KeyError, TypeError, control.ModelControlError, subprocess.SubprocessError, Interrupted) as error:
        reason = getattr(error, 'reason', 'start_failed' if starting else 'stop_failed')
        if starting and changed:
            try:
                if host.guard is not None and not host.guard.installed:
                    host.fence(state)
                # Only the just-started, fenced fixed unit; no user/GPU process signals.
                host.service('stop')
                if not host.stopped(host.current().get('_pid')):
                    starting = False
                    reason = 'stop_failed'
            except (OSError, ValueError, subprocess.SubprocessError, Interrupted):
                starting = False
                reason = 'stop_failed'
        if changed:
            state.update(phase='start_failed' if starting else 'error', desired='stopped', reason=reason, guard=True)
            try:
                host.save_state(state)
            except OSError:
                pass  # Existing non-running root record and fence stay fail-closed.
        elif fenced:
            if not service.held(previous):
                try:
                    host.end_maintenance()
                except (OSError, ValueError):
                    state.update(phase='error', reason='guard_failed')
                    host.save_state(state)
        elif getattr(host, 'guard', None) and host.guard.marked and not service.held(previous):
            state.update(phase='error', reason='guard_failed')
            host.save_state(state)
        if intent_written and not changed and not (getattr(host, 'guard', None) and host.guard.marked and not service.held(previous)):
            # Refused before touching the service: restore admission, keeping the
            # generation so old confirmations cannot survive an intervening action.
            restored = dict(previous or dict(phase='running' if host.unit().get('ActiveState') == 'active' else 'stopped',
                                             desired='running' if host.unit().get('ActiveState') == 'active' else 'stopped', guard=False))
            restored.update(generation=state['generation'], reason=reason if service.held(previous) else '')
            host.save_state(restored)
        return dict(status='refused', reason=reason)


def public_gpu(gpu):
    return {k: v for k, v in gpu.items() if not k.startswith('_')} if gpu else None


def check_start_memory(host, state, saved, policy):
    assessment = control.model_service.startup_assessment(host, state, saved, policy)
    if assessment['reason']:raise MaintenanceError(assessment['reason'])
    return assessment


def main():
    import syslog
    result = {"status": "refused", "reason": "policy_or_runtime_refused"}
    inspection = False
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise ValueError("requires root and no command arguments")
        payload = parse_request(sys.stdin.buffer.read(1025))
        inspection = payload.get('operation') == 'inspect'
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        result = execute_change(payload)
    except (OSError, ValueError, TypeError, control.ModelControlError, Interrupted, RecursionError):
        pass
    if not inspection:
        syslog.syslog(syslog.LOG_NOTICE, f"ccslab model control: {result['status']} {result['reason']}")
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] in {"applied", "inspected"} else 1


if __name__ == "__main__":
    sys.exit(main())
