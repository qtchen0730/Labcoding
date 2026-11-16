"""Strict, payload-independent classification of llama.cpp context errors."""
import json
import re


def context_error(body):
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    known = {"context_length_exceeded", "exceed_context_size_error", "context_window_exceeded"}
    code = error.get("code")
    kind = error.get("type")
    message = error.get("message", "")
    if not isinstance(message, str):
        return None
    match = re.fullmatch(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)(?:, try increasing it)?", message, re.I)
    if not match:
        match = re.fullmatch(r"input \((\d+) tokens\) is larger than the max context size \((\d+) tokens\)\. skipping", message, re.I)
    if not (isinstance(code, str) and code in known) and not (isinstance(kind, str) and kind in known) and match is None:
        # Legacy variants accepted only as a complete diagnostic, never a search
        # through an error that might quote user input or a schema field.
        if message.lower().strip().rstrip(".") not in {"context length exceeded", "prompt is too long"}:
            return None
    result = {"code": "context_length_exceeded", "input_tokens": None, "context_limit": None}
    if match:
        values = [int(x) for x in match.groups()]
        if all(0 < x <= 2_147_483_647 for x in values):
            result.update(input_tokens=values[0], context_limit=values[1])
    return result
