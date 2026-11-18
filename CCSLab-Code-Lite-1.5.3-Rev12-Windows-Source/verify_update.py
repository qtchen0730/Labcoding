"""Focused tests for the HTTPS-only, manual Windows update flow."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import urllib3

from app import App, update_check_error_message
from update_system import (
    UpdateCandidate, UpdateError, UpdateNetworkError, UpdateService,
    UpdateTrustError, VerifiedPackage, _default_http_client, _network_failure,
    is_windows_x64_pe,
)
from versioning import SemVer, VersionError


def version_info(version="1.6.0-beta.1", channel="beta"):
    return {
        "version": version,
        "release_sequence": 1,
        "channel": channel,
        "repository": "qtchen0730/Labcoding",
        "installer_asset": f"CCSLab-Labcoding-{version}-Setup-x64.exe",
    }


def make_pe(machine=0x8664, magic=0x20B, tail=b"fixture"):
    data = bytearray(512 + len(tail))
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = machine.to_bytes(2, "little")
    data[0x98:0x9A] = magic.to_bytes(2, "little")
    data[512:] = tail
    return bytes(data)


class Response:
    def __init__(self, body=b"", status=200, headers=None, error=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.error = error

    def stream(self, *_args, **_kwargs):
        if self.body:
            yield self.body
        if self.error:
            raise self.error

    def release_conn(self):
        pass


class QueueHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def release(version, *, channel=None, payload=b"installer", digest=True, notes="test notes"):
    tag = "v" + version
    channel = channel or ("stable" if SemVer(version).stable else "beta")
    name = f"CCSLab-Labcoding-{version}-Setup-x64.exe"
    asset = {
        "name": name,
        "browser_download_url": UpdateService.release_asset_url(tag, name),
        "size": len(payload),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest() if digest else None,
    }
    return {
        "draft": False,
        "tag_name": tag,
        "html_url": f"https://github.com/qtchen0730/Labcoding/releases/tag/{tag}",
        "prerelease": channel == "beta",
        "body": notes,
        "assets": [asset],
    }


def api_response(value):
    return Response(json.dumps(value).encode("utf-8"))


def candidate_for(service, version="1.6.0-beta.2", payload=None, digest=True):
    payload = payload if payload is not None else make_pe()
    item = release(version, payload=payload, digest=digest)
    return service._release_candidate(item)


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def service(self, info=None, http=None, max_download_bytes=300 * 1024 * 1024):
        return UpdateService(self.root, info or version_info(), http=http,
                             max_download_bytes=max_download_bytes)

    def test_semver_compares_beta_and_stable_versions(self):
        self.assertLess(SemVer("1.6.0-beta.1"), SemVer("1.6.0-beta.2"))
        self.assertLess(SemVer("1.6.0-beta.2"), SemVer("1.6.0"))
        with self.assertRaises(VersionError):
            SemVer("01.6.0")

    def test_release_source_and_asset_urls_are_fixed_https_github_urls(self):
        valid = UpdateService.release_asset_url(
            "v1.6.0-beta.2", "CCSLab-Labcoding-1.6.0-beta.2-Setup-x64.exe")
        self.assertEqual(valid, "https://github.com/qtchen0730/Labcoding/releases/download/"
                               "v1.6.0-beta.2/CCSLab-Labcoding-1.6.0-beta.2-Setup-x64.exe")
        for url in ("http://github.com/qtchen0730/Labcoding/file.exe",
                    "https://evil.example/update.exe", "https://user@github.com/file.exe",
                    "https://github.com:444/file.exe"):
            with self.subTest(url=url), self.assertRaises(UpdateTrustError):
                UpdateService._check_url(url)
        with self.assertRaises(UpdateTrustError):
            UpdateService.release_asset_url("v1.6.0-beta.2", "https://evil.example/app.exe")

    def test_beta_check_finds_newest_release_and_sends_no_business_credentials(self):
        releases = [release("1.6.0-beta.2", notes="Bug fixes"), release("1.6.0-beta.1")]
        http = QueueHttp([api_response(releases)])
        candidate = self.service(http=http).check()
        self.assertEqual(candidate.version, "1.6.0-beta.2")
        self.assertEqual(candidate.notes, "Bug fixes")
        self.assertEqual(http.requests[0][1],
                         "https://api.github.com/repos/qtchen0730/Labcoding/releases?per_page=30")
        self.assertNotIn("Authorization", http.requests[0][2]["headers"])

    def test_no_newer_release_returns_none(self):
        http = QueueHttp([api_response([release("1.6.0-beta.1")])])
        self.assertIsNone(self.service(http=http).check())

    def test_stable_channel_uses_only_the_stable_latest_endpoint(self):
        stable_info = version_info("1.5.3", "stable")
        stable_release = release("1.6.0", channel="stable")
        http = QueueHttp([api_response(stable_release)])
        candidate = self.service(stable_info, http=http).check()
        self.assertEqual(candidate.channel, "stable")
        self.assertTrue(http.requests[0][1].endswith("/releases/latest"))

    def test_stable_client_does_not_select_beta_release(self):
        stable_info = version_info("1.5.3", "stable")
        http = QueueHttp([api_response(release("1.6.0-beta.2"))])
        candidate = self.service(stable_info, http=http)._release_candidate(release("1.6.0-beta.2"))
        self.assertIsNone(candidate)

    def test_beta_channel_can_select_a_newer_stable_release(self):
        http = QueueHttp([api_response([release("1.6.0-beta.2"),
                                        release("1.6.0", channel="stable")])])
        candidate = self.service(http=http).check()
        self.assertEqual(candidate.version, "1.6.0")
        self.assertEqual(candidate.channel, "stable")

    def test_missing_asset_digest_is_not_fabricated(self):
        candidate = self.service()._release_candidate(release("1.6.0-beta.2", digest=False))
        self.assertIsNone(candidate.sha256)

    def test_malformed_asset_digest_is_rejected(self):
        item = release("1.6.0-beta.2")
        item["assets"][0]["digest"] = "sha1:" + "a" * 40
        with self.assertRaises(UpdateTrustError):
            self.service()._release_candidate(item)

    def test_missing_or_ambiguous_x64_asset_is_not_offered(self):
        item = release("1.6.0-beta.2")
        item["assets"] = []
        with self.assertRaises(UpdateError):
            self.service()._release_candidate(item)
        item = release("1.6.0-beta.2")
        item["assets"].append(dict(item["assets"][0]))
        with self.assertRaises(UpdateError):
            self.service()._release_candidate(item)

    def test_release_metadata_from_another_repository_is_rejected(self):
        item = release("1.6.0-beta.2")
        item["html_url"] = "https://github.com/attacker/repo/releases/tag/v1.6.0-beta.2"
        with self.assertRaises(UpdateTrustError):
            self.service()._release_candidate(item)

    def test_network_failure_is_classified(self):
        http = QueueHttp([urllib3.exceptions.NewConnectionError(None, "offline")])
        with self.assertRaises(UpdateNetworkError):
            self.service(http=http).check()

    def test_windows_https_proxy_is_used_without_disabling_tls_validation(self):
        proxy_client = object()
        with patch("update_system.urllib.request.getproxies", return_value={
                "https": "http://proxy.example:8080"}), \
             patch("update_system.urllib3.ProxyManager", return_value=proxy_client) as manager:
            self.assertIs(_default_http_client(), proxy_client)
        manager.assert_called_once_with("http://proxy.example:8080", cert_reqs="CERT_REQUIRED")

    def test_direct_connection_remains_when_no_system_proxy_is_configured(self):
        direct_client = object()
        with patch("update_system.urllib.request.getproxies", return_value={}), \
             patch("update_system.urllib3.PoolManager", return_value=direct_client) as manager:
            self.assertIs(_default_http_client(), direct_client)
        manager.assert_called_once_with(cert_reqs="CERT_REQUIRED")

    def test_connect_timeout_gets_specific_network_message(self):
        error = urllib3.exceptions.ConnectTimeoutError(None, "connect timeout")
        mapped = _network_failure(error)
        self.assertIn("连接 GitHub 超时", str(mapped))
        self.assertNotIn("connect timeout", str(mapped))

    def test_proxy_dns_and_read_timeout_are_distinguished(self):
        cases = (
            (urllib3.exceptions.ProxyError("proxy failed", OSError()), "系统代理连接失败"),
            (urllib3.exceptions.NameResolutionError("api.github.com", None, OSError()),
             "DNS 解析失败"),
            (urllib3.exceptions.ReadTimeoutError(None, "api.github.com", "read timeout"),
             "等待 GitHub 响应超时"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIn(expected, str(_network_failure(error)))

    def test_tls_certificate_errors_remain_trust_errors(self):
        http = QueueHttp([urllib3.exceptions.SSLError("certificate verify failed")])
        with self.assertRaises(UpdateTrustError):
            self.service(http=http).check()

    def test_github_404_latest_is_not_claimed_as_latest_release_metadata(self):
        http = QueueHttp([Response(status=404)])
        with self.assertRaises(UpdateError):
            self.service(version_info("1.5.3", "stable"), http=http).check()

    def test_empty_beta_release_list_is_not_claimed_as_latest(self):
        http = QueueHttp([api_response([])])
        with self.assertRaises(UpdateError):
            self.service(http=http).check()


    def test_x64_pe_architecture_is_checked_from_file_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "setup.exe"
            path.write_bytes(make_pe())
            self.assertTrue(is_windows_x64_pe(path))
            path.write_bytes(make_pe(machine=0xAA64))
            self.assertFalse(is_windows_x64_pe(path))
            path.write_bytes(make_pe(magic=0x10B))
            self.assertFalse(is_windows_x64_pe(path))

    def test_download_verifies_size_digest_architecture_and_reports_progress(self):
        payload = make_pe()
        candidate = candidate_for(self.service(), payload=payload)
        http = QueueHttp([Response(payload, headers={"Content-Length": str(len(payload))})])
        user_file = self.root / "saved-login.bin"
        user_file.write_bytes(b"keep-user-data")
        service = self.service(http=http)
        progress = []
        package = service.download(candidate, progress=lambda done, total: progress.append((done, total)))
        self.assertTrue(package.digest_verified)
        self.assertTrue(package.architecture_verified)
        self.assertEqual(package.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(package.path.read_bytes(), payload)
        self.assertTrue(progress)
        self.assertEqual(user_file.read_bytes(), b"keep-user-data")

    def test_missing_digest_download_is_allowed_but_not_marked_as_digest_verified(self):
        payload = make_pe()
        candidate = candidate_for(self.service(), payload=payload, digest=False)
        service = self.service(http=QueueHttp([Response(payload,
            headers={"Content-Length": str(len(payload))})]))
        package = service.download(candidate)
        self.assertFalse(package.digest_verified)
        self.assertEqual(package.sha256, hashlib.sha256(payload).hexdigest())

    def test_digest_mismatch_removes_download(self):
        payload = make_pe()
        candidate = candidate_for(self.service(), payload=payload)
        candidate = UpdateCandidate(candidate.version, candidate.channel, candidate.tag,
            candidate.notes, candidate.size, "0" * 64, candidate.asset_name, candidate.asset_url)
        service = self.service(http=QueueHttp([Response(payload,
            headers={"Content-Length": str(len(payload))})]))
        with self.assertRaises(UpdateTrustError):
            service.download(candidate)
        self.assertEqual(list((self.root / "updates" / candidate.version).iterdir()), [])

    def test_wrong_architecture_download_is_removed(self):
        payload = make_pe(machine=0xAA64)
        candidate = candidate_for(self.service(), payload=make_pe())
        candidate = UpdateCandidate(candidate.version, candidate.channel, candidate.tag,
            candidate.notes, len(payload), hashlib.sha256(payload).hexdigest(),
            candidate.asset_name, candidate.asset_url)
        service = self.service(http=QueueHttp([Response(payload,
            headers={"Content-Length": str(len(payload))})]))
        with self.assertRaises(UpdateTrustError):
            service.download(candidate)
        self.assertEqual(list((self.root / "updates" / candidate.version).iterdir()), [])

    def test_cancelled_download_leaves_no_partial_file(self):
        candidate = candidate_for(self.service())
        cancel = Event()
        cancel.set()
        with self.assertRaises(Exception):
            self.service(http=QueueHttp([])).download(candidate, cancel=cancel)
        self.assertEqual(list((self.root / "updates" / candidate.version).iterdir()), [])

    def test_interrupted_download_cleans_partial_file(self):
        payload = make_pe()
        candidate = candidate_for(self.service(), payload=payload)
        response = Response(payload[:64], headers={"Content-Length": str(len(payload))},
                            error=urllib3.exceptions.ReadTimeoutError(None, None, "interrupted"))
        with self.assertRaises(UpdateNetworkError):
            self.service(http=QueueHttp([response])).download(candidate)
        self.assertEqual(list((self.root / "updates" / candidate.version).iterdir()), [])

    def test_external_redirect_is_rejected(self):
        candidate = candidate_for(self.service())
        response = Response(status=302, headers={"Location": "https://evil.example/setup.exe"})
        with self.assertRaises(UpdateTrustError):
            self.service(http=QueueHttp([response])).download(candidate)

    def test_candidate_rejects_arbitrary_external_asset_url_and_downgrade(self):
        service = self.service()
        candidate = candidate_for(service)
        bad_url = UpdateCandidate(candidate.version, candidate.channel, candidate.tag,
            candidate.notes, candidate.size, candidate.sha256, candidate.asset_name,
            "https://evil.example/setup.exe")
        with self.assertRaises(UpdateTrustError):
            service._validate_candidate(bad_url)
        old = UpdateCandidate("1.6.0-beta.1", "beta", "v1.6.0-beta.1", "", 10,
            None, "CCSLab-Labcoding-1.6.0-beta.1-Setup-x64.exe",
            UpdateService.release_asset_url("v1.6.0-beta.1", "CCSLab-Labcoding-1.6.0-beta.1-Setup-x64.exe"))
        with self.assertRaises(UpdateTrustError):
            service._validate_candidate(old)


class UpdateUiTests(unittest.TestCase):
    class Variable:
        def __init__(self, value=""):
            self.value = value
            self.set_threads = []
        def set(self, value):
            self.value = value
            self.set_threads.append(get_ident())
        def get(self):
            return self.value

    class Button:
        def __init__(self):
            self.states = []
        def winfo_exists(self):
            return True
        def configure(self, **values):
            self.states.append(values.get("state"))

    class Banner:
        def winfo_manager(self):
            return ""

    class Page:
        def __init__(self):
            self.destroyed = False
            self.packed = False
        def destroy(self):
            self.destroyed = True
        def pack(self, **_kwargs):
            self.packed = True
        def pack_forget(self):
            self.packed = False

    def app(self, service=None):
        app = object.__new__(App)
        app.update_checking = False
        app.update_downloading = False
        app.update_package = None
        app.update_candidate = None
        app.update_snoozed_until = 0
        app.closing = False
        app.update_service = service
        app.update_cancel = Event()
        app.update_status = self.Variable()
        app.update_message = self.Variable()
        app.update_check_button = self.Button()
        app.update_download_button = self.Button()
        app.current_page = "settings"
        app.events = Queue()
        app.version_info = version_info()
        app.update_actions = lambda: None
        app.update_banner = self.Banner()
        return app

    def test_check_starts_immediately_and_returns_on_ui_queue(self):
        entered, release_gate = Event(), Event()
        class Delayed:
            def check(self):
                entered.set()
                release_gate.wait(3)
                return None
        app = self.app(Delayed())
        main_thread = get_ident()
        app.busy = ""
        app.connection_mode = "account"
        app.account = {"status": "active"}
        app.page_panel = self.Page()
        app.workspace_view = self.Page()
        app.apply_theme = lambda: None
        scheduled = []
        app.schedule = lambda *args: scheduled.append(args)
        App.check_for_updates(app, manual=True)
        self.assertEqual(app.update_status.get(), "正在检查更新…")
        self.assertTrue(entered.wait(2))
        self.assertEqual(app.update_status.set_threads, [main_thread])
        App.show_page(app, "workspace")
        role_callbacks = []
        release_gate.set()
        deadline = time.monotonic() + 3
        while app.update_checking and time.monotonic() < deadline:
            time.sleep(0.01)
            App.pump(app)
        app.events.put(lambda: role_callbacks.append("role switch completed"))
        App.pump(app)
        self.assertEqual(app.update_status.get(), "当前已是最新版本")
        self.assertIsNone(app.update_check_button)
        self.assertFalse(app.update_checking)
        self.assertEqual(role_callbacks, ["role switch completed"])
        self.assertTrue(scheduled)

    def test_leaving_settings_clears_page_owned_update_controls_before_destroy(self):
        app = self.app()
        old_page, workspace = self.Page(), self.Page()
        app.closing = False
        app.busy = ""
        app.connection_mode = "account"
        app.account = {"status": "active"}
        app.current_page = "settings"
        app.page_panel = old_page
        app.workspace_view = workspace
        app.update_candidate = None
        app.apply_theme = lambda: None
        app.update_actions = lambda: None
        App.show_page(app, "workspace")
        self.assertTrue(old_page.destroyed)
        self.assertTrue(workspace.packed)
        for name in ("update_check_button", "update_settings_download_button",
                     "update_settings_later_button", "update_settings_open_folder_button"):
            self.assertIsNone(getattr(app, name))

    def test_event_pump_reschedules_after_a_callback_raises(self):
        app = self.app()
        app.closing = False
        scheduled = []
        completed = []
        app.schedule = lambda *args: scheduled.append(args)
        app.events.put(lambda: (_ for _ in ()).throw(RuntimeError("callback failure")))
        app.events.put(lambda: completed.append(True))
        with self.assertRaisesRegex(RuntimeError, "callback failure"):
            App.pump(app)
        self.assertEqual(len(scheduled), 1)
        App.pump(app)
        self.assertEqual(completed, [True])

    def test_new_version_shows_version_notes_and_signature_warning(self):
        app = self.app()
        candidate = UpdateCandidate("1.6.0-beta.2", "beta", "v1.6.0-beta.2", "Fixes",
            100, None, "CCSLab-Labcoding-1.6.0-beta.2-Setup-x64.exe",
            UpdateService.release_asset_url("v1.6.0-beta.2",
                "CCSLab-Labcoding-1.6.0-beta.2-Setup-x64.exe"))
        App._update_check_done(app, candidate, True)
        self.assertIn("1.6.0-beta.2", app.update_status.get())
        self.assertIn("\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1", app.update_message.get())
        self.assertIn("\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1", app.update_message.get())
        self.assertIn("\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1", app.update_message.get())
        self.assertIn("\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1", app.update_message.get())

    def test_check_failures_are_never_reported_as_latest(self):
        errors = (
            (UpdateNetworkError("offline"), "网络连接失败"),
            (UpdateTrustError("invalid asset"), "安全验证未通过"),
            (UpdateError("release missing"), "更新检查失败"),
        )
        for error, expected in errors:
            with self.subTest(error=type(error).__name__):
                app = self.app()
                App._update_check_done(app, error, True)
                self.assertIn(expected, app.update_status.get())
                self.assertNotEqual(app.update_status.get(), "当前已是最新版本")

    def test_settings_error_is_not_duplicated_in_detail_label(self):
        app = self.app()
        App._update_check_done(app, UpdateNetworkError("connect timeout"), True)
        self.assertTrue(app.update_status.get())
        self.assertEqual(app.update_message.get(), "")

    def test_download_completion_warns_and_never_executes_installer(self):
        app = self.app()
        package = VerifiedPackage(Path("C:/updates/setup.exe"), "1.6.0-beta.2", 100,
                                  "a" * 64, False, True)
        with patch.object(os, "startfile", create=True) as open_folder:
            App._update_download_done(app, package, None)
            self.assertIn("\u672a\u7ecf\u72ec\u7acb\u53d1\u5e03\u8005\u7b7e\u540d\u9a8c\u8bc1", app.update_status.get())
            self.assertIn("手动运行安装包", app.update_status.get())
            open_folder.assert_not_called()

    def test_verified_folder_action_only_opens_download_directory(self):
        app = self.app()
        candidate = SimpleNamespace()
        package = VerifiedPackage(Path("C:/user-data/updates/v/setup.exe"), "1.6.0-beta.2",
                                  100, "a" * 64, True, True)
        class Service:
            def verify_local_package(self, _candidate):
                return package
        app.update_service = Service()
        app.update_candidate = candidate
        app.update_package = package
        with patch.object(os, "startfile", create=True) as open_folder:
            App.open_update_folder(app)
        open_folder.assert_called_once_with(str(package.path.parent))

    def test_error_message_helper_preserves_network_and_trust_categories(self):
        self.assertIn("网络连接失败", update_check_error_message(UpdateNetworkError("offline")))
        self.assertIn("安全验证未通过", update_check_error_message(UpdateTrustError("bad")))


if __name__ == "__main__":
    unittest.main()
