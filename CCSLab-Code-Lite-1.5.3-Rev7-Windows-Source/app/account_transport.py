"""Shared, certificate-validating connection preparation for account requests."""

from base64 import b64encode
from dataclasses import dataclass
from fnmatch import fnmatchcase
from http.client import HTTPConnection, HTTPException, HTTPResponse
from ipaddress import ip_address
from queue import Empty, SimpleQueue
import re
import socket
import ssl
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

from urllib3.util.ssltransport import SSLTransport

from windows_proxy import ProxyDiscoveryError, windows_proxy


CONNECTION_MODES = ("auto", "system", "direct")
_DISCOVERY_SLOTS = BoundedSemaphore(4)
_PROXY_TTL = 30


class TransportError(RuntimeError):
    def __init__(self, message, *, category, stage, technical="", route="direct", fallback=False):
        super().__init__(message)
        self.category = category
        self.stage = stage
        self.technical = technical
        self.route = route
        self.fallback = fallback


@dataclass(frozen=True, repr=False)
class ProxyRoute:
    kind: str
    origin: str = ""
    host: str = ""
    port: int = 0
    scheme: str = ""
    authorization: str = ""


_DIRECT = ProxyRoute("direct")


def _notify(observer, stage, state, detail):
    if observer is not None:
        observer(stage, state, detail)


def _bounded_call(action, timeout):
    """Bound waits on DNS and PAC native calls without retaining exit-blocking threads."""
    if not _DISCOVERY_SLOTS.acquire(blocking=False):
        raise TimeoutError
    result = SimpleQueue()

    def worker():
        try:
            result.put((True, action()))
        except Exception as error:
            result.put((False, error))
        finally:
            _DISCOVERY_SLOTS.release()

    thread = Thread(target=worker, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _DISCOVERY_SLOTS.release()
        raise TimeoutError from None
    try:
        ok, value = result.get(timeout=timeout)
    except Empty:
        raise TimeoutError from None
    if not ok:
        raise value
    return value


def _remaining(deadline):
    available = deadline - monotonic()
    if available <= 0:
        raise TimeoutError
    return available


def _authority(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def normalize_host(value):
    if not value or "%" in value:
        raise ValueError
    try:
        return ip_address(value).compressed
    except ValueError:
        labels = value.encode("idna").decode("ascii").rstrip(".").lower()
        if len(labels) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in labels.split(".")):
            raise ValueError from None
        return labels


def valid_authority(value):
    if value.startswith("["):
        return bool(re.fullmatch(r"\[[^\[\]]+\](?::[0-9]+)?", value))
    return bool(re.fullmatch(r"[^:\[\]]+(?::[0-9]+)?", value))


def _proxy(value, kind):
    item = value.strip()
    prefix = item.split(None, 1)
    if len(prefix) == 2 and prefix[0].upper() in {"PROXY", "HTTP", "HTTPS"}:
        item = ("https://" if prefix[0].upper() == "HTTPS" else "http://") + prefix[1]
    if "://" not in item:
        item = "http://" + item
    try:
        if any(ord(character) < 33 or ord(character) == 127 or character == "\\" for character in item):
            raise ValueError
        parsed = urlsplit(item)
        host = normalize_host(parsed.hostname)
        if (
            parsed.scheme not in {"http", "https"} or not host or parsed.port == 0
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or "?" in item or "#" in item
            or not valid_authority(parsed.netloc.rsplit("@", 1)[-1])
        ):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        auth = ""
        if parsed.username is not None:
            pair = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
            if any(ord(character) < 32 or ord(character) == 127 for character in pair):
                raise ValueError
            auth = "Basic " + b64encode(pair.encode("utf-8")).decode("ascii")
        credentials = parsed.netloc.rsplit("@", 1)[0] + "@" if auth else ""
        origin = f"{parsed.scheme}://{credentials}{_authority(host, port)}"
        return ProxyRoute(kind, origin, host, port, parsed.scheme, auth)
    except (ValueError, UnicodeError):
        raise TransportError(
            "当前代理地址无效，或使用了不支持的协议。请选择 HTTP 或 HTTPS 代理。",
            category="proxy", stage="route", technical="unsupported_proxy_configuration", route=kind,
        ) from None


def _windows_bypass(host, port, bypass):
    authority = _authority(host, port)
    entries = re.split(r"[;\s]+", bypass.lower())
    for entry in entries:
        if entry == "<local>" and "." not in host and ":" not in host:
            return True
        if entry and (fnmatchcase(host, entry) or fnmatchcase(authority, entry)):
            return True
    return False


def _is_loopback(host):
    try:
        address = ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")
    return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)


