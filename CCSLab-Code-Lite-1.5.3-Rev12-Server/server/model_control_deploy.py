#!/usr/bin/python3 -I
"""One-shot, fixed-scope installer for the Labcoding model-control feature."""

import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sqlite3
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Windows is used only for local unit tests, never deployment.
    fcntl = None
try:
    import pwd
except ImportError:  # Windows is used only for local unit tests, never deployment.
    pwd = None


DEPLOY_PATH = Path("/usr/local/sbin/ccslab-model-control-deploy")
TEMP_RULE = Path("/etc/sudoers.d/ccslab-model-control-deploy")
MODEL_RULE = Path("/etc/sudoers.d/ccslab-model-controller")
HELPER_PATH = Path("/usr/local/sbin/ccslab-model-controller")
LIB_DIR = Path("/usr/local/lib/ccslab-model-control")
SPEC = Path("/etc/ccslab/model-control.json")
CONTEXT_ENV = Path("/etc/ccslab/model-context.env")
MODEL_DROPIN_DIR = Path("/etc/systemd/system/ccslab-llm.service.d")
GATEWAY_DROPIN_DIR = Path("/etc/systemd/system/ccslab-gateway.service.d")
MODEL_DROPIN = MODEL_DROPIN_DIR / "90-model-control.conf"
GATEWAY_DROPIN = GATEWAY_DROPIN_DIR / "90-model-control.conf"
MODEL_UNIT_FILE = Path("/etc/systemd/system/ccslab-llm.service")
GATEWAY_UNIT_FILE = Path("/etc/systemd/system/ccslab-gateway.service")
LLM_ENV = Path("/etc/ccslab/llm.env")
GATEWAY_ROOT = Path("/home/server/ccslab-gateway-rev12/CCSLab-Code-Lite-1.5.3-Rev12-Server")
PAYLOAD_DIR = Path("/home/server/.cache/ccslab-model-control-deploy/payload")
BACKUP_ROOT = Path("/var/backups/ccslab-model-control-deploy")
LOCK_PATH = Path("/run/lock/ccslab-model-control-deploy.lock")
MODEL_UNIT = "ccslab-llm.service"
GATEWAY_UNIT = "ccslab-gateway.service"
MODEL_BIN = "/home/server/llm-service/llama.cpp/build/bin/llama-server"
MODEL_ID = "lab-general"
LLAMA_URL = "http://172.17.0.1:8000"
BASELINE = {"total_context": 131072, "slots": 5, "slot_context": 26368}
MAX_PAYLOAD = 2 * 1024 * 1024
MAINTENANCE_PROCESS_NAMES = {
    "apt", "apt-get", "dpkg", "dpkg-deb", "unattended-upgrade", "packagekitd",
    "systemctl", "systemd-run", "visudo", "sudoedit", "ccslab-model-control-deploy",
    "ccslab-model-controller",
}
SHELL_PROCESS_NAMES = {"bash", "sh", "dash", "zsh", "fish", "sshd", "login"}
READ_ONLY_TTY_PROCESS_NAMES = {"top", "htop", "btop", "less", "more", "man", "journalctl"}
TEMP_RULE_TEXT = 'server ALL=(root) NOPASSWD: /usr/local/sbin/ccslab-model-control-deploy ""\n'
MODEL_RULE_TEXT = 'server ALL=(root) NOPASSWD: /usr/local/sbin/ccslab-model-controller ""\n'
PAYLOAD_SHA256 = {
    "model_control.py": "b1b6cd99a61a750dd1291ba75b0abb8f026f3a2868ee2bc2410e0291695259ab",
    "model_host.py": "cf29105f4bfb759067e027355e04da52b51b6e6b4898c93cc812acce96847879",
}
BACKUP_FILES = (
    MODEL_UNIT_FILE, GATEWAY_UNIT_FILE, LLM_ENV,
    GATEWAY_ROOT / "server/model_control.py", GATEWAY_ROOT / "server/model_host.py",
)
NEW_FILES = (
    SPEC, CONTEXT_ENV, MODEL_RULE, MODEL_DROPIN, GATEWAY_DROPIN,
    LIB_DIR / "model_control.py", HELPER_PATH,
)
NEW_DIRS = (LIB_DIR, MODEL_DROPIN_DIR, GATEWAY_DROPIN_DIR)


class DeployError(RuntimeError):
    pass


def _interrupt(_signum, _frame):
    raise DeployError("deployment interrupted")


def _failure_report(stage, error):
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    source = next((frame for frame in reversed(frames)
                   if Path(frame.filename).name == "model_control_deploy.py"), None)
    source = source or (frames[-1] if frames else None)
    location = (f"{Path(source.filename).name}:{source.name}:{source.lineno}"
                if source else "location-unavailable")
    message = f"deployment failed: stage={stage} exception={type(error).__name__} at={location}"
    if isinstance(error, DeployError):
        message += " reason=" + str(error)[:140]
    return message


