"""互访编排服务的核心行为测试。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import (
    COORD,
    HOME,
    HOST,
    TEACHER_WANG,
    W1_END,
    W1_START,
    W2_END,
    W2_START,
    W3_END,
    W3_START,
    FakeClock,
    confirm_application,
    make_application,
    make_service,
    prepare_materials,
)

from exchange_service.errors import (
    ConflictError,
    EligibilityError,
    ForbiddenError,
    StateError,
    ValidationError,
)
from exchange_service.models import Actor, Role
from exchange_service.service import ExchangeService
from exchange_service.store import Store
from exchange_service.timeutil import material_deadline, parse_iso, to_iso


class HappyPathTest(unittest.TestCase):
    def test_full_flow(self):
        service = make_service()
        app = make_application(service)
        self.assertEqual(app["state"], "准备")
        prepare_materials(service, app["id"])
        held = service.request_hold(COORD, app["id"])
        self.assertEqual(held["state"], "暂占")
        self.assertIsNotNone(held["hold_expires_at"])
        held_types = {h["resource_type"] for h in held["holds"] if h["state"] == "tentative"}
        self.assertEqual(held_types, {"course_slot", "dorm_unit", "reception_quota"})
        confirmed = service.confirm(COORD, app["id"])
        self.assertEqual(confirmed["state"], "确认")
        self.assertTrue(all(h["state"] == "confirmed" for h in confirmed["holds"]))
        self.assertIsNone(confirmed["hold_expires_at"])


class MaterialGateTest(unittest.TestCase):
    def test_confirm_blocked_until_visa_ready(self):
        """签证未齐：名额可以暂占，但绝不能确认。"""
        service = make_service()
        app = make_application(service)
        prepare_materials(service, app["id"], kinds=("passport", "invitation_letter"))
        service.request_hold(COORD, app["id"])
        with self.assertRaises(StateError) as ctx:
            service.confirm(COORD, app["id"])
        self.assertEqual(ctx.exception.detail["missing_materials"], ["visa"])
        self.assertEqual(service.get_application(COORD, app["id"])["state"], "暂占")

    def test_material_resubmit_resets_verification(self):
        service = make_service()
        app = make_application(service)
        service.submit_material(HOME, app["id"], "visa", "旧签证页")
        service.verify_material(COORD, app["id"], "visa")
        service.submit_material(HOME, app["id"], "visa", "新签证页")
        materials = service.list_materials(COORD, app["id"])
        self.assertFalse(materials[0]["verified"])


class ConflictTest(unittest.TestCase):
    def test_conflict_returns_alternatives(self):
        """日期提前撞上宿舍与配额：冲突结果必须给出可选方案。"""
        service = make_service()
        first = make_application(service, teacher="T-WANG")
        confirm_application(service, first["id"])
        second = make_application(service, teacher="T-LI")  # 同一窗口
        with self.assertRaises(ConflictError) as ctx:
            service.request_hold(COORD, second["id"])
        resources = {c["resource"] for c in ctx.exception.conflicts}
        self.assertIn("dorm_unit", resources)
        self.assertIn("reception_quota", resources)
        self.assertGreaterEqual(len(ctx.exception.alternatives), 1)
        # 采纳备选方案：改期到备选窗口后可以暂占
        alt = ctx.exception.alternatives[0]
        service.reschedule(COORD, second["id"],
                           visit_start=alt["visit_start"], visit_end=alt["visit_end"])
        held = service.request_hold(COORD, second["id"])
        self.assertEqual(held["state"], "暂占")

    def test_course_demand_conflict(self):
        service = make_service()
        app = make_application(service, sessions=28)  # 每周可用时段不足
        with self.assertRaises(ConflictError) as ctx:
            service.request_hold(COORD, app["id"])
        resources = {c["resource"] for c in ctx.exception.conflicts}
        self.assertIn("course_slot", resources)


class RescheduleTest(unittest.TestCase):
    def test_reschedule_success_is_atomic(self):
        service = make_service()
        first = make_application(service, teacher="T-WANG")
        confirm_application(service, first["id"])
        second = make_application(service, teacher="T-LI", start=W2_START, end=W2_END)
        prepare_materials(service, second["id"])
        service.request_hold(COORD, second["id"])
        moved = service.reschedule(COORD, second["id"],
                                   visit_start=W3_START, visit_end=W3_END)
        self.assertEqual(moved["visit_start"], W3_START)
        self.assertEqual(moved["state"], "暂占")
        states = [h["state"] for h in moved["holds"]]
        self.assertIn("released", states)    # 旧占用已释放
        self.assertIn("tentative", states)   # 新占用已建立
        # 旧窗口资源已可用：王老师改期到该窗口不冲突
        service.reschedule(COORD, first["id"], visit_start=W2_START, visit_end=W2_END)

    def test_reschedule_conflict_keeps_everything(self):
        service = make_service()
        first = make_application(service, teacher="T-WANG")
        confirm_application(service, first["id"])
        second = make_application(service, teacher="T-LI", start=W2_START, end=W2_END)
        prepare_materials(service, second["id"])
        service.request_hold(COORD, second["id"])
        with self.assertRaises(ConflictError):
            service.reschedule(COORD, second["id"],
                               visit_start=W1_START, visit_end=W1_END)
        view = service.get_application(COORD, second["id"])
        self.assertEqual(view["visit_start"], W2_START)
        self.assertEqual(view["state"], "暂占")
        self.assertTrue(all(h["state"] == "tentative" for h in view["holds"]))

    def test_reschedule_confirmed_keeps_confirmed(self):
        service = make_service()
        app = make_application(service, teacher="T-WANG")
        confirm_application(service, app["id"])
        moved = service.reschedule(COORD, app["id"],
                                   visit_start=W3_START, visit_end=W3_END)
        self.assertEqual(moved["state"], "确认")
        self.assertTrue(all(h["state"] in ("confirmed", "released")
                            for h in moved["holds"]))


class SubstituteTest(unittest.TestCase):
    def test_substitute_teacher_atomic_reset(self):
        """替代教师：换人、材料重置、占用回退暂占，一次事务完成。"""
        service = make_service()
        app = make_application(service, teacher="T-WANG")
        confirm_application(service, app["id"])
        result = service.substitute_teacher(COORD, app["id"], "T-LI")
        self.assertEqual(result["teacher_id"], "T-LI")
        self.assertEqual(result["state"], "暂占")
        self.assertEqual(service.list_materials(COORD, app["id"]), [])
        view = service.get_application(COORD, app["id"])
        self.assertTrue(all(h["state"] == "tentative" for h in view["holds"]))
        # 新材料齐备后可重新确认
        prepare_materials(service, app["id"])
        self.assertEqual(service.confirm(COORD, app["id"])["state"], "确认")

    def test_substitute_requires_same_subject(self):
        service = make_service()
        app = make_application(service, teacher="T-WANG")
        with self.assertRaises(EligibilityError):
            service.substitute_teacher(COORD, app["id"], "T-ZHAO")  # 物理 ≠ 数学

    def test_substitute_rejects_busy_teacher(self):
        """替代教师本人在同一窗口另有在办申请（含缓冲）→ 冲突拒绝。"""
        service = make_service()
        first = make_application(service, teacher="T-WANG")
        confirm_application(service, first["id"])
        # 李老师名下有一笔同窗口的在办申请（尚未暂占）
        make_application(service, teacher="T-LI")
        with self.assertRaises(ConflictError) as ctx:
            service.substitute_teacher(COORD, first["id"], "T-LI")
        self.assertEqual(ctx.exception.conflicts[0]["resource"], "teacher_schedule")


class CancelTest(unittest.TestCase):
    def test_cancel_releases_resources_atomically(self):
        service = make_service()
        first = make_application(service, teacher="T-WANG")
        prepare_materials(service, first["id"])
        service.request_hold(COORD, first["id"])
        service.cancel(COORD, first["id"], reason="行程取消")
        view = service.get_application(COORD, first["id"])
        self.assertEqual(view["state"], "取消")
        self.assertTrue(all(h["state"] == "released" for h in view["holds"]))
        # 资源已释放：李老师同窗口可以暂占
        second = make_application(service, teacher="T-LI")
        prepare_materials(service, second["id"])
        self.assertEqual(service.request_hold(COORD, second["id"])["state"], "暂占")

    def test_home_institution_can_cancel(self):
        service = make_service()
        app = make_application(service)
        self.assertEqual(service.cancel(HOME, app["id"])["state"], "取消")

    def test_host_cannot_cancel(self):
        service = make_service()
        app = make_application(service)
        with self.assertRaises(ForbiddenError):
            service.cancel(HOST, app["id"])


class ExpiryAndRecoveryTest(unittest.TestCase):
    def test_sweep_releases_expired_holds(self):
        clock = FakeClock()
        service = make_service(hold_ttl=timedelta(hours=1), clock=clock)
        app = make_application(service, teacher="T-WANG")
        service.request_hold(COORD, app["id"])
        clock.advance(hours=2)
        report = service.sweep()
        self.assertEqual(report["reverted_applications"], [app["id"]])
        view = service.get_application(COORD, app["id"])
        self.assertEqual(view["state"], "准备")
        self.assertTrue(all(h["state"] == "expired" for h in view["holds"]))
        # 名额释放后其他教师可申请同一窗口
        second = make_application(service, teacher="T-LI")
        self.assertEqual(service.request_hold(COORD, second["id"])["state"], "暂占")

    def test_recovery_after_restart(self):
        """进程重启后：新实例继续清理上次遗留的逾期暂占。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "exchange.db")
            clock = FakeClock()
            service_a = make_service(db, hold_ttl=timedelta(hours=1), clock=clock)
            app = make_application(service_a, teacher="T-WANG")
            service_a.request_hold(COORD, app["id"])
            service_a.store.close()
            # 模拟进程重启：时钟已越过暂占过期时间
            clock.advance(hours=2)
            store_b = Store(db)
            service_b = ExchangeService(store_b, clock=clock)
            report = service_b.recover()
            self.assertIn(app["id"], report["sweep"]["reverted_applications"])
            self.assertEqual(
                service_b.get_application(COORD, app["id"])["state"], "准备"
            )
            store_b.close()

    def test_recovery_fixes_orphan_held_application(self):
        """处于暂占但已无任何有效占用的申请，恢复时回退到准备。"""
        service = make_service()
        app = make_application(service)
        service.request_hold(COORD, app["id"])
        with service.store.tx() as conn:
            conn.execute(
                "UPDATE holds SET state = 'released' WHERE application_id = ?",
                (app["id"],),
            )
        report = service.recover()
        self.assertIn(app["id"], report["orphan_held_applications"])
        self.assertEqual(service.get_application(COORD, app["id"])["state"], "准备")

    def test_visit_lifecycle_via_sweep(self):
        clock = FakeClock()
        service = make_service(clock=clock)
        app = service.create_invitation(
            COORD, teacher_id="T-WANG", host_institution_id="DE-UNI",
            visit_start="2026-10-20T08:00:00+00:00",
            visit_end="2026-10-21T16:00:00+00:00",
        )
        service.start_preparation(COORD, app["id"])
        prepare_materials(service, app["id"])
        service.request_hold(COORD, app["id"])
        service.confirm(COORD, app["id"])
        clock.advance(days=22)  # 访问已结束
        report = service.sweep()
        self.assertEqual(report["completed"], [app["id"]])
        view = service.get_application(COORD, app["id"])
        self.assertEqual(view["state"], "完成")
        self.assertTrue(all(h["state"] == "released" for h in view["holds"]))


