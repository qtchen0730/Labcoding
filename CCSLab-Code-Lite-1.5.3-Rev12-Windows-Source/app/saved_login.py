"""Opt-in login storage encrypted for the current user.

On Windows this uses DPAPI. On Linux credentials are stored in the system
keyring (Secret Service / GNOME Keyring via the ``keyring`` library).
Saved credentials are always server-pinned to prevent accidental leakage.
"""
import json
import os
from pathlib import Path

from account_client import normalize_server, valid_account_password

if os.name == "nt":
    from connection import _dpapi

    _KEYRING_AVAILABLE = True
else:
    _KEYRING_AVAILABLE = False

    def _ensure_keyring():
        """Import keyring at runtime and verify the backend is usable.

        Returns True when the backend can store and retrieve secrets.
        Raises OSError when the system keyring is not available.
        """
        global _KEYRING_AVAILABLE
        if _KEYRING_AVAILABLE:
            return True
        try:
            import keyring  # noqa: F811
        except ImportError:
            raise OSError("系统未安装 keyring 库，无法安全保存登录信息。") from None

        # Detect the null / disabled backend that silently drops secrets.
        backend = keyring.get_keyring()
        backend_name = type(backend).__name__.lower()
        if "null" in backend_name or "disabled" in backend_name:
            raise OSError("当前环境未配置可用的系统密钥环，无法安全保存登录信息。")

        # Quick round-trip probe to confirm the backend actually works.
        _service = "labcoding-keyring-probe"
        try:
            keyring.set_password(_service, "probe-user", "probe-value")
            value = keyring.get_password(_service, "probe-user")
            if value != "probe-value":
                raise OSError("密钥环写入验证失败，无法确认凭据安全。")
            keyring.delete_password(_service, "probe-user")
        except Exception as exc:
            raise OSError(f"系统密钥环不可用：{exc}") from exc

        _KEYRING_AVAILABLE = True
        return True


def _is_legacy_file(path: Path) -> bool:
    """Return True when *path* looks like an old plaintext credential store."""
    if not path.is_file():
        return False
    if path.suffix == ".json":
        return True
    # DPAPI blobs are always binary; a JSON suffix is the only legacy marker.
    return False


def _has_legacy_credentials(data_root: Path) -> bool:
    """Check whether an old plaintext JSON credential file exists."""
    legacy = data_root / "saved-login.json"
    return legacy.is_file()


def _remove_legacy_credentials(data_root: Path) -> None:
    """Remove the old plaintext credential file, if it exists."""
    legacy = data_root / "saved-login.json"
    try:
        legacy.unlink(missing_ok=True)
    except OSError:
        pass


# ── Windows (unchanged DPAPI path) ────────────────────────────────────