def _cleanup_after_run(lock_fd):
    failures = []
    try:
        if TEMP_RULE.exists() and not _revoke_temp_rule():
            failures.append(("temporary authorization cleanup", DeployError("revocation was not verified")))
    except Exception as error:
        failures.append(("temporary authorization cleanup", error))
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except Exception as error:
            failures.append(("lock descriptor cleanup", error))
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception as error:
            failures.append(("lock file cleanup", error))
    return failures


def _run(argv, timeout=20, *, check=True):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    if check and result.returncode:
        raise DeployError("fixed system operation failed")
    return result.stdout.decode("utf-8", "replace").strip()


def _systemd_properties(unit, names):
    raw = _run(["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(names)])
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def _duration_seconds(value):
    if not isinstance(value, str):
        raise DeployError("systemd start timeout has an unexpected type")
    if value == "infinity":
        return None
    matches = list(re.finditer(r"(\d+)\s*(h|min|s|ms|us)", value))
    if not matches or "".join(match.group(0) for match in matches).replace(" ", "") != value.replace(" ", ""):
        raise DeployError("systemd start timeout could not be interpreted")
    scale = {"h": 3600, "min": 60, "s": 1, "ms": 0.001, "us": 0.000001}
    return sum(int(match.group(1)) * scale[match.group(2)] for match in matches)


def _proc(pid, name):
    return Path(f"/proc/{pid}/{name}").read_bytes()


def _proc_args(pid):
    raw = _proc(pid, "cmdline")
    return [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]


def _proc_env(pid):
    result = {}
    for entry in _proc(pid, "environ").split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep:
            result[key.decode("ascii", "replace")] = value.decode("utf-8", "replace")
    return result


def _dimensions(argv):
    found = {}
    for index, arg in enumerate(argv):
        name, equals, value = arg.partition("=")
        if name not in ("-c", "--ctx-size", "-np", "--parallel"):
            continue
        if not equals:
            if index + 1 >= len(argv):
                raise DeployError("model launch dimensions are incomplete")
            value = argv[index + 1]
        if not value.isascii() or not value.isdigit():
            raise DeployError("model launch dimensions are invalid")
        found.setdefault("context" if name in ("-c", "--ctx-size") else "slots", []).append(int(value))
    return found


def strip_managed_flags(exec_start):
    """Remove only the verified context/parallel pairs from one unit directive."""
    argv = shlex.split(exec_start, posix=True)
    if any(not re.fullmatch(r"[A-Za-z0-9_./:=,+@-]+", arg) for arg in argv):
        raise DeployError("active ExecStart contains syntax that cannot be safely reconstructed")
    dims = _dimensions(argv)
    if dims.get("context") != [131072] or dims.get("slots") != [5]:
        raise DeployError("active model launch flags do not match 131072/5")
    if not argv or argv[0] != MODEL_BIN:
        raise DeployError("active model executable changed")
    filtered = []
    index = 0
    while index < len(argv):
        flag = argv[index].partition("=")[0]
        if flag in ("-c", "--ctx-size", "-np", "--parallel"):
            if "=" not in argv[index]:
                index += 2
            else:
                index += 1
            continue
        filtered.append(argv[index])
        index += 1
    if _dimensions(filtered):
        raise DeployError("failed to remove model launch overrides")
    return " ".join(shlex.quote(arg) for arg in filtered)


def _unit_execstart():
    raw = MODEL_UNIT_FILE.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.startswith("ExecStart=")]
    if len(lines) != 1 or lines[0].endswith("\\"):
        raise DeployError("model unit must have one simple ExecStart")
    return raw, lines[0][len("ExecStart="):]


def _atomic_write(path, contents, mode, uid=0, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".ccslab-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _trusted_payload():
    payload = {}
    directory = PAYLOAD_DIR.lstat()
    if (stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != 1002 or directory.st_mode & 0o077):
        raise DeployError("fixed payload directory is invalid")
    if {entry.name for entry in PAYLOAD_DIR.iterdir()} != set(PAYLOAD_SHA256):
        raise DeployError("payload directory contains unexpected files")
    for name in PAYLOAD_SHA256:
        path = PAYLOAD_DIR / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not _payload_file_metadata_trusted(info):
                raise DeployError("fixed payload file is invalid")
            raw = stream.read(MAX_PAYLOAD + 1)
        payload[name] = raw
    _validate_payload_bytes(payload)

    # Freeze verified bytes in a root-only directory before installation. The
    # source may be writable by server, so never reopen it after this point.
    run_dir = Path("/run")
    run_info = run_dir.lstat()
    if (stat.S_ISLNK(run_info.st_mode) or not stat.S_ISDIR(run_info.st_mode)
            or run_info.st_uid != 0 or run_info.st_mode & 0o022):
        raise DeployError("root staging parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix="ccslab-model-control-", dir=str(run_dir)))
    try:
        os.chown(staging, 0, 0)
        os.chmod(staging, 0o700)
        for name, raw in payload.items():
            _atomic_write(staging / name, raw, 0o400)
        staged = {}
        for name in PAYLOAD_SHA256:
            path = staging / name
            info = path.lstat()
            if not _root_staged_file_metadata_trusted(info) or path.is_symlink():
                raise DeployError("root staged payload permissions are unsafe")
            staged[name] = path.read_bytes()
        return _validate_payload_bytes(staged)
    finally:
        shutil.rmtree(staging)


def _payload_file_metadata_trusted(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 1002
            and not info.st_mode & 0o022 and info.st_size <= MAX_PAYLOAD)


def _root_staged_file_metadata_trusted(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o400)


def _validate_payload_bytes(payload):
    if not isinstance(payload, dict) or set(payload) != set(PAYLOAD_SHA256):
        raise DeployError("payload file list mismatch")
    for name, expected in PAYLOAD_SHA256.items():
        raw = payload[name]
        if not isinstance(raw, bytes) or len(raw) > MAX_PAYLOAD \
                or hashlib.sha256(raw).hexdigest() != expected:
            raise DeployError("payload checksum mismatch")
    return payload


def _capture_backup(runtime):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = BACKUP_ROOT / stamp
    for parent in (Path("/var"), Path("/var/backups"), BACKUP_ROOT):
        try:
            parent_info = parent.lstat()
        except FileNotFoundError:
            if parent != BACKUP_ROOT:
                raise DeployError("backup parent directory is missing")
            continue
        if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != 0 or parent_info.st_mode & 0o022):
            raise DeployError("backup directory ownership or permissions are unsafe")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chown(BACKUP_ROOT, 0, 0)
    os.chmod(BACKUP_ROOT, 0o700)
    os.chown(destination, 0, 0)
    os.chmod(destination, 0o700)
    files_dir = destination / "files"
    files_dir.mkdir(mode=0o700)
    metadata = {"baseline": BASELINE, "runtime": runtime, "files": [], "absent_dirs": []}
    for index, source in enumerate(BACKUP_FILES):
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or source.is_symlink():
            raise DeployError("required backup input is not a regular file")
        content = source.read_bytes()
        backup = files_dir / str(index)
        _atomic_write(backup, content, 0o600)
        if backup.read_bytes() != content:
            raise DeployError("backup verification failed")
        metadata["files"].append({
            "path": str(source), "backup": str(index), "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid, "sha256": hashlib.sha256(content).hexdigest(),
        })
    for directory in NEW_DIRS:
        if directory.exists() or directory.is_symlink():
            raise DeployError("unexpected existing service drop-in directory")
        metadata["absent_dirs"].append(str(directory))
    for path in NEW_FILES:
        if path.exists() or path.is_symlink():
            raise DeployError("a model-control target already exists")
    manifest = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write(destination / "manifest.json", manifest, 0o600)
    for root, dirs, files in os.walk(destination):
        os.chown(root, 0, 0)
        os.chmod(root, 0o700)
        for filename in files:
            path = Path(root) / filename
            os.chown(path, 0, 0)
            os.chmod(path, 0o600)
    return destination


def _restore_backup(directory):
    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected_manifest_keys = {"baseline", "runtime", "files", "absent_dirs"}
    if (not isinstance(metadata, dict) or set(metadata) != expected_manifest_keys
            or metadata.get("baseline") != BASELINE or not isinstance(metadata.get("runtime"), dict)
            or not isinstance(metadata.get("files"), list)
            or not isinstance(metadata.get("absent_dirs"), list)
            or metadata["absent_dirs"] != [str(path) for path in NEW_DIRS]
            or len(metadata["files"]) != len(BACKUP_FILES)):
        raise DeployError("backup baseline mismatch")
    for index, item in enumerate(metadata["files"]):
        if (not isinstance(item, dict)
                or set(item) != {"path", "backup", "mode", "uid", "gid", "sha256"}
                or item.get("path") != str(BACKUP_FILES[index])
                or item.get("backup") != str(index)
                or type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o777
                or type(item.get("uid")) is not int or item["uid"] < 0
                or type(item.get("gid")) is not int or item["gid"] < 0
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise DeployError("backup manifest entry is invalid")
        content = (directory / "files" / str(index)).read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise DeployError("backup checksum mismatch")
        _atomic_write(BACKUP_FILES[index], content, item["mode"], item["uid"], item["gid"])
    for path in NEW_FILES:
        if path.exists() and path.is_file() and path not in BACKUP_FILES:
            path.unlink()
    for directory_path in NEW_DIRS:
        if directory_path.exists():
            shutil.rmtree(directory_path)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)


def _verify_identity_and_quiet():
    if fcntl is None or pwd is None:
        raise DeployError("deployment entry requires Linux")
    if os.geteuid() != 0 or os.environ.get("SUDO_USER") != "server":
        raise DeployError("must be invoked by server through the temporary sudo rule")
    if set(sys.argv[1:]):
        raise DeployError("arguments are not accepted")
    try:
        info = DEPLOY_PATH.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise DeployError("deployment entry ownership or permissions are unsafe")
        temp = TEMP_RULE.lstat()
        if (temp.st_uid != 0 or temp.st_gid != 0 or stat.S_IMODE(temp.st_mode) != 0o440
                or TEMP_RULE.read_text(encoding="ascii") != TEMP_RULE_TEXT):
            raise DeployError("temporary sudoers rule does not match the fixed grant")
    except OSError as error:
        raise DeployError("temporary authorization is unavailable") from error
    host_ips = _run(["/bin/hostname", "-I"]).split()
    if "192.168.1.107" not in host_ips:
        raise DeployError("server identity does not match 192.168.1.107")
    _assert_no_concurrent_maintenance()
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise DeployError("another model-control deployment holds the lock") from error
    os.write(lock_fd, str(os.getpid()).encode("ascii"))
    os.fsync(lock_fd)
    return lock_fd


def _assert_no_concurrent_maintenance():
    """Allow the invoking SSH session and idle shells; reject active or unclear work."""
    rows = _read_process_table()
    if not rows:
        raise DeployError("process table is unavailable")

    current = os.getpid()
    ancestors = set()
    cursor = current
    while cursor > 1:
        row = rows.get(cursor)
        if row is None or cursor in ancestors:
            raise DeployError("current SSH execution session could not be identified")
        ancestors.add(cursor)
        cursor = row["ppid"]

    children = {}
    for pid, row in rows.items():
        children.setdefault(row["ppid"], []).append(pid)
    own_process_tree = set(ancestors)
    pending = [current]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child not in own_process_tree:
                own_process_tree.add(child)
                pending.append(child)

    for pid, row in rows.items():
        if pid in own_process_tree:
            continue
        command = row["command"]
        if command in MAINTENANCE_PROCESS_NAMES:
            raise DeployError("another maintenance operation is active")
        tty = row["tty"]
        if not tty.startswith("pts/"):
            continue
        if command in SHELL_PROCESS_NAMES or command in READ_ONLY_TTY_PROCESS_NAMES:
            continue
        raise DeployError("another SSH session has active or unclassifiable work")


def _read_process_table():
    """Read the kernel process table directly; do not depend on procps/ps output."""
    rows = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="replace")
            fields = {}
            for line in status.splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"Name", "State", "PPid", "Uid"}:
                    fields[key] = value.strip()
            if not {"Name", "State", "PPid", "Uid"}.issubset(fields):
                continue
            pid = int(entry.name)
            ppid = int(fields["PPid"])
            uid = int(fields["Uid"].split()[0])
            state = fields["State"].split()[0]
            tty = "?"
            for descriptor in ("0", "1", "2"):
                try:
                    target = os.readlink(entry / "fd" / descriptor)
                except OSError:
                    continue
                if target.startswith("/dev/pts/"):
                    tty = target.removeprefix("/dev/").split(" ", 1)[0]
                    break
            rows[pid] = {"ppid": ppid, "user": uid, "tty": tty,
                         "state": state, "command": fields["Name"].lower()}
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            # Processes may exit while /proc is sampled. A missing ancestor is
            # detected below and fails closed; unrelated transient entries are safe to skip.
            continue
    return rows


