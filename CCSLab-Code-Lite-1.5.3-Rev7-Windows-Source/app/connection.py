"""CCSLab model HTTP-over-SSH transport and per-user credential storage."""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO

from windows_process import WindowsJob as _SshJob, owns_loopback_listener as _owns_loopback_listener


DEFAULT_HOST = "192.168.1.107"
MODEL_ID = "lab-general"
REMOTE_TARGET = "172.17.0.1:8000"
_LOOPBACK = "127.0.0.1"
_NO_UI = 1


class ConnectionError(RuntimeError):
    """A user-facing SSH or model service error."""


class _Blob(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(payload: bytes, *, encrypt: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    api = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    api.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR if encrypt else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    api.restype = wintypes.BOOL
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    source = _Blob(len(payload), buffer)
    result = _Blob()
    if not api(ctypes.byref(source), None, None, None, None, _NO_UI, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        local_free(result.data)


def _destination(host: str, user: str, port: int) -> tuple[str, str, int]:
    host, user = host.strip().lower(), user.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host):
        raise ConnectionError("请输入 SSH 主机名或 IPv4 地址，例如 192.168.1.107。")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", user):
        raise ConnectionError("请输入有效的 Ubuntu SSH 用户名。")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ConnectionError("SSH 端口应为 1 至 65535。") from None
    if not 1 <= port <= 65535:
        raise ConnectionError("SSH 端口应为 1 至 65535。")
    return host, user, port


@dataclass
class _Tunnel:
    process: subprocess.Popen
    job: _SshJob
    destination: tuple[str, str, int]
    port: int
    errors: BinaryIO | None
    verified: bool = False

    def close(self) -> None:
        self.job.close()
        if self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.errors:
            self.errors.close()


def _ssh_error(tunnel: _Tunnel) -> ConnectionError:
    if tunnel.errors:
        tunnel.errors.seek(0)
        output = tunnel.errors.read(8192).decode("utf-8", "replace").lower()
        if "identification has changed" in output:
            return ConnectionError("SSH 主机密钥发生变化。请让服务器管理员核对新的指纹后重试。")
        if "host key verification failed" in output or "host key is known" in output:
            return ConnectionError("SSH 主机尚未通过验证。请点「验证 SSH 认证」，核对服务器指纹后确认。")
        if "permission denied" in output or "authentication failed" in output:
            return ConnectionError("SSH 登录失败。请检查用户名和 SSH 密钥，或点「验证 SSH 认证」登录。")
        if "address already in use" in output or "cannot listen" in output:
            return ConnectionError("SSH 本地转发端口被占用，请重新连接。")
        if "refused" in output or "timed out" in output or "resolve hostname" in output:
            return ConnectionError("无法连接 SSH 服务器。请检查局域网、服务器地址及 SSH 端口。")
    return ConnectionError("SSH 转发已退出。请检查服务器和认证窗口；必要时重新认证。")


class Connection:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self._profile = self.state_dir / "connection.dpapi"
        self._lock = threading.Lock()
        self._tunnel: _Tunnel | None = None
        atexit.register(self.close)

    def load(self) -> dict:
        defaults = {"host": DEFAULT_HOST, "user": "", "port": 22, "token": ""}
        try:
            encrypted = self._profile.read_bytes()
        except FileNotFoundError:
            return defaults
        try:
            values = json.loads(_dpapi(encrypted, encrypt=False).decode("utf-8"))
            host, user, port = _destination(values["host"], values["user"], values["port"])
            return {"host": host, "user": user, "port": port, "token": values["token"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ConnectionError("当前 Windows 账户无法读取已保存的连接，请重新录入连接信息。") from error

    def _save(self, destination: tuple[str, str, int], token: str) -> None:
        host, user, port = destination
        profile = {"host": host, "user": user, "port": port, "token": token}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = _dpapi(json.dumps(profile).encode("utf-8"), encrypt=True)
        with tempfile.NamedTemporaryFile(dir=self.state_dir, prefix="connection-", delete=False) as file:
            path = Path(file.name)
            file.write(payload)
        try:
            os.replace(path, self._profile)
        finally:
            path.unlink(missing_ok=True)

    def _start(self, destination: tuple[str, str, int], *, interactive: bool) -> _Tunnel:
        ssh = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if not ssh.is_file():
            raise ConnectionError("未检测到 Windows OpenSSH 客户端。请在 Windows 可选功能中安装它。")
        host, user, port = destination
        with socket.socket() as reservation:
            reservation.bind((_LOOPBACK, 0))
            local_port = reservation.getsockname()[1]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        known_hosts = (self.state_dir / "ssh_known_hosts").absolute().as_posix()
        args = [
            str(ssh), "-F", "NUL", "-N", "-T",
            "-l", user, "-p", str(port),
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "ConnectionAttempts=1",
            "-o", "ConnectTimeout=8",
            "-o", f'UserKnownHostsFile="{known_hosts}" ~/.ssh/known_hosts ~/.ssh/known_hosts2',
            "-o", "StrictHostKeyChecking=ask" if interactive else "StrictHostKeyChecking=yes",
            "-o", "BatchMode=no" if interactive else "BatchMode=yes",
            "-L", f"{_LOOPBACK}:{local_port}:{REMOTE_TARGET}", "--", host,
        ]
        job = _SshJob()
        errors = None
        process = None
        try:
            if interactive:
                process = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                errors = tempfile.TemporaryFile()
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            job.add(process.pid)
            return _Tunnel(process, job, destination, local_port, errors)
        except OSError as error:
            job.close()
            if process and process.poll() is None:
                process.kill()
            if errors:
                errors.close()
            raise ConnectionError(f"无法启动 Windows SSH 进程（{error}）。") from error

    def authenticate(self, host: str, user: str, port: int = 22) -> None:
        destination = _destination(host, user, port)
        with self._lock:
            self._close_locked()
            self._tunnel = self._start(destination, interactive=True)

    def connect(self, host: str, user: str, token: str, port: int = 22) -> str:
        destination = _destination(host, user, port)
        token = token.strip()
        if not token or len(token) > 8192 or not re.fullmatch(r"[!-~]+", token):
            raise ConnectionError("请输入有效的模型 API Key。")
        with self._lock:
            tunnel = self._tunnel
            if not tunnel or tunnel.destination != destination or tunnel.process.poll() is not None:
                self._close_locked()
                tunnel = self._start(destination, interactive=False)
                self._tunnel = tunnel
            tunnel.verified = False

        # Keep the UI able to cancel or close while SSH is asking in its own window.
        deadline = time.monotonic() + (30 if tunnel.errors is None else 10)
        while not self._listener_ready(tunnel):
            if time.monotonic() >= deadline:
                raise ConnectionError("等待 SSH 转发超时。请完成认证后点击「连接模型」，或检查服务器。")
            time.sleep(0.15)

        self._verify_model(tunnel.port, token)
        with self._lock:
            if self._tunnel is not tunnel or tunnel.process.poll() is not None:
                raise ConnectionError("SSH 转发已断开。")
            try:
                self._save(destination, token)
            except OSError as error:
                raise ConnectionError("无法使用 Windows DPAPI 保存连接。请检查应用数据目录权限。") from error
            tunnel.verified = True
        return f"http://{_LOOPBACK}:{tunnel.port}/v1"

    def _listener_ready(self, tunnel: _Tunnel) -> bool:
        with self._lock:
            if self._tunnel is not tunnel:
                raise ConnectionError("连接已取消。")
            if tunnel.process.poll() is not None:
                error = _ssh_error(tunnel)
                self._close_locked()
                raise error
            return _owns_loopback_listener(tunnel.process.pid, tunnel.port)

    @staticmethod
    def _verify_model(port: int, token: str) -> None:
        # llama.cpp exposes /v1/models publicly. Its own UI probes /props for authentication.
        Connection._get_model_endpoint(port, token, "/props")
        content = Connection._get_model_endpoint(port, token, "/v1/models")
        try:
            models = json.loads(content)["data"]
            if not isinstance(models, list) or not any(
                isinstance(model, dict) and model.get("id") == MODEL_ID for model in models
            ):
                raise ConnectionError(f"模型 API 未提供 {MODEL_ID}。请核对服务器的模型路由。")
        except (KeyError, TypeError, ValueError):
            raise ConnectionError("模型 API 的 /v1/models 返回了无法识别的数据。") from None

    @staticmethod
    def _get_model_endpoint(port: int, token: str, path: str) -> bytes:
        connection = http.client.HTTPConnection(_LOOPBACK, port, timeout=8)
        try:
            connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
            response = connection.getresponse()
            if response.status in (401, 403):
                raise ConnectionError("模型 API 拒绝访问，请核对 API Key 或服务器权限。")
            if response.status == 502:
                raise ConnectionError("模型服务返回 HTTP 502，请确认推理服务可用后重试。")
            if response.status != 200:
                raise ConnectionError(f"模型验证请求 {path} 失败（HTTP {response.status}）。")
            return response.read(1_048_576)
        except (OSError, http.client.HTTPException):
            raise ConnectionError(
                f"SSH 已建立，但无法访问模型 API。请检查服务器上的 {REMOTE_TARGET} 和 SSH 转发权限。"
            ) from None
        finally:
            connection.close()

    def alive(self) -> bool:
        with self._lock:
            tunnel = self._tunnel
            return bool(tunnel and tunnel.verified and tunnel.process.poll() is None)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None
