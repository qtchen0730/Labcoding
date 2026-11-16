"""Validate public-model tool calling against a disposable read-only workspace."""

from pathlib import Path
import tempfile

from engine import Engine, EngineError


def check_read_tool(api_base, token, ca_bundle):
    with tempfile.TemporaryDirectory(prefix="ccslab-model-test-", ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        project = root / "project"
        project.mkdir()
        marker = "CCSLAB_TOOL_CHECK_271"
        (project / "connection-check.txt").write_text(marker + "\n", encoding="ascii")
        engine = Engine(root / "engine", api_base, token, ca_bundle=ca_bundle)
        read_ok = False
        reply = []

        def event(item):
            nonlocal read_ok
            if (item.get("type") == "tool" and item.get("name") == "workspace_read"
                    and item.get("status") == "completed" and marker in str(item.get("output") or "")):
                read_ok = True
            if item.get("type") == "delta" and isinstance(item.get("text"), str):
                reply.append(item["text"])

        try:
            engine.open_project(str(project))
            session = engine.new_session()
            engine.run(session,
                       "请实际调用 workspace_read 读取 connection-check.txt，然后回答文件中读到的内容。",
                       event, lambda _request: False, read_only=True)
        finally:
            engine.close()
        if not read_ok:
            raise EngineError("已连接模型，但没有收到成功的工作区读取调用。请检查服务的工具调用支持。")
        if not "".join(reply).strip():
            raise EngineError("模型成功读取测试文件，但没有生成最终回复。")
