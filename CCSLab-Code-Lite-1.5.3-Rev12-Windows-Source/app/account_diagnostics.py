"""Read-only connection diagnostics through the configured account transport."""

from dataclasses import dataclass
from datetime import datetime
import platform
import re
from time import monotonic

from account_client import ACCOUNT_STATUS, DEFAULT_SERVER, AccountClient, AccountError, normalize_server


_CHECKS = {
    "address": "服务器地址",
    "dns": "DNS 解析",
    "tcp": "TCP 连接",
    "tls": "HTTPS 证书",
    "health": "Gateway 健康检查",
    "account": "账号接口",
    "auth": "用户认证",
    "model": "模型服务",
}
_STATES = {
    "normal": "正常", "warning": "警告", "failure": "失败",
    "unchecked": "未检查", "na": "不适用",
}
_MODES = {"auto": "自动", "system": "系统代理", "direct": "直连"}
_ROUTES = {
    "direct": "直连", "system": "系统代理", "environment": "环境变量代理", "proxy": "HTTP 代理",
    "直连": "直连", "系统代理": "系统代理", "环境变量代理": "环境变量代理", "HTTP 代理": "HTTP 代理",
}
_NETWORK = {
    "dns": "无法解析连接使用的域名。请检查 DNS 和所选连接模式。",
    "tcp": "无法建立 TCP 连接。请检查网络、端口或网关状态。",
    "timeout": "连接或等待响应超时。请稍后重试。",
    "proxy": "当前网络代理无法建立连接。请检查代理状态或尝试其他连接模式。",
    "certificate": "服务器 HTTPS 证书验证失败。",
    "tls": "HTTPS 握手失败。请检查服务器协议、端口和网络。",
    "response": "服务器响应不符合预期。请确认域名指向账号网关。",
    "invalid_health": '健康检查未返回预期的 JSON {"ok": true}。',
    "transport": "连接中断。请检查网络或重试。",
    "connection": "连接中断。请检查网络或重试。",
    "unknown": "无法完成连接检查，请稍后重试。",
}
_HTTP = {
    401: "登录无效或已过期，也可能由网关访问策略拒绝。",
    403: "当前账号或网络没有访问权限。",
    404: "请求接口不存在。请检查客户端和服务端版本及路由。",
    405: "服务器不支持该请求方法。请检查接口配置。",
    429: "请求过于频繁，请稍后重试。",
    502: "网关或上游服务暂时不可用。",
    503: "网关或模型服务正在维护或暂时不可用。",
    504: "网关等待上游服务响应超时。",
    525: "Cloudflare 与回源服务的 TLS 握手失败。",
    526: "Cloudflare 无法验证回源服务证书。",
    530: "Cloudflare 无法连接回源服务。请让管理员检查 Tunnel 状态。",
}
_VERIFY_CODES = {
    9: "证书尚未生效，请检查电脑时间。",
    10: "证书已过期，请检查电脑时间和证书有效期。",
    18: "证书为自签名，且不在受信任的证书链中。",
    19: "证书链包含不受信任的自签名证书。",
    20: "无法建立到受信任签发机构的证书链。",
    21: "无法验证服务器证书的签发链。",
    23: "服务器证书已被吊销。",
    62: "证书域名与服务器地址不匹配。",
    64: "证书 IP 地址与服务器地址不匹配。",
}
_MODEL_STATES = {
    "running": "运行中", "sleeping": "休眠中", "unavailable": "暂不可用",
    "maintenance": "维护中", "starting": "启动中", "stopped": "已停止",
}
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|authorization|bearer|session[ _-]?token|api[ _-]?key|"
    r"access[ _-]?token|refresh[ _-]?token|secret|口令|密码|令牌|密钥)", re.IGNORECASE,
)
_TOKEN_LIKE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]{24,}(?![A-Za-z0-9_])")
_VERIFY_CODE = re.compile(r"\bverify(?:_code)?\s*[=:]\s*([0-9]{1,3})\b", re.IGNORECASE)
_NETWORK_CODE = re.compile(r"\b(errno|winerror|connect_status|code)=(-?[0-9]{1,6})\b")
_TLS_VERSION = re.compile(r"^TLSv1\.[0-3]\b")


@dataclass(frozen=True)
class DiagnosticCheck:
    stage: str
    state: str
    detail: str
    elapsed_ms: int | None = None

    @property
    def name(self):
        return _CHECKS[self.stage]

    @property
    def status(self):
        return _STATES[self.state]


