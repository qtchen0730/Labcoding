"""Narrow, in-memory session authorization for workspace file writes."""

import hashlib
import json
import os
from threading import Lock


def session_scope(account, project, session):
    """Bind a grant to the active account, canonical project, and session."""
    account_id = account.get("id") or account.get("username") if isinstance(account, dict) else None
    if not account_id or not project or not session:
        return None
    return str(account_id), os.path.normcase(os.path.realpath(project)), str(session)


def grantable(request):
    """Only exact single-file creates/replacements can receive session grants."""
    return (isinstance(request, dict) and request.get("kind") == "write"
            and request.get("title") in {"创建文件", "替换文件内容"}
            and isinstance(request.get("path"), str) and bool(request["path"])
            and isinstance(request.get("detail"), str) and bool(request["detail"]))


def signature(request):
    """Hash every request field so near matches never inherit authorization."""
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SessionGrants:
    def __init__(self):
        self._grants = set()
        self._lock = Lock()

    def allow(self, scope, request):
        if scope and grantable(request):
            with self._lock:
                self._grants.add((scope, signature(request)))
            return True
        return False

    @staticmethod
    def allowable(request):
        return grantable(request)

    def allows(self, scope, request):
        if not scope or not grantable(request):
            return False
        with self._lock:
            return (scope, signature(request)) in self._grants

    def clear(self):
        with self._lock:
            self._grants.clear()

    def has_scope(self, scope):
        with self._lock:
            return any(grant_scope == scope for grant_scope, _ in self._grants)
