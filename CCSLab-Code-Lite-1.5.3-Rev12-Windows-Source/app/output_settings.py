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
