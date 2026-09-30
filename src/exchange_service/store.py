"""SQLite 持久化：所有多步写入都在单个事务中完成，保证原子性。

- `tx()` 使用 BEGIN IMMEDIATE，读写串行化，异常即整体回滚；
- 改期、取消、替代教师、逾期释放等多表调整都经由 `tx()` 一次提交；
- WAL 模式保证进程崩溃后已提交数据不丢，配合 recovery 清理遗留暂占。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    timezone        TEXT NOT NULL,          -- IANA 时区，用于跨时区截止计算
    reception_quota INTEGER NOT NULL DEFAULT 1  -- 同时在访人数上限
);

CREATE TABLE IF NOT EXISTS teachers (
    id                  TEXT PRIMARY KEY,
    home_institution_id TEXT NOT NULL REFERENCES institutions(id),
    name                TEXT NOT NULL,
    subject             TEXT NOT NULL,      -- 学科，用于课程需求匹配
    active              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dorm_units (
    id             TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    label          TEXT NOT NULL
);

-- 接收院校既有授课安排（每周循环），占用对应课程时段
CREATE TABLE IF NOT EXISTS course_commitments (
    id             TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    weekday        INTEGER NOT NULL,        -- 0=周一 .. 6=周日
    period         INTEGER NOT NULL,        -- 每日第几时段
    label          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id                   TEXT PRIMARY KEY,
    teacher_id           TEXT NOT NULL REFERENCES teachers(id),
    host_institution_id  TEXT NOT NULL REFERENCES institutions(id),
    visit_start          TEXT NOT NULL,     -- UTC ISO
    visit_end            TEXT NOT NULL,     -- UTC ISO
    buffer_days          INTEGER NOT NULL,  -- 行程缓冲（前后各 N 天）
    sessions_per_week    INTEGER NOT NULL,  -- 课程需求：每周授课节数
    state                TEXT NOT NULL,     -- 邀请/准备/暂占/确认/访问/完成/取消/逾期
    material_deadline    TEXT NOT NULL,     -- 材料截止（接收院校当地 17:00，存 UTC）
    hold_expires_at      TEXT,              -- 暂占过期时间；确认后为空
    version              INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- 身份材料：内容按角色隔离，接收院校只能看到清单状态
CREATE TABLE IF NOT EXISTS materials (
    id                TEXT PRIMARY KEY,
    application_id    TEXT NOT NULL REFERENCES applications(id),
    kind              TEXT NOT NULL,
    content           TEXT NOT NULL,        -- 材料内容/编号，仅授权角色可读
    submitted_by_role TEXT NOT NULL,
    submitted_by      TEXT NOT NULL,
    submitted_at      TEXT NOT NULL,
    verified          INTEGER NOT NULL DEFAULT 0,
    verified_by       TEXT,
    verified_at       TEXT,
    UNIQUE(application_id, kind)
);

-- 资源占用记录：一条申请对应多条（课程时段若干 + 宿舍 + 配额）
CREATE TABLE IF NOT EXISTS holds (
    id              TEXT PRIMARY KEY,
    application_id  TEXT NOT NULL REFERENCES applications(id),
    resource_type   TEXT NOT NULL,          -- course_slot / dorm_unit / reception_quota
    resource_ref    TEXT NOT NULL,          -- 课程时段 "院校:w{周几}p{时段}"；宿舍为单元 ID；配额为 "院校:quota"
    window_start    TEXT NOT NULL,          -- UTC ISO
    window_end      TEXT NOT NULL,          -- UTC ISO
    state           TEXT NOT NULL,          -- tentative/confirmed/released/expired
    expires_at      TEXT,                   -- 暂占过期时间
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holds_resource ON holds(resource_type, resource_ref, state);
CREATE INDEX IF NOT EXISTS idx_holds_app ON holds(application_id, state);

-- 审计日志：与业务写入同事务提交，用于核对原子调整
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT,
    action         TEXT NOT NULL,
    detail         TEXT NOT NULL,           -- JSON
    actor          TEXT NOT NULL,
    at             TEXT NOT NULL
);
"""


class Store:
    """SQLite 存储：事务边界即原子性边界。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE ... COMMIT / ROLLBACK。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @contextmanager
    def read(self):
        with self._lock:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()