@dataclass(frozen=True)
class DiagnosticReport:
    rows: tuple[DiagnosticCheck, ...]
    version: str
    platform: str
    server: str
    mode: str
    route: str
    fallback: bool
    started_at: str
    elapsed_ms: int
    error: str
    summary: str
    connection_error: str
    healthy: bool

    def to_text(self):
        route = self.route or "未建立"
        if self.fallback:
            route += "（自动模式回退到直连）"
        header = [
            "CCSLab Code Lite 连接诊断",
            f"软件版本  {self.version}",
            f"操作系统  {self.platform}",
            f"服务器地址  {self.server or '无有效地址'}",
            f"连接模式  {_MODES.get(self.mode, '未知')}",
            f"检测路由  {route}",
            f"诊断时间  {self.started_at}",
            f"总耗时  {self.elapsed_ms} ms",
            f"错误类型  {self.error or '无'}",
            "",
        ]
        for row in self.rows:
            elapsed = f"  {row.elapsed_ms} ms" if row.elapsed_ms is not None else ""
            header.append(f"{row.name}  {row.status}{elapsed}")
            if row.detail:
                header.append(f"  {row.detail}")
        return "\n".join(header)


def _ms(started):
    return round((monotonic() - started) * 1000)


def _state(value):
    return value if isinstance(value, str) and value in _STATES else "unchecked"


def _integer(value, minimum=1, maximum=2_000_000_000):
    return value if type(value) is int and minimum <= value <= maximum else None


def _route(value):
    if not isinstance(value, str):
        return ""
    return _ROUTES.get(value, "")


def _status(issue):
    return _integer(getattr(issue, "status", None), 100, 599)


def _category(issue):
    category = getattr(issue, "category", "")
    status = _status(issue)
    if status and status >= 300:
        return f"http_{status}"
    return category if isinstance(category, str) and category in _NETWORK else "unknown"


def _error_details(issue):
    category = _category(issue)
    if category.startswith("http_"):
        status = _status(issue)
        if 300 <= status < 400:
            text = "网关返回了重定向。请核对账号 API 域名和访问策略。"
        else:
            text = _HTTP.get(status, "网关返回异常响应。")
        return f"{text}（HTTP {status}）"
    text = _NETWORK[category]
    tech = getattr(issue, "technical", "")
    tech = tech[:1000] if isinstance(tech, str) else ""
    if category == "certificate":
        code = _VERIFY_CODE.search(tech)
        if code:
            number = int(code.group(1))
            text += f" {_VERIFY_CODES.get(number, '请核对证书链和服务器域名。')}（证书验证代码 {number}）"
    elif category == "dns":
        route = _route(getattr(issue, "route", ""))
        if route == "直连":
            text = "系统 DNS 未能解析服务器域名。请检查当前 Windows 的 DNS 配置。"
        elif route:
            text = "Windows 未能解析代理服务器。代理后的目标域名由代理端解析。"
    elif category == "proxy":
        if getattr(issue, "stage", "") == "route":
            text = "Windows 代理发现或配置失败。请检查代理设置，或尝试其他连接模式。"
        elif getattr(issue, "stage", "") == "dns":
            text = "Windows DNS 未能解析当前代理主机。Gateway 域名由代理端解析。"
        elif "connect_status=407" in tech:
            text = "代理需要认证。请检查 Windows 或环境变量中的代理配置。"
    codes = list(dict.fromkeys(_NETWORK_CODE.findall(tech)))[:3]
    if codes:
        text += "（" + "，".join(f"{name}={int(value)}" for name, value in codes) + "）"
    return text


def _failing_stage(issue):
    if _category(issue).startswith("http_"):
        return "health"
    stage = getattr(issue, "stage", "")
    stages = {"dns": "dns", "tcp": "tcp", "connect": "tcp", "proxy": "tcp", "route": "health",
              "tls": "tls", "certificate": "tls", "health": "health", "http": "health"}
    if isinstance(stage, str) and stage in stages:
        return stages[stage]
    return {
        "dns": "dns", "tcp": "tcp", "proxy": "tcp", "tls": "tls", "certificate": "tls",
    }.get(_category(issue), "health")


def _model_text(value, token):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        return None
    if (token and token in value) or _SENSITIVE_FIELD.search(value) or _TOKEN_LIKE.search(value):
        return None
    # A model label is text, never a URL, header, path, raw body, or multiline value.
    if not all(char.isalnum() or char in " ._+-()[]" for char in value):
        return None
    return value.strip() or None


