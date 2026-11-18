"""Narrow, in-memory session authorization for workspace file writes."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from threading import Lock


def session_scope(account, project, session):
    """Bind a grant to the active account, canonical project, and session."""
    account_id = account.get("id") or account.get("username") if isinstance(account, dict) else None
    if not account_id or not project or not session:
        return None
    return str(account_id), os.path.normcase(os.path.realpath(project)), str(session)


def replacement_details_complete(request):
    path, old, new = (request.get(key) for key in ("path", "old_text", "new_text"))
    return (isinstance(path, str) and bool(path) and isinstance(old, str) and bool(old)
            and isinstance(new, str)
            and request.get("detail") == f"{path}\n\n原文本\n{old}\n\n替换文本\n{new}")


def grantable(request):
    """Exact file writes or a narrowly safe query command may receive grants."""
    if (isinstance(request, dict) and request.get("kind") == "write"
            and request.get("title") in {"创建文件", "替换文件内容"}
            and isinstance(request.get("path"), str) and bool(request["path"])
            and isinstance(request.get("detail"), str) and bool(request["detail"])):
        return request["title"] != "替换文件内容" or replacement_details_complete(request)
    return isinstance(request, dict) and request.get("kind") == "command" and _safe_query_command(request)


def _safe_query_command(request):
    """Allow only where.exe queries for one bare executable filename."""
    context = request.get("execution_context")
    if (set(request) != {"kind", "title", "command", "path", "project_root", "execution_context", "detail"}
            or request.get("title") != "运行 Windows 命令" or not isinstance(context, dict)
            or set(context) != {"executable", "arguments", "working_directory", "timeout_seconds",
                                "file_size", "mtime_ns", "file_device", "file_index"}):
        return False
    executable = context.get("executable")
    arguments = context.get("arguments")
    cwd = context.get("working_directory")
    project = request.get("project_root")
    command = request.get("command")
    timeout = context.get("timeout_seconds")
    if (not isinstance(executable, str) or not isinstance(arguments, list) or len(arguments) != 1
            or not isinstance(cwd, str) or not isinstance(request.get("path"), str)
            or not isinstance(project, str) or not isinstance(command, str)
            or not isinstance(timeout, int) or isinstance(timeout, bool)
            or timeout < 1 or timeout > 180
            or not all(isinstance(context.get(name), int) and not isinstance(context[name], bool)
                       and context[name] >= 0
                       for name in ("file_size", "mtime_ns", "file_device", "file_index"))):
        return False
    target = arguments[0]
    if not isinstance(target, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,56}\.exe", target, re.IGNORECASE):
        return False
    normalize = lambda value: os.path.normcase(os.path.realpath(value))
    if normalize(cwd) != normalize(request["path"]):
        return False
    system_where = normalize(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe")
    if normalize(executable) != system_where:
        return False
    if command.casefold() != subprocess.list2cmdline([executable, target]).casefold():
        return False
    try:
        return os.path.commonpath((normalize(project), normalize(cwd))) == normalize(project)
    except (OSError, ValueError):
        return False


def signature(request):
    """Hash every request field so near matches never inherit authorization."""
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SessionGrants:
    def __init__(self):
        self._grants = set()
        self._lock = Lock()

    def allow(self, scope, request):
        if scope and self.allowable(request, scope):
            with self._lock:
                self._grants.add((scope, signature(request)))
            return True
        return False

    @staticmethod
    def allowable(request, scope=None):
        if not scope or len(scope) != 3 or not grantable(request):
            return False
        if request.get("kind") != "command":
            return True
        context = request["execution_context"]
        project = os.path.normcase(os.path.realpath(request["project_root"]))
        workdir = os.path.normcase(os.path.realpath(context["working_directory"]))
        try:
            return project == scope[1] and os.path.commonpath((project, workdir)) == project
        except (OSError, ValueError):
            return False

    def allows(self, scope, request):
        if not scope or not self.allowable(request, scope):
            return False
        with self._lock:
            return (scope, signature(request)) in self._grants

    def clear(self):
        with self._lock:
            self._grants.clear()

    def has_scope(self, scope):
        with self._lock:
            return any(grant_scope == scope for grant_scope, _ in self._grants)
