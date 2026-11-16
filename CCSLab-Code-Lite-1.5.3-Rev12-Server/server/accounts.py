"""Account persistence and authorization for the Ubuntu gateway."""

from contextlib import contextmanager
from hashlib import sha256
from hmac import compare_digest, digest as hmac_digest
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
from threading import BoundedSemaphore
from .quotas import Quotas
from .account_errors import AccountError
from time import time
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


SESSION_TTL = 8 * 60 * 60
TARGET_STATES = frozenset({"active", "disabled", "rejected"})
_SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_ARGON2 = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_HASH_SLOTS = BoundedSemaphore(4)
_USER_COLUMNS = "id, username, role, status"
_ADMIN_CODE = re.compile(r"[0-9]{6}\Z")
_CHALLENGE_WINDOW = 15 * 60
_INITIAL_OPEN_WEBUI_VERIFIER = (
    "$argon2id$v=19$m=19456,t=2,p=1$d583UQ6OeFtZrcVBI53VSQ$thtVyQE/1I9oMWminJGVkNuJifQT7cbYDS76lDXOWtY"
)
_OPEN_WEBUI_GRANT_TTL = 10 * 60
_OPEN_WEBUI_GRANT_PATTERN = re.compile(r"([0-9]{1,12})\.([a-f0-9]{64})\Z")
_ACCOUNT_MIGRATIONS = ("001_accounts.sql", "003_session_roles.sql")


def normalize_username(username):
    if not isinstance(username, str) or len(username) > 64:
        raise AccountError("用户名须为 3–40 位字母、数字或 ._-。")
    username = unicodedata.normalize("NFKC", username.strip())
    if not 3 <= len(username) <= 40 or not all(c.isalnum() or c in "._-" for c in username):
        raise AccountError("用户名须为 3–40 位字母、数字或 ._-。")
    return username


def validate_password(password):
    if not isinstance(password, str) or not 6 <= len(password) <= 128:
        raise AccountError("密码长度须为 6–128 个字符。")
    if "\x00" in password:
        raise AccountError("密码包含无效字符。")
    try:
        password.encode("utf-8")
    except UnicodeError as error:
        raise AccountError("密码包含无效字符。") from error


def write_admin_code(path: Path, code: str):
    """Set the server's private administrator authorization verifier."""
    if not isinstance(code, str) or not _ADMIN_CODE.fullmatch(code):
        raise AccountError("管理员授权密码须为 6 位数字。")
    _write_verifier(path, _ARGON2.hash(code))


