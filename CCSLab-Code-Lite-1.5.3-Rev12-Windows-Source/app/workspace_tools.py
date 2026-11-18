"""MCP bridge for OpenCode's Windows workspace operations."""

import codecs
import hmac
import json
import locale
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from windows_process import WindowsJob


MAX_FILE = 1024 * 1024
MAX_VISITED = 5000
HIDDEN_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", ".codex", ".opencode"}
PROTECTED_NAMES = {"id_rsa", "id_ed25519", ".npmrc", ".netrc", "credentials.json"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tool(name, description, required=(), **properties):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "required": list(required),
            "additionalProperties": False,
            "properties": {
                key: {"type": value} if isinstance(value, str) else value
                for key, value in properties.items()
            },
        },
    }


TOOLS = [
    tool("list", "List workspace paths recursively. Use a relative path or '.' for the authorized workspace.",
         path="string"),
    tool("search", "Search source files for case-insensitive literal text. Returns relative paths and line numbers.",
         ("query",), path="string", query="string"),
    tool("read", "Read text in complete-line pages. Defaults to 120 lines, up to 6000 UTF-8 bytes. "
         "Follow next_line to continue; oversized single lines require start_column and char_count.",
         ("path",), path="string", start_line="integer", line_count="integer",
         start_column="integer", char_count="integer"),
    tool("replace", "Replace one exact, unique text span in a file. Read the file first. Use LF in tool input.",
         ("path", "old_text", "new_text"), path="string", old_text="string", new_text="string"),
    tool("create", "Create one new UTF-8 file within the workspace. Existing files must use replace.",
         ("path", "text"), path="string", text="string"),
    tool("delete", "Request confirmation to delete one regular workspace file.",
         ("path",), path="string"),
    tool("run", "Request approval for a Windows executable and arguments, especially for running tests. "
         "Use python with args [\"-m\", \"unittest\", \"discover\", \"-v\"] for Python tests. "
         "No shell syntax. Returns exit code and combined output. Maximum timeout: 180 seconds.",
         ("program", "args"), program="string", args={"type": "array", "items": {"type": "string"}},
         path="string", timeout_seconds="integer"),
]


