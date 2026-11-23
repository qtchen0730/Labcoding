"""Read-only project changes, with no repository-provided command execution."""

from dataclasses import dataclass
from pathlib import Path
import codecs
import difflib
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile


LIMIT = 180_000
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


@dataclass
class Change:
    path: str
    status: str
    previous: str = ""


def plain_path(path):
    info = path.lstat()
    return not (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


class Changes:
    def __init__(self, directory):
        self.root = Path(directory).resolve(strict=True)
        search_path = os.pathsep.join(
            item for item in os.get_exec_path() if os.path.isabs(item)
            and not Path(item).is_relative_to(self.root)
        )
        self.git = shutil.which("git", path=search_path)
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
        self.repo = False
        self.baseline = {}
        self.warning = ""
        if self.git:
            code, output = self._git("rev-parse", "--show-toplevel")
            self.repo = code == 0 and Path(output.strip()).resolve() == self.root
            if self.repo:
                self._check_config()
        if not self.repo:
            self.baseline = self._snapshot()

    def _git(self, *args):
        with tempfile.TemporaryFile() as result:
            run_kwargs = dict(env=self.env, stdin=subprocess.DEVNULL, stdout=result,
                             stderr=result, timeout=15)
            if os.name == "nt":
                run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.run(
                [self.git, "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
                 "-c", "core.untrackedCache=false", "-c", "core.quotePath=false",
                 "-c", "core.hooksPath=NUL", "-C", str(self.root), *args],
                **run_kwargs,
            )
            result.seek(0)
            raw = result.read(LIMIT + 1)
        output = raw[:LIMIT].decode("utf-8", errors="replace")
        if len(raw) > LIMIT:
            output += "\n… 输出超出显示上限\n"
        return process.returncode, output

    def _check_config(self):
        code, config = self._git("config", "--local", "--no-includes", "--null", "--name-only", "--list")
        risky = ("filter.", "include.", "includeif.", "extensions.worktreeconfig")
        if code or any(key.lower().startswith(risky) for key in config.split("\0")):
            self.repo = False
            self.warning = "此仓库使用自定义 Git 配置，改用本地文件变更对比。"

    def _file(self, relative):
        leaf = Path(relative)
        if leaf.anchor or ".." in leaf.parts:
            raise ValueError("文件路径不在项目内")
        path = self.root
        for part in leaf.parts:
            if ":" in part or part.rstrip(" .") != part:
                raise ValueError("文件路径不在项目内")
            path = path / part
            if not plain_path(path):
                raise ValueError("已跳过链接或重解析点")
        return path

    def _snapshot(self):
        files = {}
        text_budget = 12_000_000
        for base, directories, names in os.walk(self.root, followlinks=False):
            base = Path(base)
            directories[:] = [name for name in directories if name.lower() not in SKIP
                              and plain_path(base / name)]
            for name in names:
                path = base / name
                try:
                    if not plain_path(path) or not path.is_file():
                        continue
                    info = path.stat()
                    blob = None
                    if info.st_size <= LIMIT:
                        with path.open("rb") as stream:
                            raw = stream.read(LIMIT + 1)
                        signature = hashlib.sha256(raw).digest()
                        if len(raw) <= text_budget:
                            blob = raw
                            text_budget -= len(raw)
                    else:
                        signature = (info.st_mtime_ns, info.st_size)
                    files[path.relative_to(self.root).as_posix()] = (signature, blob)
                    if len(files) >= 20_000:
                        self.warning = "变更扫描仅覆盖前 20,000 个文件。"
                        return files
                except OSError:
                    continue
        return files

    def status(self):
        if self.repo:
            self._check_config()
            if not self.repo:
                self.baseline = self._snapshot()
        if not self.repo:
            current = self._snapshot()
            changes = []
            for path in sorted(self.baseline.keys() | current.keys()):
                if path not in self.baseline:
                    changes.append(Change(path, "新增"))
                elif path not in current:
                    changes.append(Change(path, "删除"))
                elif self.baseline[path][0] != current[path][0]:
                    changes.append(Change(path, "修改"))
            return "本地文件夹", changes
        code, output = self._git("status", "--porcelain=v1", "-z",
                                 "--untracked-files=all", "--ignore-submodules=all")
        if code:
            raise RuntimeError(output.strip())
        records = iter(output.split("\0")[:-1])
        changes = []
        for record in records:
            status = record[:2]
            previous = next(records, "") if "R" in status or "C" in status else ""
            changes.append(Change(record[3:], status, previous))
        code, branch = self._git("symbolic-ref", "--short", "-q", "HEAD")
        if not branch.strip():
            _, head = self._git("rev-parse", "--short", "HEAD")
            branch = f"游离 HEAD · {head.strip()}"
        return branch.strip(), changes

    def _read(self, relative):
        path = self._file(relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            return None
        with path.open("rb") as stream:
            content = stream.read(LIMIT + 1)
        return content if len(content) <= LIMIT else None

    def diff(self, change):
        if self.repo and change.status != "??":
            self._check_config()
            if not self.repo:
                return self.warning
            paths = [change.path, change.previous] if change.previous else [change.path]
            flags = ("--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all")
            chunks = []
            for title, staged in (("已暂存", ("--cached",)), ("未暂存", ())):
                _, value = self._git("diff", *flags, *staged, "--", *paths)
                if value:
                    chunks.append(f"〔{title}〕\n{value}")
            return "\n".join(chunks) or "此文件没有可显示的文本差异。"
        before = self.baseline.get(change.path, (None, b""))[1]
        after = b"" if change.status == "删除" else self._read(change.path)
        try:
            if before is None or after is None:
                raise ValueError
            if b"\0" in before or b"\0" in after:
                raise ValueError
            left = codecs.decode(before, "utf-8-sig").splitlines(keepends=True)
            right = codecs.decode(after, "utf-8-sig").splitlines(keepends=True)
        except ValueError:
            return "无法生成文本预览。文件可能为二进制、采用其他编码或超出预览大小。"
        lines = difflib.unified_diff(left, right, f"a/{change.path}", f"b/{change.path}")
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)[:LIMIT] or "空文件"
