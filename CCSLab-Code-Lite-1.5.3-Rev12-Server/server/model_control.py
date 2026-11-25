"""Read llama.cpp telemetry and authorize narrowly scoped configuration changes."""

from contextlib import closing, nullcontext
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


CONTEXT_OPTIONS = (32768, 65536, 131072, 147456, 163840, 196608)
MODEL_SPEC = Path("/etc/ccslab/model-control.json")
CONTEXT_ENV = Path("/etc/ccslab/model-context.env")
ROLLBACK_PENDING = Path("/etc/ccslab/model-change.pending")
DEPLOYMENT_HOLD = Path("/run/ccslab-gateway-context-update.hold")
MODEL_MAINTENANCE = Path("/run/ccslab-model-context-maintenance")
MANAGED_OPENCODE = Path("/home/server/.config/opencode/opencode.json")
CONTROLLER = "/usr/local/sbin/ccslab-model-controller"
SYSTEMCTL = "/usr/bin/systemctl"
NVIDIA_SMI = "/usr/bin/nvidia-smi"
MODEL_UNIT = "ccslab-llm.service"
MIN_SLOTS = 1
MAX_SLOTS = 10
CONFIRM_SECONDS = 90
MAX_RESPONSE = 512 * 1024
OUTCOMES = {"applied", "rolled_back", "rollback_failed", "refused"}
REASONS = {"", "restart_failed", "health_or_dimensions_failed", "interrupted", "restore_failed",
           "policy_or_runtime_refused", "previous_transaction_pending", "unknown",
           "independent_running", "process_check_failed", "managed_config_failed",
           "maintenance_not_enrolled", "direct_guard_failed", "direct_guard_release_failed", "model_busy",
           "managed_config_permission", "managed_config_missing", "managed_config_io", "managed_config_mismatch"}


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
            NVIDIA_SMI, "--query-gpu=index,uuid,name,memory.used,memory.total,memory.free",
            "--format=csv,noheader,nounits", "--id", str(index),
        ])
        row, uuid, name, used, total, free = [item.strip() for item in raw.split(",")]
        gpu = {"index": int(row), "name": name, "used_mib": int(used), "total_mib": int(total),
               "free_mib": int(free), "sampled_at": time(), "_uuid": uuid}
        if gpu["index"] != index or not name or len(name) > 200 or not re.fullmatch(r"GPU-[a-fA-F0-9-]+", uuid):
            return None
        if (gpu["total_mib"] <= 0 or not 0 <= gpu["used_mib"] <= gpu["total_mib"]
                or not 0 <= gpu["free_mib"] <= gpu["total_mib"] - gpu["used_mib"]):
            return None
        gpu["utilization_percent"] = round(100 * gpu["used_mib"] / gpu["total_mib"], 1)
        # A driver accounting difference; do not label total-used as memory.free.
        gpu["unaccounted_mib"] = gpu["total_mib"] - gpu["used_mib"] - gpu["free_mib"]
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
                gpu["model_process_mib"] = matching[0]
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
        "ready_checked_at": time() if ready else None,
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


@dataclass(frozen=True)
class Policy:
    digest: str
    gpu_index: int
    llama_url: str
    model_id: str
    api_key_file: Path
    managed_opencode_config: Path | None = None

    @classmethod
    def read(cls, spec_path):
        raw = _root_file(spec_path)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("enabled") is not True:
            raise ModelControlError("服务器尚未启用模型配置修改。", 503)
        index = data.get("gpu_index")
        llama_url = normalized_llama_url(data.get("llama_url", ""))
        model_id = data.get("model_id", "")
        key_file = data.get("api_key_file")
        fields = {"enabled", "gpu_index", "llama_url", "model_id", "api_key_file"}
        managed = data.get("managed_opencode_config")
        if (set(data) not in (fields, fields | {"managed_opencode_config"})
                or ("managed_opencode_config" in data and managed != str(MANAGED_OPENCODE))
                or type(index) is not int or not 0 <= index < 16
                or not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model_id)
                or not isinstance(key_file, str) or not Path(key_file).is_absolute()):
            raise ValueError("invalid fixed model-control configuration")
        return cls(hashlib.sha256(raw).hexdigest(), index, llama_url, model_id, Path(key_file),
                   Path(managed) if managed else None)