class MaterialIsolationTest(unittest.TestCase):
    def test_materials_isolated_by_role(self):
        service = make_service()
        app = make_application(service)
        service.submit_material(HOME, app["id"], "passport", "E12345678")
        # 接收院校：只能看到清单状态，看不到内容
        checklist = service.list_materials(HOST, app["id"])
        self.assertEqual(checklist[0]["kind"], "passport")
        self.assertNotIn("content", checklist[0])
        with self.assertRaises(ForbiddenError):
            service.get_material(HOST, app["id"], "passport")
        # 协调员 / 派出院校 / 教师本人：可读内容
        self.assertEqual(
            service.get_material(COORD, app["id"], "passport")["content"], "E12345678"
        )
        self.assertEqual(
            service.get_material(HOME, app["id"], "passport")["content"], "E12345678"
        )
        self.assertEqual(
            service.get_material(TEACHER_WANG, app["id"], "passport")["content"],
            "E12345678",
        )
        # 无关人员：拒绝
        stranger = Actor(Role.TEACHER, "T-LI")
        with self.assertRaises(ForbiddenError):
            service.list_materials(stranger, app["id"])


class TimezoneTest(unittest.TestCase):
    def test_material_deadline_berlin(self):
        """柏林（夏令时 UTC+2）：11-02 09:00 到访，提前 10 天、当地 17:00 截止。"""
        service = make_service()
        app = service.create_invitation(
            COORD, teacher_id="T-WANG", host_institution_id="DE-UNI",
            visit_start=W1_START, visit_end=W1_END,
        )
        # 2026-10-23 仍是夏令时：17:00+02:00 = 15:00Z
        self.assertEqual(app["material_deadline"], "2026-10-23T15:00:00+00:00")

    def test_material_deadline_shanghai(self):
        start = parse_iso("2026-11-02T01:00:00+00:00")  # 上海 09:00
        deadline = material_deadline(start, "Asia/Shanghai", 10)
        self.assertEqual(to_iso(deadline), "2026-10-23T09:00:00+00:00")


class EligibilityTest(unittest.TestCase):
    def test_invitation_eligibility(self):
        service = make_service()
        with service.store.tx() as conn:
            conn.execute("UPDATE teachers SET active = 0 WHERE id = 'T-ZHAO'")
        with self.assertRaises(EligibilityError):
            service.create_invitation(COORD, teacher_id="T-ZHAO",
                                      host_institution_id="DE-UNI",
                                      visit_start=W1_START, visit_end=W1_END)
        with self.assertRaises(EligibilityError):
            service.create_invitation(COORD, teacher_id="T-WANG",
                                      host_institution_id="CN-UNI",  # 派出=接收
                                      visit_start=W1_START, visit_end=W1_END)
        make_application(service)  # 王老师已有在办申请
        with self.assertRaises(EligibilityError):
            service.create_invitation(COORD, teacher_id="T-WANG",
                                      host_institution_id="DE-UNI",
                                      visit_start=W2_START, visit_end=W2_END)
        with self.assertRaises(ValidationError):
            service.create_invitation(COORD, teacher_id="T-LI",
                                      host_institution_id="DE-UNI",
                                      visit_start="2026-09-01T00:00:00+00:00",
                                      visit_end="2026-09-05T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