if os.name == "nt":
    class SavedLogin:
        """Windows: DPAPI-encrypted credential store (unchanged)."""

        def __init__(self, data_root):
            self.path = data_root / "saved-login.dpapi"

        def _encrypt(self, plaintext_bytes):
            return _dpapi(plaintext_bytes, encrypt=True)

        def _decrypt(self, ciphertext_bytes):
            return _dpapi(ciphertext_bytes, encrypt=False)

        def forget(self):
            self.path.unlink(missing_ok=True)

        def save(self, server, username, password, token=""):
            record = {"server": normalize_server(server), "username": username,
                      "password": password, "token": token}
            if not isinstance(username, str) or not username or not valid_account_password(password):
                raise ValueError("Invalid saved account")
            encrypted = self._encrypt(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            temporary = self.path.with_suffix(".tmp")
            try:
                temporary.write_bytes(encrypted)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

        def load(self, server):
            if not self.path.exists():
                return None
            if self.path.stat().st_size > 65536:
                raise ValueError("Invalid saved account")
            value = json.loads(self._decrypt(self.path.read_bytes()).decode("utf-8"))
            if (not isinstance(value, dict) or not isinstance(value.get("username"), str)
                    or not valid_account_password(value.get("password"))
                    or not isinstance(value.get("token", ""), str)):
                raise ValueError("Invalid saved account")
            if normalize_server(value.get("server")) != normalize_server(server):
                return None
            return value

        def update_session(self, server, token):
            record = self.load(server)
            if record:
                self.save(server, record["username"], record["password"], token)

# ── Linux (keyring-backed store) ──────────────────────────────────────
else:
    _KEYRING_SERVICE = "labcoding-login"

    class _KeyringPath:
        """Pseudo-path object so callers can still use ``saved_login.path.exists()``
        and ``saved_login.path.is_file()`` to check whether credentials are stored.

        On Linux the real storage is the system keyring; there is no credential
        file on disk.  This shim reports ``True`` when any keyring entry exists
        for our application service, or when a legacy plaintext file is present.
        """

        def __init__(self, data_root: Path):
            self._data_root = Path(data_root)
            self._legacy = self._data_root / "saved-login.json"

        def exists(self):
            """True when credentials are stored (keyring or legacy file)."""
            if _has_legacy_credentials(self._data_root):
                return True
            return self._has_keyring_entry()

        def is_file(self):
            """Same contract as ``exists()`` — callers use them interchangeably."""
            return self.exists()

        @staticmethod
        def _has_keyring_entry():
            try:
                import keyring
                # Check for any entry under our service namespace.
                # We use the service name itself as a sentinel — if any
                # server-specific entry exists, the service is "active".
                # Also check the sentinel marker we set on first save.
                v = keyring.get_password(_KEYRING_SERVICE, _KEYRING_SERVICE)
                if v is not None:
                    return True
                return False
            except Exception:
                return False

    class SavedLogin:
        """Linux: keyring-backed credential store.

        Credentials are stored in the system keyring (Secret Service API /
        GNOME Keyring / KWallet depending on the desktop environment).

        Each Linux user gets their own keyring namespace automatically; no
        cross-user credential sharing is possible.

        When the system keyring is unavailable:
        - ``save()`` raises ``OSError`` instead of silently falling back to
          plaintext.
        - ``load()`` raises ``OSError``.
        - The application can catch these and offer the user a session-only
          login experience.
        """

        def __init__(self, data_root):
            self._data_root = Path(data_root)
            self.path = _KeyringPath(data_root)
            self._service = _KEYRING_SERVICE
            self._server_key = None  # server → keyring username mapping cache

        # ── helpers ──────────────────────────────────────────────────

        def _keyring_username(self, server):
            """Derive a keyring username from the server URL.

            Different servers get different keyring entries even for the same
            Linux user, preventing cross-origin credential confusion.
            """
            return normalize_server(server)

        def _check_keyring(self):
            """Raise OSError when the system keyring is not usable."""
            _ensure_keyring()

        # ── public API (same signature as Windows) ──────────────────

        def forget(self):
            """Delete all keyring entries for this application.

            We track server URLs in the sentinel marker as a comma-separated
            list, so that ``forget()`` can clean up entries from multiple
            servers.
            """
            import keyring
            try:
                marker = keyring.get_password(self._service, self._service)
                if marker:
                    # Sentinel may contain comma-separated server URLs.
                    for url in marker.split(","):
                        url = url.strip()
                        if url:
                            try:
                                keyring.delete_password(self._service, url)
                            except Exception:
                                pass
                keyring.delete_password(self._service, self._service)
            except Exception:
                pass
            # Also remove legacy file if present.
            _remove_legacy_credentials(self._data_root)

        def save(self, server, username, password, token=""):
            """Save credentials in the system keyring.

            Raises OSError when the keyring is not available, instead of
            silently falling back to plaintext.
            """
            if not isinstance(username, str) or not username:
                raise ValueError("Invalid saved account")
            if not valid_account_password(password):
                raise ValueError("Invalid saved account")

            self._check_keyring()

            import keyring
            normalized = normalize_server(server)

            # Build a JSON payload (server-pinned, not written to disk).
            record = json.dumps({
                "server": normalized,
                "username": username,
                "password": password,
                "token": token,
            }, ensure_ascii=False)

            # Store the JSON blob in the keyring under the server URL as
            # the keyring "username" field.  This way different servers
            # get independent entries in the same keyring service.
            keyring.set_password(self._service, normalized, record)

            # Update the sentinel marker with the set of known server URLs.
            # This lets ``forget()`` find and delete all entries later.
            try:
                existing = keyring.get_password(self._service, self._service) or ""
            except Exception:
                existing = ""
            known = set(u.strip() for u in existing.split(",") if u.strip())
            known.add(normalized)
            keyring.set_password(self._service, self._service, ",".join(sorted(known)))

        def load(self, server):
            """Load credentials from the system keyring.

            Returns None when no credentials are stored for *server*.
            Raises OSError when the keyring is unavailable.
            Raises ValueError when legacy plaintext credentials are detected.
            """
            # First check for legacy plaintext file and refuse to use it.
            if _has_legacy_credentials(self._data_root):
                raise ValueError(
                    "检测到旧版明文凭据文件，出于安全考虑已停用。请重新登录。"
                )

            self._check_keyring()

            import keyring
            normalized = normalize_server(server)
            blob = keyring.get_password(self._service, normalized)
            if blob is None:
                return None

            try:
                value = json.loads(blob)
            except (json.JSONDecodeError, TypeError):
                raise ValueError("Invalid saved account")

            if (not isinstance(value, dict)
                    or not isinstance(value.get("username"), str)
                    or not valid_account_password(value.get("password"))
                    or not isinstance(value.get("token", ""), str)):
                raise ValueError("Invalid saved account")

            # Never send saved credentials to a different configured server.
            if normalize_server(value.get("server")) != normalized:
                return None

            return value

        def update_session(self, server, token):
            """Update the stored token for an existing credential record."""
            record = self.load(server)
            if record:
                self.save(server, record["username"], record["password"], token)
