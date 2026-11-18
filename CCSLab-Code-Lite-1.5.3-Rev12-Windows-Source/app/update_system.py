"""Small HTTPS-only updater for the fixed CCSLab Labcoding GitHub repository."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Event, Lock
from time import monotonic
import urllib.request
from urllib.parse import quote, urljoin, urlsplit

import urllib3
from urllib3.util import Timeout

from versioning import SemVer, VersionError


REPOSITORY = "qtchen0730/Labcoding"
API_ROOT = "https://api.github.com/repos/qtchen0730/Labcoding"
RELEASES_API = API_ROOT + "/releases"
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
DOWNLOAD_TIMEOUT = Timeout(connect=8.0, read=20.0)
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})


class UpdateError(RuntimeError):
    pass


class UpdateTrustError(UpdateError):
    pass


class UpdateNetworkError(UpdateError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateCandidate:
    version: str
    channel: str
    tag: str
    notes: str
    size: int
    sha256: str | None
    asset_name: str
    asset_url: str


@dataclass(frozen=True)
class VerifiedPackage:
    path: Path
    version: str
    size: int
    sha256: str
    digest_verified: bool
    architecture_verified: bool


def _is_reparse_point(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _default_http_client():
    """Use the HTTPS proxy configured in the Windows/Python proxy settings."""
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if proxy:
        return urllib3.ProxyManager(proxy, cert_reqs="CERT_REQUIRED")
    return urllib3.PoolManager(cert_reqs="CERT_REQUIRED")


def _network_failure(error, *, during_download=False):
    """Map urllib3's wrapped errors to short, credential-free user messages."""
    chain = []
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = (getattr(current, "reason", None)
                   or getattr(current, "__cause__", None)
                   or getattr(current, "__context__", None))
    names = {type(item).__name__ for item in chain}
    if "ProxyError" in names:
        reason = "系统代理连接失败"
    elif names & {"NameResolutionError", "gaierror"}:
        reason = "DNS 解析失败"
    elif names & {"ConnectTimeoutError", "TimeoutError"}:
        reason = "连接 GitHub 超时"
    elif "ReadTimeoutError" in names:
        reason = "等待 GitHub 响应超时"
    elif names & {"ProtocolError", "RemoteDisconnected"}:
        reason = "GitHub 连接中断"
    else:
        reason = "连接 GitHub 失败"
    if during_download:
        return UpdateNetworkError(f"{reason}，下载未完成")
    return UpdateNetworkError(reason)


