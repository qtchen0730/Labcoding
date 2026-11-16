"""Public HTTPS model transport with optional Open WebUI session authentication."""

from http.client import HTTPSConnection, HTTPException
from ipaddress import IPv6Address
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
from urllib.parse import urlsplit

from account_client import PASSWORD_RANGE, valid_account_password

_DEFAULT_HOST = "ccslab-llm.qtchen.cn"
BASE_URL = f"https://{_DEFAULT_HOST}/api"
MODEL_ID = "lab-general"
_MAX_RESPONSE = 1_048_576
_BEARER = re.compile(r"[!-~]{1,8192}", re.ASCII)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH_SEGMENT = re.compile(r"[-A-Za-z0-9._~!$&'()*+,;=:@]+")


class DirectError(RuntimeError):
    """A safe connection error suitable for display without logging credentials."""

    def __init__(self, message, status=0, category="connection"):
        super().__init__(message)
        self.status = status
        self.category = category


def normalize_base_url(address):
    """Normalize an HTTPS origin or compatible API base to a canonical URL."""
    try:
        if not isinstance(address, str):
            raise ValueError
        value = address.strip()
        if (len(value) > 2048 or any(character in "\\?#%" or not character.isprintable()
                                    or character.isspace() for character in value)):
            raise ValueError
        url = urlsplit(value)
        if url.scheme != "https" or not url.netloc or url.username is not None:
            raise ValueError
        raw_host = url.hostname
        if not raw_host or url.netloc.endswith(":") or url.port == 0:
            raise ValueError
        if ":" in raw_host:
            address_part, _, tail = url.netloc.partition("]")
            if not url.netloc.startswith("[") or (tail and not re.fullmatch(r":[0-9]+", tail)):
                raise ValueError
            host = f"[{IPv6Address(address_part[1:]).compressed}]"
        else:
            if url.netloc.startswith("["):
                raise ValueError
            host = raw_host.removesuffix(".").encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
                raise ValueError
        if "//" in url.path:
            raise ValueError
        path = url.path.rstrip("/")
        if path and any(segment in {".", ".."} or not _PATH_SEGMENT.fullmatch(segment)
                        for segment in path[1:].split("/")):
            raise ValueError
        if path.endswith("/chat/completions"):
            path = path.removesuffix("/chat/completions")
            if path.endswith("/chat/completions"):
                raise ValueError
        port = f":{url.port}" if url.port not in {None, 443} else ""
        default_path = "/api" if host == _DEFAULT_HOST and not port else "/v1"
        return f"https://{host}{port}{path or default_path}"
    except (AttributeError, UnicodeError, ValueError):
        raise DirectError("请输入有效的 HTTPS 模型地址，例如 https://example.com/v1。",
                          category="configuration") from None


def _webui_sign_in_path(base):
    path = urlsplit(base).path
    if path.endswith("/api/v1"):
        return path + "/auths/signin"
    if path.endswith("/api"):
        return path + "/v1/auths/signin"
    return None


def supports_webui_sign_in(address):
    try:
        return _webui_sign_in_path(normalize_base_url(address)) is not None
    except DirectError:
        return False


def _atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="ccslab-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _status_error(status, retry_after, *, signing_in=False, authenticated=False):
    if signing_in:
        if status in {400, 401}:
            return DirectError("Open WebUI 登录失败。请检查邮箱和密码。", status, "unauthorized")
        if status == 403:
            return DirectError("Open WebUI 拒绝密码登录（HTTP 403）。请检查该站点的登录方式。", status, "forbidden")
        if status in {404, 405}:
            return DirectError(f"Open WebUI 登录接口不可用（HTTP {status}）。请检查站点地址。", status, "unavailable")
    if status == 401:
        message = ("登录会话无效或已过期（HTTP 401）。请重新登录。" if authenticated else
                   "模型服务要求登录（HTTP 401）。可使用 Open WebUI 账号或 CCSLab 账号网关。")
        return DirectError(message, status, "unauthorized")
    if status == 403:
        return DirectError("模型服务拒绝访问（HTTP 403）。请检查账号的模型使用权限。", status, "forbidden")
    if status == 429:
        wait = f"请在 {int(retry_after)} 秒后重试。" if re.fullmatch(r"[0-9]{1,6}", retry_after) else "请稍后重试。"
        return DirectError("模型服务正在限流（HTTP 429）。" + wait, status, "rate_limit")
    if status in {404, 405}:
        return DirectError(f"模型生成接口不可用（HTTP {status}）。请检查 API 地址及 lab-general 模型。",
                           status, "unavailable")
    if 500 <= status < 600:
        return DirectError(f"模型服务暂时不可用（HTTP {status}）。请稍后重试。", status, "unavailable")
    if 300 <= status < 400:
        return DirectError("模型服务地址返回了重定向，连接已停止。", status, "redirect")
    operation = "登录" if signing_in else "生成"
    return DirectError(f"服务拒绝了{operation}请求（HTTP {status}）。", status, "response")


