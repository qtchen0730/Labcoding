"""Validate quota snapshots and format the ordinary user's balance."""
from decimal import Decimal, InvalidOperation

EXHAUSTED = "用户额度已经用完，请充值。"


def snapshot(user):
    value = user.get("quota") if isinstance(user, dict) else None
    if not isinstance(value, dict):
        return None
    if value.get("mode") == "unlimited":
        return value
    if value.get("mode") == "limited":
        limit, remaining = value.get("limit_tokens"), value.get("remaining_tokens")
        if (type(limit) is int and type(remaining) is int and
                0 <= remaining <= limit <= 10**12):
            return value
    return None


def balance_text(user, *, online=True):
    value = snapshot(user)
    if value is None:
        return "额度不可用"
    if not online:
        return "额度待刷新"
    if value["mode"] == "unlimited":
        return "额度：不限额"
    if value.get("unconfirmed"):
        return "额度：用量待核对"
    limit, remaining = value["limit_tokens"], value["remaining_tokens"]
    percent = 100 * remaining / limit if limit else 0
    formatted = "0" if remaining == 0 else ("<0.1" if percent < 0.1 else f"{percent:.1f}".removesuffix(".0"))
    suffix = " · 含请求预留" if value.get("held_tokens", 0) else ""
    return f"剩余额度 {formatted}%{suffix}"


def blocked_message(user):
    value = snapshot(user)
    if value and value["mode"] == "limited":
        if value.get("unconfirmed"):
            return "请求用量待核对，请联系管理员。"
        if value["remaining_tokens"] == 0:
            return "请求额度已预留，等待结算。" if value.get("held_tokens", 0) else EXHAUSTED
    return ""


def parse_k(text):
    try:
        value = Decimal(text.strip()) * 1000
        if not value.is_finite() or value < 0 or value > 10**12 or value != value.to_integral_value():
            raise ValueError
        return int(value)
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError("请输入 0–1,000,000,000k 的数量，最多三位小数；1k = 1000 Token。") from None
