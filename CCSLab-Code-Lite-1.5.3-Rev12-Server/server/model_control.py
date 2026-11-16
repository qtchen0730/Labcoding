"""Read llama.cpp telemetry and authorize narrowly scoped configuration changes."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import subprocess
from threading import Lock
from time import time
from urllib.parse import urlsplit


CONTEXT_OPTIONS = (32768, 65536, 131072, 262144)
MODEL_SPEC = Path("/etc/ccslab/model-control.json")
MAINTENANCE = Path("/etc/ccslab/model-maintenance-until")
CONTEXT_ENV = Path("/etc/ccslab/model-context.env")
LLAMA_API_KEY = Path("/etc/ccslab/llama-api-key")
ROLLBACK_PENDING = Path("/etc/ccslab/model-change.pending")
CONTROLLER = "/usr/local/sbin/ccslab-model-controller"
SYSTEMCTL = "/usr/bin/systemctl"
NVIDIA_SMI = "/usr/bin/nvidia-smi"
CONFIRM_SECONDS = 90
MAX_RESPONSE = 512 * 1024
OUTCOMES = {"applied", "rolled_back", "rollback_failed", "refused"}
REASONS = {"", "restart_failed", "health_or_dimensions_failed", "interrupted", "restore_failed",
           "policy_or_runtime_refused", "previous_transaction_pending", "unknown"}


class ModelControlError(RuntimeError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def positive(value):
    return value if type(value) is int and value > 0 else None


def unit_name(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}\.service", value)


def _fixed_command(args, timeout=3):
    return subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        check=True,
    ).stdout.strip()


def llama_pid(unit):
    if not unit_name(unit):
        return None
    try:
        pid = int(_fixed_command([SYSTEMCTL, "show", "--property=MainPID", "--value", "--", unit]))
        if pid > 1:
            try:
                name = Path(f"/proc/{pid}/exe").resolve(strict=True).name
            except PermissionError:
                argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
                name = Path(argv0.decode("utf-8", "replace")).name
            return pid if name == "llama-server" else None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _proc_config(pid, *, environment_only=False):
    """Keep only documented numeric launch flags. Never return the command line."""
    if pid is None:
        return {}
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        return {}
    env = {}
    try:
        for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            key, _, value = entry.partition(b"=")
            if key in (b"LLAMA_ARG_CTX_SIZE", b"LLAMA_ARG_N_PARALLEL"):
                env[key.decode()] = value.decode("ascii", "replace")
    except OSError:
        pass
    result = {}
    for field, flags, env_key in (
        ("total_context", {"-c", "--ctx-size"}, "LLAMA_ARG_CTX_SIZE"),
        ("slots", {"-np", "--parallel"}, "LLAMA_ARG_N_PARALLEL"),
    ):
        explicit = []
        for i, argument in enumerate(argv[:-1]):
            flag, equals, value = argument.partition("=")
            if flag in flags:
                explicit.append(value if equals else argv[i + 1])
        if len(explicit) > 1 or (explicit and environment_only):
            continue
        raw = explicit[0] if explicit else env.get(env_key, "")
        if raw.isascii() and raw.isdigit() and len(raw) <= 10 and int(raw) > 0:
            result[field] = int(raw)
    return result


def _llama_get(base, token, endpoint, timeout=2.5):
    url = urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        return None
    connection = (HTTPSConnection if url.scheme == "https" else HTTPConnection)(
        url.hostname, url.port, timeout=timeout,
    )
    path = url.path.rstrip("/").removesuffix("/v1") + endpoint
    try:
        connection.request("GET", path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                return None
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) <= MAX_RESPONSE:
                return json.loads(payload)
    except (OSError, HTTPException, ValueError):
        return None
    finally:
        connection.close()


def _read_gpu(index, pid):
    try:
        raw = _fixed_command([
            NVIDIA_SMI, "--query-gpu=index,uuid,memory.used,memory.total",
            "--format=csv,noheader,nounits", "--id", str(index),
        ])
        row, uuid, used, total = [item.strip() for item in raw.split(",")]
        gpu = {"index": int(row), "used_mib": int(used), "total_mib": int(total), "_uuid": uuid}
        if gpu["index"] != index or not re.fullmatch(r"GPU-[a-fA-F0-9-]+", uuid):
            return None
        if not 0 <= gpu["used_mib"] <= gpu["total_mib"]:
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if pid:
        try:
            processes = _fixed_command([
                NVIDIA_SMI, "--query-compute-apps=pid,gpu_uuid,used_gpu_memory", "--format=csv,noheader,nounits",
            ])
            matching = []
            for line in processes.splitlines():
                fields = [value.strip() for value in line.split(",")]
                if len(fields) == 3 and fields[0] == str(pid) and fields[1] == uuid:
                    matching.append(int(fields[2]))
            if len(matching) == 1 and 0 < matching[0] <= gpu["used_mib"]:
                gpu["_model_mib"] = matching[0]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return gpu


def read_runtime(base, token, model_id, unit=None, gpu_index=0, *, verify_environment=False):
    """Return verified sources separately from nullable display fields."""
    health = _llama_get(base, token, "/health")
    models = _llama_get(base, token, "/v1/models") or {}
    props = _llama_get(base, token, "/props") or {}
    models = models if isinstance(models, dict) else {}
    props = props if isinstance(props, dict) else {}
    data = models.get("data")
    target = next((entry for entry in data if isinstance(entry, dict) and entry.get("id") == model_id), {}) \
        if isinstance(data, list) else {}
    ready = isinstance(health, dict) and health.get("status") == "ok" and bool(target) and bool(props)
    slots_data = _llama_get(base, token, "/slots") if ready and not props.get("is_sleeping") else None
    slots_data = slots_data if isinstance(slots_data, list) and slots_data and all(
        isinstance(row, dict) for row in slots_data
    ) else None
    ids = [row.get("id") for row in slots_data] if slots_data else []
    if slots_data and (not all(type(i) is int and i >= 0 for i in ids) or len(set(ids)) != len(ids)):
        slots_data = None
    pid = llama_pid(unit)
    config = _proc_config(pid)
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, dict) else {}
    params = settings.get("params")
    params = params if isinstance(params, dict) else {}
    meta = target.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    values = [positive(row.get("n_ctx")) for row in slots_data] if slots_data else []
    observed = min(values) if values and all(values) else None
    actual_slots = len(slots_data) if slots_data else positive(props.get("total_slots"))
    total = (
        config.get("total_context") or positive(props.get("n_ctx_total")) or positive(props.get("total_context"))
    ) if ready else None
    pretty_name = target.get("name") or props.get("model_path")
    if not isinstance(pretty_name, str):
        pretty_name = model_id
    pretty_name = PurePosixPath(pretty_name.replace("\\", "/")).name[:200] or model_id
    return {
        "model_id": model_id, "model_name": pretty_name,
        "status": ("sleeping" if props.get("is_sleeping") else "running") if ready else "unavailable",
        "total_context": total, "slots": actual_slots if ready else None,
        "effective_context_limit": (observed or positive(settings.get("n_ctx"))) if ready else None,
        "output_limit": (
            positive(params.get("n_predict")) or positive(params.get("max_tokens"))
            or positive(settings.get("n_predict"))
        ) if ready else None,
        "gpu": _read_gpu(gpu_index, pid),
        "_train_context": positive(meta.get("n_ctx_train")),
        "_observed_slot_context": observed,
        "_uniform_context": bool(observed and all(value == observed for value in values)),
        "_active_slots": (
            sum(row["is_processing"] for row in slots_data) if slots_data
            and all(type(row.get("is_processing")) is bool for row in slots_data) else None
        ),
        "_pid": pid,
        "_config": config,
        "_managed_environment": _proc_config(pid, environment_only=True) if verify_environment else None,
    }


def _root_file(path):
    """Only trust operator-owned regular files under an operator-owned path."""
    resolved = Path(path).absolute()
    for entry in (resolved, *resolved.parents):
        info = entry.lstat()
        if getattr(info, "st_uid", -1) != 0 or info.st_mode & 0o022 or entry.is_symlink():
            raise ValueError("not an operator-controlled file")
    if not stat.S_ISREG(resolved.lstat().st_mode) or resolved.stat().st_size > 64 * 1024:
        raise ValueError("invalid operator file")
    return resolved.read_bytes()


def context_env(raw):
    body = raw.decode("ascii")
    match = re.fullmatch(r"LLAMA_ARG_CTX_SIZE=([0-9]{1,10})\nLLAMA_ARG_N_PARALLEL=([0-9]{1,3})\n", body)
    if not match or not all(int(value) > 0 for value in match.groups()):
        raise ValueError("invalid model context environment")
    return {"total_context": int(match[1]), "slots": int(match[2])}


def normalized_llama_url(value):
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path.rstrip("/") not in ("", "/v1")):
        raise ValueError("invalid llama.cpp route")
    return f"{url.scheme}://{url.netloc}"


def maintenance_open():
    try:
        until = int(_root_file(MAINTENANCE).strip())
        return time() < until <= time() + 900
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class Profile:
    total_context: int
    slots: int
    slot_context: int
    peak_model_mib: int


@dataclass(frozen=True)
class Policy:
    digest: str
    unit: str
    gpu_index: int
    gpu_uuid: str
    reserve_mib: int
    profiles: dict
    llama_url: str
    model_id: str

    @classmethod
    def read(cls, spec_path):
        raw = _root_file(spec_path)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("enabled") is not True:
            raise ModelControlError("服务器尚未启用模型配置修改。", 503)
        unit, index, uuid = data.get("unit"), data.get("gpu_index"), data.get("gpu_uuid")
        reserve = positive(data.get("reserve_mib"))
        llama_url = normalized_llama_url(data.get("llama_url", ""))
        model_id = data.get("model_id", "")
        if not (unit_name(unit) and type(index) is int and 0 <= index < 16 and isinstance(uuid, str)
                and re.fullmatch(r"GPU-[a-fA-F0-9-]+", uuid) and reserve and reserve >= 512
                and isinstance(model_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model_id)):
            raise ValueError("invalid GPU or systemd model-control policy")
        profiles = {}
        if not isinstance(data.get("profiles"), list) or not 2 <= len(data["profiles"]) <= 40:
            raise ValueError("missing qualified profiles")
        for item in data["profiles"]:
            p = Profile(**item)
            if (type(p.total_context) is not int or p.total_context not in CONTEXT_OPTIONS
                    or type(p.slots) is not int or not 1 <= p.slots <= 10
                    or not positive(p.slot_context) or not positive(p.peak_model_mib)
                    or (p.total_context, p.slots) in profiles):
                raise ValueError("invalid qualified profile")
            profiles[p.total_context, p.slots] = p
        return cls(hashlib.sha256(raw).hexdigest(), unit, index, uuid, reserve, profiles, llama_url, model_id)


def validate_change(policy, runtime, requested, require_idle=False):
    current = policy.profiles.get((runtime["total_context"], runtime["slots"]))
    if (runtime["status"] != "running" or not current or not runtime["_pid"]
            or runtime["_config"] != {"total_context": current.total_context, "slots": current.slots}
            or not runtime["_uniform_context"] or runtime["_observed_slot_context"] != current.slot_context):
        raise ModelControlError("当前运行配置与服务器核准的基线不一致，暂不能修改。", 409)
    if (not runtime["_train_context"]
            or max(current.slot_context, requested.slot_context) > runtime["_train_context"]):
        raise ModelControlError("缺少模型训练上下文证据，或每槽上下文超出模型支持范围。", 409)
    if runtime["output_limit"] and runtime["output_limit"] >= requested.slot_context:
        raise ModelControlError("预计每槽上下文必须大于当前单次输出上限。", 409)
    if require_idle and runtime["_active_slots"] != 0:
        raise ModelControlError("模型仍在处理请求，或无法确认槽位空闲。请关闭其他调用后重试。", 409)
    gpu = runtime["gpu"]
    if not gpu or gpu.get("_uuid") != policy.gpu_uuid or not positive(gpu.get("_model_mib")):
        raise ModelControlError("无法验证指定 GPU 和模型进程的实际显存用量。", 409)
    if gpu["_model_mib"] > current.peak_model_mib:
        raise ModelControlError("当前显存已超过基线实测峰值，请重新核准配置。", 409)
    estimated = gpu["used_mib"] - gpu["_model_mib"] + requested.peak_model_mib
    if estimated + policy.reserve_mib > gpu["total_mib"]:
        raise ModelControlError("目标配置的实测显存预算和安全余量超过该 GPU 容量。", 409)
    return current, estimated


def _public(runtime, control_available, detail):
    result = {key: value for key, value in runtime.items() if not key.startswith("_")}
    if result["gpu"]:
        result["gpu"] = {key: value for key, value in result["gpu"].items() if not key.startswith("_")}
    result["control_available"] = control_available
    result["detail"] = detail
    return result


def migrate_model_control(database_path):
    migration = Path(__file__).with_name("migrations") / "002_model_control.sql"
    with closing(sqlite3.connect(database_path)) as db, db:
        db.executescript(migration.read_text(encoding="utf-8"))


class ModelControl:
    def __init__(self, database_path, llama_url, llama_token, config_path=None, *, model_id="lab-general",
                 runtime_unit=None, gpu_index=0, gateway_output_limit=None):
        self.db = database_path
        self.base = llama_url
        self.token = llama_token
        self.model_id = model_id
        self.config_path = Path(config_path) if config_path else None
        self.runtime_unit = runtime_unit
        self.gpu_index = gpu_index
        self.gateway_output_limit = positive(gateway_output_limit)
        self._gate = Lock()
        self._in_flight = 0
        self._maintenance = False
        migrate_model_control(self.db)

    def begin_inference(self):
        with self._gate:
            if self._maintenance or (self.config_path == MODEL_SPEC and ROLLBACK_PENDING.exists()):
                return False
            self._in_flight += 1
            return True

    def end_inference(self):
        with self._gate:
            self._in_flight -= 1

    def _policy(self):
        if self.config_path != MODEL_SPEC or not Path(CONTROLLER).is_file():
            raise ModelControlError("服务器未启用受控模型重配置。当前页面仅提供运行状态。", 503)
        try:
            _root_file(Path(CONTROLLER))
            policy = Policy.read(self.config_path)
            if policy.llama_url != normalized_llama_url(self.base) or policy.model_id != self.model_id:
                raise ValueError("control and proxy disagree")
            return policy
        except (OSError, ValueError, TypeError) as error:
            raise ModelControlError("无法验证服务器模型控制组件或核准配置。", 503) from error

    def _runtime(self, policy=None):
        unit = policy.unit if policy else self.runtime_unit
        index = policy.gpu_index if policy else self.gpu_index
        runtime = read_runtime(self.base, self.token, self.model_id, unit, index)
        if self.gateway_output_limit:
            upstream_cap = runtime["output_limit"] or self.gateway_output_limit
            runtime["output_limit"] = min(upstream_cap, self.gateway_output_limit)
        if policy and runtime["_pid"]:
            # The API UID can read the active environment file even if procfs protects
            # another service UID's environ. The privileged helper verifies procfs.
            try:
                selected = context_env(_root_file(CONTEXT_ENV))
                if all(selected.get(key) == value for key, value in runtime["_config"].items()):
                    runtime["_config"] = selected
                    if runtime["total_context"] is None and runtime["status"] == "running":
                        runtime["total_context"] = selected["total_context"]
            except (OSError, ValueError):
                pass
        return runtime

    def status(self):
        try:
            policy = self._policy()
            allowed = maintenance_open() and not ROLLBACK_PENDING.exists()
            detail = "" if allowed else "模型配置只能在 Ubuntu 运维人员开启的维护窗口内修改。"
            if ROLLBACK_PENDING.exists():
                detail = "上次变更未完成。模型请求已暂停，等待 Ubuntu 运维检查。"
        except ModelControlError as error:
            policy, allowed, detail = None, False, str(error)
        return _public(self._runtime(policy), allowed, detail)

    def _check_window(self):
        if ROLLBACK_PENDING.exists():
            raise ModelControlError("上次变更未完成。请联系 Ubuntu 运维。", 409)
        if not maintenance_open():
            raise ModelControlError("模型配置只能在 Ubuntu 运维人员开启的维护窗口内修改。", 409)

    def preview(self, actor, total_context, slots):
        if type(total_context) is not int or total_context not in CONTEXT_OPTIONS \
                or type(slots) is not int or not 1 <= slots <= 10:
            raise ModelControlError("上下文或槽位数超出可选范围。")
        policy = self._policy()
        self._check_window()
        runtime = self._runtime(policy)
        requested = policy.profiles.get((total_context, slots))
        if not requested:
            raise ModelControlError("此组合未通过该模型与 GPU 的实测核准。")
        current, estimated = validate_change(policy, runtime, requested)
        if current == requested:
            raise ModelControlError("请求的数值与当前配置一致。")
        token = secrets.token_urlsafe(32)
        payload = dict(old_context=current.total_context, old_slots=current.slots,
                       new_context=total_context, new_slots=slots, policy=policy.digest)
        with closing(sqlite3.connect(self.db)) as db, db:
            now = time()
            db.execute("DELETE FROM model_confirmations WHERE expires <= ? OR actor = ?", (now, actor))
            db.execute("INSERT INTO model_confirmations VALUES (?, ?, ?, ?)",
                       (self._digest(token), actor, now + CONFIRM_SECONDS, json.dumps(payload)))
        return {
            "confirmation": token, "current": _public(runtime, True, ""),
            "requested": {"total_context": total_context, "slots": slots},
            "estimated_slot_context": requested.slot_context,
            "gpu_risk": (f"估算 GPU 峰值 {estimated} MiB，安全余量 {policy.reserve_mib} MiB，"
                         f"设备容量 {runtime['gpu']['total_mib']} MiB。负载变化仍可能导致启动失败。"),
            "restart_required": True, "user_impact": "会重启模型服务。维护期间所有模型请求暂停，服务缓存会清空。",
            "expires_in": CONFIRM_SECONDS,
        }

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _consume(self, actor, confirmation):
        if not isinstance(confirmation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", confirmation):
            raise ModelControlError("确认无效或已过期。请重新预览。", 409)
        digest = self._digest(confirmation)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute(
                "SELECT payload FROM model_confirmations WHERE digest = ? AND actor = ? AND expires > ?",
                (digest, actor, time()),
            ).fetchone()
            if not item:
                raise ModelControlError("确认无效或已过期。请重新预览。", 409)
            db.execute("DELETE FROM model_confirmations WHERE digest = ?", (digest,))
            return json.loads(item[0])

    def _audit(self, actor, payload, result, reason=""):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO model_configuration_audit "
                "(occurred, actor, result, old_context, old_slots, new_context, new_slots, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time(), actor, result, payload["old_context"], payload["old_slots"],
                 payload["new_context"], payload["new_slots"], reason),
            )

    def _invoke(self, payload):
        try:
            process = subprocess.run(
                ["/usr/bin/sudo", "-n", "--", CONTROLLER], input=json.dumps(payload),
                timeout=250, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
            )
            result = json.loads(process.stdout[:4096])
            if isinstance(result, dict) and result.get("status") in OUTCOMES:
                reason = result.get("reason")
                return {"status": result["status"], "reason": reason if reason in REASONS else "unknown"}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return {"status": "unknown", "reason": "unknown"}

    def apply(self, actor, confirmation):
        with self._gate:
            if self._maintenance or self._in_flight:
                raise ModelControlError("还有在线模型任务。请等任务结束后再应用。", 409)
            self._maintenance = True
        try:
            policy = self._policy()
            self._check_window()
            payload = self._consume(actor, confirmation)
            if payload["policy"] != policy.digest:
                raise ModelControlError("服务器核准配置已经变更。请重新预览。", 409)
            requested = policy.profiles[payload["new_context"], payload["new_slots"]]
            current, _ = validate_change(policy, self._runtime(policy), requested, require_idle=True)
            if (current.total_context, current.slots) != (payload["old_context"], payload["old_slots"]):
                raise ModelControlError("服务基线已变更。请重新预览。", 409)
            self._audit(actor, payload, "requested")
            result = self._invoke(payload)
            outcome = result["status"]
            self._audit(actor, payload, outcome, result.get("reason", ""))
            if outcome == "applied":
                return {"status": outcome, "model": self.status()}
            messages = {
                "rolled_back": "新配置启动或校验失败，已经恢复并验证原配置。",
                "rollback_failed": "新配置启动失败，恢复校验也未通过。请运维从备份检查。",
                "refused": "服务器控制组件拒绝应用，请运维检查维护窗口、模型基线和显存证据。",
                "unknown": "无法确认服务器控制组件响应。请运维检查配置与运行状态。",
            }
            raise ModelControlError(messages[outcome], 503)
        finally:
            with self._gate:
                self._maintenance = False
