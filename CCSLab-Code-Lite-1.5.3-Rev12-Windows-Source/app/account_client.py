"""HTTPS transport for the shared CCSLab account service."""

from http.client import HTTPException
import json
import re
import socket
import ssl
from time import time
from urllib.parse import urlsplit

from account_transport import AccountTransport, CONNECTION_MODES, TransportError, normalize_host, valid_authority


DEFAULT_SERVER = "https://ccslab-code.qtchen.cn"
LEGACY_DEFAULT_SERVER = "https://192.168.1.107:8443"
ACCOUNT_STATUS = {
    "pending": "待审批", "active": "已激活", "rejected": "已拒绝", "disabled": "已禁用",
}
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128
PASSWORD_RANGE = f"{PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} 个字符"
_WEBUI_GRANT = re.compile(r"[A-Za-z0-9_.-]{32,1024}")
_GATEWAY_UPGRADE = "账号网关需要更新到配套的 1.5.3 修订版，才能使用此功能。"


class AccountError(RuntimeError):
    def __init__(self, message, status=0, *, category=None, stage="http",
                 technical="", route="", api_error=False):
        super().__init__(message)
        self.status = status
        self.category = category or _http_category(status)
        self.stage = stage
        self.technical = technical
        self.route = route
        self.api_error = api_error

    def with_message(self, message):
        return AccountError(message, self.status, category=self.category, stage=self.stage,
                            technical=self.technical, route=self.route, api_error=self.api_error)

    @classmethod
    def from_transport(cls, error):
        return cls(str(error), category=error.category, stage=error.stage,
                   technical=error.technical, route=error.route)


def _http_category(status):
    if status == 0:
        return "connection"
    if 500 <= status < 600:
        return "unavailable"
    if 300 <= status < 400:
        return "redirect"
    return {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method",
            429: "rate_limit"}.get(status, "response")


def normalize_server(value):
    try:
        text = value.strip().rstrip("/")
        if any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in text):
            raise ValueError
        url = urlsplit(text)
        if (
            url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.path or url.query or url.fragment
            or "?" in text or "#" in text or not valid_authority(url.netloc)
        ):
            raise ValueError
        port = url.port
        if port == 0:
            raise ValueError
        host = normalize_host(url.hostname)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise AccountError("请输入有效的 HTTPS 服务器地址，例如 " + DEFAULT_SERVER,
                           category="invalid_url", stage="url") from None
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}" + (f":{port}" if port else "")


def preferred_account_server(prefs):
    saved = prefs.get("account_server")
    if not isinstance(saved, str) or not saved.strip():
        return DEFAULT_SERVER
    if not prefs.get("account_server_manual") and saved.strip().rstrip("/").lower() == LEGACY_DEFAULT_SERVER:
        return DEFAULT_SERVER
    return saved


def valid_account_password(password):
    if not isinstance(password, str) or "\0" in password:
        return False
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        return False
    try:
        password.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _gateway_error(status, *, edge_code, cloudflare, detail):
    if 300 <= status < 400:
        return (f"账号网关返回了跳转（HTTP {status}）。请核对发布的是 CCSLab 账号 API；"
                "如配置了 Cloudflare Access，请让管理员检查桌面客户端的访问方式。")
    if edge_code == "1033":
        return "Cloudflare Tunnel 未连接（1033）。请检查 cloudflared 服务和 Tunnel 状态。"
    if cloudflare and status == 530:
        return "Cloudflare 无法连接账号网关（HTTP 530）。请检查域名的 Tunnel 路由和 cloudflared 状态。"
    if cloudflare and status in {525, 526}:
        return f"Cloudflare 与账号网关之间的 TLS 验证失败（HTTP {status}）。请检查 Tunnel 的源站协议和证书。"
    if cloudflare and (edge_code or not detail) and status in {502, 503, 504, 520, 521, 522, 523, 524}:
        return f"Cloudflare 暂时无法访问账号网关（HTTP {status}）。请检查 Tunnel 路由和源站服务是否正在监听。"
    if status == 405 or (status == 404 and detail.casefold() in {"", "not found"}):
        return f"账号 API 路由不可用（HTTP {status}）。请在设置中确认账号网关地址，并检查反向代理是否转发账号 API。"
    if cloudflare and status in {401, 403} and (edge_code or not detail):
        return f"Cloudflare 拒绝了请求（HTTP {status}）。请让管理员检查该域名的 Access 或 WAF 策略。"
    return detail or {
        401: "登录无效或已过期，请重新登录。",
        403: "该账号没有此操作权限。",
        429: "请求过于频繁，请稍后再试。",
        502: "账号网关暂不可用（HTTP 502）。请检查服务和反向代理。",
        503: "账号网关暂不可用（HTTP 503）。请确认服务正在运行。",
        504: "账号网关响应超时（HTTP 504）。请检查服务和反向代理。",
    }.get(status, f"请求失败（HTTP {status}）。")