def _model_detail(model, token):
    name = _model_text(model.get("model_name"), token)
    model_id = _model_text(model.get("model_id"), token)
    service = model.get("status")
    status = _MODEL_STATES.get(service, "暂不可用") if isinstance(service, str) else "暂不可用"
    total = _integer(model.get("total_context"))
    slots = _integer(model.get("slots"), maximum=4096)
    context = _integer(model.get("effective_context_limit"))
    gpu = model.get("gpu")
    used, memory = None, None
    if isinstance(gpu, dict):
        used = _integer(gpu.get("used_mib"), 0)
        memory = _integer(gpu.get("total_mib"))
    memory_text = f"{used:,} / {memory:,} MiB" if used is not None and memory is not None and used <= memory else ""
    fields = (
        ("模型名称", name), ("模型 ID", model_id), ("状态", status),
        ("总上下文", f"{total:,} Token" if total else None), ("并发槽位", slots),
        ("每槽上下文", f"{context:,} Token" if context else None), ("GPU 显存", memory_text),
    )
    detail = "；".join(f"{name} {value if value else '暂不可用'}" for name, value in fields)
    if service != "running":
        return "warning", detail
    return ("normal" if all(value for _, value in fields) else "warning"), detail


class _Run:
    def __init__(self, mode, version, full):
        self.started = monotonic()
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.mode = mode if isinstance(mode, str) and mode in _MODES else "auto"
        self.version = version if isinstance(version, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) else "1.5.3"
        self.full = full
        self.server = ""
        self.route = ""
        self.fallback = False
        self.error = ""
        self.rows = {key: DiagnosticCheck(key, "unchecked", "") for key in _CHECKS}

    def write(self, stage, state, detail, start=None):
        self.rows[stage] = DiagnosticCheck(stage, state, detail, _ms(start) if start is not None else None)

    def observe(self, stage, state, detail):
        # The transport reports the stage of the actual login route. Keep only known
        # stage/state values. Observer messages are intentionally never copied.
        if stage == "route":
            label = {
                "当前路线：直连。": "直连",
                "当前路线：Windows 系统代理。": "系统代理",
                "当前路线：环境变量代理。": "环境变量代理",
            }.get(detail) if isinstance(detail, str) else None
            self.route = _route(detail) or label or self.route
            self.fallback = self.fallback or state == "warning"
            if self.fallback:
                self.route = "直连"
            return
        if stage not in ("dns", "tcp", "tls"):
            return
        state = _state(state)
        states = {
            "dns": {
                "normal": "Windows DNS 已解析本次连接使用的主机。",
                "warning": "代理端目标解析无法独立验证。",
                "na": "目标解析由代理处理，或连接地址为 IP。",
            },
            "tcp": {
                "normal": "当前连接路由已建立 TCP 连接。",
                "warning": "当前路由的 TCP 状态需要进一步检查。",
            },
            "tls": {
                "normal": "已通过 TLS 握手、证书链、地址匹配和有效期验证。",
                "warning": "TLS 握手尚未完成验证。",
            },
        }
        message = states.get(stage, {}).get(state, "")
        if stage == "tls" and state == "normal" and isinstance(detail, str):
            version = _TLS_VERSION.match(detail)
            if version:
                message = f"{version.group()} · {message}"
        self.write(stage, state, message)

    def run(self, client):
        started = monotonic()
        try:
            result = client.health(observe=self.observe)
            if not isinstance(result, dict) or result.get("ok") is not True:
                self.error = "response"
                self.write("health", "failure", '预期返回 JSON {"ok": true}。', started)
            else:
                self.write("health", "normal", 'HTTP 200，已验证 JSON {"ok": true}。', started)
        except AccountError as issue:
            self.error = _category(issue)
            self.route = _route(getattr(issue, "route", "")) or self.route
            stage = _failing_stage(issue)
            detail = _error_details(issue)
            self.write(stage, "failure", detail, started)
            if stage != "health":
                self.write("health", "unchecked", "连接未完成，未收到健康检查响应。")
        if self.rows["health"].state == "normal":
            self.route = self.route or _route(getattr(client, "route", ""))
        self.fallback = self.fallback or getattr(client, "fallback", False) is True
        self._describe_dns()
        if self.rows["health"].state != "normal":
            return
        if not self.full:
            for stage in ("account", "auth", "model"):
                self.write(stage, "unchecked", "运行完整诊断可检查此项。")
            return
        self._account_api(client)

    def _describe_dns(self):
        row = self.rows["dns"]
        if row.state not in {"normal", "na"} or not self.route:
            return
        if self.route == "直连":
            detail = ("Windows 系统 DNS 已解析 Gateway 域名。"
                      if row.state == "normal" else "Gateway 地址已经是 IP，无需 DNS 检查。")
        else:
            prefix = "Windows 系统 DNS 已解析代理主机。" if row.state == "normal" else "使用配置的代理 IP。"
            detail = prefix + "Gateway 域名由代理端解析。"
        self.rows["dns"] = DiagnosticCheck(row.stage, row.state, detail, row.elapsed_ms)

    def _account_api(self, client):
        started = monotonic()
        try:
            client.request("GET", "/api/me", auth=False)
        except AccountError as issue:
            if _status(issue) == 401 and getattr(issue, "api_error", False) is True:
                self.write("account", "normal", "GET /api/me 返回预期的 HTTP 401 账号 API 响应。", started)
            else:
                self.error = _category(issue)
                self.write("account", "failure", _error_details(issue), started)
                return
        else:
            self.error = "account_api"
            self.write("account", "failure", "GET /api/me 未按预期拒绝匿名请求。请检查 Gateway 路由。", started)
            return

        if not client.token:
            self.write("auth", "warning", "未登录。")
            self.write("model", "na", "登录并激活账号后可检查模型服务。")
            return
        started = monotonic()
        try:
            user = client.me()
        except AccountError as issue:
            status = _status(issue)
            text = _error_details(issue)
            if status == 401 and getattr(issue, "api_error", False) is True:
                text = "登录已过期或会话已撤销，请重新登录。（HTTP 401）"
            elif status == 403:
                text = "当前认证没有访问权限。（HTTP 403）"
            self.error = _category(issue)
            self.write("auth", "failure", text, started)
            self.write("model", "na", "需要有效的已激活会话。")
            return
        state = user.get("status") if isinstance(user, dict) else None
        label = ACCOUNT_STATUS.get(state) if isinstance(state, str) else None
        if not label:
            self.error = "auth_response"
            self.write("auth", "failure", "网关未返回可识别的账号状态。", started)
            return
        self.write("auth", "normal" if state == "active" else "warning", f"账号{label}。", started)
        if state != "active":
            self.write("model", "na", "该账号状态没有模型访问权限。")
            return
        self._model(client)

    def _model(self, client):
        started = monotonic()
        try:
            model = client.model()
        except AccountError as issue:
            self.error = _category(issue)
            status = _status(issue)
            self.write("model", "na" if status in (401, 403) else "failure", _error_details(issue), started)
            return
        if not isinstance(model, dict) or not model:
            self.error = "model_response"
            self.write("model", "warning", "Gateway 未返回模型状态或遥测数据。", started)
            return
        state, detail = _model_detail(model, client.token)
        self.write("model", state, detail, started)

    def report(self):
        healthy = self.rows["health"].state == "normal"
        if healthy:
            summary = "Gateway 连接正常"
            if self.route:
                summary += f"（{self.route}）"
            if self.fallback:
                summary += "。自动模式已回退到直连"
            account = self.rows["account"]
            if account.state == "failure":
                summary += "，账号接口检查未通过"
                error = account.detail
            else:
                error = ""
            summary += "。"
        else:
            error = next((row.detail for row in self.rows.values() if row.state == "failure"),
                         "无法完成连接检查，请稍后重试。")
            if self.fallback:
                error += "自动模式已尝试代理和直连。"
            summary = error
        return DiagnosticReport(tuple(self.rows.values()), self.version,
                                f"{platform.system()} {platform.release()} ({platform.version()})",
                                self.server, self.mode, self.route, self.fallback, self.started_at,
                                _ms(self.started), self.error, summary, error, healthy)


