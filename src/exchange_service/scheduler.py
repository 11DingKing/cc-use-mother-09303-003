"""统一排程器：把课程需求、可用时段、行程缓冲、接待配额放进同一次可行性评估。

评估结果为 HoldPlan（可行）或冲突列表；冲突时由 find_alternatives 给出
前后平移的可选窗口。所有函数只读，不写库，写库由 service 在事务内完成。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import ACTIVE_HOLD_STATES, TERMINAL_STATES, ResourceType
from .timeutil import parse_iso, to_iso

WEEKDAYS = range(5)    # 周一至周五
PERIODS = range(1, 7)  # 每日 6 个授课时段


@dataclass
class HoldPlan:
    """一个可行窗口对应的资源方案。"""

    visit_start: datetime
    visit_end: datetime
    buffer_days: int
    course_slots: list[str]
    dorm_unit_id: str
    quota_ref: str

    @property
    def dorm_window(self) -> tuple[datetime, datetime]:
        """宿舍占用窗口 = 访问窗口 + 前后行程缓冲。"""
        return (
            self.visit_start - timedelta(days=self.buffer_days),
            self.visit_end + timedelta(days=self.buffer_days),
        )


@dataclass
class Conflict:
    resource: str
    detail: str

    def as_dict(self) -> dict:
        return {"resource": self.resource, "detail": self.detail}


def slot_ref(institution_id: str, weekday: int, period: int) -> str:
    return f"{institution_id}:w{weekday}p{period}"


def quota_ref(institution_id: str) -> str:
    return f"{institution_id}:quota"


def _weeks_covering(start: datetime, end: datetime):
    """访问窗口跨越的每一个自然周（以周一为界）。"""
    week = (start - timedelta(days=start.weekday())).date()
    last = (end - timedelta(days=end.weekday())).date()
    while week <= last:
        yield week
        week += timedelta(days=7)


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def plan_window(
    conn: sqlite3.Connection,
    *,
    host: sqlite3.Row,
    teacher_id: str,
    visit_start: datetime,
    visit_end: datetime,
    sessions_per_week: int,
    buffer_days: int,
    exclude_application_id: str | None = None,
) -> tuple[HoldPlan | None, list[Conflict]]:
    """评估一个访问窗口；可行返回 (HoldPlan, [])，否则返回 (None, 冲突列表)。"""
    conflicts: list[Conflict] = []
    exclude = exclude_application_id or ""
    host_id = host["id"]

    # 1) 教师行程：本人其他在办访问（含行程缓冲）不得重叠
    my_buffered = (visit_start - timedelta(days=buffer_days), visit_end + timedelta(days=buffer_days))
    rows = conn.execute(
        "SELECT id, visit_start, visit_end, buffer_days FROM applications "
        "WHERE teacher_id = ? AND id <> ? AND state NOT IN ({})".format(
            ",".join("?" for _ in TERMINAL_STATES)
        ),
        (teacher_id, exclude, *TERMINAL_STATES),
    ).fetchall()
    for row in rows:
        other = (
            parse_iso(row["visit_start"]) - timedelta(days=row["buffer_days"]),
            parse_iso(row["visit_end"]) + timedelta(days=row["buffer_days"]),
        )
        if _overlap(my_buffered[0], my_buffered[1], other[0], other[1]):
            conflicts.append(Conflict(
                "teacher_schedule",
                f"教师在申请 {row['id']} 中的访学行程（含缓冲）与本窗口重叠",
            ))

    # 2) 课程需求：访问覆盖的每一周都要有足够的可用授课时段
    committed = {
        (r["weekday"], r["period"])
        for r in conn.execute(
            "SELECT weekday, period FROM course_commitments WHERE institution_id = ?",
            (host_id,),
        )
    }
    held_slots = {
        r["resource_ref"]
        for r in conn.execute(
            "SELECT resource_ref FROM holds "
            "WHERE resource_type = ? AND state IN (?, ?) AND application_id <> ? "
            "AND window_start < ? AND window_end > ?",
            (
                ResourceType.COURSE_SLOT.value,
                *ACTIVE_HOLD_STATES,
                exclude,
                to_iso(visit_end),
                to_iso(visit_start),
            ),
        )
    }
    course_slots: list[str] = []
    for week in _weeks_covering(visit_start, visit_end):
        free = [
            (w, p)
            for w in WEEKDAYS
            for p in PERIODS
            if (w, p) not in committed and slot_ref(host_id, w, p) not in held_slots
        ]
        if len(free) < sessions_per_week:
            conflicts.append(Conflict(
                "course_slot",
                f"{week.isoformat()} 当周课程需求 {sessions_per_week} 节，"
                f"可用时段仅剩 {len(free)} 个",
            ))
        else:
            course_slots.extend(slot_ref(host_id, w, p) for w, p in free[:sessions_per_week])
    # 同一每周时段跨多周只保留一条占用记录
    course_slots = list(dict.fromkeys(course_slots))

    # 3) 接待宿舍：占用窗口含行程缓冲
    dorm_start, dorm_end = my_buffered
    dorm_unit_id = None
    for unit in conn.execute(
        "SELECT id FROM dorm_units WHERE institution_id = ? ORDER BY id", (host_id,)
    ):
        clash = conn.execute(
            "SELECT 1 FROM holds WHERE resource_type = ? AND resource_ref = ? "
            "AND state IN (?, ?) AND application_id <> ? "
            "AND window_start < ? AND window_end > ? LIMIT 1",
            (
                ResourceType.DORM_UNIT.value,
                unit["id"],
                *ACTIVE_HOLD_STATES,
                exclude,
                to_iso(dorm_end),
                to_iso(dorm_start),
            ),
        ).fetchone()
        if clash is None:
            dorm_unit_id = unit["id"]
            break
    if dorm_unit_id is None:
        conflicts.append(Conflict(
            "dorm_unit",
            f"行程缓冲窗口 {to_iso(dorm_start)} ~ {to_iso(dorm_end)} 内接待宿舍已满",
        ))

    # 4) 接待配额：同时在访人数（暂占与确认都计入）
    overlapping = conn.execute(
        "SELECT COUNT(DISTINCT application_id) AS n FROM holds "
        "WHERE resource_type = ? AND resource_ref = ? AND state IN (?, ?) "
        "AND application_id <> ? AND window_start < ? AND window_end > ?",
        (
            ResourceType.RECEPTION_QUOTA.value,
            quota_ref(host_id),
            *ACTIVE_HOLD_STATES,
            exclude,
            to_iso(visit_end),
            to_iso(visit_start),
        ),
    ).fetchone()["n"]
    if overlapping >= host["reception_quota"]:
        conflicts.append(Conflict(
            "reception_quota",
            f"接待配额已满（{overlapping}/{host['reception_quota']}）",
        ))

    if conflicts:
        return None, conflicts
    return (
        HoldPlan(
            visit_start=visit_start,
            visit_end=visit_end,
            buffer_days=buffer_days,
            course_slots=course_slots,
            dorm_unit_id=dorm_unit_id,
            quota_ref=quota_ref(host_id),
        ),
        [],
    )


def find_alternatives(
    conn: sqlite3.Connection,
    *,
    not_before: datetime,
    max_shift_days: int = 14,
    limit: int = 3,
    **kwargs,
) -> list[HoldPlan]:
    """冲突时的可选方案：窗口整体前后平移（更早优先），逐个重新评估。"""
    visit_start: datetime = kwargs["visit_start"]
    visit_end: datetime = kwargs["visit_end"]
    found: list[HoldPlan] = []
    for step in range(1, max_shift_days + 1):
        for delta in (-step, step):
            start = visit_start + timedelta(days=delta)
            end = visit_end + timedelta(days=delta)
            if start < not_before:
                continue
            plan, _ = plan_window(conn, **{**kwargs, "visit_start": start, "visit_end": end})
            if plan is not None:
                found.append(plan)
                if len(found) >= limit:
                    return found
    return found