def is_windows_x64_pe(path):
    """Verify the downloaded installer is a PE32+ AMD64 executable."""
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(header[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > 1024 * 1024:
                return False
            stream.seek(pe_offset)
            pe_header = stream.read(26)
            return (len(pe_header) == 26 and pe_header[:4] == b"PE\0\0"
                    and int.from_bytes(pe_header[4:6], "little") == 0x8664
                    and int.from_bytes(pe_header[24:26], "little") == 0x20B)
    except OSError:
        return False


class UpdateService:
    def __init__(self, data_root, version_info, *, http=None, clock=None,
                 max_download_bytes=MAX_INSTALLER_BYTES):
        self.data_root = Path(data_root)
        self.version_info = dict(version_info)
        self.http = http if http is not None else _default_http_client()
        if self.version_info.get("repository") != REPOSITORY:
            raise UpdateTrustError("更新源不是内置的 CCSLab Labcoding 仓库")
        if self.version_info.get("channel") not in {"stable", "beta"}:
            raise UpdateTrustError("更新通道配置无效")
        try:
            self.current_version = SemVer(self.version_info["version"])
        except (KeyError, VersionError) as error:
            raise UpdateTrustError("当前客户端版本无效") from error
        self.clock = clock or __import__("time").time
        self.max_download_bytes = max_download_bytes
        self._download_lock = Lock()

    @staticmethod
    def _check_url(url, *, api=False):
        parsed = urlsplit(url)
        allowed = {"api.github.com"} if api else ALLOWED_DOWNLOAD_HOSTS
        if (parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username
                or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            raise UpdateTrustError("更新地址不属于固定的 GitHub HTTPS 来源")
        return url

    @staticmethod
    def release_asset_url(tag, name):
        if not isinstance(tag, str) or not re.fullmatch(r"v[0-9A-Za-z.+-]+", tag):
            raise UpdateTrustError("GitHub Release 标签无效")
        if not isinstance(name, str) or not re.fullmatch(
                r"CCSLab-Labcoding-[0-9A-Za-z.+-]+-Setup-x64\.exe", name):
            raise UpdateTrustError("更新资产名不是固定的 Windows x64 安装包格式")
        return f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='.-+')}/{quote(name, safe='.-')}"

    def _get(self, url, *, max_bytes, cancel=None, progress=None, expected_size=None):
        api = urlsplit(url).hostname == "api.github.com"
        current = self._check_url(url, api=api)
        deadline = monotonic() + (900 if expected_size else 60)
        for _ in range(6):
            if cancel and cancel.is_set():
                raise UpdateCancelled("下载已取消")
            try:
                response = self.http.request(
                    "GET", current,
                    headers={"Accept": "application/vnd.github+json",
                             "User-Agent": "CCSLab-Labcoding-Updater"},
                    preload_content=False, redirect=False, retries=False, timeout=DOWNLOAD_TIMEOUT,
                )
            except urllib3.exceptions.SSLError as error:
                raise UpdateTrustError("GitHub TLS 证书验证失败") from error
            except (urllib3.exceptions.HTTPError, OSError, TimeoutError) as error:
                raise _network_failure(error) from error
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise UpdateTrustError("GitHub 返回了无目标的重定向")
                    current = self._check_url(urljoin(current, location))
                    continue
                if response.status == 404 and api and current == RELEASES_API + "/latest":
                    raise UpdateError("GitHub 仓库当前没有可检查的 Release")
                if response.status != 200:
                    if response.status in {408, 425, 429, 500, 502, 503, 504}:
                        raise UpdateNetworkError(f"GitHub 服务暂时不可用（HTTP {response.status}）")
                    raise UpdateError(f"GitHub 请求失败（HTTP {response.status}）")
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared = int(declared)
                    except ValueError as error:
                        raise UpdateTrustError("下载长度无效") from error
                    if (declared < 0 or declared > max_bytes
                            or expected_size is not None and declared != expected_size):
                        raise UpdateTrustError("下载文件大小与 GitHub Release 元数据不一致")
                chunks = []
                size = 0
                try:
                    for chunk in response.stream(64 * 1024, decode_content=False):
                        if monotonic() > deadline:
                            raise UpdateNetworkError("GitHub 响应超时")
                        if cancel and cancel.is_set():
                            raise UpdateCancelled("下载已取消")
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > max_bytes or expected_size is not None and size > expected_size:
                            raise UpdateTrustError("下载内容超过 Release 声明大小")
                        chunks.append(chunk)
                        if progress:
                            progress(size, expected_size or declared or 0)
                except urllib3.exceptions.SSLError as error:
                    raise UpdateTrustError("GitHub TLS 证书验证失败") from error
                except (urllib3.exceptions.HTTPError, OSError, TimeoutError) as error:
                    raise _network_failure(error, during_download=bool(expected_size)) from error
                if expected_size is not None and size != expected_size:
                    raise UpdateTrustError("下载长度与 GitHub Release 元数据不匹配")
                return b"".join(chunks)
            finally:
                response.release_conn()
        raise UpdateTrustError("GitHub 重定向次数超限")

    def _api_json(self, url):
        body = self._get(url, max_bytes=2 * 1024 * 1024)
        if body is None:
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("GitHub Release 元数据格式无效") from error
        return value

    def _release_candidate(self, release):
        if not isinstance(release, dict) or release.get("draft") is not False:
            return None
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise UpdateTrustError("GitHub Release 标签无效")
        expected_page = f"https://github.com/{REPOSITORY}/releases/tag/{quote(tag, safe='.-+')}"
        if release.get("html_url") != expected_page:
            raise UpdateTrustError("Release 页面不属于固定仓库")
        prerelease = release.get("prerelease")
        if type(prerelease) is not bool:
            raise UpdateTrustError("Release 通道标记无效")
        version = tag[1:]
        try:
            parsed = SemVer(version)
        except VersionError as error:
            raise UpdateTrustError("Release 版本号无效") from error
        channel = "beta" if prerelease else "stable"
        if parsed.stable != (channel == "stable"):
            raise UpdateTrustError("Release 通道与版本号不匹配")
        if self.version_info["channel"] == "stable" and channel != "stable":
            return None
        if parsed <= self.current_version:
            return None

        name = f"CCSLab-Labcoding-{version}-Setup-x64.exe"
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise UpdateTrustError("Release 资产列表无效")
        matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matching) != 1:
            raise UpdateError(f"新版本 {version} 未提供唯一的 Windows x64 安装包")
        asset = matching[0]
        expected_url = self.release_asset_url(tag, name)
        if asset.get("browser_download_url") != expected_url:
            raise UpdateTrustError("Release 安装包下载地址与固定来源不一致")
        size = asset.get("size")
        if type(size) is not int or size < 1 or size > self.max_download_bytes:
            raise UpdateTrustError("Release 安装包大小无效或超出限制")
        digest_field = asset.get("digest")
        digest = None
        if digest_field not in (None, ""):
            if not isinstance(digest_field, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest_field):
                raise UpdateTrustError("GitHub Release SHA-256 摘要格式无效")
            digest = digest_field.split(":", 1)[1].lower()
        notes = release.get("body") or ""
        if not isinstance(notes, str) or len(notes) > 30_000:
            raise UpdateTrustError("Release 更新说明无效")
        return UpdateCandidate(version, channel, tag, notes, size, digest, name, expected_url)

    def check(self):
        if self.version_info["channel"] == "stable":
            releases = [self._api_json(RELEASES_API + "/latest")]
        else:
            releases = self._api_json(RELEASES_API + "?per_page=30")
            if not isinstance(releases, list):
                raise UpdateError("GitHub Release 列表格式无效")
            if not releases:
                raise UpdateError("GitHub 仓库当前没有可检查的 Release")
        candidates = [candidate for release in releases if release
                      for candidate in [self._release_candidate(release)] if candidate]
        return max(candidates, key=lambda item: SemVer(item.version), default=None)

    def _validate_candidate(self, candidate):
        if not isinstance(candidate, UpdateCandidate):
            raise UpdateTrustError("更新候选不是固定仓库 Release 资产")
        try:
            version = SemVer(candidate.version)
        except VersionError as error:
            raise UpdateTrustError("更新版本格式无效") from error
        expected_name = f"CCSLab-Labcoding-{candidate.version}-Setup-x64.exe"
        if (candidate.tag != "v" + candidate.version or candidate.asset_name != expected_name
                or candidate.channel not in {"stable", "beta"}
                or version.stable != (candidate.channel == "stable")
                or self.version_info["channel"] == "stable" and candidate.channel != "stable"
                or version <= self.current_version or type(candidate.size) is not int
                or candidate.size < 1 or candidate.size > self.max_download_bytes):
            raise UpdateTrustError("更新候选的版本、通道、架构或大小不符合预期")
        if candidate.asset_url != self.release_asset_url(candidate.tag, candidate.asset_name):
            raise UpdateTrustError("安装包来源与固定 GitHub Release 不匹配")
        if candidate.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", candidate.sha256):
            raise UpdateTrustError("GitHub Release SHA-256 摘要无效")

    def download(self, candidate, *, cancel=None, progress=None):
        if not self._download_lock.acquire(blocking=False):
            raise UpdateError("已有更新下载正在进行")
        try:
            return self._download_unlocked(candidate, cancel=cancel, progress=progress)
        finally:
            self._download_lock.release()

    def _download_unlocked(self, candidate, *, cancel=None, progress=None):
        self._validate_candidate(candidate)
        updates_root = self.data_root / "updates"
        updates_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(updates_root):
            raise UpdateTrustError("更新缓存目录不能是符号链接")
        target_dir = updates_root / candidate.version
        target_dir.mkdir(exist_ok=True)
        if _is_reparse_point(target_dir):
            raise UpdateTrustError("版本缓存目录不能是符号链接")
        if shutil.disk_usage(target_dir).free < candidate.size * 2:
            raise UpdateError("磁盘空间不足，已取消更新下载")
        if cancel and cancel.is_set():
            raise UpdateCancelled("下载已取消")
        target = target_dir / candidate.asset_name
        fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=target_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        replaced = False
        try:
            self._download_to(candidate, temporary, cancel=cancel, progress=progress)
            digest = _sha256_file(temporary)
            if candidate.sha256 and digest != candidate.sha256:
                raise UpdateTrustError("安装包 SHA-256 与 GitHub Release Asset 摘要不匹配")
            if not is_windows_x64_pe(temporary):
                raise UpdateTrustError("下载的文件不是 Windows x64 PE 安装程序")
            os.replace(temporary, target)
            replaced = True
            return self.verify_local_package(candidate)
        except Exception:
            temporary.unlink(missing_ok=True)
            if replaced:
                target.unlink(missing_ok=True)
            raise

    def verified_package_path(self, candidate):
        self._validate_candidate(candidate)
        updates_root = self.data_root / "updates"
        version_dir = updates_root / candidate.version
        expected = version_dir / candidate.asset_name
        if (_is_reparse_point(updates_root) or _is_reparse_point(version_dir)
                or _is_reparse_point(expected)):
            raise UpdateTrustError("安装包路径包含符号链接")
        return expected

    def verify_local_package(self, candidate):
        path = self.verified_package_path(candidate)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != candidate.size:
            raise UpdateTrustError("已下载的安装包缺失、大小错误或不是普通文件")
        if not is_windows_x64_pe(path):
            raise UpdateTrustError("本地安装包不是 Windows x64 PE 程序")
        digest = _sha256_file(path)
        if candidate.sha256 and digest != candidate.sha256:
            raise UpdateTrustError("本地安装包 SHA-256 与 GitHub Release Asset 摘要不匹配")
        return VerifiedPackage(path, candidate.version, candidate.size, digest,
                               candidate.sha256 is not None, True)

    def _download_to(self, candidate, path, *, cancel=None, progress=None):
        current = self._check_url(candidate.asset_url)
        deadline = monotonic() + 900
        for _ in range(6):
            if cancel and cancel.is_set():
                raise UpdateCancelled("下载已取消")
            try:
                response = self.http.request("GET", current,
                    headers={"Accept": "application/octet-stream",
                             "User-Agent": "CCSLab-Labcoding-Updater"},
                    preload_content=False, redirect=False, retries=False, timeout=DOWNLOAD_TIMEOUT)
            except urllib3.exceptions.SSLError as error:
                raise UpdateTrustError("GitHub TLS 证书验证失败") from error
            except (urllib3.exceptions.HTTPError, OSError, TimeoutError) as error:
                raise _network_failure(error, during_download=True) from error
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise UpdateTrustError("GitHub 返回无效的下载重定向")
                    current = self._check_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise UpdateError(f"GitHub 安装包下载失败（HTTP {response.status}）")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) != candidate.size:
                    raise UpdateTrustError("安装包长度与 Release Asset 元数据不一致")
                size = 0
                try:
                    with path.open("wb") as stream:
                        for chunk in response.stream(64 * 1024, decode_content=False):
                            if monotonic() > deadline:
                                raise UpdateNetworkError("GitHub 下载超时")
                            if cancel and cancel.is_set():
                                raise UpdateCancelled("下载已取消")
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > candidate.size or size > self.max_download_bytes:
                                raise UpdateTrustError("安装包超过 Release Asset 声明大小")
                            stream.write(chunk)
                            if progress:
                                progress(size, candidate.size)
                        stream.flush()
                        os.fsync(stream.fileno())
                except urllib3.exceptions.SSLError as error:
                    raise UpdateTrustError("GitHub TLS 证书验证失败") from error
                except (urllib3.exceptions.HTTPError, OSError, TimeoutError) as error:
                    raise _network_failure(error, during_download=True) from error
                if size != candidate.size:
                    raise UpdateTrustError("安装包长度与 Release Asset 元数据不匹配")
                return
            finally:
                response.release_conn()
        raise UpdateTrustError("GitHub 下载重定向次数超限")
