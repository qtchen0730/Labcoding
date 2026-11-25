"""Read and update the server user's independent OpenCode output budget."""

import json
import os
from pathlib import Path
import tempfile
import time
from threading import RLock


# Both admin settings operations update the same document in the single Gateway worker.
_CONFIG_LOCK = RLock()


def opencode_config_path(home=None):
    return Path(home or Path.home()) / ".config" / "opencode" / "opencode.json"


def read_output_limit(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    return config["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"]


def write_output_limit(path, value):
    with _CONFIG_LOCK:
        path = Path(path)
        config = json.loads(path.read_text(encoding="utf-8"))
        config["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"] = value
        _write_config(path, config)
        return value


def _write_config(path, config):
    encoded = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".opencode-output-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class OpenCodeContext:
    """Only the operator-enrolled JSON file; no path is accepted from an API request."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.runtime = None

    def transaction(self):
        return _CONFIG_LOCK

    def _read(self):
        if (self.path.is_symlink() or not self.path.is_file()
                or self.path.stat().st_size > 1024 * 1024):
            raise ValueError("invalid managed config")
        config = json.loads(self.path.read_text(encoding="utf-8"))
        limits = config["provider"]["ccslab"]["models"]["lab-general"]["limit"]
        if not isinstance(limits, dict):
            raise ValueError("invalid model limits")
        return config, limits

    def status(self, context):
        if self.path is None:
            return {"managed": False, "status": "unmanaged", "configured_context_limit": None,
                    "effective_context_limit": None, "detail": "未纳管，未修改独立 OpenCode。"}
        result = {"managed": True, "status": "error", "configured_context_limit": None,
                  "configured_input_limit": None, "effective_context_limit": None,
                  "detail": "无法读取或校验受管 OpenCode 配置，请检查文件和权限后重试。"}
        try:
            with _CONFIG_LOCK:
                _, limits = self._read()
            values = [limits.get(key) for key in ("context", "input", "output")]
            if not all(type(values[i]) is int and values[i] > 0 for i in (0, 2)):
                return result
            result.update(configured_context_limit=values[0], configured_input_limit=values[1])
            if context is None:
                result.update(status="unverified", detail="模型实际每槽上下文不可用，尚无法核对配置同步状态。")
                return result
            matched = values[0] == context and values[2] < context
            result.update(status="pending_reload" if matched else "out_of_sync",
                          detail=("配置已同步；独立 OpenCode 需重新启动/加载，运行值未核实。" if matched
                                  else "配置未与模型实际每槽上下文同步。"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return result

    def runtime_status(self, context):
        """Gateway-only observation; the fixed helper keeps file-only readback."""
        state = self.status(context)
        if self.runtime is None and os.name == 'posix' and self.path == Path('/home/server/.config/opencode/opencode.json'):
            from .opencode_runtime import OpenCodeRuntime
            self.runtime = OpenCodeRuntime(self.path)
        if self.runtime is not None and state['managed']:
            runtime = self.runtime.status(context)
            state.update({key: value for key, value in runtime.items() if key not in ('status', 'detail')})
            if state['status'] == 'pending_reload':
                state.update(status=runtime['status'], detail=runtime['detail'])
            else:
                state['detail'] += ' ' + runtime['detail']
        return state

    def synchronize_runtime(self, context):
        """Wait briefly for native file observation; never force location.reload."""
        deadline = time.monotonic() + 2
        while True:
            state = self.runtime_status(context)
            if (self.runtime is None or state['status'] not in {'pending_reload', 'unverified', 'partial'}
                    or time.monotonic() >= deadline):
                return state
            time.sleep(.15)

    def sync(self, context):
        if self.path is None:
            return
        with _CONFIG_LOCK:
            config, limits = self._read()
            output = limits.get("output")
            if type(output) is not int or not 0 < output < context:
                raise ValueError("independent output budget exceeds context")
            if limits.get("context") != context:
                # This adapter owns only context. Preserve input, output, keys,
                # compaction and all other operator settings, including absence.
                if os.name == "posix" and self.path.stat().st_uid != os.geteuid():
                    raise PermissionError("managed config must be owned by the service user")
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".opencode-context-backup-",
                                                 delete=False) as backup:
                    backup.write(self.path.read_bytes())
                    backup.flush()
                    os.fsync(backup.fileno())
                limits["context"] = context
                _write_config(self.path, config)
            state = self.status(context)
            if state["status"] != "pending_reload":
                raise ValueError("managed context readback mismatch")
