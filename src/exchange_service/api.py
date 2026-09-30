"""HTTP JSON 接口：路由 + 角色头解析 + 错误映射。

`dispatch` 是纯函数（不依赖 socket），便于直接测试；
`make_server` 只是把它包装进 http.server。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DomainError, ValidationError
from .models import Actor, Role
from .service import ExchangeService


def actor_from_headers(headers) -> Actor:
    """从请求头解析操作者身份：X-Actor-Role / X-Actor-Id / X-Actor-Institution。"""
    role_raw = headers.get("X-Actor-Role")
    actor_id = headers.get("X-Actor-Id")
    if not role_raw or not actor_id:
        raise ValidationError("缺少身份头：X-Actor-Role / X-Actor-Id")
    try:
        role = Role(role_raw)
    except ValueError:
        raise ValidationError(f"未知角色：{role_raw}") from None
    institution_id = headers.get("X-Actor-Institution")
    if role in (Role.HOME_INSTITUTION, Role.HOST_INSTITUTION) and not institution_id:
        raise ValidationError("院校角色必须提供 X-Actor-Institution")
    return Actor(role=role, actor_id=actor_id, institution_id=institution_id)


def _body_of(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValidationError("请求体不是合法 JSON") from None
    if not isinstance(value, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return value


def dispatch(service: ExchangeService, method: str, path: str,
             actor: Actor | None, body: dict) -> tuple[int, dict]:
    """路由分发；返回 (HTTP 状态码, JSON 负载)。"""
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    def need_actor() -> Actor:
        if actor is None:
            raise ValidationError("缺少身份头：X-Actor-Role / X-Actor-Id")
        return actor

    app_match = re.fullmatch(r"/applications/([^/]+)(?:/([^/]+)(?:/([^/]+))?)?", path)

    if method == "POST" and path == "/invitations":
        return 201, service.create_invitation(need_actor(), **body)
    if method == "POST" and path == "/maintenance/sweep":
        return 200, service.sweep()
    if method == "POST" and path == "/maintenance/recover":
        return 200, service.recover()

    if app_match:
        app_id, action, extra = app_match.group(1), app_match.group(2), app_match.group(3)
        if method == "GET" and action is None:
            return 200, service.get_application(need_actor(), app_id)
        if method == "POST" and action == "prepare":
            return 200, service.start_preparation(need_actor(), app_id)
        if method == "POST" and action == "hold":
            return 200, service.request_hold(need_actor(), app_id)
        if method == "POST" and action == "confirm":
            return 200, service.confirm(need_actor(), app_id)
        if method == "POST" and action == "reschedule":
            return 200, service.reschedule(need_actor(), app_id, **body)
        if method == "POST" and action == "cancel":
            return 200, service.cancel(need_actor(), app_id, **body)
        if method == "POST" and action == "substitute":
            return 200, service.substitute_teacher(need_actor(), app_id, **body)
        if action == "materials" and extra is None:
            if method == "GET":
                return 200, {"materials": service.list_materials(need_actor(), app_id)}
            if method == "POST":
                return 201, service.submit_material(need_actor(), app_id, **body)
        if action == "materials" and extra is not None:
            if method == "GET":
                return 200, service.get_material(need_actor(), app_id, extra)
            if method == "POST" and body.get("verify"):
                return 200, service.verify_material(need_actor(), app_id, extra)

    return 404, {"error": {"code": "not_found", "message": f"未知路由：{method} {path}"}}


def make_handler(service: ExchangeService):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            try:
                actor = actor_from_headers(self.headers)
            except DomainError as exc:
                actor = None
                status, payload = exc.http_status, self._error_payload(exc)
                if self.path != "/health":
                    return self._respond(status, payload)
            try:
                body = _body_of(self)
                status, payload = dispatch(service, self.command, self.path, actor, body)
            except DomainError as exc:
                status, payload = exc.http_status, self._error_payload(exc)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                status, payload = 500, {"error": {"code": "internal_error",
                                                  "message": str(exc)}}
            self._respond(status, payload)

        @staticmethod
        def _error_payload(exc: DomainError) -> dict:
            return {"error": {"code": exc.code, "message": exc.message, **exc.detail}}

        def _respond(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, *args):  # 静默访问日志
            pass

    return Handler


def make_server(service: ExchangeService, host: str = "127.0.0.1", port: int = 8080):
    return ThreadingHTTPServer((host, port), make_handler(service))