def _address(run, server):
    try:
        run.server = normalize_server(server if server is not None else DEFAULT_SERVER)
    except AccountError:
        run.error = "address"
        run.write("address", "failure", "请输入完整且有效的 HTTPS 服务器地址。")
        return False
    run.write("address", "normal", "HTTPS URL、主机和端口格式有效。")
    return True


def unavailable_report(server, *, mode="auto", version="1.5.3", phase="health"):
    """Return a shareable report when the client or local TLS runtime cannot initialize."""
    run = _Run(mode, version, False)
    if _address(run, server):
        if phase == "tls":
            run.error = "client_tls"
            run.write("tls", "failure", "客户端无法初始化 TLS 验证。请检查 Windows 证书服务。")
        else:
            run.error = "diagnostic"
            run.write("health", "failure", "客户端无法运行连接检查，请稍后重试。")
    return run.report()


def run_diagnostics(client=None, *, server=None, mode="auto", version="1.5.3", full=True):
    """Collect observable connection facts; no credentials or response bodies are reported."""
    run = _Run(getattr(client, "mode", mode), version, full)
    if not _address(run, getattr(client, "base", server)):
        return run.report()
    try:
        if client is None:
            client = AccountClient(run.server, mode=run.mode)
    except AccountError as issue:
        run.error = _category(issue)
        run.write(_failing_stage(issue), "failure", _error_details(issue))
        return run.report()
    except OSError:
        return unavailable_report(run.server, mode=run.mode, version=version, phase="tls")
    run.run(client)
    return run.report()