def _windows_route(host, port, proxy, bypass):
    if not proxy or _windows_bypass(host, port, bypass):
        return _DIRECT
    entries = [item.strip() for item in proxy.split(";") if item.strip()]
    scheme_entries = {name.lower().strip(): value for item in entries if "=" in item
                      for name, value in [item.split("=", 1)] if name.lower().strip() in {"http", "https", "ftp", "socks"}}
    if scheme_entries:
        endpoint = scheme_entries.get("https")
        return _proxy(endpoint, "system") if endpoint else _DIRECT
    if entries:
        entry = entries[0]
        if entry.upper() == "DIRECT":
            return _DIRECT
        if re.match(r"(?i)^SOCKS(?:4|5)?\s", entry):
            raise TransportError("Windows 为此地址选择了不支持的 SOCKS 代理。",
                                 category="proxy", stage="route",
                                 technical="unsupported_proxy_protocol", route="system")
        return _proxy(entry, "system")
    return _DIRECT


def _error_code(error):
    parts = [type(error).__name__]
    for name in ("errno", "winerror", "verify_code"):
        value = getattr(error, name, None)
        if isinstance(value, int):
            parts.append(f"{name}={value}")
    return " ".join(parts)


def _connection_error(error, stage, route):
    detail = _error_code(error)
    if isinstance(error, ssl.SSLCertVerificationError):
        message = "服务器 HTTPS 证书验证失败，请核对证书链、域名和电脑时间。"
        if stage == "proxy_tls":
            message = "HTTPS 代理的证书验证失败，请核对代理证书链和域名。"
        return TransportError(message, category="certificate", stage="tls", technical=detail, route=route.kind)
    if isinstance(error, ssl.SSLError):
        message = "账号网关 HTTPS 握手失败，请核对地址、协议和端口。"
        if stage == "proxy_tls":
            message = "HTTPS 代理握手失败，请核对代理协议和端口。"
        return TransportError(message, category="tls", stage="tls", technical=detail, route=route.kind)
    if route.host and stage in {"dns", "tcp", "proxy", "proxy_tls"}:
        message = "无法解析当前网络的代理主机，请检查代理配置。"
        public_stage = stage
        if stage != "dns":
            message = "当前网络代理无法连接账号服务器，请检查代理状态。"
            public_stage = "tls" if stage == "proxy_tls" else "tcp"
        return TransportError(message, category="proxy", stage=public_stage, technical=detail,
                              route=route.kind, fallback=stage != "proxy_tls")
    if isinstance(error, TimeoutError):
        return TransportError("账号服务器响应超时，请稍后重试。", category="timeout",
                              stage=stage, technical=detail, route=route.kind)
    if isinstance(error, socket.gaierror):
        return TransportError("无法解析服务器域名，请检查电脑的 DNS 和网络设置。", category="dns",
                              stage=stage, technical=detail, route=route.kind)
    if stage == "tcp":
        return TransportError("无法连接账号服务器，请检查网络、端口和网关状态。", category="tcp",
                              stage=stage, technical=detail, route=route.kind)
    return TransportError("账号服务连接中断或未返回有效响应。", category="response",
                          stage=stage, technical=detail, route=route.kind)


def _tcp_connection(addresses, deadline):
    last_error = None
    for index, (family, kind, protocol, _, remote) in enumerate(addresses):
        try:
            raw = socket.socket(family, kind, protocol)
            try:
                remaining_attempts = len(addresses) - index
                raw.settimeout(_remaining(deadline) / remaining_attempts)
                raw.connect(remote)
            except OSError:
                raw.close()
                raise
            return raw
        except OSError as error:
            last_error = error
    raise last_error or OSError


