"""Validation and persistence helpers for Labcoding's output budget."""

DEFAULT_OUTPUT_LIMIT = 4096
MIN_OUTPUT_LIMIT = 128
MAX_OUTPUT_LIMIT = 8192


def load_output_limit(preferences):
    value = preferences.get("output_limit", DEFAULT_OUTPUT_LIMIT)
    return value if type(value) is int and MIN_OUTPUT_LIMIT <= value <= MAX_OUTPUT_LIMIT else DEFAULT_OUTPUT_LIMIT


def set_output_limit(preferences, value):
    if type(value) is not int or not MIN_OUTPUT_LIMIT <= value <= MAX_OUTPUT_LIMIT:
        raise ValueError("输出 Token 上限须为 128–8192 的整数。")
    preferences["output_limit"] = value
    return value


def load_account_output_limit(preferences, username):
    """Load a personal budget scoped to a CCSLab account, migrating legacy once."""
    accounts = preferences.get("output_limit_accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        preferences["output_limit_accounts"] = accounts
    key = str(username or "").strip().casefold()
    if key in accounts:
        value = accounts[key]
        return value if type(value) is int and MIN_OUTPUT_LIMIT <= value <= MAX_OUTPUT_LIMIT else DEFAULT_OUTPUT_LIMIT
    value = load_output_limit(preferences) if not accounts else DEFAULT_OUTPUT_LIMIT
    accounts[key] = value
    preferences["output_limit"] = value
    return value


def set_account_output_limit(preferences, username, value):
    set_output_limit(preferences, value)
    accounts = preferences.setdefault("output_limit_accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        preferences["output_limit_accounts"] = accounts
    accounts[str(username or "").strip().casefold()] = value
    return value