def _check_entry(path: Path, *, allow_missing: bool = False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError("不允许访问符号链接、目录联接或其他重解析点。")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError("不允许访问具有多个硬链接的文件。")
    return info


def _protected(name: str) -> bool:
    name = name.casefold()
    return (
        name in HIDDEN_DIRS or name in PROTECTED_NAMES or name == ".env"
        or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"})
        or name.endswith((".pem", ".pfx", ".p12", ".key"))
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bound_int(value, default, lo, hi):
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"数值必须在 {lo} 至 {hi} 之间。")
    return value


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or self.root.parent == self.root:
            raise ValueError("请选择具体的项目文件夹。")
        self.root_id = self.identity(self.root)
        self.operations = {"list": self.list_files, "search": self.search, "read": self.read,
                           "replace": self.replace, "create": self.create, "delete": self.delete,
                           "run": self.run}

    @staticmethod
    def identity(path):
        info = _check_entry(path)
        return info.st_dev, info.st_ino

    def path(self, relative, *, allow_missing=False):
        if not isinstance(relative, str) or not relative.strip() or len(relative) > 4096:
            raise ValueError("请提供工作区内的相对路径。")
        win = PureWindowsPath(relative)
        if win.anchor or ".." in win.parts:
            raise ValueError("路径必须位于已选项目内，且不能包含 ..。")
        if self.identity(self.root) != self.root_id or self.root.resolve(strict=True) != self.root:
            raise ValueError("项目目录在任务期间发生变化。请重新打开。")
        candidate = self.root
        for part in win.parts:
            if (part.endswith((" ", ".")) or _protected(part) or "~" in part
                    or re.search(r'[<>:"|?*\x00-\x1f]', part) or win.with_name(part).is_reserved()):
                raise ValueError(f"路径名称受保护或无效：{part}")
            candidate /= part
            _check_entry(candidate, allow_missing=allow_missing)
        if not _inside(candidate.resolve(strict=False), self.root):
            raise ValueError("该路径位于工作区外。")
        return candidate

    def walk(self, relative, stop):
        top = self.path(relative)
        if not top.is_dir():
            raise ValueError("请提供目录路径。")
        dirs = [top]
        count = 0
        while dirs and count < MAX_VISITED and not stop.is_set():
            directory = dirs.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if count >= MAX_VISITED or stop.is_set():
                        return
                    count += 1
                    if _protected(entry.name):
                        continue
                    path = Path(entry.path)
                    try:
                        info = _check_entry(path)
                    except (OSError, ValueError):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        dirs.append(path)
                        yield path, True
                    elif stat.S_ISREG(info.st_mode):
                        yield path, False

    def list_files(self, job, path="."):
        rows = []
        for filename, directory in self.walk(path, job.stop):
            rows.append(filename.relative_to(self.root).as_posix() + ("/" if directory else ""))
            if len(rows) >= 500:
                return "\n".join(rows)[:18_000] + "\n[其余结果已截断，可指定子目录。]"
        return "\n".join(rows)[:18_000] or "目录没有可读取的文件。"

    def search(self, job, query, path="."):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("查询必须包含 1 至 256 个字符。")
        rows = []
        query = query.casefold()
        for filename, directory in self.walk(path, job.stop):
            if directory:
                continue
            try:
                data, _, _ = self.read_text(self.path(filename.relative_to(self.root).as_posix()))
            except (OSError, UnicodeError, ValueError):
                continue
            for index, text in enumerate(data.splitlines(), 1):
                if query in text.casefold():
                    rows.append(f"{filename.relative_to(self.root).as_posix()}:{index}: {text[:320]}")
                    if len(rows) >= 50:
                        return "\n".join(rows)[:18_000] + "\n[其余结果已截断。]"
        return "\n".join(rows)[:18_000] or "在已扫描文件中未找到匹配。"

    @staticmethod
    def read_text(path: Path):
        info = _check_entry(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ValueError("仅支持不超过 1 MiB 的普通文本文件。")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("文件超过 1 MiB。")
        codec = "utf-8"
        if data.startswith(codecs.BOM_UTF8):
            codec = "utf-8-sig"
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            codec = "utf-16-le" if data.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
        text = data.decode(codec)
        if "\0" in text:
            raise ValueError("文件包含二进制内容。")
        if codec.startswith("utf-16"):
            text = text.removeprefix("\ufeff")
        return text.replace("\r\n", "\n"), codec, "\r\n" if "\r\n" in text else "\n"

    def read(self, job, path, start_line=None, line_count=None, start_column=None, char_count=None):
        start = _bound_int(start_line, 1, 1, MAX_FILE)
        count = _bound_int(line_count, 120, 1, 500)
        text, _, _ = self.read_text(self.path(path))
        text = text.lstrip("\ufeff")
        lines = text.splitlines()
        if start_column is not None or char_count is not None:
            column = _bound_int(start_column, 1, 1, MAX_FILE)
            width = _bound_int(char_count, 1000, 1, 1000)
            line = lines[start - 1] if start <= len(lines) else ""
            fragment = line[column - 1:column - 1 + width]
            return (f"{path} · 第 {start} 行，列 {column} 起，共 {len(line)} 字符\n{fragment}\n"
                    f"[显式片段；next_column={column + len(fragment)}；完整文件未更改。]")
        rows, size = [], 0
        next_line = start
        for i, line in enumerate(lines[start - 1:start + count - 1], start):
            row = f"{i}: {line}"
            needed = len(row.encode("utf-8")) + 1
            if size + needed > 6000:
                if not rows:
                    return (f"{path} · {len(lines)} 行\n[第 {i} 行超过页面预算，未返回部分代码。"
                            "请用 start_column 和 char_count 显式读取该行片段。]")
                break
            rows.append(row)
            size += needed
            next_line = i + 1
        more = f"[分页；next_line={next_line}；其余内容尚未读取。]" if next_line <= len(lines) else "[文件结束]"
        return f"{path} · {len(lines)} 行\n" + "\n".join(rows) + "\n" + more

    @staticmethod
    def approve(job, **detail):
        if not callable(getattr(job, "approve", None)):
            raise ValueError("缺少操作审批回调，已拒绝执行。")
        if job.stop.is_set() or not job.approve(detail):
            raise ValueError("用户未批准此操作。")
        if job.stop.is_set():
            raise ValueError("任务已停止。")
        if job.authorize:
            try:
                job.authorize()
            except RuntimeError:
                raise ValueError("无法再次确认账号权限，本次操作被拒绝。") from None

    def replace(self, job, path, old_text, new_text):
        filename = self.path(path)
        text, codec, newline = self.read_text(filename)
        if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
            raise ValueError("请提供待替换的准确非空内容及替换文本。")
        old_text = old_text.replace("\r\n", "\n")
        new_text = new_text.replace("\r\n", "\n")
        if text.count(old_text) != 1:
            raise ValueError("待替换内容没有唯一匹配。请重新读取文件并提供准确上下文。")
        self.approve(job, kind="write", title="替换文件内容", path=path,
                     old_text=old_text, new_text=new_text,
                     detail=f"{path}\n\n原文本\n{old_text}\n\n替换文本\n{new_text}")
        if job.stop.is_set():
            raise ValueError("任务已停止。")
        filename = self.path(path)
        if self.read_text(filename) != (text, codec, newline):
            raise ValueError("文件在操作期间发生变化。请重新读取。")
        changed = text.replace(old_text, new_text, 1).replace("\n", newline)
        data = changed.encode(codec)
        if codec == "utf-16-le":
            data = codecs.BOM_UTF16_LE + data
        elif codec == "utf-16-be":
            data = codecs.BOM_UTF16_BE + data
        if len(data) > MAX_FILE:
            raise ValueError("修改后文件超过 1 MiB。")
        fd, temp = tempfile.mkstemp(prefix=".ccslab-", dir=filename.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            shutil.copymode(filename, temp)
            os.replace(temp, filename)
        finally:
            Path(temp).unlink(missing_ok=True)
        return f"已修改 {path}"

    def create(self, job, path, text):
        data = text.encode("utf-8")
        if len(data) > MAX_FILE:
            raise ValueError("新文件超过 1 MiB。")
        filename = self.path(path, allow_missing=True)
        if filename.exists():
            raise ValueError("文件已经存在。请先读取文件并使用 replace。")
        if job.strict:
            self.approve(job, kind="write", title="创建文件", path=path, detail=f"{path}\n\n{text}")
        if job.stop.is_set():
            raise ValueError("任务已停止。")
        filename = self.path(path, allow_missing=True)
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename = self.path(path, allow_missing=True)
        with filename.open("xb") as out:
            out.write(data)
        return f"已创建 {path}"

    def delete(self, job, path):
        filename = self.path(path)
        item = filename.stat()
        if not stat.S_ISREG(item.st_mode):
            raise ValueError("只能删除单个普通文件。")
        self.approve(job, kind="delete", title="删除文件", path=path,
                     detail=f"删除 {filename}。此操作不会经过回收站。")
        filename = self.path(path)
        current = filename.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (item.st_ino, item.st_size, item.st_mtime_ns):
            raise ValueError("文件在操作期间发生变化。请重新读取。")
        filename.unlink()
        return f"已删除 {path}"

    def run(self, job, program, args, path=".", timeout_seconds=None):
        timeout = _bound_int(timeout_seconds, 120, 1, 180)
        cwd = self.path(path)
        if not cwd.is_dir():
            raise ValueError("命令工作目录必须是项目内的目录。")
        if not isinstance(program, str) or not re.fullmatch(r"[\w.-]{1,64}", program):
            raise ValueError("请提供已安装程序的名称。")
        if not isinstance(args, list) or len(args) > 60 or not all(
            isinstance(arg, str) and len(arg) <= 500 and not any(c in arg for c in "\0\r\n") for arg in args
        ):
            raise ValueError("请为命令提供有效的独立参数列表。")
        for arg in args:
            for value in (arg, arg.partition("=")[2]):
                if not value:
                    continue
                windows_path = PureWindowsPath(value)
                if windows_path.anchor or ".." in windows_path.parts:
                    raise ValueError("命令参数不能直接引用项目外路径。")
        stem = Path(program).stem.casefold()
        if stem == "git" and (not args or args[0] not in {"status", "diff", "log", "branch", "rev-parse", "ls-files"}):
            raise ValueError("Agent 的命令工具仅开放 Git 查询。")
        executable_name = program if program.lower().endswith(".exe") else program + ".exe"
        candidates = (Path(folder) / executable_name for folder in os.get_exec_path() if Path(folder).is_absolute())
        exe = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None) if os.name == "nt" else shutil.which(program)
        if not exe:
            raise ValueError(f"在 Windows PATH 中未找到可直接运行的 {program} 可执行文件。")
        exact = [os.path.abspath(exe), *args]
        command = subprocess.list2cmdline(exact)
        binary = Path(exact[0])
        binary_info = binary.stat()
        execution_context = {
            "executable": os.path.realpath(binary),
            "arguments": list(args),
            "working_directory": os.path.normcase(os.path.realpath(cwd)),
            "timeout_seconds": timeout,
            "file_size": binary_info.st_size,
            "mtime_ns": binary_info.st_mtime_ns,
            "file_device": binary_info.st_dev,
            "file_index": binary_info.st_ino,
        }
        self.approve(job, kind="command", title="运行 Windows 命令", command=command, path=str(cwd),
                     project_root=str(self.root), execution_context=execution_context,
                     detail=f"工作目录：{cwd}\n\n{command}\n\n此命令和它启动的脚本拥有当前 Windows "
                            "用户的权限，可能读写项目外文件、启动子进程或访问网络。请核对命令和脚本后批准。")
        current_binary = binary.stat()
        if (current_binary.st_dev, current_binary.st_ino, current_binary.st_size,
                current_binary.st_mtime_ns) != (binary_info.st_dev, binary_info.st_ino,
                                                 binary_info.st_size, binary_info.st_mtime_ns):
            raise ValueError("命令可执行文件在审批期间发生变化，已拒绝运行。")
        return _run_command(job, exact, self.path(path), timeout)


def _end_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()


def _run_command(job, command, cwd, timeout):
    allowed_vars = {"systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp",
                    "programfiles", "programfiles(x86)", "userprofile", "appdata", "localappdata"}
    env = {key: value for key, value in os.environ.items() if key.casefold() in allowed_vars}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # CPython can read an older .pyc after same-sized edits within a single second.
        "PYTHONPYCACHEPREFIX": str(Path(tempfile.gettempdir()) / ("ccslab-python-" + secrets.token_hex(16))),
        "GIT_TERMINAL_PROMPT": "0",
    })
    process_job = WindowsJob() if os.name == "nt" else None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        if process_job is not None:
            try:
                process_job.add(process.pid)
            except OSError:
                if process.poll() is None:
                    _end_process(process)
                    raise
        return _read_output(job, process, process_job, timeout)
    finally:
        if process_job is not None:
            process_job.close()


