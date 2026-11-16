"""Atomic reservations and actual-usage accounting; no prompts or credentials stored."""
import json
from time import time
import uuid
from .account_errors import AccountError, QuotaError


MAX_QUOTA = 10**12
EXHAUSTED = "用户额度已经用完，请充值。"


def usage(data):
    """Return confirmed input/output counts, never estimates or cached double counts."""
    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return None
    values = data["usage"]
    prompt, completion = values.get("prompt_tokens"), values.get("completion_tokens")
    if any(type(n) is not int or not 0 <= n <= MAX_QUOTA for n in (prompt, completion)):
        return None
    total = values.get("total_tokens", prompt + completion)
    if type(total) is not int or total != prompt + completion:
        return None
    return prompt, completion


class Quotas:
    def __init__(self, accounts):
        self.accounts = accounts

    def snapshot(self, db, user_id):
        row = db.execute("SELECT * FROM token_quotas WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return {"mode": "unlimited", "limit_tokens": None, "used_tokens": 0,
                    "remaining_tokens": None, "held_tokens": 0, "unconfirmed": False}
        pending = db.execute(
            "SELECT COALESCE(SUM(reserved_tokens), 0), MAX(state = 'unconfirmed') "
            "FROM token_requests WHERE user_id = ? AND generation = ? "
            "AND state IN ('reserved', 'unconfirmed')", (user_id, row["generation"]),
        ).fetchone()
        limit, held = row["limit_tokens"], pending[0]
        return {"mode": "unlimited" if limit is None else "limited", "limit_tokens": limit,
                "used_tokens": row["used_tokens"], "held_tokens": held,
                "remaining_tokens": None if limit is None else max(0, limit-row["used_tokens"]-held),
                "unconfirmed": bool(pending[1])}

    def set(self, token, user_id, mode, limit):
        if type(user_id) is not int or not 1 <= user_id <= 2**63-1:
            raise AccountError("找不到该用户。", 404)
        if (mode not in ("unlimited", "limited") or
                (mode == "unlimited" and limit is not None) or
                (mode == "limited" and (type(limit) is not int or not 0 <= limit <= MAX_QUOTA))):
            raise AccountError("额度须为不限额，或 0–1,000,000,000k 之间的 Token 数量。")
        with self.accounts.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actor, _ = self.accounts._session(db, token, admin=True)
            target = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise AccountError("找不到该用户。", 404)
            if target["role"] != "user":
                raise AccountError("只能为普通用户设置额度。", 409)
            db.execute(
                "INSERT INTO token_quotas(user_id, generation, limit_tokens) VALUES (?, 1, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET generation = generation+1, limit_tokens = ?, used_tokens = 0",
                (user_id, limit, limit),
            )
            generation = db.execute("SELECT generation FROM token_quotas WHERE user_id = ?", (user_id,)).fetchone()[0]
            db.execute("INSERT INTO token_quota_audit(actor_id,user_id,generation,limit_tokens,created_at) "
                       "VALUES(?,?,?,?,?)", (actor["id"], user_id, generation, limit, int(time())))
            return self.snapshot(db, user_id)

    def reserve(self, token, request_id, tokens):
        with self.accounts.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user, _ = self.accounts._session(db, token, active=True)
            db.execute("INSERT OR IGNORE INTO token_quotas(user_id) VALUES (?)", (user["id"],))
            quota = self.snapshot(db, user["id"])
            if quota["mode"] == "limited":
                if quota["unconfirmed"]:
                    raise QuotaError("有请求用量待核对，请联系管理员核对并重新设置额度。", "token_quota_unconfirmed", 409)
                if quota["remaining_tokens"] == 0:
                    raise QuotaError(EXHAUSTED, "token_quota_exhausted")
                if tokens is None:
                    raise QuotaError("额度设置已更新，请重试本次任务。", "token_quota_changed", 409)
                if type(tokens) is not int or tokens < 1:
                    raise AccountError("请求预留额无效。")
                if quota["remaining_tokens"] < tokens:
                    raise QuotaError("剩余额度不足以覆盖本次输入及输出预留，请充值或缩小任务。", "token_quota_insufficient")
            generation = db.execute("SELECT generation FROM token_quotas WHERE user_id = ?", (user["id"],)).fetchone()[0]
            identity = uuid.uuid4().hex  # Never trust a caller's request ID for ledger identity.
            db.execute("INSERT INTO token_requests VALUES (?,?,?,?,?,'reserved',NULL,NULL,?,NULL)",
                       (identity, request_id, user["id"], generation,
                        tokens if quota["mode"] == "limited" else 0, int(time())))
            return identity

    def finish(self, identity, confirmed, *, sent):
        with self.accounts.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM token_requests WHERE id = ?", (identity,)).fetchone()
            if row is None or row["state"] != "reserved":
                return
            state = "settled" if confirmed is not None else ("unconfirmed" if sent else "released")
            prompt, completion = confirmed if confirmed is not None else (None, None)
            db.execute("UPDATE token_requests SET state=?,prompt_tokens=?,completion_tokens=?,finished_at=? WHERE id=?",
                       (state, prompt, completion, int(time()), identity))
            if confirmed is not None:
                db.execute("UPDATE token_quotas SET used_tokens=used_tokens+? WHERE user_id=? AND generation=?",
                           (prompt+completion, row["user_id"], row["generation"]))


class UsageObserver:
    def __init__(self):
        self.confirmed = None
        self.conflict = False

    def body(self, raw):
        try:
            value = usage(json.loads(raw))
        except (ValueError, RecursionError):
            return
        if value is not None:
            if self.confirmed is not None and value != self.confirmed:
                self.conflict = True
            self.confirmed = value

    def frame(self, lines):
        raw = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
        if raw and raw != "[DONE]":
            self.body(raw)

    @property
    def result(self):
        return None if self.conflict else self.confirmed
