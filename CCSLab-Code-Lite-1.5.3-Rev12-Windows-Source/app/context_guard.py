"""Observe the complete model request without changing its messages or tools.

The byte heuristic is deliberately labelled estimated; only upstream usage is
confirmed. A local size rejection enters OpenCode's native compaction path.
No prompts, code, tool arguments, response bodies or credentials are logged.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import threading
import uuid
from urllib.parse import unquote, urlsplit

import urllib3
from context_errors import context_error


def estimate_input(payload):
    # Includes system instructions, history, tool calls/results and tool schemas.
    value = json.dumps({k: payload[k] for k in ("messages", "tools", "tool_choice") if k in payload},
                       ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(ord(c) < 128 for c in value)
    return (ascii_count + 2) // 3 + (len(value) - ascii_count) * 2 + 16 * len(payload.get("messages", []))


def usage_numbers(usage):
    if not isinstance(usage, dict):
        return {}
    return {k: v for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(v := usage.get(k)) is int and 0 <= v <= 2_147_483_647}


class ContextGuard:
    def __init__(self, base, token, limits, callback, log_path, *, ca_bundle=None, proxy_url=None, task_id=None):
        self.task_id = task_id
        self.base, self.token, self.limits = base.rstrip("/"), token, limits
        self.callback, self.log_path = callback, Path(log_path)
        self.secret = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.requests = {}
        self.closed = threading.Event()
        self.budget_rejections = 0
        tls = ssl.create_default_context()
        if ca_bundle:
            tls.load_verify_locations(cafile=str(ca_bundle))
        options = {"ssl_context": tls, "retries": False, "timeout": urllib3.Timeout(connect=15, read=60)}
        if proxy_url:
            proxy = urlsplit(proxy_url)
            auth = (unquote(proxy.username) + ":" + unquote(proxy.password or "")) if proxy.username else None
            proxy_headers = urllib3.make_headers(proxy_basic_auth=auth) if auth else {}
            self.pool = urllib3.ProxyManager(proxy_url, proxy_headers=proxy_headers, proxy_ssl_context=tls, **options)
        else:
            self.pool = urllib3.PoolManager(**options)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.guard = self
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, reading):
        # reading is built exclusively from numeric counts, generated IDs and enums.
        self.callback({"type": "context", **reading})

    def save(self, reading):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **reading}) + "\n")

    def close(self):
        self.closed.set()
        with self.request_lock:
            requests = list(self.requests.items())
        for _done, response in requests:
            if response is not None:
                # urllib3 2.8.0 exposes _sock_shutdown and shutdown().
                # On older urllib3 the socket is released when the response is closed.
                shutdown = getattr(response, "_sock_shutdown", None)
                try:
                    response.shutdown()  # Interrupt a read waiting for the next upstream token.
                except (OSError, ValueError, RuntimeError, AttributeError):
                    pass  # The handler may have already released this response.
                if os.name == "nt" and shutdown is not None:
                    # Windows' pending select/recv is not woken by shutdown alone;
                    # makefile also delays socket.close until that read returns.
                    # Detach then close the OS handle so the reader can unwind safely.
                    handle = shutdown.__self__.detach()
                    if handle != -1:
                        socket.close(handle)
        self.server.shutdown()
        # Before headers arrive urllib3's connect/read deadlines still apply. This runs on
        # the task worker, so UI stays responsive and the task remains cancelling until drained.
        for done, _response in requests:
            done.wait()
        self.server.server_close()
        self.pool.clear()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        guard = self.server.guard
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + guard.secret):
            self.reply(403, {"error": {"message": "Local model bridge authorization failed"}})
            return
        if self.path != "/v1/chat/completions":
            self.reply(404, {"error": {"message": "Unknown model endpoint"}})
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 32 * 1024 * 1024:
                self.reply(413, {"error": {"message": "Request body too large"}})
                return
            payload = json.loads(self.rfile.read(size))
            requested = payload.get("max_tokens", payload.pop("max_completion_tokens", guard.limits[1]))
            if type(requested) is not int or requested <= 0:
                raise ValueError()
            payload["max_tokens"] = min(requested, guard.limits[1])
            if payload.get("stream"):
                payload["stream_options"] = {"include_usage": True}
            estimate = estimate_input(payload)
        except (ValueError, TypeError, KeyError):
            self.reply(400, {"error": {"message": "Invalid model request"}})
            return
        reading = {"state": "request", "request_id": uuid.uuid4().hex, "input_tokens": None,
                   "estimated_input_tokens": estimate, "output_reserve": payload["max_tokens"],
                   "context_limit": guard.limits[0], "message_count": len(payload.get("messages", [])),
                   "tool_count": len(payload.get("tools", [])), "request_bytes": size,
                   "stage": "preflight", "estimate_method": "ascii/3+nonascii*2+framing"}
        guard.publish(reading)
        # Native compaction requests have no tools. Let its own bounded summary
        # conversion run; never trim the request here or replay any tool calls.
        if payload.get("tools") and estimate + payload["max_tokens"] + 1024 >= guard.limits[0]:
            guard.budget_rejections += 1
            reading.update(state="request_failed", stage="preflight", category="context_estimate")
            guard.publish(reading)
            guard.save(reading)
            retry = guard.budget_rejections <= 2
            self.reply(400, {"error": {"code": "context_length_exceeded" if retry else "local_budget_guard", "message":
                ("Context length budget exceeded by local estimate; compact history before retrying." if retry else
                 "Local input budget cannot recover safely; reduce read range. Session and changes preserved."),
                "context_diagnostics": reading}})
            return
        response = None
        started = False
        done = threading.Event()
        with guard.request_lock:
            cancelled = guard.closed.is_set()
            if not cancelled:
                guard.requests[done] = None
        if cancelled:
            self.reply(503, {"error": {"message": "Local model bridge is closing"}})
            return
        try:
            reading["stage"] = "gateway"
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json",
                       "X-Request-ID": reading["request_id"]}
            if guard.token:
                headers["Authorization"] = "Bearer " + guard.token
            if guard.task_id:
                headers["X-CCSLab-Task"] = guard.task_id
            response = guard.pool.request("POST", guard.base + "/chat/completions", headers=headers,
                                          body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                          preload_content=False, redirect=False)
            with guard.request_lock:
                guard.requests[done] = response
                cancelled = guard.closed.is_set()
            if cancelled:
                return
            reading["http_status"] = response.status
            if response.status >= 400:
                body = response.read(65536)
                diagnostic = context_error(body)
                if diagnostic:
                    reading["category"] = "context"
                    reading["stage"] = "upstream_context"
                    reading["input_tokens"] = diagnostic.get("input_tokens")
                    try:
                        extra = json.loads(body)["error"].get("context_diagnostics", {})
                        for key in ("input_tokens", "context_limit"):
                            value = extra.get(key)
                            if type(value) is int and 0 < value <= 2_147_483_647:
                                reading[key] = value
                    except (ValueError, KeyError, TypeError, AttributeError):
                        pass
                reading["state"] = "request_failed"
                guard.publish(reading)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                started = True
                self.wfile.write(body)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            self.send_header("X-Request-ID", reading["request_id"])
            self.end_headers()
            started = True
            # Stream promptly. Parse usage only; no response text survives this call.
            pending = b""
            stream_error = False
            while chunk := response.read(4096):
                self.wfile.write(chunk)
                self.wfile.flush()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            frame = json.loads(line[5:])
                            if isinstance(frame, dict) and frame.get("error"):
                                stream_error = True
                                reading["stage"] = "upstream_stream"
                            numbers = usage_numbers(frame.get("usage"))
                            if numbers:
                                reading.update(numbers)
                                reading["input_tokens"] = numbers.get("prompt_tokens")
                        except (ValueError, AttributeError):
                            pass
                if len(pending) > 1024 * 1024:
                    pending = b""  # parser only; the complete bytes were forwarded
            reading["state"] = ("request_cancelled" if guard.closed.is_set() else
                                "request_failed" if stream_error else "request_complete")
            if payload.get("tools") and not stream_error:
                guard.budget_rejections = 0
            guard.publish(reading)
        except (urllib3.exceptions.HTTPError, OSError) as error:
            reading.update(state="request_failed", stage="transport")
            guard.publish(reading)
            if not started:
                kind = "TLS" if isinstance(error, urllib3.exceptions.SSLError) else (
                    "proxy" if isinstance(error, urllib3.exceptions.ProxyError) else "network")
                self.reply(502, {"error": {"message": f"Model {kind} connection failed."}})
        finally:
            try:
                if response is not None:
                    response.close()
                    response.release_conn()
                if guard.closed.is_set():
                    reading.update(state="request_cancelled", stage="cancelled")
                guard.save(reading)
            finally:
                with guard.request_lock:
                    guard.requests.pop(done, None)
                done.set()