class AccountClient:
    def acquire_task(self, task_id):
        try:
            return self.request("POST", f"/api/tasks/{task_id}/acquire")["acquired"] is True
        except AccountError as error:
            if error.status in {404, 405}:
                raise error.with_message("请先更新配套 Gateway，才能使用全局任务槽位。") from None
            raise

    def heartbeat_task(self, task_id):
        self.request("POST", f"/api/tasks/{task_id}/heartbeat")

    def release_task(self, task_id):
        self.request("DELETE", f"/api/tasks/{task_id}")

    def __init__(self, server, *, tls=None, mode="auto", proxy_mode=None):
        self.base = normalize_server(server)
        url = urlsplit(self.base)
        self.host = url.hostname
        self.port = url.port or 443
        self.tls = tls or ssl.create_default_context()
        self.token = ""
        self._open_webui_access = None
        self.mode = proxy_mode if proxy_mode is not None else mode
        self.proxy_mode = self.mode
        try:
            self.transport = AccountTransport(self.base, self.host, self.port, self.tls, self.mode)
        except TransportError as error:
            raise AccountError.from_transport(error) from None

    @property
    def model_base(self):
        return self.base + "/v1"

    @property
    def route(self):
        return self.transport.route

    @property
    def fallback(self):
        return self.transport.fallback

    def engine_proxy_url(self):
        try:
            return self.transport.engine_proxy_url()
        except TransportError as error:
            raise AccountError.from_transport(error) from None

    def health(self, *, observe=None, refresh=True):
        if refresh:
            self.transport.refresh()
        data = self.request("GET", "/health", auth=False, observe=observe, expected_status=200)
        if data.get("ok") is not True:
            raise AccountError("健康检查未返回预期的 CCSLab Gateway 响应。", 200,
                               category="invalid_health", technical="unexpected_health_json", route=self.route)
        return data

    def request(self, method, path, body=None, *, timeout=10, auth=True, observe=None, expected_status=None,
                webui_grant=None):
        headers = {"Accept": "application/json"}
        token = self.token
        if auth and token:
            headers["Authorization"] = "Bearer " + token
        if webui_grant is not None:
            if not isinstance(webui_grant, str) or not _WEBUI_GRANT.fullmatch(webui_grant):
                raise AccountError("最高权限授权无效，请重新验证。", 403)
            headers["X-CCSLab-WebUI-Grant"] = webui_grant
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection = None
        try:
            connection = self.transport.connect(timeout, observe)
            connection.request(method, path, body, headers)
            with connection.getresponse() as reply:
                status = reply.status
                edge_code = reply.getheader("cf-error-type", "")
                cloudflare = bool(edge_code or reply.getheader("cf-ray") or
                                  "cloudflare" in reply.getheader("Server", "").lower())
                challenge = reply.getheader("WWW-Authenticate", "")
                raw = reply.read(1_000_001)
        except TransportError as error:
            raise AccountError.from_transport(error) from None
        except TimeoutError:
            raise AccountError("账号服务器响应超时，请稍后重试。", category="timeout",
                               technical="TimeoutError", route=self.route) from None
        except (OSError, HTTPException, ValueError) as error:
            raise AccountError("账号服务连接中断或未返回有效响应。", category="response",
                               technical=type(error).__name__, route=self.route) from None
        finally:
            if connection:
                connection.close()
        if len(raw) > 1_000_000:
            raise AccountError("账号服务响应超出客户端大小限制。", status,
                               technical="response_too_large", route=self.route)
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            data = None
        if 200 <= status < 300 and not isinstance(data, dict):
            raise AccountError(f"账号服务未返回 JSON（HTTP {status}）。请检查域名路由是否指向 CCSLab 账号 API。",
                               status, technical="non_json_response", route=self.route)
        if 200 <= status < 300 and expected_status is not None and status != expected_status:
            raise AccountError(f"账号服务返回意外状态（HTTP {status}）。", status,
                               technical=f"expected_status={expected_status}", route=self.route)
        if not 200 <= status < 300:
            fields = data if isinstance(data, dict) else {}
            detail = fields.get("detail") or fields.get("error") or fields.get("message")
            if isinstance(detail, dict):
                detail = detail.get("message")
            if not isinstance(detail, str):
                detail = ""
            detail = _gateway_error(status, edge_code=edge_code, cloudflare=cloudflare, detail=detail)
            if token:
                detail = detail.replace(token, "••••")
            if webui_grant:
                detail = detail.replace(webui_grant, "••••")
            detail = self.transport.redact_proxy(detail)
            api_error = bool(isinstance(fields.get("detail"), str) and fields["detail"] and not edge_code)
            if status == 401 and path == "/api/me" and not auth:
                gateway_challenge = challenge.split(None, 1)
                api_error = api_error and (fields.get("detail") == "请先登录。" or
                                           bool(gateway_challenge and gateway_challenge[0].lower() == "bearer"))
            raise AccountError(detail[:2500], status, technical=f"http_status={status}",
                               route=self.route, api_error=api_error)
        return data

    def register(self, username, password):
        return self._private_post("/api/register", {"username": username, "password": password}, password)

    def register_admin(self, username, password, authorization_code):
        return self._private_post("/api/register/admin", {
            "username": username, "password": password, "authorization_code": authorization_code,
        }, password, authorization_code)

    def login(self, username, password, *, authorization_code=None):
        fields = {"username": username, "password": password}
        role = "user"
        if authorization_code is not None:
            role = "admin"
            fields.update(role=role, authorization_code=authorization_code)
        reply = self._private_post("/api/login", fields, password, authorization_code)
        return self._accept_session(reply, role)

    def switch_role(self, role, authorization_code):
        reply = self._private_post("/api/session/role",
                                   {"role": role, "authorization_code": authorization_code},
                                   authorization_code)
        return self._accept_session(reply, role)

    def set_user_quota(self, user_id, mode, limit_tokens):
        return self._upgrade_request(lambda: self.request("POST", f"/api/admin/users/{int(user_id)}/quota",
                                     {"mode": mode, "limit_tokens": limit_tokens}, expected_status=200))

    def change_authorization_code(self, current_code, new_code):
        self._private_post("/api/admin/authorization-code",
                           {"current_code": current_code, "new_code": new_code},
                           current_code, new_code)

    def reset_user_password(self, user_id, password, authorization_code):
        self._private_post(f"/api/admin/users/{int(user_id)}/password",
                           {"password": password, "authorization_code": authorization_code},
                           password, authorization_code)

    def change_own_password(self, current_password, new_password, confirm_password):
        fields = {"current_password": current_password, "new_password": new_password,
                  "confirm_password": confirm_password}
        self._upgrade_request(lambda: self._private_post("/api/me/password", fields, *fields.values(),
                                                        expected_status=204))
        self.token = ""
        self.clear_open_webui_access()

    @property
    def open_webui_expires_at(self):
        access = self._open_webui_access
        return access[2] if access and access[0] == self.token and access[2] > time() else 0

    def clear_open_webui_access(self):
        self._open_webui_access = None

    def verify_open_webui_access(self, password):
        self.clear_open_webui_access()
        session = self.token
        try:
            reply = self._open_webui_password_request("/api/open-webui/access", password, expected_status=200)
        except AccountError as error:
            if error.status == 204:
                raise error.with_message(_GATEWAY_UPGRADE) from None
            raise
        grant, expires_at = reply.get("grant"), reply.get("expires_at")
        if (not session or session != self.token or not isinstance(grant, str) or not _WEBUI_GRANT.fullmatch(grant)
                or type(expires_at) is not int or expires_at <= time()):
            raise AccountError("账号网关未返回有效的最高权限授权。", category="credential")
        self._open_webui_access = session, grant, expires_at
        return {"expires_at": expires_at}

    def _require_open_webui_access(self):
        access = self._open_webui_access
        if not access or access[0] != self.token or access[2] <= time():
            self.clear_open_webui_access()
            raise AccountError("请先重新验证 Open WebUI 最高权限。", 403)
        return access[1]

    def check_open_webui_access(self):
        grant = self._require_open_webui_access()
        try:
            self._upgrade_request(lambda: self.request("GET", "/api/open-webui/access", expected_status=204,
                                                        webui_grant=grant))
        except AccountError as error:
            if error.status in {401, 403}:
                self.clear_open_webui_access()
            raise

    def change_open_webui_access(self, password):
        grant = self._require_open_webui_access()
        try:
            self._open_webui_password_request("/api/admin/open-webui/password", password,
                                              expected_status=204, webui_grant=grant)
        except AccountError as error:
            if error.status in {401, 403}:
                self.clear_open_webui_access()
            raise
        self.clear_open_webui_access()

    def _open_webui_password_request(self, path, password, **options):
        if not valid_account_password(password):
            raise AccountError(f"Open WebUI 访问密码须为 {PASSWORD_RANGE}。", category="credential")
        return self._upgrade_request(lambda: self._private_post(path, {"password": password}, password, **options))

    @staticmethod
    def _upgrade_request(action):
        try:
            return action()
        except AccountError as error:
            if error.status in {404, 405}:
                raise error.with_message(_GATEWAY_UPGRADE) from None
            raise

    def _private_post(self, path, body, *secrets, **options):
        try:
            return self.request("POST", path, body, **options)
        except AccountError as error:
            message = str(error)
            for secret in secrets:
                if isinstance(secret, str) and secret:
                    message = message.replace(secret, "••••")
            raise error.with_message(message) from None

    def _accept_session(self, reply, expected_role):
        value = reply.get("token")
        if not isinstance(value, str) or not value.isascii() or not value.isprintable() or " " in value or not value:
            raise AccountError("服务器未返回有效的登录会话。")
        user = self.user(reply)
        if user["role"] != expected_role:
            raise AccountError("账号网关需要升级，才能按选择的角色登录。")
        self.clear_open_webui_access()
        self.token = value
        return user

    @staticmethod
    def user(reply):
        user = reply.get("user")
        if (not isinstance(user, dict) or not isinstance(user.get("username"), str)
                or user.get("role") not in {"admin", "user"} or user.get("status") not in ACCOUNT_STATUS
                or not isinstance(user.get("id"), (int, str))):
            raise AccountError("服务器未返回有效的账号状态。")
        assigned = user.get("assigned_role")
        if user["role"] == "admin" and assigned != "admin":
            raise AccountError("账号网关需要升级后才能启用管理员会话。")
        if assigned not in {None, "admin", "user"}:
            raise AccountError("账号服务返回了无效的角色。")
        return {**user, "assigned_role": assigned or "user"}

    def me(self):
        return self.user(self.request("GET", "/api/me"))

    def model(self):
        reply = self.request("GET", "/api/model")
        model = reply.get("model", reply)
        return model if isinstance(model, dict) else {}

    def opencode_output_limit(self):
        return self.request("GET", "/api/opencode/output-limit").get("output_limit")

    def set_opencode_output_limit(self, value):
        return self.request("PUT", "/api/admin/opencode/output-limit", {"output_limit": value})

    def engine_ca_bundle(self, state_path):
        """Give Bun the same CA roots used for account HTTPS on this desktop."""
        certs = self.tls.get_ca_certs(binary_form=True)
        if not certs:
            return None
        path = state_path / "account-ca.pem"
        temporary = path.with_suffix(".tmp")
        temporary.write_text("".join(map(ssl.DER_cert_to_PEM_cert, certs)), encoding="ascii")
        temporary.replace(path)
        return path

    def logout(self):
        try:
            self.request("POST", "/api/logout", timeout=5)
        finally:
            self.token = ""
            self.clear_open_webui_access()
