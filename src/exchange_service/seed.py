"""演示/开发用种子数据：两所院校、教师、宿舍与既有授课安排。"""
from __future__ import annotations

from .store import Store

INSTITUTIONS = [
    ("CN-UNI", "华东示范大学", "Asia/Shanghai", 2),
    ("DE-UNI", "柏林应用科技大学", "Europe/Berlin", 1),
]

TEACHERS = [
    ("T-WANG", "CN-UNI", "王老师", "数学", 1),
    ("T-LI", "CN-UNI", "李老师", "数学", 1),
    ("T-ZHAO", "CN-UNI", "赵老师", "物理", 1),
]

DORM_UNITS = [
    ("DORM-DE-1", "DE-UNI", "接待公寓 1 号"),
    ("DORM-CN-1", "CN-UNI", "专家楼 101"),
    ("DORM-CN-2", "CN-UNI", "专家楼 102"),
]

# 接收院校既有授课安排（每周循环），占用对应课程时段
COURSE_COMMITMENTS = [
    ("CMT-1", "DE-UNI", 0, 1, "本校本科课程"),
    ("CMT-2", "DE-UNI", 0, 2, "本校本科课程"),
    ("CMT-3", "DE-UNI", 1, 1, "合作院校联合授课"),
    ("CMT-4", "DE-UNI", 2, 3, "研究生研讨"),
]


def seed(store: Store) -> None:
    """幂等写入种子数据。"""
    with store.tx() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO institutions(id, name, timezone, reception_quota) "
            "VALUES (?,?,?,?)",
            INSTITUTIONS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO teachers(id, home_institution_id, name, subject, active) "
            "VALUES (?,?,?,?,?)",
            TEACHERS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO dorm_units(id, institution_id, label) VALUES (?,?,?)",
            DORM_UNITS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO course_commitments(id, institution_id, weekday, period, label) "
            "VALUES (?,?,?,?,?)",
            COURSE_COMMITMENTS,
        )
