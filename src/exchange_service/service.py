"""互访协调应用服务。

职责：
- 邀请资格审核与申请创建（跨时区材料截止）；
- 申请阶段暂占关联资源（课程时段 + 宿舍 + 接待配额，单事务）；
- 材料齐备后才允许确认；
- 改期 / 取消 / 替代教师 / 逾期释放均为原子调整；
- 身份材料按角色隔离；
- 冲突时给出可选方案；
- sweep / recover 清理逾期与遗留暂占。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from .errors import (
    ConflictError,
    EligibilityError,
    ForbiddenError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .models import (
    ACTIVE_HOLD_STATES,
    REQUIRED_MATERIALS,
    TERMINAL_STATES,
    Actor,
    ApplicationState,
    HoldState,
    MaterialKind,
    ResourceType,
    Role,
)
from .scheduler import HoldPlan, find_alternatives, plan_window
from .store import Store
from .timeutil import material_deadline, parse_iso, to_iso, utcnow


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ExchangeService:
    def __init__(
        self,
        store: Store,
        *,
        hold_ttl: timedelta = timedelta(hours=72),
        material_days_before: int = 10,
        clock=utcnow,
    ):
        self.store = store
        self.hold_ttl = hold_ttl                # 暂占最长保留时长
        self.material_days_before = material_days_before
        self.clock = clock                      # 可注入，便于测试与恢复演练

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self.clock()

    def _audit(self, conn, application_id, action, actor: Actor | None, detail=None):
        conn.execute(
            "INSERT INTO audit_log(application_id, action, detail, actor, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                application_id,
                action,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                "system" if actor is None else f"{actor.role.value}:{actor.actor_id}",
                to_iso(self._now()),
            ),
        )

    @staticmethod
    def _get_app(conn, application_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"申请不存在：{application_id}")
        return row

    @staticmethod
    def _get_teacher(conn, teacher_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"教师不存在：{teacher_id}")
        return row

    @staticmethod
    def _get_institution(conn, institution_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM institutions WHERE id = ?", (institution_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"院校不存在：{institution_id}")
        return row

    @staticmethod
    def _require(actor: Actor, *roles: Role):
        if actor.role not in roles:
            raise ForbiddenError(f"角色 {actor.role.value} 无权执行该操作")

    def _check_view(self, actor: Actor, app, teacher):
        """查看权限：协调员全部；派出/接收院校限本校相关；教师限本人。"""
        if actor.role == Role.COORDINATOR:
            return
        if actor.role == Role.TEACHER and actor.actor_id == app["teacher_id"]:
            return
        if actor.role == Role.HOME_INSTITUTION and actor.institution_id == teacher["home_institution_id"]:
            return
        if actor.role == Role.HOST_INSTITUTION and actor.institution_id == app["host_institution_id"]:
            return
        raise ForbiddenError("无权查看该申请")

    def _material_visibility(self, actor: Actor, app, teacher) -> str:
        """身份材料可见级别：full=可读内容；checklist=仅清单状态。"""
        if actor.role == Role.COORDINATOR:
            return "full"
        if actor.role == Role.TEACHER and actor.actor_id == app["teacher_id"]:
            return "full"
        if actor.role == Role.HOME_INSTITUTION and actor.institution_id == teacher["home_institution_id"]:
            return "full"
        if actor.role == Role.HOST_INSTITUTION and actor.institution_id == app["host_institution_id"]:
            return "checklist"
        raise ForbiddenError("无权查看该申请的身份材料")

    # ------------------------------------------------------------------ 视图

    def _app_view(self, conn, app, actor: Actor | None = None) -> dict:
        teacher = self._get_teacher(conn, app["teacher_id"])
        view = {
            "id": app["id"],
            "teacher_id": app["teacher_id"],
            "teacher_name": teacher["name"],
            "host_institution_id": app["host_institution_id"],
            "visit_start": app["visit_start"],
            "visit_end": app["visit_end"],
            "buffer_days": app["buffer_days"],
            "sessions_per_week": app["sessions_per_week"],
            "state": app["state"],
            "material_deadline": app["material_deadline"],
            "hold_expires_at": app["hold_expires_at"],
            "version": app["version"],
        }
        view["holds"] = [
            self._hold_view(r)
            for r in conn.execute(
                "SELECT * FROM holds WHERE application_id = ? ORDER BY created_at",
                (app["id"],),
            )
        ]
        if actor is not None:
            try:
                visibility = self._material_visibility(actor, app, teacher)
            except ForbiddenError:
                visibility = "none"
            view["materials"] = self._material_list(conn, app["id"], visibility)
        return view

    @staticmethod
    def _hold_view(row) -> dict:
        return {
            "id": row["id"],
            "resource_type": row["resource_type"],
            "resource_ref": row["resource_ref"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "state": row["state"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _material_list(conn, application_id: str, visibility: str) -> list[dict]:
        if visibility == "none":
            return []
        rows = conn.execute(
            "SELECT * FROM materials WHERE application_id = ? ORDER BY kind",
            (application_id,),
        ).fetchall()
        result = []
        for r in rows:
            item = {
                "kind": r["kind"],
                "submitted_at": r["submitted_at"],
                "verified": bool(r["verified"]),
            }
            if visibility == "full":
                # 内容仅授权角色可见；接收院校（checklist）拿不到
                item.update(
                    {
                        "content": r["content"],
                        "submitted_by_role": r["submitted_by_role"],
                        "submitted_by": r["submitted_by"],
                        "verified_by": r["verified_by"],
                        "verified_at": r["verified_at"],
                    }
                )
            result.append(item)
        return result

    def get_application(self, actor: Actor, application_id: str) -> dict:
        with self.store.read() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            self._check_view(actor, app, teacher)
            return self._app_view(conn, app, actor)

    # ------------------------------------------------------------ 邀请与申请

    def create_invitation(
        self,
        actor: Actor,
        *,
        teacher_id: str,
        host_institution_id: str,
        visit_start: str,
        visit_end: str,
        sessions_per_week: int = 2,
        buffer_days: int = 2,
    ) -> dict:
        """发出邀请：校验邀请资格，创建“邀请”态申请并计算跨时区材料截止。"""
        self._require(actor, Role.COORDINATOR, Role.HOST_INSTITUTION)
        start, end = parse_iso(visit_start), parse_iso(visit_end)
        if end <= start:
            raise ValidationError("visit_end 必须晚于 visit_start")
        if start <= self._now():
            raise ValidationError("访问开始时间必须在未来")
        if sessions_per_week < 1:
            raise ValidationError("sessions_per_week 必须 >= 1")
        if buffer_days < 0:
            raise ValidationError("buffer_days 不能为负")

        with self.store.tx() as conn:
            teacher = self._get_teacher(conn, teacher_id)
            host = self._get_institution(conn, host_institution_id)
            if actor.role == Role.HOST_INSTITUTION and actor.institution_id != host["id"]:
                raise ForbiddenError("接收院校只能以本校名义发出邀请")
            # 邀请资格：在职、派出校与接收校不同、无重叠在办行程
            if not teacher["active"]:
                raise EligibilityError("教师不在职，不具备受邀资格")
            if teacher["home_institution_id"] == host["id"]:
                raise EligibilityError("派出院校与接收院校不能相同")
            busy = conn.execute(
                "SELECT id FROM applications WHERE teacher_id = ? "
                "AND state NOT IN ({})".format(",".join("?" for _ in TERMINAL_STATES)),
                (teacher_id, *TERMINAL_STATES),
            ).fetchone()
            if busy is not None:
                raise EligibilityError(
                    "教师已有在办访学申请，行程未结",
                    detail={"blocking_application_id": busy["id"]},
                )

            now = to_iso(self._now())
            app_id = _new_id("app")
            deadline = material_deadline(start, host["timezone"], self.material_days_before)
            conn.execute(
                "INSERT INTO applications(id, teacher_id, host_institution_id, visit_start, "
                "visit_end, buffer_days, sessions_per_week, state, material_deadline, "
                "hold_expires_at, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,0,?,?)",
                (
                    app_id,
                    teacher_id,
                    host["id"],
                    to_iso(start),
                    to_iso(end),
                    buffer_days,
                    sessions_per_week,
                    ApplicationState.INVITED.value,
                    to_iso(deadline),
                    now,
                    now,
                ),
            )
            self._audit(conn, app_id, "create_invitation", actor,
                        {"visit_start": to_iso(start), "visit_end": to_iso(end)})
            return self._app_view(conn, self._get_app(conn, app_id))

    def start_preparation(self, actor: Actor, application_id: str) -> dict:
        """接受邀请，进入材料准备阶段。"""
        self._require(actor, Role.COORDINATOR, Role.HOME_INSTITUTION)
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            if actor.role == Role.HOME_INSTITUTION and actor.institution_id != teacher["home_institution_id"]:
                raise ForbiddenError("只有派出院校或协调员可以启动准备")
            if app["state"] != ApplicationState.INVITED.value:
                raise StateError(f"当前状态 {app['state']} 不能进入准备")
            self._set_state(conn, app, ApplicationState.PREPARING.value)
            self._audit(conn, application_id, "start_preparation", actor)
            return self._app_view(conn, self._get_app(conn, application_id))

    # ------------------------------------------------------------------ 材料

    def submit_material(self, actor: Actor, application_id: str, kind: str, content: str) -> dict:
        """提交/重新提交身份材料；重新提交会重置核验状态。"""
        if kind not in {m.value for m in MaterialKind}:
            raise ValidationError(f"未知材料类型：{kind}")
        if not content:
            raise ValidationError("材料内容不能为空")
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            allowed = (
                actor.role == Role.COORDINATOR
                or (actor.role == Role.TEACHER and actor.actor_id == app["teacher_id"])
                or (actor.role == Role.HOME_INSTITUTION
                    and actor.institution_id == teacher["home_institution_id"])
            )
            if not allowed:
                raise ForbiddenError("只有协调员、派出院校或教师本人可以提交材料")
            if app["state"] not in (ApplicationState.PREPARING.value, ApplicationState.HELD.value):
                raise StateError(f"当前状态 {app['state']} 不能提交材料")
            now = to_iso(self._now())
            conn.execute(
                "INSERT INTO materials(id, application_id, kind, content, submitted_by_role, "
                "submitted_by, submitted_at, verified, verified_by, verified_at) "
                "VALUES (?,?,?,?,?,?,?,0,NULL,NULL) "
                "ON CONFLICT(application_id, kind) DO UPDATE SET "
                "content=excluded.content, submitted_by_role=excluded.submitted_by_role, "
                "submitted_by=excluded.submitted_by, submitted_at=excluded.submitted_at, "
                "verified=0, verified_by=NULL, verified_at=NULL",
                (
                    _new_id("mat"),
                    application_id,
                    kind,
                    content,
                    actor.role.value,
                    actor.actor_id,
                    now,
                ),
            )
            self._audit(conn, application_id, "submit_material", actor, {"kind": kind})
            return {"application_id": application_id, "kind": kind, "submitted_at": now}

    def verify_material(self, actor: Actor, application_id: str, kind: str) -> dict:
        """核验材料：仅协调员。"""
        self._require(actor, Role.COORDINATOR)
        with self.store.tx() as conn:
            self._get_app(conn, application_id)
            row = conn.execute(
                "SELECT * FROM materials WHERE application_id = ? AND kind = ?",
                (application_id, kind),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"材料未提交：{kind}")
            now = to_iso(self._now())
            conn.execute(
                "UPDATE materials SET verified = 1, verified_by = ?, verified_at = ? "
                "WHERE application_id = ? AND kind = ?",
                (actor.actor_id, now, application_id, kind),
            )
            self._audit(conn, application_id, "verify_material", actor, {"kind": kind})
            return {"application_id": application_id, "kind": kind, "verified": True}

    def list_materials(self, actor: Actor, application_id: str) -> list[dict]:
        """按角色隔离的材料视图：接收院校只能看到清单状态，看不到内容。"""
        with self.store.read() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            visibility = self._material_visibility(actor, app, teacher)
            return self._material_list(conn, application_id, visibility)

    def get_material(self, actor: Actor, application_id: str, kind: str) -> dict:
        """读取材料内容：仅协调员、派出院校、教师本人。"""
        with self.store.read() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            if self._material_visibility(actor, app, teacher) != "full":
                raise ForbiddenError("身份材料按角色隔离，接收院校无权查看材料内容")
            row = conn.execute(
                "SELECT * FROM materials WHERE application_id = ? AND kind = ?",
                (application_id, kind),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"材料未提交：{kind}")
            return {
                "kind": row["kind"],
                "content": row["content"],
                "submitted_by": row["submitted_by"],
                "submitted_at": row["submitted_at"],
                "verified": bool(row["verified"]),
            }

    # ------------------------------------------------------------ 暂占与确认

    def _hold_expiry(self, app) -> datetime:
        """暂占过期时间 = min(现在 + TTL, 材料截止)。"""
        deadline = parse_iso(app["material_deadline"])
        expiry = min(self._now() + self.hold_ttl, deadline)
        if expiry <= self._now():
            raise StateError("材料截止时间已过，无法暂占资源，请先改期")
        return expiry

    def _insert_holds(self, conn, app_id: str, plan: HoldPlan, state: str,
                      expires_at: datetime | None):
        now = to_iso(self._now())
        expiry = to_iso(expires_at) if expires_at else None
        rows = [
            (ResourceType.COURSE_SLOT.value, ref, plan.visit_start, plan.visit_end)
            for ref in plan.course_slots
        ]
        dorm_start, dorm_end = plan.dorm_window
        rows.append((ResourceType.DORM_UNIT.value, plan.dorm_unit_id, dorm_start, dorm_end))
        rows.append((ResourceType.RECEPTION_QUOTA.value, plan.quota_ref,
                     plan.visit_start, plan.visit_end))
        for resource_type, ref, w_start, w_end in rows:
            conn.execute(
                "INSERT INTO holds(id, application_id, resource_type, resource_ref, "
                "window_start, window_end, state, expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _new_id("hold"),
                    app_id,
                    resource_type,
                    ref,
                    to_iso(w_start),
                    to_iso(w_end),
                    state,
                    expiry,
                    now,
                    now,
                ),
            )

    def _plan_or_raise(self, conn, app, host, *, start: datetime, end: datetime) -> HoldPlan:
        plan, conflicts = plan_window(
            conn,
            host=host,
            teacher_id=app["teacher_id"],
            visit_start=start,
            visit_end=end,
            sessions_per_week=app["sessions_per_week"],
            buffer_days=app["buffer_days"],
            exclude_application_id=app["id"],
        )
        if plan is not None:
            return plan
        alternatives = find_alternatives(
            conn,
            host=host,
            teacher_id=app["teacher_id"],
            visit_start=start,
            visit_end=end,
            sessions_per_week=app["sessions_per_week"],
            buffer_days=app["buffer_days"],
            exclude_application_id=app["id"],
            not_before=self._now(),
        )
        raise ConflictError(
            "访问窗口与现有安排冲突",
            conflicts=[c.as_dict() for c in conflicts],
            alternatives=[self._plan_view(p) for p in alternatives],
        )

    @staticmethod
    def _plan_view(plan: HoldPlan) -> dict:
        return {
            "visit_start": to_iso(plan.visit_start),
            "visit_end": to_iso(plan.visit_end),
            "course_slots": plan.course_slots,
            "dorm_unit_id": plan.dorm_unit_id,
        }

    def request_hold(self, actor: Actor, application_id: str) -> dict:
        """申请阶段暂占关联资源：课程时段 + 宿舍（含缓冲）+ 接待配额，单事务。"""
        self._require(actor, Role.COORDINATOR)
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            if app["state"] != ApplicationState.PREPARING.value:
                raise StateError(f"当前状态 {app['state']} 不能申请暂占")
            host = self._get_institution(conn, app["host_institution_id"])
            expiry = self._hold_expiry(app)
            plan = self._plan_or_raise(
                conn, app, host,
                start=parse_iso(app["visit_start"]), end=parse_iso(app["visit_end"]),
            )
            self._insert_holds(conn, app["id"], plan, HoldState.TENTATIVE.value, expiry)
            conn.execute(
                "UPDATE applications SET state = ?, hold_expires_at = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (ApplicationState.HELD.value, to_iso(expiry), to_iso(self._now()), app["id"]),
            )
            self._audit(conn, app["id"], "request_hold", actor,
                        {"expires_at": to_iso(expiry), "plan": self._plan_view(plan)})
            return self._app_view(conn, self._get_app(conn, app["id"]))

    def confirm(self, actor: Actor, application_id: str) -> dict:
        """材料齐备且核验通过后才确认：暂占 -> 确认，单事务。"""
        self._require(actor, Role.COORDINATOR)
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            if app["state"] != ApplicationState.HELD.value:
                raise StateError(f"当前状态 {app['state']} 不能确认")
            now = self._now()
            if parse_iso(app["material_deadline"]) <= now:
                raise StateError("材料截止时间已过，无法确认，请先改期")
            if app["hold_expires_at"] and parse_iso(app["hold_expires_at"]) <= now:
                raise StateError("暂占已逾期，请重新申请暂占")
            missing = [
                kind
                for kind in REQUIRED_MATERIALS
                if conn.execute(
                    "SELECT 1 FROM materials WHERE application_id = ? AND kind = ? "
                    "AND verified = 1",
                    (application_id, kind),
                ).fetchone()
                is None
            ]
            if missing:
                raise StateError(
                    "材料未齐备，不能确认",
                    detail={"missing_materials": missing},
                )
            conn.execute(
                "UPDATE holds SET state = ?, expires_at = NULL, updated_at = ? "
                "WHERE application_id = ? AND state = ?",
                (HoldState.CONFIRMED.value, to_iso(now), application_id,
                 HoldState.TENTATIVE.value),
            )
            conn.execute(
                "UPDATE applications SET state = ?, hold_expires_at = NULL, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (ApplicationState.CONFIRMED.value, to_iso(now), application_id),
            )
            self._audit(conn, application_id, "confirm", actor)
            return self._app_view(conn, self._get_app(conn, application_id))

    # ------------------------------------------------------------ 原子调整

    def _release_active_holds(self, conn, application_id: str,
                              target: str = HoldState.RELEASED.value):
        conn.execute(
            "UPDATE holds SET state = ?, expires_at = NULL, updated_at = ? "
            "WHERE application_id = ? AND state IN (?, ?)",
            (target, to_iso(self._now()), application_id, *ACTIVE_HOLD_STATES),
        )

    def reschedule(self, actor: Actor, application_id: str,
                   visit_start: str, visit_end: str) -> dict:
        """改期：新窗口可行才替换；旧占用释放与新暂占建立在同一事务。

        - 准备态：仅调整窗口与材料截止（尚无占用），便于冲突后改用备选窗口；
        - 暂占态：旧暂占释放 + 新暂占建立，原子完成；
        - 确认态：材料已核验，新占用直接保持确认。
        """
        self._require(actor, Role.COORDINATOR)
        start, end = parse_iso(visit_start), parse_iso(visit_end)
        if end <= start:
            raise ValidationError("visit_end 必须晚于 visit_start")
        if start <= self._now():
            raise ValidationError("新的访问开始时间必须在未来")
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            if app["state"] not in (
                ApplicationState.PREPARING.value,
                ApplicationState.HELD.value,
                ApplicationState.CONFIRMED.value,
            ):
                raise StateError(f"当前状态 {app['state']} 不能改期")
            host = self._get_institution(conn, app["host_institution_id"])
            new_deadline = material_deadline(start, host["timezone"], self.material_days_before)
            if new_deadline <= self._now():
                raise ConflictError(
                    "新窗口的材料截止时间已过",
                    conflicts=[{"resource": "material_deadline",
                                "detail": f"新窗口材料截止 {to_iso(new_deadline)} 已过"}],
                    alternatives=[],
                )
            plan = self._plan_or_raise(conn, app, host, start=start, end=end)
            was_confirmed = app["state"] == ApplicationState.CONFIRMED.value
            # 原子替换：先释放旧占用，再建立新占用，同一事务提交
            self._release_active_holds(conn, app["id"])
            if was_confirmed:
                # 材料已核验，改期后直接保持确认态
                self._insert_holds(conn, app["id"], plan, HoldState.CONFIRMED.value, None)
                new_state, expiry = ApplicationState.CONFIRMED.value, None
            elif app["state"] == ApplicationState.HELD.value:
                expiry = min(self._now() + self.hold_ttl, new_deadline)
                self._insert_holds(conn, app["id"], plan, HoldState.TENTATIVE.value, expiry)
                new_state = ApplicationState.HELD.value
            else:  # 准备态：尚无占用，仅调整窗口与截止
                new_state, expiry = ApplicationState.PREPARING.value, None
            conn.execute(
                "UPDATE applications SET visit_start = ?, visit_end = ?, state = ?, "
                "material_deadline = ?, hold_expires_at = ?, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (
                    to_iso(start), to_iso(end), new_state, to_iso(new_deadline),
                    to_iso(expiry) if expiry else None, to_iso(self._now()), app["id"],
                ),
            )
            self._audit(conn, app["id"], "reschedule", actor,
                        {"visit_start": to_iso(start), "visit_end": to_iso(end),
                         "kept_confirmed": was_confirmed})
            return self._app_view(conn, self._get_app(conn, app["id"]))

    def substitute_teacher(self, actor: Actor, application_id: str,
                           new_teacher_id: str) -> dict:
        """替代教师：资格校验 + 换教师 + 身份材料重置 + 占用回退暂占，单事务。"""
        self._require(actor, Role.COORDINATOR)
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            if app["state"] not in (
                ApplicationState.PREPARING.value,
                ApplicationState.HELD.value,
                ApplicationState.CONFIRMED.value,
            ):
                raise StateError(f"当前状态 {app['state']} 不能更换教师")
            old_teacher = self._get_teacher(conn, app["teacher_id"])
            new_teacher = self._get_teacher(conn, new_teacher_id)
            if not new_teacher["active"]:
                raise EligibilityError("替代教师不在职")
            if new_teacher["home_institution_id"] != old_teacher["home_institution_id"]:
                raise EligibilityError("替代教师必须来自同一派出院校")
            if new_teacher["subject"] != old_teacher["subject"]:
                raise EligibilityError("替代教师学科须与课程需求一致")
            host = self._get_institution(conn, app["host_institution_id"])
            # 替代教师本人的行程（含缓冲）不得与本访学窗口冲突
            _, conflicts = plan_window(
                conn,
                host=host,
                teacher_id=new_teacher_id,
                visit_start=parse_iso(app["visit_start"]),
                visit_end=parse_iso(app["visit_end"]),
                sessions_per_week=app["sessions_per_week"],
                buffer_days=app["buffer_days"],
                exclude_application_id=app["id"],
            )
            teacher_conflicts = [c for c in conflicts if c.resource == "teacher_schedule"]
            if teacher_conflicts:
                raise ConflictError(
                    "替代教师行程冲突",
                    conflicts=[c.as_dict() for c in teacher_conflicts],
                    alternatives=[],
                )

            now = self._now()
            # 身份材料属于原教师：换人即作废，需重新提交核验
            conn.execute("DELETE FROM materials WHERE application_id = ?", (app["id"],))
            new_state = app["state"]
            hold_expires_at = app["hold_expires_at"]
            if app["state"] in (ApplicationState.HELD.value, ApplicationState.CONFIRMED.value):
                deadline = parse_iso(app["material_deadline"])
                expiry = min(now + self.hold_ttl, deadline)
                if expiry <= now:
                    raise StateError("材料截止时间已过，无法为替代教师保留资源，请先改期")
                # 资源保留但回退为暂占，待新材料齐备后重新确认
                conn.execute(
                    "UPDATE holds SET state = ?, expires_at = ?, updated_at = ? "
                    "WHERE application_id = ? AND state IN (?, ?)",
                    (HoldState.TENTATIVE.value, to_iso(expiry), to_iso(now),
                     app["id"], *ACTIVE_HOLD_STATES),
                )
                new_state = ApplicationState.HELD.value
                hold_expires_at = to_iso(expiry)
            conn.execute(
                "UPDATE applications SET teacher_id = ?, state = ?, hold_expires_at = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (new_teacher_id, new_state, hold_expires_at, to_iso(now), app["id"]),
            )
            self._audit(conn, app["id"], "substitute_teacher", actor,
                        {"from": old_teacher["id"], "to": new_teacher_id,
                         "materials_reset": True})
            return self._app_view(conn, self._get_app(conn, app["id"]))

    def cancel(self, actor: Actor, application_id: str, reason: str = "") -> dict:
        """取消：释放全部有效占用，单事务。"""
        with self.store.tx() as conn:
            app = self._get_app(conn, application_id)
            teacher = self._get_teacher(conn, app["teacher_id"])
            allowed = (
                actor.role == Role.COORDINATOR
                or (actor.role == Role.HOME_INSTITUTION
                    and actor.institution_id == teacher["home_institution_id"])
            )
            if not allowed:
                raise ForbiddenError("只有协调员或派出院校可以取消申请")
            if app["state"] in TERMINAL_STATES:
                raise StateError(f"申请已处于终态 {app['state']}")
            self._release_active_holds(conn, app["id"])
            self._set_state(conn, app, ApplicationState.CANCELLED.value, clear_hold=True)
            self._audit(conn, application_id, "cancel", actor, {"reason": reason})
            return self._app_view(conn, self._get_app(conn, application_id))

    # ------------------------------------------------------------ 逾期与恢复

    def _set_state(self, conn, app, state: str, *, clear_hold: bool = False):
        conn.execute(
            "UPDATE applications SET state = ?, hold_expires_at = "
            + ("NULL" if clear_hold else "hold_expires_at")
            + ", version = version + 1, updated_at = ? WHERE id = ?",
            (state, to_iso(self._now()), app["id"]),
        )

    def sweep(self, now: datetime | None = None, actor: Actor | None = None) -> dict:
        """逾期清理 + 访问状态推进，单事务；可由定时任务或恢复流程调用。"""
        now = now or self._now()
        report = {"expired_holds": 0, "reverted_applications": [],
                  "visiting": [], "completed": []}
        with self.store.tx() as conn:
            # 1) 逾期暂占释放：占用记录过期，申请回退到“准备”
            expired_apps = conn.execute(
                "SELECT DISTINCT application_id FROM holds WHERE state = ? AND expires_at <= ?",
                (HoldState.TENTATIVE.value, to_iso(now)),
            ).fetchall()
            for row in expired_apps:
                app = self._get_app(conn, row["application_id"])
                if app["state"] != ApplicationState.HELD.value:
                    continue
                count = conn.execute(
                    "UPDATE holds SET state = ?, expires_at = NULL, updated_at = ? "
                    "WHERE application_id = ? AND state = ?",
                    (HoldState.EXPIRED.value, to_iso(now), app["id"],
                     HoldState.TENTATIVE.value),
                ).rowcount
                conn.execute(
                    "UPDATE applications SET state = ?, hold_expires_at = NULL, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    (ApplicationState.PREPARING.value, to_iso(now), app["id"]),
                )
                self._audit(conn, app["id"], "expire_holds", actor,
                            {"released_holds": count})
                report["expired_holds"] += count
                report["reverted_applications"].append(app["id"])

            # 2) 到访开始：确认 -> 访问
            for row in conn.execute(
                "SELECT id FROM applications WHERE state = ? AND visit_start <= ?",
                (ApplicationState.CONFIRMED.value, to_iso(now)),
            ).fetchall():
                conn.execute(
                    "UPDATE applications SET state = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ?",
                    (ApplicationState.VISITING.value, to_iso(now), row["id"]),
                )
                self._audit(conn, row["id"], "begin_visit", actor)
                report["visiting"].append(row["id"])

            # 3) 访问结束：访问 -> 完成，释放确认态占用（配额/宿舍回收）
            for row in conn.execute(
                "SELECT id FROM applications WHERE state = ? AND visit_end <= ?",
                (ApplicationState.VISITING.value, to_iso(now)),
            ).fetchall():
                conn.execute(
                    "UPDATE holds SET state = ?, updated_at = ? "
                    "WHERE application_id = ? AND state = ?",
                    (HoldState.RELEASED.value, to_iso(now), row["id"],
                     HoldState.CONFIRMED.value),
                )
                conn.execute(
                    "UPDATE applications SET state = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ?",
                    (ApplicationState.COMPLETED.value, to_iso(now), row["id"]),
                )
                self._audit(conn, row["id"], "complete_visit", actor)
                report["completed"].append(row["id"])
        return report

    def recover(self, now: datetime | None = None) -> dict:
        """进程恢复：启动时调用，继续清理遗留暂占并修复不一致状态。"""
        now = now or self._now()
        report = {"sweep": self.sweep(now), "orphan_held_applications": []}
        with self.store.tx() as conn:
            # 防御：处于“暂占”但已无任何有效占用的申请（上次中断的残留）回退到“准备”
            orphans = conn.execute(
                "SELECT a.id FROM applications a WHERE a.state = ? AND NOT EXISTS ("
                "  SELECT 1 FROM holds h WHERE h.application_id = a.id "
                "  AND h.state IN (?, ?))",
                (ApplicationState.HELD.value, *ACTIVE_HOLD_STATES),
            ).fetchall()
            for row in orphans:
                conn.execute(
                    "UPDATE applications SET state = ?, hold_expires_at = NULL, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    (ApplicationState.PREPARING.value, to_iso(now), row["id"]),
                )
                self._audit(conn, row["id"], "recover_orphan_hold", None)
                report["orphan_held_applications"].append(row["id"])
        return report
