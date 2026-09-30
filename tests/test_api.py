"""HTTP 接口层测试：路由、角色头、错误映射与真实 socket 冒烟。"""
from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.request

from helpers import (
    COORD,
    HOME,
    HOST,
    W1_END,
    W1_START,
    make_application,
    make_service,
    prepare_materials,
)

from exchange_service.api import actor_from_headers, dispatch, make_server
from exchange_service.errors import DomainError, ValidationError
from exchange_service.models import Role


def call(service, method, path, actor=None, body=None):
    """模拟 handler 的错误映射，返回 (status, payload)。"""
    try:
        return dispatch(service, method, path, actor, body or {})
    except DomainError as exc:
        return exc.http_status, {"error": {"code": exc.code, "message": exc.message,
                                           **exc.detail}}


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_health(self):
        status, payload = call(self.service, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_invitation_flow_over_api(self):
        status, app = call(self.service, "POST", "/invitations", COORD, {
            "teacher_id": "T-WANG",
            "host_institution_id": "DE-UNI",
            "visit_start": W1_START,
            "visit_end": W1_END,
            "sessions_per_week": 2,
            "buffer_days": 1,
        })
        self.assertEqual(status, 201)
        app_id = app["id"]
        status, _ = call(self.service, "POST", f"/applications/{app_id}/prepare", HOME)
        self.assertEqual(status, 200)
        status, _ = call(self.service, "POST", f"/applications/{app_id}/materials",
                         HOME, {"kind": "passport", "content": "E12345678"})
        self.assertEqual(status, 201)
        status, _ = call(self.service, "POST",
                         f"/applications/{app_id}/materials/passport",
                         COORD, {"verify": True})
        self.assertEqual(status, 200)
        # 接收院校只能看清单状态
        status, payload = call(self.service, "GET",
                               f"/applications/{app_id}/materials", HOST)
        self.assertEqual(status, 200)
        self.assertNotIn("content", payload["materials"][0])
        # 接收院校读材料内容 → 403
        status, payload = call(self.service, "GET",
                               f"/applications/{app_id}/materials/passport", HOST)
        self.assertEqual(status, 403)
        # 协调员可读
        status, payload = call(self.service, "GET",
                               f"/applications/{app_id}/materials/passport", COORD)
        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "E12345678")

    def test_conflict_payload_contains_alternatives(self):
        first = make_application(self.service, teacher="T-WANG")
        prepare_materials(self.service, first["id"])
        self.service.request_hold(COORD, first["id"])
        self.service.confirm(COORD, first["id"])
        second = make_application(self.service, teacher="T-LI")
        status, payload = call(self.service, "POST",
                               f"/applications/{second['id']}/hold", COORD)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "resource_conflict")
        self.assertTrue(payload["error"]["conflicts"])
        self.assertTrue(payload["error"]["alternatives"])

    def test_permission_denied_over_api(self):
        status, _ = call(self.service, "POST", "/invitations",
                         # 教师角色无权发邀请
                         actor=type(COORD)(Role.TEACHER, "T-WANG"),
                         body={"teacher_id": "T-WANG", "host_institution_id": "DE-UNI",
                               "visit_start": W1_START, "visit_end": W1_END})
        self.assertEqual(status, 403)

    def test_unknown_route(self):
        status, _ = call(self.service, "GET", "/nope", COORD)
        self.assertEqual(status, 404)

    def test_missing_actor(self):
        status, _ = call(self.service, "POST", "/invitations", None, {})
        self.assertEqual(status, 400)


class ActorHeaderTest(unittest.TestCase):
    def test_parse_headers(self):
        actor = actor_from_headers({"X-Actor-Role": "coordinator", "X-Actor-Id": "c1"})
        self.assertEqual(actor.role, Role.COORDINATOR)
        with self.assertRaises(ValidationError):
            actor_from_headers({"X-Actor-Id": "c1"})  # 缺角色
        with self.assertRaises(ValidationError):
            actor_from_headers({"X-Actor-Role": "home_institution",
                                "X-Actor-Id": "x"})  # 院校角色缺院校 ID
        with self.assertRaises(ValidationError):
            actor_from_headers({"X-Actor-Role": "nobody", "X-Actor-Id": "x"})


class HttpSmokeTest(unittest.TestCase):
    def test_roundtrip_over_socket(self):
        service = make_service()
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                self.assertEqual(resp.status, 200)
            # 缺身份头 → 400
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/invitations",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
