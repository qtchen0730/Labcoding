"""HTTPS account API and a revocable OpenAI-compatible inference gateway."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import json
import uuid
from .context_errors import context_error
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from anyio import CancelScope
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .accounts import AccountError, Accounts
from .model_control import ModelControl, ModelControlError
from .quotas import UsageObserver
from .account_errors import QuotaError


_MODEL_FIELDS = frozenset({
    "messages", "model", "max_tokens", "max_completion_tokens", "stream", "stream_options",
    "temperature", "top_p", "stop", "seed", "presence_penalty", "frequency_penalty",
    "logit_bias", "logprobs", "top_logprobs", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "reasoning_effort",
})
_AUTH_BODY_LIMIT = 16384
_MODEL_BODY_LIMIT = 6 * 1024 * 1024
_MODEL_RESPONSE_LIMIT = 8 * 1024 * 1024
_OPEN_WEBUI_GRANT_HEADER = "X-CCSLab-WebUI-Grant"


@dataclass(frozen=True, repr=False)
class Settings:
    database: Path
    llama_url: str
    model_id: str
    upstream_token: str
    control_spec: Path | None = None
    output_token_limit: int = 4096
    runtime_unit: str | None = None
    gpu_index: int = 0
    admin_registration_code_file: Path | None = None

    @classmethod
    def from_environment(cls):
        database = Path(os.environ.get("CCSLAB_DATABASE", "/var/lib/ccslab-code/accounts.sqlite3"))
        url = os.environ.get("CCSLAB_LLAMA_URL", "http://172.17.0.1:8000").rstrip("/")
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/v1")):
            raise ValueError("CCSLAB_LLAMA_URL 须为受信任的 llama.cpp HTTP(S) 地址。")
        url = url.removesuffix("/v1")
        file = os.environ.get("CCSLAB_LLAMA_API_KEY_FILE")
        if not file:
            raise ValueError("请设置 CCSLAB_LLAMA_API_KEY_FILE。")
        token = Path(file).read_text(encoding="utf-8").strip()
        if not token or len(token) > 8192 or not all(33 <= ord(ch) <= 126 for ch in token):
            raise ValueError("llama.cpp API Key 文件内容无效。")
        model_id = os.environ.get("CCSLAB_MODEL_ID", "lab-general")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", model_id):
            raise ValueError("CCSLAB_MODEL_ID 无效。")
        output_limit = int(os.environ.get("CCSLAB_OUTPUT_TOKEN_LIMIT", "4096"))
        if not 128 <= output_limit <= 8192:
            raise ValueError("CCSLAB_OUTPUT_TOKEN_LIMIT 必须为 128–8192。")
        control = os.environ.get("CCSLAB_MODEL_CONTROL_SPEC")
        unit = os.environ.get("CCSLAB_MODEL_RUNTIME_UNIT") or None
        if unit and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,96}\.service", unit):
            raise ValueError("CCSLAB_MODEL_RUNTIME_UNIT 须为 systemd 服务单元名称。")
        gpu = int(os.environ.get("CCSLAB_MODEL_GPU_INDEX", "0"))
        if not 0 <= gpu <= 127:
            raise ValueError("CCSLAB_MODEL_GPU_INDEX 无效。")
        admin_code_file = os.environ.get("CCSLAB_ADMIN_REGISTRATION_CODE_FILE")
        return cls(
            database, url, model_id, token, Path(control) if control else None, output_limit, unit, gpu,
            Path(admin_code_file) if admin_code_file else None,
        )


class HTTPSOnly:
    def __init__(self, app, *, allow_loopback_http=False):
        self.app = app
        self.allow_loopback_http = allow_loopback_http

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        peer = scope.get("client")
        loopback_http = (self.allow_loopback_http and scope.get("scheme") == "http"
                         and peer and peer[0] == "127.0.0.1")
        if scope.get("scheme") != "https" and not loopback_http:
            return await JSONResponse({"detail": "此服务仅接受可信 HTTPS 连接。"}, 400)(scope, receive, send)

        async def protect(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", ()) if k != b"cache-control"]
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")])
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, protect)


def _token(request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AccountError("请先登录。", 401)
    return token


async def _json_input(request, limit=_AUTH_BODY_LIMIT):
    if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 JSON 请求。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "请求超过大小上限。")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise HTTPException(400, "请求不是有效的 JSON。") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求必须是 JSON 对象。")
    return payload


def _only(payload, *fields):
    if set(payload) != set(fields):
        raise HTTPException(400, "请求参数不完整或包含未支持的字段。")
    return [payload[key] for key in fields]


def _chat_input(payload, settings):
    if set(payload) - _MODEL_FIELDS:
        raise HTTPException(400, "模型请求包含未支持的参数。")
    if payload.get("model", settings.model_id) != settings.model_id:
        raise HTTPException(404, "模型不可用。")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 512:
        raise HTTPException(400, "消息列表无效。")
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise HTTPException(400, "消息角色无效。")
        content = msg.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text"
                   or not isinstance(part.get("text"), str) for part in content):
                raise HTTPException(400, "此模型网关仅接受文本消息。")
        elif content is not None and not isinstance(content, str):
            raise HTTPException(400, "此模型网关仅接受文本消息。")
    requested = payload.pop("max_completion_tokens", None)
    tokens = payload.get("max_tokens", requested if requested is not None else settings.output_token_limit)
    if type(tokens) is not int or tokens < 1:
        raise HTTPException(400, "输出 Token 参数无效。")
    payload["max_tokens"] = min(tokens, settings.output_token_limit)
    if type(payload.get("stream", False)) is not bool:
        raise HTTPException(400, "流式输出参数无效。")
    if payload.get("stream"):
        payload["stream_options"] = {"include_usage": True}
    else:
        payload.pop("stream_options", None)
    payload["model"] = settings.model_id
    # Ensure a low-level HTTPX serializer cannot echo malformed user data in an error.
    try:
        json.dumps(payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError) as error:
        raise HTTPException(400, "模型请求参数无效。") from error
    return payload


async def _authorize_wait(awaitable, check):
    task = asyncio.ensure_future(awaitable)
    returned = False
    try:
        while True:
            finished, _ = await asyncio.wait({task}, timeout=1)
            if finished:
                result = task.result()
                returned = True
                return result
            await check()
    finally:
        if not returned:
            if not task.done():
                task.cancel()
            with CancelScope(shield=True), suppress(Exception, asyncio.CancelledError):
                result = await task
                if isinstance(result, httpx.Response):
                    await result.aclose()


async def _bounded_body(response, check):
    source = response.aiter_bytes()
    result = bytearray()
    while True:
        try:
            piece = await _authorize_wait(anext(source), check)
        except StopAsyncIteration:
            return bytes(result)
        if len(result) + len(piece) > _MODEL_RESPONSE_LIMIT:
            raise HTTPException(502, "模型服务响应超过上限。")
        result.extend(piece)


async def _event_frames(upstream, check, release, observer=None):
    """Preserve each complete SSE frame (including usage-only frames)."""
    lines = upstream.aiter_lines()
    event, size = [], 0
    try:
        while True:
            try:
                line = await _authorize_wait(anext(lines), check)
            except StopAsyncIteration:
                if event:
                    if observer:
                        observer.frame(event)
                    await check()
                    yield "\n".join(event) + "\n\n"
                break
            size += len(line)
            if size > _MODEL_RESPONSE_LIMIT:
                raise HTTPException(502, "模型服务流式响应超过上限。")
            if line:
                event.append(line)
                continue
            if event:
                if observer:
                    observer.frame(event)
                await check()
                yield "\n".join(event) + "\n\n"
                event, size = [], 0
    except (AccountError, HTTPException, sqlite3.Error, httpx.HTTPError) as error:
        message = "模型服务连接已中断。"
        kind = "server_error"
        if isinstance(error, AccountError) and error.status_code in {401, 403}:
            message = "当前账号已停用、退出或会话已过期。"
            kind = "permission_error"
        elif isinstance(error, HTTPException):
            message = error.detail
        notice = json.dumps({"error": {"message": message, "type": kind}}, ensure_ascii=False)
        yield "data: " + notice + "\n\n"
    finally:
        await release()


def create_app(settings=None, *, model_control=None, upstream_transport=None, allow_loopback_http=False):
    settings = settings or Settings.from_environment()
    accounts = Accounts(settings.database, admin_code_file=settings.admin_registration_code_file)
    accounts.migrate()
    accounts.initialize_open_webui_access()
    model = model_control or ModelControl(accounts.database, settings.llama_url, settings.upstream_token,
                                         config_path=settings.control_spec, model_id=settings.model_id,
                                         runtime_unit=settings.runtime_unit, gpu_index=settings.gpu_index,
                                         gateway_output_limit=settings.output_token_limit)

    @asynccontextmanager
    async def lifetime(app):
        async with httpx.AsyncClient(
            base_url=settings.llama_url + "/",
            headers={"Authorization": f"Bearer {settings.upstream_token}"},
            trust_env=False, follow_redirects=False, transport=upstream_transport,
            timeout=httpx.Timeout(connect=5, read=180, write=20, pool=5),
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
        ) as client:
            app.state.upstream = client
            yield

    app = FastAPI(title="CCSLab Code Lite Gateway", lifespan=lifetime,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(HTTPSOnly, allow_loopback_http=allow_loopback_http)

    @app.exception_handler(AccountError)
    @app.exception_handler(ModelControlError)
    async def expected_error(_request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
        if isinstance(error, QuotaError):
            request_id = getattr(_request.state, "model_request_id", None)
            diagnostics = {"stage": "quota", "request_id": request_id,
                           **getattr(_request.state, "quota_diagnostics", {})}
            return JSONResponse({"detail": str(error), "error": {
                "message": str(error), "code": error.code, "type": "insufficient_quota",
                "quota_diagnostics": diagnostics}}, error.status_code,
                headers={"X-Request-ID": request_id} if request_id else None)
        return JSONResponse({"detail": str(error)}, error.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, _error):
        return JSONResponse({"detail": "请求参数无效。"}, 400)

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "账号服务暂不可用。"}, 503)

    def signed_in(request: Request):
        return accounts.session(_token(request))

    def active_user(request: Request):
        return accounts.session(_token(request), active=True)

    def admin_user(request: Request):
        return accounts.session(_token(request), admin=True)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/open-webui/access")
    async def open_webui_access(request: Request, _admin=Depends(admin_user)):
        password, = _only(await _json_input(request), "password")
        source = request.client.host if request.client else "unknown"
        return await run_in_threadpool(accounts.grant_open_webui_access, _token(request), password, source)

    @app.get("/api/open-webui/access", status_code=204)
    def verify_open_webui_access(request: Request):
        accounts.verify_open_webui_grant(_token(request), request.headers.get(_OPEN_WEBUI_GRANT_HEADER))
        return Response(status_code=204)

    @app.post("/api/register", status_code=201)
    async def register(request: Request):
        username, password = _only(await _json_input(request), "username", "password")
        source = request.client.host if request.client else "unknown"
        return {"user": await run_in_threadpool(accounts.register, username, password, source)}

    @app.post("/api/register/admin", status_code=201)
    async def register_admin(request: Request):
        username, password, authorization_code = _only(
            await _json_input(request), "username", "password", "authorization_code",
        )
        source = request.client.host if request.client else "unknown"
        return {"user": await run_in_threadpool(
            accounts.register_admin, username, password, authorization_code, source,
        )}

    @app.post("/api/login")
    async def login(request: Request):
        payload = await _json_input(request)
        source = request.client.host if request.client else "unknown"
        if set(payload) == {"username", "password"}:
            return await run_in_threadpool(accounts.login, payload["username"], payload["password"], source)
        username, password, role, code = _only(payload, "username", "password", "role", "authorization_code")
        if role != "admin":
            raise HTTPException(400, "请使用已支持的登录角色。")
        return await run_in_threadpool(
            accounts.login, username, password, source, role=role, authorization_code=code,
        )

    @app.get("/api/me")
    def me(user=Depends(signed_in)):
        return {"user": user}

    @app.post("/api/me/password", status_code=204)
    async def change_own_password(request: Request, _user=Depends(signed_in)):
        current, password, confirmation = _only(
            await _json_input(request), "current_password", "new_password", "confirm_password",
        )
        source = request.client.host if request.client else "unknown"
        await run_in_threadpool(accounts.change_own_password, _token(request), current, password, confirmation, source)
        return Response(status_code=204)

    @app.post("/api/session/role")
    async def switch_role(request: Request):
        token = _token(request)
        role, code = _only(await _json_input(request), "role", "authorization_code")
        source = request.client.host if request.client else "unknown"
        return await run_in_threadpool(accounts.switch_role, token, role, code, source)

    @app.post("/api/logout", status_code=204)
    def logout(request: Request, _user=Depends(signed_in)):
        accounts.logout(_token(request))
        return Response(status_code=204)

    @app.get("/api/admin/users")
    def users(_admin=Depends(admin_user)):
        return {"users": accounts.users()}

    @app.post("/api/admin/users/{user_id}/status")
    async def change_status(user_id: int, request: Request, _admin=Depends(admin_user)):
        status, = _only(await _json_input(request), "status")
        return {"user": await run_in_threadpool(accounts.change_status, _token(request), user_id, status)}

    @app.post("/api/admin/users/{user_id}/quota")
    async def set_quota(user_id: int, request: Request, _admin=Depends(admin_user)):
        mode, limit = _only(await _json_input(request), "mode", "limit_tokens")
        return {"quota": await run_in_threadpool(accounts.quotas.set, _token(request), user_id, mode, limit)}

    @app.post("/api/admin/authorization-code", status_code=204)
    async def change_authorization_code(request: Request, _admin=Depends(admin_user)):
        current, replacement = _only(await _json_input(request), "current_code", "new_code")
        source = request.client.host if request.client else "unknown"
        await run_in_threadpool(accounts.change_authorization_code, _token(request), current, replacement, source)
        return Response(status_code=204)

    @app.post("/api/admin/users/{user_id}/password", status_code=204)
    async def reset_password(user_id: int, request: Request, _admin=Depends(admin_user)):
        password, code = _only(await _json_input(request), "password", "authorization_code")
        source = request.client.host if request.client else "unknown"
        await run_in_threadpool(accounts.reset_password, _token(request), user_id, password, code, source)
        return Response(status_code=204)

    @app.post("/api/admin/open-webui/password", status_code=204)
    async def change_open_webui_password(request: Request, _admin=Depends(admin_user)):
        password, = _only(await _json_input(request), "password")
        await run_in_threadpool(accounts.change_open_webui_password, _token(request),
                                request.headers.get(_OPEN_WEBUI_GRANT_HEADER), password)
        return Response(status_code=204)

    @app.get("/api/model")
    def model_status(_user=Depends(active_user)):
        values = model.status()
        limit = settings.output_token_limit
        upstream_limit = values.get("output_limit")
        if type(upstream_limit) is int and upstream_limit > 0:
            limit = min(limit, upstream_limit)
        return dict(values, output_limit=limit, output_token_limit=limit)

    @app.post("/api/admin/model/preview")
    async def model_preview(request: Request, admin=Depends(admin_user)):
        context, slots = _only(await _json_input(request), "total_context", "slots")
        await run_in_threadpool(accounts.session, _token(request), admin=True)
        return await run_in_threadpool(model.preview, str(admin["id"]), context, slots)

    @app.post("/api/admin/model/apply")
    async def model_apply(request: Request, admin=Depends(admin_user)):
        confirmation, = _only(await _json_input(request), "confirmation")
        if not isinstance(confirmation, str) or not 1 <= len(confirmation) <= 256:
            raise HTTPException(400, "确认令牌无效。")
        await run_in_threadpool(accounts.session, _token(request), admin=True)
        return await run_in_threadpool(model.apply, str(admin["id"]), confirmation)

    @app.get("/v1/models")
    def models(_user=Depends(active_user)):
        return {"object": "list", "data": [{"id": settings.model_id, "object": "model", "owned_by": "ccslab"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request, _user=Depends(active_user)):
        request_id = request.headers.get("X-Request-ID", "")
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            request_id = uuid.uuid4().hex
        request.state.model_request_id = request_id
        token = _token(request)
        payload = _chat_input(await _json_input(request, _MODEL_BODY_LIMIT), settings)
        request.state.quota_diagnostics = {"output_reserve": payload["max_tokens"]}

        async def check():
            await run_in_threadpool(accounts.session, token, active=True)

        await check()
        if not model.begin_inference():
            raise HTTPException(503, "模型维护中，请稍后重试。")

        upstream = None
        handed_over = False
        released = False
        reservation = None
        observer = UsageObserver()
        sent = False

        async def release():
            nonlocal released
            if released:
                return
            released = True
            with CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    try:
                        if reservation is not None:
                            await run_in_threadpool(accounts.quotas.finish, reservation, observer.result, sent=sent)
                    finally:
                        model.end_inference()

        try:
            client = request.app.state.upstream
            reserve_tokens = None
            if _user.get("quota", {}).get("mode") == "limited":
                if _user["quota"]["remaining_tokens"] == 0:
                    raise QuotaError("用户额度已经用完，请充值。", "token_quota_exhausted")
                # Use the running model's own template and tokenizer, including tools,
                # history and system messages. Never turn a character estimate into a bill.
                for path, body in (("apply-template", payload), ("tokenize", None)):
                    if body is None:
                        body = {"content": prompt, "add_special": True, "parse_special": True}
                    counted = await _authorize_wait(client.post(path, json=body), check)
                    if counted.status_code != 200 or len(counted.content) > _MODEL_RESPONSE_LIMIT:
                        raise HTTPException(503, "模型分词接口不可用，限额账号暂不能安全发起请求。")
                    try:
                        value = counted.json()
                    except (ValueError, RecursionError):
                        raise HTTPException(503, "模型分词接口返回无效统计。") from None
                    if not isinstance(value, dict):
                        raise HTTPException(503, "模型分词接口返回无效统计。")
                    if path == "apply-template":
                        prompt = value.get("prompt")
                        if not isinstance(prompt, str):
                            raise HTTPException(503, "模型模板接口返回无效内容。")
                    else:
                        tokens = value.get("tokens")
                        if not isinstance(tokens, list) or any(type(n) is not int for n in tokens):
                            raise HTTPException(503, "模型分词接口返回无效统计。")
                        reserve_tokens = len(tokens) + payload["max_tokens"]
                        request.state.quota_diagnostics["input_tokens"] = len(tokens)
            reservation = await run_in_threadpool(accounts.quotas.reserve, token, request_id, reserve_tokens)
            call = client.build_request("POST", "v1/chat/completions", json=payload)
            call.headers["X-Request-ID"] = request_id
            call.headers.pop("Cookie", None)
            sent = True
            upstream = await _authorize_wait(client.send(call, stream=True), check)
            await check()
            if not 200 <= upstream.status_code < 300:
                sent = upstream.status_code >= 500  # Unknown server failures retain an unconfirmed hold.
                if upstream.status_code == 400:
                    # The upstream body can contain caller input. Expose only known error categories.
                    body = await _bounded_body(upstream, check)
                    diagnostic = context_error(body)
                    if diagnostic:
                        diagnostic.update(request_id=request_id, output_reserve=payload["max_tokens"],
                                          stage="llama.cpp", upstream_status=400)
                        return JSONResponse({
                            "detail": "模型上下文长度不足 (context length exceeded)。",
                            "error": {"code": "context_length_exceeded", "type": "invalid_request_error",
                                      "message": "context length exceeded", "context_diagnostics": diagnostic},
                        }, status_code=400, headers={"X-Request-ID": request_id})
                    raise HTTPException(400, "模型服务拒绝了请求参数。")
                raise HTTPException(502, "模型服务请求失败。")
            if payload.get("stream"):
                if "text/event-stream" not in upstream.headers.get("Content-Type", ""):
                    raise HTTPException(502, "模型服务未返回流式响应。")
                response = StreamingResponse(
                    _event_frames(upstream, check, release, observer), media_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "X-Request-ID": request_id}, background=BackgroundTask(release),
                )
                handed_over = True
                return response
            data = await _bounded_body(upstream, check)
            observer.body(data)
            await check()
            return Response(data, media_type="application/json", headers={"X-Request-ID": request_id})
        except httpx.HTTPError:
            raise HTTPException(502, "模型服务连接失败或超时。") from None
        finally:
            if not handed_over:
                await release()

    return app