def _write_verifier(path: Path, digest: str):
    path = path.absolute()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ccslab-verifier-")
    tmp = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(digest + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_admin_code(path: Path | None):
    """Read a bounded, private Argon2id authorization verifier."""
    return _read_verifier(path)


def _read_verifier(path: Path | None):
    if path is None:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            mode = os.fstat(file.fileno()).st_mode
            if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
                return None
            digest = file.read(257).strip().decode("ascii")
        if len(digest) > 256 or _ARGON2.check_needs_rehash(digest):
            return None
    except (OSError, ValueError, InvalidHashError, OverflowError):
        return None
    return digest


def _admin_fingerprint(digest):
    return sha256(digest.encode("ascii")).digest()


def token_hash(token):
    if not isinstance(token, str) or not _SESSION_PATTERN.fullmatch(token):
        raise AccountError("请重新登录。", 401)
    return sha256(token.encode("ascii")).digest()


@contextmanager
def _hash_slot():
    if not _HASH_SLOTS.acquire(blocking=False):
        raise AccountError("身份验证暂时繁忙，请稍后重试。", 429)
    try:
        yield
    finally:
        _HASH_SLOTS.release()


class Accounts:
    def __init__(self, database, *, admin_code_file=None):
        self.database = Path(database).resolve()
        self.admin_code_file = admin_code_file
        self.open_webui_password_file = self.database.with_name(self.database.name + ".open-webui.argon2")
        self._open_webui_grant_key = secrets.token_bytes(32)
        self.dummy_hash = _ARGON2.hash(secrets.token_hex(32))
        self.quotas = Quotas(self)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self):
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.close(os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= len(_ACCOUNT_MIGRATIONS):
                raise RuntimeError("账号数据库版本高于服务端支持范围。")
            migrations = Path(__file__).with_name("migrations")
            for next_version, filename in enumerate(_ACCOUNT_MIGRATIONS[version:], start=version + 1):
                statement = ""
                for line in (migrations / filename).read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError("数据库迁移包含未完成的语句。")
                db.execute(f"PRAGMA user_version = {next_version}")
            # An additive audit table leaves the previous server's schema version readable.
            db.execute((migrations / "004_self_password_audit.sql").read_text(encoding="utf-8"))
            statement = ""
            for line in (migrations / "005_token_quotas.sql").read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""

    def initialize_open_webui_access(self):
        if (self.admin_code_file is not None
                and self.admin_code_file.resolve() == self.open_webui_password_file.resolve()):
            raise ValueError("管理员授权密码与 Open WebUI 入口密码须使用独立文件。")
        # Startup and password changes share the database write lock, so a
        # concurrent startup cannot overwrite a newly configured verifier.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not os.path.lexists(self.open_webui_password_file):
                _write_verifier(self.open_webui_password_file, _INITIAL_OPEN_WEBUI_VERIFIER)

    def _throttle(self, scopes, window):
        now = int(time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_limits WHERE expires_at <= ?", (now,))
            buckets = [(sha256(key.encode("utf-8")).digest(), limit) for key, limit in scopes]
            reservations = []
            for key, limit in buckets:
                previous = db.execute(
                    "SELECT attempts, expires_at FROM auth_limits WHERE bucket = ?", (key,),
                ).fetchone()
                if previous and previous["attempts"] >= limit:
                    raise AccountError("尝试次数过多，请稍后再试。", 429)
                reservations.append((key, previous["expires_at"] if previous else now + window))
            db.executemany(
                "INSERT INTO auth_limits VALUES (?, 1, ?) "
                "ON CONFLICT(bucket) DO UPDATE SET attempts = attempts + 1",
                reservations,
            )
        return reservations

    def _refund_throttle(self, reservations):
        # Match the reserved window so a slow success cannot refund a new one.
        with self.connect() as db:
            db.executemany(
                "UPDATE auth_limits SET attempts = attempts - 1 "
                "WHERE bucket = ? AND expires_at = ? AND attempts > 0", reservations,
            )

    def throttle(self, action, source, username):
        self._throttle((
            (f"{action}:global", 120), (f"{action}:source:{source}", 16 if action == "login" else 8),
            (f"{action}:name:{username.casefold()}", 8),
        ), 60)

    def register(self, username, password, source):
        username = normalize_username(username)
        validate_password(password)
        self.throttle("register", source, username)
        return self.create(username, password, bootstrap=False)

    def create(self, username, password, *, bootstrap):
        role, status = ("admin", "active") if bootstrap else ("user", "pending")
        return self._create(username, password, role, status, first_administrator=bootstrap)

    def _verify_admin_code(self, code, source):
        digest = read_admin_code(self.admin_code_file)
        if digest is None:
            raise AccountError("服务器尚未配置管理员授权密码。", 503)
        reservations = self._throttle((
            ("admin-code:global", 5), (f"admin-code:source:{source}", 3),
        ), _CHALLENGE_WINDOW)
        if not isinstance(code, str) or not _ADMIN_CODE.fullmatch(code):
            raise AccountError("管理员授权密码错误。", 403)
        with _hash_slot():
            try:
                _ARGON2.verify(digest, code)
            except (VerificationError, InvalidHashError):
                raise AccountError("管理员授权密码错误。", 403) from None
        self._refund_throttle(reservations)
        return digest

    def _verify_open_webui_password(self, password, source):
        try:
            validate_password(password)
        except AccountError:
            raise AccountError("Open WebUI 入口密码错误。", 403) from None
        digest = _read_verifier(self.open_webui_password_file)
        if digest is None:
            raise AccountError("服务器 Open WebUI 入口密码暂不可用。", 503)
        reservations = self._throttle((
            ("open-webui:global", 5), (f"open-webui:source:{source}", 3),
        ), _CHALLENGE_WINDOW)
        with _hash_slot():
            try:
                _ARGON2.verify(digest, password)
            except (VerificationError, InvalidHashError):
                raise AccountError("Open WebUI 入口密码错误。", 403) from None
        self._refund_throttle(reservations)
        return digest

    def _open_webui_signature(self, token, verifier, expires_at):
        message = token_hash(token) + sha256(verifier.encode("ascii")).digest() + expires_at.to_bytes(8, "big")
        return hmac_digest(self._open_webui_grant_key, message, "sha256").hex()

    def grant_open_webui_access(self, token, password, source):
        self.session(token, admin=True)
        verified = self._verify_open_webui_password(password, source)
        with self.connect() as db:
            row, _ = self._session(db, token, admin=True)
            current = _read_verifier(self.open_webui_password_file)
            if current is None or not compare_digest(current, verified):
                raise AccountError("Open WebUI 入口密码已更改，请重试。", 403)
            expires_at = min(int(time()) + _OPEN_WEBUI_GRANT_TTL, row["expires_at"])
            signature = self._open_webui_signature(token, verified, expires_at)
            return {"grant": f"{expires_at}.{signature}", "expires_at": expires_at}

    def _require_open_webui_grant(self, db, token, grant):
        self._session(db, token, admin=True)
        match = _OPEN_WEBUI_GRANT_PATTERN.fullmatch(grant) if isinstance(grant, str) else None
        if match is None:
            raise AccountError("请先验证 Open WebUI 最高权限。", 403)
        expires_at, signature = match.groups()
        expires_at = int(expires_at)
        current = _read_verifier(self.open_webui_password_file)
        if (current is None or expires_at <= int(time())
                or not compare_digest(signature, self._open_webui_signature(token, current, expires_at))):
            raise AccountError("Open WebUI 最高权限已失效，请重新验证。", 403)

    def verify_open_webui_grant(self, token, grant):
        with self.connect() as db:
            self._require_open_webui_grant(db, token, grant)

    def _require_current_code(self, digest):
        current = read_admin_code(self.admin_code_file)
        if current is None or not compare_digest(current, digest):
            raise AccountError("管理员授权密码已更改，请重试。", 403)

    def register_admin(self, username, password, authorization_code, source):
        digest = self._verify_admin_code(authorization_code, source)
        return self._create(username, password, "admin", "active", admin_verifier=digest)

    def _create(self, username, password, role, status, *, first_administrator=False, admin_verifier=None):
        username = normalize_username(username)
        validate_password(password)
        with _hash_slot():
            hashed = _ARGON2.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if admin_verifier is not None:
                self._require_current_code(admin_verifier)
            if first_administrator and db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
                raise AccountError("管理员已初始化。", 409)
            try:
                result = db.execute(
                    "INSERT INTO users (username, username_key, password_hash, role, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, username.casefold(), hashed, role, status, int(time())),
                )
            except sqlite3.IntegrityError as error:
                raise AccountError("该用户名不可用。", 409) from error
            return {"id": result.lastrowid, "username": username, "role": role, "status": status}

    def login(self, username, password, source, *, role="user", authorization_code=None):
        if role not in ("admin", "user"):
            raise AccountError("无效的登录角色。")
        username = normalize_username(username)
        if not isinstance(password, str) or len(password) > 128:
            raise AccountError("用户名或密码错误。", 401)
        self.throttle("login", source, username)
        with self.connect() as db:
            user = db.execute(
                f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE username_key = ?",
                (username.casefold(),),
            ).fetchone()
        with _hash_slot():
            try:
                valid = _ARGON2.verify(user["password_hash"] if user else self.dummy_hash, password)
            except (VerificationError, InvalidHashError, UnicodeError):
                valid = False
        if not valid or not user:
            raise AccountError("用户名或密码错误。", 401)
        digest = None
        if role == "admin":
            self._require_assigned_admin(user["role"], user["status"])
            digest = self._verify_admin_code(authorization_code, source)
        now = int(time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            live = db.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = ? AND password_hash = ?",
                (user["id"], user["password_hash"]),
            ).fetchone()
            if live is None:
                raise AccountError("用户名或密码错误。", 401)
            if digest is not None:
                self._require_assigned_admin(live["role"], live["status"])
                self._require_current_code(digest)
            return self._issue(db, live, role, now + SESSION_TTL, digest)

    @staticmethod
    def _require_assigned_admin(assigned_role, status):
        if assigned_role != "admin" or status != "active":
            raise AccountError("当前账号没有管理员身份。", 403)

    def _public_user(self, db, row, role):
        return {"id": row["id"], "username": row["username"], "role": role,
                "assigned_role": row["role"], "status": row["status"],
                "quota": self.quotas.snapshot(db, row["id"])}

    def _issue(self, db, user, role, expires_at, digest):
        token = secrets.token_urlsafe(32)
        grant = _admin_fingerprint(digest) if digest is not None else None
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, admin_code_hash) VALUES (?, ?, ?, ?)",
            (token_hash(token), user["id"], expires_at, grant),
        )
        return {"token": token, "expires_at": expires_at, "user": self._public_user(db, user, role)}

    def switch_role(self, token, role, code, source):
        if role not in ("admin", "user"):
            raise AccountError("无效的目标角色。")
        current = self.session(token)
        self._require_assigned_admin(current["assigned_role"], current["status"])
        digest = self._verify_admin_code(code, source) if role == "admin" else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, _ = self._session(db, token)
            self._require_assigned_admin(row["role"], row["status"])
            if digest is not None:
                self._require_current_code(digest)
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
            return self._issue(db, row, role, row["expires_at"], digest if role == "admin" else None)

    def _session(self, db, token, *, active=False, admin=False):
        row = db.execute(
            "SELECT u.id, u.username, u.role, u.status, s.admin_code_hash, s.expires_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash(token), int(time())),
        ).fetchone()
        if row is None:
            raise AccountError("会话已失效，请重新登录。", 401)
        role = "user"
        if row["admin_code_hash"] is not None:
            current = read_admin_code(self.admin_code_file)
            if (row["role"] != "admin" or current is None
                    or not compare_digest(row["admin_code_hash"], _admin_fingerprint(current))):
                raise AccountError("管理员授权密码已更改，请重新登录。", 401)
            role = "admin"
        if (active or admin) and row["status"] != "active":
            raise AccountError("账号尚未激活或已停用。", 403)
        if admin and role != "admin":
            raise AccountError("请使用管理员角色执行此操作。", 403)
        return row, role

    def session(self, token, *, active=False, admin=False):
        with self.connect() as db:
            row, role = self._session(db, token, active=active, admin=admin)
            return self._public_user(db, row, role)

    def change_open_webui_password(self, token, grant, password):
        self.verify_open_webui_grant(token, grant)
        validate_password(password)
        with _hash_slot():
            digest = _ARGON2.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_open_webui_grant(db, token, grant)
            try:
                _write_verifier(self.open_webui_password_file, digest)
            except OSError:
                raise AccountError("无法保存服务器 Open WebUI 入口密码。", 503) from None

    def change_own_password(self, token, current, password, confirmation, source):
        with self.connect() as db:
            row, _ = self._session(db, token)
            user_id = row["id"]
            previous = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        validate_password(password)
        if confirmation != password:
            raise AccountError("两次输入的新登录密码不一致。")
        if not isinstance(current, str) or not 1 <= len(current) <= 128 or previous is None:
            raise AccountError("当前登录密码错误。", 403)
        self._throttle(((f"change-password:account:{user_id}", 8),
                        (f"change-password:source:{source}", 16),
                        ("change-password:global", 120)), 60)
        original_hash = previous["password_hash"]
        with _hash_slot():
            try:
                _ARGON2.verify(original_hash, current)
            except (VerificationError, InvalidHashError, UnicodeError):
                raise AccountError("当前登录密码错误。", 403) from None
            if current == password:
                raise AccountError("新登录密码须与当前密码不同。")
            new_hash = _ARGON2.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session(db, token)
            updated = db.execute("UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
                                 (new_hash, user_id, original_hash))
            if updated.rowcount != 1:
                raise AccountError("登录密码已更改，请重新登录。", 409)
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            db.execute("INSERT INTO self_password_audit (user_id, changed_at) VALUES (?, ?)",
                       (user_id, int(time())))

    def reset_password(self, token, target_id, password, code, source):
        self.session(token, admin=True)
        if not 1 <= target_id <= 2**63 - 1:
            raise AccountError("找不到该用户。", 404)
        validate_password(password)
        digest = self._verify_admin_code(code, source)
        with _hash_slot():
            password_hash = _ARGON2.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor, _ = self._session(db, token, admin=True)
            self._require_current_code(digest)
            updated = db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, target_id))
            if updated.rowcount != 1:
                raise AccountError("找不到该用户。", 404)
            db.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
            self._audit_credential(db, actor["id"], target_id, "password_reset")

    def change_authorization_code(self, token, current_code, new_code, source):
        self.session(token, admin=True)
        if not isinstance(new_code, str) or not _ADMIN_CODE.fullmatch(new_code):
            raise AccountError("管理员授权密码须为 6 位数字。")
        digest = self._verify_admin_code(current_code, source)
        if compare_digest(current_code, new_code):
            raise AccountError("请输入与当前不同的管理员授权密码。")
        with _hash_slot():
            updated = _ARGON2.hash(new_code)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor, _ = self._session(db, token, admin=True)
            self._require_current_code(digest)
            db.execute("DELETE FROM sessions WHERE admin_code_hash IS NOT NULL")
            self._audit_credential(db, actor["id"], None, "authorization_code_changed")
            try:
                _write_verifier(self.admin_code_file, updated)
            except OSError:
                raise AccountError("无法更新服务器授权密码文件，请检查服务账号的写入权限。", 503) from None

    @staticmethod
    def _audit_credential(db, actor_id, target_id, action):
        db.execute(
            "INSERT INTO credential_audit (actor_id, target_id, action, changed_at) VALUES (?, ?, ?, ?)",
            (actor_id, target_id, action, int(time())),
        )

    def logout(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def users(self):
        with self.connect() as db:
            return [dict(row, quota=self.quotas.snapshot(db, row["id"])) for row in db.execute(
                f"SELECT {_USER_COLUMNS}, created_at FROM users ORDER BY (status = 'pending') DESC, id DESC"
            )]

    def has_administrator(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone())

    def change_status(self, token, target_id, status):
        if not isinstance(status, str) or status not in TARGET_STATES:
            raise AccountError("无效的账号状态。")
        if not 1 <= target_id <= 2**63 - 1:
            raise AccountError("找不到该用户。", 404)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor, _ = self._session(db, token, admin=True)
            current = db.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (target_id,)).fetchone()
            if not current:
                raise AccountError("找不到该用户。", 404)
            if current["role"] != "user":
                raise AccountError("该接口只管理普通用户。", 409)
            if current["status"] != status:
                db.execute("UPDATE users SET status = ? WHERE id = ?", (status, target_id))
                db.execute(
                    "INSERT INTO user_status_audit (actor_id, target_id, previous_status, new_status, changed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor["id"], target_id, current["status"], status, int(time())),
                )
            return dict(current) | {"status": status}