def _preflight():
    gateway = _systemd_properties(GATEWAY_UNIT,
        ("ActiveState", "SubState", "MainPID", "User", "Group", "NoNewPrivileges", "DropInPaths"))
    model = _systemd_properties(MODEL_UNIT,
        ("ActiveState", "SubState", "MainPID", "User", "Group", "DropInPaths", "FragmentPath",
         "TimeoutStartUSec"))
    if gateway.get("ActiveState") != "active" or model.get("ActiveState") != "active":
        raise DeployError("a required service is not active")
    if (gateway.get("User") != "server" or gateway.get("Group") != "server"
            or pwd.getpwnam("server").pw_uid != 1002 or gateway.get("NoNewPrivileges") != "yes"):
        raise DeployError("Gateway identity or security baseline changed")
    if gateway.get("DropInPaths") or model.get("DropInPaths"):
        raise DeployError("unexpected systemd drop-in exists")
    if model.get("FragmentPath") != str(MODEL_UNIT_FILE):
        raise DeployError("model unit location changed")
    start_timeout = _duration_seconds(model.get("TimeoutStartUSec", ""))
    wait_seconds = 1800 if start_timeout is None else max(600, int(start_timeout) + 600)
    gateway_pid, model_pid = int(gateway.get("MainPID", "0")), int(model.get("MainPID", "0"))
    if gateway_pid <= 1 or model_pid <= 1:
        raise DeployError("service process identity is unavailable")
    if not _gateway_health():
        raise DeployError("Gateway health check failed before backup")
    if Path(f"/proc/{model_pid}/exe").resolve(strict=True).name != "llama-server":
        raise DeployError("model service main process changed")
    args = _proc_args(model_pid)
    if _dimensions(args) != {"context": [131072], "slots": [5]}:
        raise DeployError("running model is not at 131072/5")
    if _run(["/usr/bin/systemctl", "list-jobs", "--no-legend"]):
        raise DeployError("systemd has an active job")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            name = (proc / "comm").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if name in {"apt", "apt-get", "dpkg", "unattended-upgrade"}:
            raise DeployError("package maintenance is active")
    if not LLM_ENV.is_file() or not GATEWAY_ROOT.is_dir():
        raise DeployError("required current configuration is missing")
    return gateway_pid, model_pid, args, wait_seconds