class DirectConnection:
    """Connect over HTTPS anonymously or with an Open WebUI session."""

    def __init__(self, state_dir, *, base_url=BASE_URL, tls=None, timeout=20.0):
        self._base = normalize_base_url(base_url)
        url = urlsplit(self._base)
        self._host = url.hostname
        self._port = url.port or 443
        self._path = url.path
        self._timeout = timeout
        self._tls = tls if tls is not None else ssl.create_default_context()
        if not self._tls.check_hostname or self._tls.verify_mode != ssl.CERT_REQUIRED:
            raise DirectError("模型连接需要证书与主机名校验。", category="certificate")
        self._state = Path(state_dir)

    @property
    def model_base(self):
        return self._base

    @property
    def model_id(self):
        return MODEL_ID

    def for_address(self, address):
        base = normalize_base_url(address)
        if base == self.model_base:
            return self
        return DirectConnection(self._state, base_url=base, tls=self._tls, timeout=self._timeout)

    def engine_ca_bundle(self, state_path=None):
        """Give the Agent the same trusted roots used for the connection probe."""
        certs = self._tls.get_ca_certs(binary_form=True)
        if not certs:
            return None
        parent = self._state if state_path is None else Path(state_path)
        path = parent / "direct-ca.pem"
        _atomic_write(path, "".join(map(ssl.DER_cert_to_PEM_cert, certs)).encode("ascii"))
        return path

    def _post(self, path, settings, *, token=None, signing_in=False, changing_password=False):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            if not isinstance(token, str) or not _BEARER.fullmatch(token):
                raise DirectError("登录会话无效。请重新登录。", category="credential")
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPSConnection(self._host, self._port, timeout=self._timeout, context=self._tls)
        try:
            connection.request("POST", path, json.dumps(settings).encode("ascii"), headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    if changing_password and response.status in {404, 405}:
                        raise DirectError("当前站点未开放 Open WebUI 账号改密接口。请使用网页端账户设置。",
                                          response.status, "unavailable")
                    if changing_password and response.status == 400:
                        raise DirectError("Open WebUI 拒绝修改密码。请核对当前密码和站点密码规则。", 400, "credential")
                    raise _status_error(response.status, response.getheader("Retry-After", ""),
                                        signing_in=signing_in, authenticated=bool(token))
                raw = response.read(_MAX_RESPONSE + 1)
        except TimeoutError:
            raise DirectError("模型连接或生成请求超时。请检查网络后重试。", category="timeout") from None
        except ssl.SSLCertVerificationError:
            raise DirectError("模型服务的 HTTPS 证书验证失败。", category="certificate") from None
        except (OSError, HTTPException):
            raise DirectError("无法连接公网模型服务。请检查网络后重试。", category="network") from None
        finally:
            connection.close()

        try:
            if len(raw) > _MAX_RESPONSE:
                raise ValueError
            reply = json.loads(raw)
            if not isinstance(reply, bool if changing_password else dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise DirectError("服务未返回有效的 JSON 响应。请检查接口地址。", 200, "response") from None
        return reply

    def sign_in(self, email, password):
        """Exchange explicitly entered Open WebUI credentials for an in-memory session."""
        path = _webui_sign_in_path(self.model_base)
        if path is None:
            raise DirectError("Open WebUI 登录要求 API 地址以 /api 或 /api/v1 结尾。", category="configuration")
        email = email.strip() if isinstance(email, str) else ""
        if not email or len(email) > 254 or not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise DirectError("请输入有效的 Open WebUI 登录邮箱和密码。", category="credential")
        reply = self._post(path, {"email": email, "password": password}, signing_in=True)
        step = reply.get("next_step")
        if step == "pending" or reply.get("role") == "pending":
            raise DirectError("Open WebUI 账号正在等待审批。", 403, "pending")
        if step in ("verify", "enroll", "recover"):
            raise DirectError("此 Open WebUI 账号需要多因素验证。请使用 Open WebUI 网页或 CCSLab 账号网关。", 403, "mfa")
        token, token_type = reply.get("token"), reply.get("token_type")
        if not (isinstance(token, str) and _BEARER.fullmatch(token)
                and isinstance(token_type, str) and token_type.lower() == "bearer"):
            raise DirectError("Open WebUI 未返回有效的登录会话。", 200, "response")
        return token

    def change_webui_account_password(self, token, current_password, new_password):
        """Change only the Open WebUI account identified by its own bearer session."""
        path = _webui_sign_in_path(self.model_base)
        if path is None or not token:
            raise DirectError("请先登录支持改密的 Open WebUI 账号。", 401, "credential")
        if (not isinstance(current_password, str) or not 1 <= len(current_password) <= 1024
                or not valid_account_password(new_password) or current_password == new_password):
            raise DirectError(f"请输入当前密码和不同的新密码。新密码须为 {PASSWORD_RANGE}。", category="credential")
        change_path = path.removesuffix("/signin") + "/update/password"
        if self._post(change_path, {"password": current_password, "new_password": new_password},
                      token=token, changing_password=True) is not True:
            raise DirectError("Open WebUI 没有确认密码更新。", 200, "response")

    def probe(self, token=None):
        """Require an actual assistant completion through the public chat route."""
        reply = self._post(self._path + "/chat/completions", {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "Reply with the single word Ready."}],
            "stream": False,
            "max_tokens": 256,
        }, token=token)
        try:
            if reply["model"] != MODEL_ID:
                raise ValueError
            message = reply["choices"][0]["message"]
            if message["role"] != "assistant" or not isinstance(message["content"], str) or not message["content"].strip():
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError):
            raise DirectError("模型服务未返回可验证的 lab-general 助手回复。", 200, "response") from None
        return {"model_id": MODEL_ID, "model_name": MODEL_ID, "status": "running"}