def slot_context(total_context, slots):
    return ((total_context // slots + 255) // 256) * 256


def validate_change(runtime, total_context, slots, require_idle=False):
    current = {"total_context": runtime["total_context"], "slots": runtime["slots"]}
    if (runtime["status"] != "running" or not runtime["_pid"]
            or current["total_context"] not in CONTEXT_OPTIONS
            or type(current["slots"]) is not int or not MIN_SLOTS <= current["slots"] <= MAX_SLOTS
            or runtime["_config"] != current or not runtime["_uniform_context"]
            or runtime["_managed_environment"] != current
            or runtime["_observed_slot_context"] != slot_context(*current.values())):
        raise ModelControlError("当前模型配置或槽位状态无法核实，暂不能修改。", 409)
    requested_slot_context = slot_context(total_context, slots)
    if runtime["_train_context"] and requested_slot_context > runtime["_train_context"]:
        raise ModelControlError("目标每槽上下文超过模型训练上下文。", 409)
    if runtime["output_limit"] and runtime["output_limit"] >= requested_slot_context:
        raise ModelControlError("目标每槽上下文必须大于当前单次输出上限。", 409)
    if require_idle and runtime["_active_slots"] != 0:
        raise ModelControlError("模型仍在处理请求，或无法确认槽位空闲。请等槽位空闲后重试。", 409)
    return current, requested_slot_context


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
                 runtime_unit=None, gpu_index=0, gateway_output_limit=None, context_store=None):
        self.db = database_path
        self.base = llama_url
        self.token = llama_token
        self.model_id = model_id
        self.config_path = Path(config_path) if config_path else None
        self.runtime_unit = runtime_unit
        self.gpu_index = gpu_index
        self.gateway_output_limit = positive(gateway_output_limit)
        self.context_store = context_store
        self._gate = Lock()
        self._in_flight = 0
        self._maintenance = False
        migrate_model_control(self.db)

    def begin_inference(self):
        with self._gate:
            if self._maintenance or DEPLOYMENT_HOLD.exists() or MODEL_MAINTENANCE.exists() or (self.config_path == MODEL_SPEC and ROLLBACK_PENDING.exists()):
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
        unit = MODEL_UNIT if policy else self.runtime_unit
        index = policy.gpu_index if policy else self.gpu_index
        runtime = read_runtime(self.base, self.token, self.model_id, unit, index, verify_environment=bool(policy))
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
            allowed = not ROLLBACK_PENDING.exists()
            detail = "" if allowed else "上次模型重启未完成恢复。模型请求已暂停，等待运维检查。"
            if ROLLBACK_PENDING.exists():
                detail = "上次模型重启未完成恢复。模型请求已暂停，等待运维检查。"
        except ModelControlError as error:
            policy, allowed, detail = None, False, str(error)
        runtime = self._runtime(policy)
        result = _public(runtime, allowed, detail)
        if DEPLOYMENT_HOLD.exists():
            result.update(control_available=False,
                          detail="服务器忙碌：运维正在验证 Gateway 更新，暂不接受新任务或模型配置变更。")
        result["deployment_hold"] = DEPLOYMENT_HOLD.exists()
        result["model_maintenance"] = MODEL_MAINTENANCE.exists()
        if result["model_maintenance"]:
            result.update(control_available=False, detail="模型处于受控维护状态；新任务和直连模型请求暂停，请等待配置核验或恢复完成。")
        result["configuration_version"] = self._version(runtime)
        with closing(sqlite3.connect(self.db)) as db:
            last_applied = db.execute("SELECT max(occurred) FROM model_configuration_audit "
                                      "WHERE result = 'applied'").fetchone()[0]
            last = db.execute("SELECT result, reason FROM model_configuration_audit "
                              "WHERE result <> 'requested' ORDER BY id DESC LIMIT 1").fetchone()
        result["last_applied_at"] = last_applied
        result["last_configuration_result"] = {"status": last[0], "reason": last[1]} if last else None
        saved = None
        if policy:
            try:
                saved = context_env(_root_file(CONTEXT_ENV))
            except (OSError, ValueError):
                pass
        observed = runtime.get("_observed_slot_context") if runtime.get("_uniform_context") else None
        matched = (saved is not None and runtime["status"] == "running" and observed is not None
                   and saved == {"total_context": runtime["total_context"], "slots": runtime["slots"]}
                   and observed == slot_context(saved["total_context"], saved["slots"]))
        result["context_sync"] = {
            "saved_configuration": saved,
            "backend": {"status": "synced" if matched else "out_of_sync" if saved and observed else "unverified",
                        "effective_context_limit": observed, "instance_pid": runtime.get("_pid"),
                        "ready_checked_at": runtime.get("ready_checked_at")},
            "builtin": {"status": "new_task" if observed else "unavailable",
                        "context_limit": observed, "effective_context_limit": None},
            "opencode": (getattr(self.context_store, "runtime_status", self.context_store.status)(observed) if self.context_store else
                         {"managed": False, "status": "unmanaged", "effective_context_limit": None,
                          "detail": "未纳管，未修改独立 OpenCode。"}),
        }
        return result

    def _version(self, runtime):
        # Include the audit generation as well as the instance: A->B->A and
        # a same-target repair must invalidate older administrator operations.
        with closing(sqlite3.connect(self.db)) as db:
            generation = db.execute("SELECT coalesce(max(id), 0) FROM model_configuration_audit "
                                    "WHERE result <> 'requested'").fetchone()[0]
        values = [generation, runtime.get("_pid"), runtime.get("_config"),
                  runtime.get("_observed_slot_context"), runtime.get("slots")]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def _check_decrease(self, runtime, requested_slot_context):
        if requested_slot_context < runtime["_observed_slot_context"]:
            policy = self._policy()
            if (policy.managed_opencode_config is None or self.context_store is None
                    or self.context_store.path != policy.managed_opencode_config):
                raise ModelControlError("降容已阻止，原配置未改动：受控 helper 尚未纳管指定独立 OpenCode，或与 Gateway 纳管路径不一致。请安装配套维护配置。", 409)

    def _check_pending(self):
        if MODEL_MAINTENANCE.exists():
            raise ModelControlError("模型维护尚未结束，请等待核验或由运维恢复；新请求继续暂停。", 503)
        if DEPLOYMENT_HOLD.exists():
            raise ModelControlError("服务器忙碌：Gateway 更新尚未完成核验，请稍后重试。", 503)
        if ROLLBACK_PENDING.exists():
            raise ModelControlError("上次模型重启未完成恢复。请运维检查后再试。", 409)

    def preview(self, actor, total_context, slots, expected_version=None):
        if type(total_context) is not int or total_context not in CONTEXT_OPTIONS \
                or type(slots) is not int or not MIN_SLOTS <= slots <= MAX_SLOTS:
            raise ModelControlError("上下文或槽位数超出可选范围。")
        policy = self._policy()
        self._check_pending()
        runtime = self._runtime(policy)
        version = self._version(runtime)
        if expected_version is not None and expected_version != version:
            raise ModelControlError("配置版本已变更，请刷新后重新预览；旧操作未应用。", 409)
        current, requested_slot_context = validate_change(runtime, total_context, slots)
        self._check_decrease(runtime, requested_slot_context)
        restart = (current["total_context"], current["slots"]) != (total_context, slots)
        token = secrets.token_urlsafe(32)
        payload = dict(old_context=current["total_context"], old_slots=current["slots"],
                       new_context=total_context, new_slots=slots, policy=policy.digest, version=version)
        with closing(sqlite3.connect(self.db)) as db, db:
            now = time()
            db.execute("DELETE FROM model_confirmations WHERE expires <= ? OR actor = ?", (now, actor))
            db.execute("INSERT INTO model_confirmations VALUES (?, ?, ?, ?)",
                       (self._digest(token), actor, now + CONFIRM_SECONDS, json.dumps(payload)))
        return {
            "confirmation": token, "current": _public(runtime, True, ""),
            "requested": {"total_context": total_context, "slots": slots},
            "estimated_slot_context": requested_slot_context,
            "configuration_version": version,
            "gpu_risk": "未测量目标配置的显存峰值。提高上下文或槽位可能导致显存不足、模型重启失败。",
            "restart_required": restart,
            "opencode": (self.context_store.status(requested_slot_context) if self.context_store else
                         {"managed": False, "status": "unmanaged"}),
            "user_impact": ("安全降容需重启模型：请先正常退出服务器独立 OpenCode；若 Ubuntu Labcoding 内置引擎仍运行，也需正常退出该客户端。确认后服务器检查全部任务/租约/实际请求，临时封闭直连入口，核实 OpenCode 进程已退出后才应用。完成后重新启动或连接客户端；会话保留。"
                            if requested_slot_context < runtime["_observed_slot_context"] else
                            "会重启模型服务。维护期间所有模型请求暂停，服务缓存会清空。" if restart else
                            "后端参数未变，仅重新核验并同步 OpenCode 配置，不重启模型。"),
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
                ["/usr/bin/sudo", "-n", "--", CONTROLLER],
                input=json.dumps(payload, separators=(",", ":")), timeout=250, encoding="utf-8",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
            )
            result = json.loads(process.stdout[:4096])
            if (isinstance(result, dict) and set(result) == {"status", "reason"}
                    and result["status"] in OUTCOMES and isinstance(result["reason"], str)):
                if result["status"] == "applied" and process.returncode != 0:
                    return {"status": "unknown", "reason": "unknown"}
                return {"status": result["status"],
                        "reason": result["reason"] if result["reason"] in REASONS else "unknown"}
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
            self._check_pending()
            payload = self._consume(actor, confirmation)
            if payload["policy"] != policy.digest:
                raise ModelControlError("服务器模型控制配置已经变更。请重新预览。", 409)
            before = self._runtime(policy)
            if payload.get("version") != self._version(before):
                raise ModelControlError("配置版本或后端实例已变更，请重新预览；旧操作未应用。", 409)
            current, requested_context = validate_change(before, payload["new_context"], payload["new_slots"],
                                         require_idle=True)
            self._check_decrease(before, requested_context)
            if (current["total_context"], current["slots"]) != (payload["old_context"], payload["old_slots"]):
                raise ModelControlError("服务基线已变更。请重新预览。", 409)
            self._audit(actor, payload, "requested")
            unchanged = (payload["old_context"], payload["old_slots"]) == (payload["new_context"], payload["new_slots"])
            decreasing = requested_context < before["_observed_slot_context"]
            helper_payload = {key: payload[key] for key in ("old_context", "old_slots", "new_context", "new_slots", "policy")}
            # The helper owns backend + file rollback during a decrease. Keep
            # output-setting writes in this Gateway out of its read/modify/write.
            with (self.context_store.transaction() if decreasing else nullcontext()):
                result = {"status": "applied"} if unchanged else self._invoke(helper_payload)
            outcome = result["status"]
            self._audit(actor, payload, outcome, result.get("reason", ""))
            if outcome == "applied":
                live = self._runtime(policy)
                target = {"total_context": payload["new_context"], "slots": payload["new_slots"]}
                context = live.get("_observed_slot_context")
                if (live["status"] != "running" or live.get("_config") != target
                        or (live["total_context"], live["slots"]) != (payload["new_context"], payload["new_slots"])
                        or not live.get("_uniform_context")
                        or context != slot_context(payload["new_context"], payload["new_slots"])):
                    raise ModelControlError("后端报告已应用，但实际上下文尚未确认生效；未同步 OpenCode。", 503)
                sync_error = False
                sync_reason = ""
                if self.context_store and not decreasing:
                    try:
                        self.context_store.sync(context)
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        sync_error = True
                        sync_reason = ("文件权限不足" if isinstance(error, PermissionError) else
                                       "文件不存在" if isinstance(error, FileNotFoundError) else
                                       "文件读写失败" if isinstance(error, OSError) else "模型上下文或输出配置无效")
                        self._audit(actor, payload, "sync_failed", "opencode_config_failed")
                if self.context_store and not sync_error:
                    observe = getattr(self.context_store, "synchronize_runtime", None)
                    if observe:
                        observe(context)
                # Fresh telemetry includes GPU usage after verification and config readback.
                values = self.status()
                if sync_error:
                    values["context_sync"]["opencode"].update(
                        status="error", detail=f"独立 OpenCode 文件同步失败：{sync_reason}。增容后保留健康后端，可补同步；运行值未核实。")
                return {"status": "partially_applied" if sync_error else outcome,
                        "configuration_saved": {"backend": True,
                                                "opencode": (not sync_error if self.context_store
                                                             and values["context_sync"]["opencode"]["managed"] else None)},
                        "runtime_loaded": {"opencode": (True if values["context_sync"]["opencode"]["status"] == "synced" else None),
                                           "builtin": None},
                        "fully_synchronized": False,
                        "configuration_errors": ([{"component": "independent_opencode_file", "reason": sync_reason}]
                                                 if sync_error else []), "model": values}
            messages = {
                "rolled_back": "新配置启动或校验失败，已经恢复并验证原配置。",
                "rollback_failed": "新配置启动失败，恢复校验也未通过。请运维从备份检查。",
                "refused": "固定模型控制程序拒绝应用，请运维检查模型状态和服务配置。",
                "unknown": "无法确认服务器控制组件响应。请运维检查配置与运行状态。",
            }
            reasons = {
                "independent_running": "检测到服务器 OpenCode 进程仍在运行。请正常退出独立 OpenCode；若旧 Ubuntu Labcoding 内置引擎仍运行，请正常退出该客户端，然后重新预览。未强杀进程。",
                "process_check_failed": "无法核实服务器 OpenCode 进程是否已退出，原配置未改动。",
                "maintenance_not_enrolled": "helper 尚未纳管指定独立 OpenCode，原配置未改动。",
                "managed_config_failed": "独立 OpenCode 配置读写或核验失败。",
                "managed_config_permission": "独立 OpenCode 文件所有者、0600权限或目录访问权限不满足要求。",
                "managed_config_missing": "纳管的独立 OpenCode 配置文件不存在。",
                "managed_config_io": "独立 OpenCode 配置发生文件读写错误。",
                "managed_config_mismatch": "独立 OpenCode 文件与当前实际每槽上下文不一致，请先对当前配置执行重新同步，再重新预览降容。",
                "model_busy": "模型仍有实际请求或无法确认空闲，请等待请求结束后重新预览。",
                "direct_guard_failed": "无法建立固定模型端口的直连维护屏障，请检查 nftables 或遗留维护状态。",
                "direct_guard_release_failed": "直连维护屏障解除失败，请运维核验恢复状态。",
            }
            detail = reasons.get(result.get("reason"), "")
            raise ModelControlError(messages[outcome] + (" " + detail if detail else ""), 503)
        finally:
            with self._gate:
                self._maintenance = False