def _read_output(job, process, process_job, timeout):
    output = bytearray()
    truncated = False

    def consume():
        nonlocal truncated
        try:
            with process.stdout:
                for chunk in iter(lambda: process.stdout.read(4096), b""):
                    output.extend(chunk)
                    if len(output) > 24000:
                        truncated = True
                        del output[:-24000]
        except OSError:
            pass

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    summary = ""
    while process.poll() is None:
        if job.stop.wait(0.15):
            summary = "命令已取消。"
            break
        if time.monotonic() >= deadline:
            summary = f"命令超过 {timeout} 秒，已终止。"
            break
    if process_job is not None:
        process_job.close()
    elif process.poll() is None:
        _end_process(process)
    process.wait(timeout=10)
    reader.join(timeout=2)
    data = bytes(output)
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False)
        decoded = data.decode(encoding, errors="replace")
    return (
        f"退出码：{process.returncode}\n{summary}\n"
        + ("[仅保留末尾输出]\n" if truncated else "")
        + decoded[-16000:]
    ).strip()


class ToolBridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, workspace, get_job):
        self.workspace = workspace
        self.get_job = get_job
        self.secret = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _MCPHandler)
        self.worker = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/mcp"

    def close(self):
        self.shutdown()
        self.server_close()


class _MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def authenticated(self):
        return self.path == "/mcp" and hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.secret}"
        )

    def do_GET(self):
        self.send_error(405 if self.authenticated() else 403)

    def do_POST(self):
        if not self.authenticated():
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 2 * MAX_FILE:
                self.send_error(413)
                return
            request = json.loads(self.rfile.read(size))
            method = request["method"]
        except (ValueError, TypeError, KeyError):
            self.send_error(400)
            return
        if "id" not in request:
            self.send_response(202)
            self.end_headers()
            return
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = self.dispatch(method, request.get("params") or {})
        except (ValueError, KeyError, TypeError):
            response["error"] = {"code": -32602, "message": "Invalid MCP request"}
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass

    def dispatch(self, method, params):
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ccslab-workspace", "version": "1.0.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method != "tools/call":
            raise ValueError("Unsupported MCP method")
        job = self.server.get_job()
        try:
            if job is None or job.stop.is_set():
                raise ValueError("任务未运行或已停止。")
            name = params["name"]
            operation = self.server.workspace.operations[name]
            if job.read_only and name not in {"list", "read", "search"}:
                raise ValueError("方案模式只允许读取和搜索项目。")
            using_write_lock = name in {"create", "replace", "delete", "run"}
            if using_write_lock:
                with job.mutation_lock:
                    if job.stop.is_set():
                        raise ValueError("任务已停止。")
                    if job.authorize:
                        try:
                            job.authorize()
                        except RuntimeError:
                            raise ValueError("账号未激活或账号服务不可达，本次写入或命令被拒绝。") from None
                    output = operation(job, **params.get("arguments", {}))
            else:
                output = operation(job, **params.get("arguments", {}))
            return {"content": [{"type": "text", "text": output}]}
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"操作失败：{exc}"}]}