def _read_gateway_settings(gateway_pid):
    env = _proc_env(gateway_pid)
    if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
        raise DeployError("Gateway process environment has an unexpected type")
    base = env.get("CCSLAB_LLAMA_URL", "")
    model_id = env.get("CCSLAB_MODEL_ID", MODEL_ID)
    key_file = env.get("CCSLAB_LLAMA_API_KEY_FILE", "")
    if not all(isinstance(value, str) for value in (base, model_id, key_file)):
        raise DeployError("Gateway route settings have an unexpected type")
    try:
        parsed = urlsplit(base)
        parsed_port = parsed.port
    except ValueError as error:
        raise DeployError("Gateway model route is malformed") from error
    if (base != LLAMA_URL or parsed.scheme != "http" or parsed.hostname != "172.17.0.1"
            or parsed_port != 8000 or parsed.username or parsed.password or parsed.path not in ("", "/")
            or model_id != MODEL_ID or not key_file.startswith("/")):
        raise DeployError("Gateway model route or identity differs from deployment baseline")
    key_path = Path(key_file)
    key_info = key_path.lstat()
    if (not stat.S_ISREG(key_info.st_mode) or key_path.is_symlink() or key_info.st_mode & 0o022):
        raise DeployError("Gateway API key file is unsafe")
    token = key_path.read_text(encoding="utf-8").strip()
    if (not isinstance(token, str) or not token or len(token) > 8192
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise DeployError("Gateway API key file is invalid")
    database_value = env.get("CCSLAB_DATABASE", "")
    if not isinstance(database_value, str) or not database_value:
        raise DeployError("Gateway database path is missing or invalid")
    database = Path(database_value)
    if (not database.is_absolute() or not database.is_file() or database.is_symlink()
            or not database.with_name(database.name + ".open-webui.argon2").is_file()):
        raise DeployError("Gateway database startup state is not ready for a no-data-change restart")
    required_tables = {"users", "sessions", "auth_limits", "user_status_audit", "credential_audit",
                       "self_password_audit", "token_quotas", "token_requests", "token_quota_audit",
                       "model_confirmations", "model_configuration_audit"}
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            version_row = db.execute("PRAGMA user_version").fetchone()
            journal_row = db.execute("PRAGMA journal_mode").fetchone()
            table_rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if (not isinstance(version_row, tuple) or len(version_row) != 1
                or type(version_row[0]) is not int
                or not isinstance(journal_row, tuple) or len(journal_row) != 1
                or not isinstance(journal_row[0], str)
                or not isinstance(table_rows, list)
                or any(not isinstance(row, tuple) or len(row) != 1 or not isinstance(row[0], str)
                       for row in table_rows)):
            raise DeployError("Gateway database query returned an unexpected type")
        version, journal = version_row[0], journal_row[0]
        tables = {row[0] for row in table_rows}
        if version != 2 or journal.lower() != "wal" or not required_tables.issubset(tables):
            raise DeployError("Gateway database would require migration during restart")
    except sqlite3.Error as error:
        raise DeployError("Gateway database read-only preflight failed") from error
    gpu_index_value = env.get("CCSLAB_MODEL_GPU_INDEX", "0")
    if not isinstance(gpu_index_value, str) or not re.fullmatch(r"[0-9]{1,2}", gpu_index_value):
        raise DeployError("GPU index is invalid")
    gpu_index = int(gpu_index_value)
    if not 0 <= gpu_index < 16:
        raise DeployError("GPU index is outside the fixed range")
    return base, model_id, key_file, token, gpu_index, str(database)


def _api_get(base, token, path):
    connection = http.client.HTTPConnection("172.17.0.1", 8000, timeout=5)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + token,
                                                  "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None
        return json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _model_snapshot(base, token, model_id, expected_pid=None, *, managed=False):
    health = _api_get(base, token, "/health")
    models = _api_get(base, token, "/v1/models")
    props = _api_get(base, token, "/props")
    slots = _api_get(base, token, "/slots")
    model_data = models.get("data") if isinstance(models, dict) else None
    model_present = isinstance(model_data, list) and any(
        isinstance(item, dict) and item.get("id") == model_id for item in model_data
    )
    if (not isinstance(health, dict) or health.get("status") != "ok"
            or not model_present
            or not isinstance(props, dict) or props.get("is_sleeping")
            or not isinstance(slots, list) or len(slots) != 5):
        return None
    if (props.get("total_slots") != 5
            or any(not isinstance(row, dict) or type(row.get("id")) is not int
                   or type(row.get("n_ctx")) is not int or row["n_ctx"] != 26368
                   or row.get("is_processing") is not False for row in slots)):
        return None
    service = _systemd_properties(MODEL_UNIT, ("ActiveState", "MainPID"))
    pid_raw = service.get("MainPID", "")
    if service.get("ActiveState") != "active":
        return None
    if not pid_raw.isdigit() or int(pid_raw) <= 1:
        return None
    pid = int(pid_raw)
    if expected_pid and pid == expected_pid:
        return None
    args = _proc_args(pid)
    env = _proc_env(pid)
    if managed:
        if (_dimensions(args) or env.get("LLAMA_ARG_CTX_SIZE") != "131072"
                or env.get("LLAMA_ARG_N_PARALLEL") != "5"):
            return None
    elif _dimensions(args) != {"context": [131072], "slots": [5]}:
        return None
    if Path(f"/proc/{pid}/exe").resolve(strict=True).name != "llama-server":
        return None
    try:
        gpu = _run(["/usr/bin/nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                    "--format=csv,noheader,nounits"])
        if not any(row.split(",")[0].strip() == str(pid)
                   and row.split(",")[1].strip().isdigit()
                   and int(row.split(",")[1].strip()) > 0 for row in gpu.splitlines() if "," in row):
            return None
    except DeployError:
        return None
    return {"pid": pid, "total_context": 131072, "slots": 5, "slot_context": 26368}


def _verify_sudo_rule():
    _run(["/usr/sbin/visudo", "-cf", "/etc/sudoers"], timeout=15)


def _install_payload(payload):
    _atomic_write(GATEWAY_ROOT / "server/model_control.py", payload["model_control.py"], 0o644,
                  os.stat(GATEWAY_ROOT / "server/model_control.py").st_uid,
                  os.stat(GATEWAY_ROOT / "server/model_control.py").st_gid)
    LIB_DIR.mkdir(parents=True, mode=0o755, exist_ok=True)
    os.chown(LIB_DIR, 0, 0); os.chmod(LIB_DIR, 0o755)
    _atomic_write(LIB_DIR / "model_control.py", payload["model_control.py"], 0o644)
    _atomic_write(HELPER_PATH, payload["model_host.py"], 0o755)


def _make_spec(base, model_id, key_file, gpu_index, enabled):
    return (json.dumps({"enabled": enabled, "gpu_index": gpu_index, "llama_url": base,
                        "model_id": model_id, "api_key_file": key_file},
                       sort_keys=True, indent=2) + "\n").encode("utf-8")


def _context_environment():
    return b"LLAMA_ARG_CTX_SIZE=131072\nLLAMA_ARG_N_PARALLEL=5\n"


def _prepare_configs(exec_start, base, model_id, key_file, gpu_index):
    raw, original = _unit_execstart()
    if original != exec_start:
        raise DeployError("active ExecStart differs from the installed unit")
    changed = strip_managed_flags(original)
    llm_conf = ("[Service]\nEnvironmentFile=/etc/ccslab/model-context.env\n"
                "ExecStart=\nExecStart=" + changed + "\n").encode("utf-8")
    gateway_conf = ("[Service]\nEnvironment=CCSLAB_MODEL_CONTROL_SPEC=/etc/ccslab/model-control.json\n"
                    "NoNewPrivileges=no\n").encode("ascii")
    return llm_conf, gateway_conf, _make_spec(base, model_id, key_file, gpu_index, False)


def _gateway_health():
    connection = http.client.HTTPConnection("127.0.0.1", 8444, timeout=5)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(4096)
        payload = json.loads(body)
        return response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _wait_gateway(previous_pid):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            props = _systemd_properties(GATEWAY_UNIT, ("ActiveState", "MainPID", "NoNewPrivileges"))
            pid = int(props.get("MainPID", "0"))
            environment = _proc_env(pid) if pid > 1 else {}
            if (props.get("ActiveState") == "active" and props.get("NoNewPrivileges") == "no"
                    and environment.get("CCSLAB_MODEL_CONTROL_SPEC") == str(SPEC)
                    and pid > 1 and pid != previous_pid and _gateway_health()):
                return pid
        except (OSError, ValueError, DeployError):
            pass
        time.sleep(3)
    return None


def _verify_control_backend(settings):
    base, model_id, _key_file, token, gpu_index, _database = settings
    deployed_module = GATEWAY_ROOT / "server/model_control.py"
    if hashlib.sha256(deployed_module.read_bytes()).hexdigest() != PAYLOAD_SHA256["model_control.py"]:
        raise DeployError("Gateway model-control source checksum mismatch")
    root_module_path = LIB_DIR / "model_control.py"
    root_dir_info = LIB_DIR.lstat()
    root_info = root_module_path.lstat()
    if (not stat.S_ISDIR(root_dir_info.st_mode) or root_dir_info.st_uid != 0
            or root_dir_info.st_mode & 0o022 or LIB_DIR.is_symlink()
            or not stat.S_ISREG(root_info.st_mode) or root_info.st_uid != 0
            or root_info.st_mode & 0o022 or root_module_path.is_symlink()
            or hashlib.sha256(root_module_path.read_bytes()).hexdigest()
            != PAYLOAD_SHA256["model_control.py"]):
        raise DeployError("root model-control module is not trusted")
    module_spec = importlib.util.spec_from_file_location("_ccslab_root_model_control", root_module_path)
    if module_spec is None or module_spec.loader is None:
        raise DeployError("root model-control module could not be loaded")
    control_module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = control_module
    try:
        module_spec.loader.exec_module(control_module)
    except Exception:
        sys.modules.pop(module_spec.name, None)
        raise DeployError("root model-control module failed to load")
    control = control_module.ModelControl.__new__(control_module.ModelControl)
    control.db = ":memory:"
    control.base = base
    control.token = token
    control.model_id = model_id
    control.config_path = SPEC
    control.runtime_unit = MODEL_UNIT
    control.gpu_index = gpu_index
    control.gateway_output_limit = None
    control._gate = __import__("threading").Lock()
    control._in_flight = 0
    control._maintenance = False
    status = control.status()
    if (not isinstance(status, dict) or status.get("control_available") is not True
            or status.get("total_context") != 131072
            or status.get("slots") != 5 or status.get("status") != "running"):
        raise DeployError("model-control backend did not report the expected configuration")
    try:
        control.preview("deployment-read-only-check", 131072, 5)
    except control_module.ModelControlError as error:
        if type(error.status_code) is not int or error.status_code != 422:
            raise DeployError("read-only preview validation returned an unexpected status") from error
    else:
        raise DeployError("read-only preview check unexpectedly created a confirmation")


def _verify_rollback(settings, baseline):
    try:
        deadline = time.monotonic() + baseline["wait_seconds"]
        model_ok = False
        while time.monotonic() < deadline:
            model = _systemd_properties(MODEL_UNIT, ("ActiveState", "MainPID"))
            if model.get("ActiveState") == "failed":
                return False
            if _model_snapshot(settings[0], settings[3], settings[1], managed=False):
                model_ok = True
                break
            time.sleep(5)
        if not model_ok:
            return False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            gateway = _systemd_properties(GATEWAY_UNIT,
                ("ActiveState", "MainPID", "User", "Group", "NoNewPrivileges"))
            pid = int(gateway.get("MainPID", "0"))
            if (gateway.get("ActiveState") == "active" and gateway.get("User") == "server"
                    and gateway.get("Group") == "server" and gateway.get("NoNewPrivileges") == "yes"
                    and pid > 1 and "CCSLAB_MODEL_CONTROL_SPEC" not in _proc_env(pid)
                    and _gateway_health()):
                return not SPEC.exists() and not MODEL_RULE.exists()
            time.sleep(3)
        return False
    except (OSError, ValueError, KeyError, DeployError):
        return False


def _wait_model(base, token, model_id, previous_pid, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            service = _systemd_properties(MODEL_UNIT, ("ActiveState",))
            if service.get("ActiveState") in ("failed", "inactive"):
                return None
            result = _model_snapshot(base, token, model_id, previous_pid, managed=True)
            if result:
                return result
        except (OSError, ValueError, DeployError):
            pass
        time.sleep(5)
    return None


def _deploy(backup, payload, baseline, settings, state):
    base, model_id, key_file, token, gpu_index, _database = settings
    state["phase"] = "prepare fixed model and Gateway configuration"
    model_conf, gateway_conf, spec_disabled = _prepare_configs(baseline["exec_start"], base, model_id,
                                                               key_file, gpu_index)
    _install_payload(payload)
    _atomic_write(CONTEXT_ENV, _context_environment(), 0o644)
    _atomic_write(SPEC, spec_disabled, 0o644)
    _atomic_write(MODEL_RULE, MODEL_RULE_TEXT.encode("ascii"), 0o440)
    MODEL_DROPIN_DIR.mkdir(mode=0o755)
    os.chown(MODEL_DROPIN_DIR, 0, 0); os.chmod(MODEL_DROPIN_DIR, 0o755)
    _atomic_write(MODEL_DROPIN, model_conf, 0o600)
    GATEWAY_DROPIN_DIR.mkdir(mode=0o755)
    os.chown(GATEWAY_DROPIN_DIR, 0, 0); os.chmod(GATEWAY_DROPIN_DIR, 0o755)
    _atomic_write(GATEWAY_DROPIN, gateway_conf, 0o644)
    state["phase"] = "validate sudoers and systemd configuration"
    _verify_sudo_rule()
    _run(["/usr/bin/systemd-analyze", "verify", str(MODEL_UNIT_FILE), str(GATEWAY_UNIT_FILE)], timeout=30)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
    state["phase"] = "restart and verify llama.cpp baseline"
    state["model_restarted"] = True
    _run(["/usr/bin/systemctl", "restart", "--", MODEL_UNIT], timeout=None)
    model = _wait_model(base, token, model_id, baseline["model_pid"], baseline["wait_seconds"])
    if not model:
        raise DeployError("model health/parameters/GPU verification timed out")
    state["phase"] = "restart and verify Gateway"
    state["gateway_restarted"] = True
    _run(["/usr/bin/systemctl", "restart", "--", GATEWAY_UNIT], timeout=None)
    gateway_pid = _wait_gateway(baseline["gateway_pid"])
    if not gateway_pid:
        raise DeployError("Gateway health verification failed")
    # Activate only after both services passed their initial unchanged-baseline checks.
    state["phase"] = "enable and verify administrator control status"
    _atomic_write(SPEC, _make_spec(base, model_id, key_file, gpu_index, True), 0o644)
    _verify_control_backend(settings)
    return model


def _revoke_temp_rule():
    try:
        info = TEMP_RULE.lstat()
        if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o440:
            return False
        if TEMP_RULE.read_text(encoding="ascii") != TEMP_RULE_TEXT:
            return False
        TEMP_RULE.unlink()
        try:
            info = DEPLOY_PATH.lstat()
            if info.st_uid == 0 and stat.S_ISREG(info.st_mode):
                DEPLOY_PATH.unlink()
        except FileNotFoundError:
            pass
        _verify_sudo_rule()
        return not TEMP_RULE.exists()
    except (OSError, DeployError):
        return False


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0:
        print("deployment refused: fixed no-argument root entry required")
        return 2
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    lock_fd = None
    backup = None
    settings = None
    changed = False
    state = {"model_restarted": False, "gateway_restarted": False}
    result = 1
    phase = "initialization"
    try:
        phase = "identity and maintenance preflight"
        lock_fd = _verify_identity_and_quiet()
        phase = "service baseline preflight"
        gateway_pid, model_pid, model_args, wait_seconds = _preflight()
        phase = "Gateway route and database preflight"
        settings = _read_gateway_settings(gateway_pid)
        phase = "live model health and slot validation"
        current_model = _model_snapshot(settings[0], settings[3], settings[1], None, managed=False)
        if not current_model:
            raise DeployError("current model API/GPU state is not healthy at 131072/5")
        phase = "root-private payload validation"
        payload = _trusted_payload()
        phase = "active systemd launch inspection"
        raw_exec = _run(["/usr/bin/systemctl", "show", MODEL_UNIT, "--property=ExecStart", "--value"])
        match = re.search(r"argv\[\]=(.*?)(?:\s+;\s+ignore_errors=|\s*}\s*$)", raw_exec)
        if not match:
            raise DeployError("active model ExecStart could not be parsed")
        exec_start = match.group(1).strip()
        if shlex.split(exec_start) != model_args:
            raise DeployError("active model arguments disagree with the service manager")
        baseline = {"gateway_pid": gateway_pid, "model_pid": model_pid, "model_args": model_args,
                    "exec_start": exec_start, "wait_seconds": wait_seconds}
        phase = "immediate production backup"
        backup = _capture_backup({
            "gateway_pid": gateway_pid, "model_pid": model_pid, "model_dimensions": _dimensions(model_args),
            "slot_context": current_model["slot_context"], "gateway_user": "server",
            "gateway_uid": 1002, "gateway_nnp": "yes", "model_state": "active",
            "model_exec_sha256": hashlib.sha256(exec_start.encode("utf-8")).hexdigest(),
        })
        changed = True
        phase = "deployment and health verification"
        state["phase"] = phase
        _deploy(backup, payload, baseline, settings, state)
        phase = "temporary authorization revocation"
        if not _revoke_temp_rule():
            raise DeployError("temporary sudo authorization could not be revoked")
        result = 0
        print("deployment complete: baseline 131072/5 preserved; admin preview/apply not invoked")
    except Exception as error:
        phase = state.get("phase", phase)
        print(_failure_report(phase, error))
        if changed and backup:
            rollback_phase = "restore backed-up files"
            try:
                _restore_backup(backup)
                if state["model_restarted"]:
                    rollback_phase = "restart restored model service"
                    _run(["/usr/bin/systemctl", "restart", "--", MODEL_UNIT], timeout=None)
                    state["model_restarted"] = False
                if state["gateway_restarted"]:
                    rollback_phase = "restart restored Gateway service"
                    _run(["/usr/bin/systemctl", "restart", "--", GATEWAY_UNIT], timeout=None)
                    state["gateway_restarted"] = False
                rollback_phase = "verify restored services and model baseline"
                if settings and not _verify_rollback(settings, baseline):
                    raise DeployError("rollback health or 131072/5 verification failed")
                print("deployment rollback: files restored; service recovery requested")
            except Exception as rollback_error:
                print(_failure_report("rollback: " + rollback_phase, rollback_error))
        result = 1
    finally:
        for cleanup_phase, cleanup_error in _cleanup_after_run(lock_fd):
            print(_failure_report(cleanup_phase, cleanup_error))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