def _connect_tunnel(sock, authority, route):
    headers = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
    if route.authorization:
        headers.append(f"Proxy-Authorization: {route.authorization}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    with HTTPResponse(sock, method="HEAD") as response:
        response.begin()
        code = response.status
        if code not in range(200, 300):
            message = ("当前代理需要授权，请检查代理凭据。" if code == 407 else
                       f"代理未建立到账号服务器的连接（HTTP {code}）。")
            raise TransportError(message, category="proxy", stage="tcp",
                                 technical=f"connect_status={code}", route=route.kind,
                                 fallback=code in {502, 503, 504})


class AccountTransport:
    def __init__(self, base, host, port, tls, mode):
        if mode not in CONNECTION_MODES:
            raise TransportError("请选择自动、系统代理或直连模式。", category="invalid_url", stage="url")
        if not tls.check_hostname or tls.verify_mode != ssl.CERT_REQUIRED:
            raise TransportError("账号连接需要证书与域名验证。", category="certificate", stage="tls")
        self.base, self.host, self.port, self.tls, self.mode = base, host, port, tls, mode
        self._lock = Lock()
        self._cached = None
        self._route = None
        self._fallback = False

    @property
    def route(self):
        with self._lock:
            return self._route.kind if self._route else ""

    @property
    def fallback(self):
        with self._lock:
            return self._fallback

    def refresh(self):
        with self._lock:
            self._cached = None

    def redact_proxy(self, message):
        with self._lock:
            route = self._route
        if route and route.authorization:
            parsed = urlsplit(route.origin)
            # Hide reflected proxy credentials regardless of how they were encoded.
            pieces = [route.origin, route.authorization, route.authorization.removeprefix("Basic ")]
            for value in (parsed.username, parsed.password):
                if value:
                    pieces.extend((value, unquote(value)))
            for value in sorted(set(pieces), key=len, reverse=True):
                if value:
                    message = message.replace(value, "••••")
        return message

    def _resolve(self, timeout):
        if self.mode == "direct" or _is_loopback(self.host):
            return _DIRECT
        if self.mode == "auto":
            proxies = getproxies_environment()
            if proxy_bypass_environment(_authority(self.host, self.port), proxies):
                return _DIRECT
            value = proxies.get("https") or proxies.get("all") or proxies.get("http")
            if value:
                return _proxy(value, "environment")
        try:
            proxy, bypass = _bounded_call(lambda: windows_proxy(self.base), timeout)
        except (OSError, TimeoutError, ProxyDiscoveryError) as error:
            code = error.code if isinstance(error, ProxyDiscoveryError) else None
            detail = f"WinHTTP code={code}" if code is not None else _error_code(error)
            raise TransportError("无法读取 Windows 系统代理或自动代理配置，请检查当前网络设置。",
                                 category="proxy", stage="route", technical=detail,
                                 route="system", fallback=code not in {12015, 5}) from None
        return _windows_route(self.host, self.port, proxy, bypass)

    def engine_proxy_url(self):
        with self._lock:
            last = self._route
        if last is not None:
            return last.origin or None
        try:
            return self._resolve(3).origin or None
        except TransportError as error:
            if self.mode == "auto" and error.fallback:
                return None
            raise

    def connect(self, timeout, observer=None):
        deadline = monotonic() + timeout
        with self._lock:
            cached = self._cached
        route = cached[0] if cached and cached[1] > monotonic() else None
        fallback = cached[2] if route else False
        try:
            if route is None:
                route = self._resolve(min(timeout * 0.4, 3))
            source = {"direct": "直连", "system": "Windows 系统代理", "environment": "环境变量代理"}
            _notify(observer, "route", "warning" if fallback else "normal",
                    "已采用代理失败后的直连回退。" if fallback else f"当前路线：{source[route.kind]}。")
            route_deadline = min(deadline, monotonic() + max(timeout * 0.55, 0.15)) if (
                self.mode == "auto" and route.host
            ) else deadline
            connection = self._connect_route(route, route_deadline, observer)
        except TransportError as error:
            if self.mode != "auto" or not error.fallback:
                _notify(observer, error.stage, "failure", str(error))
                raise
            route, fallback = _DIRECT, True
            _notify(observer, "route", "warning", "代理预连接失败，正在安全回退到直连。")
            try:
                connection = self._connect_route(route, deadline, observer)
            except TransportError as direct_error:
                _notify(observer, direct_error.stage, "failure", str(direct_error))
                raise TransportError(
                    f"自动模式代理失败，直连也失败。{direct_error}", category=direct_error.category,
                    stage=direct_error.stage, technical=direct_error.technical, route="direct",
                ) from None
        connection.sock.settimeout(timeout)
        with self._lock:
            self._route, self._fallback = route, fallback
            if not cached or cached[0] is not route or cached[1] <= monotonic():
                self._cached = route, monotonic() + _PROXY_TTL, fallback
        return connection

    def _connect_route(self, route, deadline, observer):
        raw = None
        stage = "dns"
        host, port = (route.host, route.port) if route.host else (self.host, self.port)
        try:
            try:
                numeric_host = bool(ip_address(host))
            except ValueError:
                numeric_host = False
            action = lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = action() if numeric_host else _bounded_call(action, _remaining(deadline))
            _notify(observer, "dns", "na" if numeric_host else "normal", (
                "使用配置的 IP 地址，目标域名由代理解析。" if numeric_host and route.host else
                "使用配置的 IP 地址。" if numeric_host else
                "已解析代理主机，目标域名由代理解析。" if route.host else "已解析账号服务器域名。"
            ))
            stage = "tcp"
            raw = _tcp_connection(addresses, deadline)
            if route.host:
                if route.scheme == "https":
                    stage = "proxy_tls"
                    raw.settimeout(_remaining(deadline))
                    raw = self.tls.wrap_socket(raw, server_hostname=route.host)
                stage = "proxy"
                raw.settimeout(_remaining(deadline))
                _connect_tunnel(raw, _authority(self.host, self.port), route)
            _notify(observer, "tcp", "normal", "已建立代理 CONNECT 隧道。" if route.host else "账号服务器端口可达。")
            stage = "tls"
            raw.settimeout(_remaining(deadline))
            if route.scheme == "https":
                raw = SSLTransport(raw, self.tls, server_hostname=self.host)
            else:
                raw = self.tls.wrap_socket(raw, server_hostname=self.host)
            _notify(observer, "tls", "normal", f"{raw.version()} 握手成功，证书链、有效期与域名校验通过。")
            connection = HTTPConnection(self.host, self.port)
            connection.default_port = 443
            connection.sock = raw
            connection.auto_open = 0  # A request may be sent only on this prepared connection.
            return connection
        except TransportError:
            if raw:
                raw.close()
            raise
        except (OSError, HTTPException, ValueError) as error:
            if raw:
                raw.close()
            raise _connection_error(error, stage, route) from None
