"""跨时区时间工具：统一以 UTC 存储，按接收院校当地时区计算截止时间。"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: str) -> datetime:
    """解析 ISO 时间；缺省时区按 UTC 处理，返回值统一为 UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def material_deadline(
    visit_start: datetime,
    tz_name: str,
    days_before: int,
    cutoff: time = time(17, 0),
) -> datetime:
    """材料截止时刻：访问开始前 N 天、接收院校当地 cutoff 时刻（默认 17:00）。

    返回 UTC 时刻。例如访问 11-02 09:00（柏林，UTC+1），提前 10 天，
    截止为 10-23 17:00+01:00，即 10-23T16:00:00Z。
    """
    local = visit_start.astimezone(ZoneInfo(tz_name))
    day = (local - timedelta(days=days_before)).date()
    return datetime.combine(day, cutoff, tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
