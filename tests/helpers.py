"""测试公共装置：固定时钟、种子数据与常用操作者。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exchange_service.models import Actor, Role
from exchange_service.seed import seed
from exchange_service.service import ExchangeService
from exchange_service.store import Store

T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)

COORD = Actor(Role.COORDINATOR, "coord-1")
HOME = Actor(Role.HOME_INSTITUTION, "officer-cn", institution_id="CN-UNI")
HOST = Actor(Role.HOST_INSTITUTION, "officer-de", institution_id="DE-UNI")
TEACHER_WANG = Actor(Role.TEACHER, "T-WANG")

W1_START = "2026-11-02T08:00:00+00:00"
W1_END = "2026-11-06T16:00:00+00:00"
W2_START = "2026-11-09T08:00:00+00:00"
W2_END = "2026-11-13T16:00:00+00:00"
W3_START = "2026-11-16T08:00:00+00:00"
W3_END = "2026-11-20T16:00:00+00:00"


class FakeClock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_service(db_path=":memory:", *, hold_ttl=timedelta(hours=72), clock=None):
    store = Store(db_path)
    seed(store)
    return ExchangeService(store, hold_ttl=hold_ttl, clock=clock or FakeClock())


def make_application(service, teacher="T-WANG", start=W1_START, end=W1_END,
                     sessions=2, buffer_days=1):
    app = service.create_invitation(
        COORD,
        teacher_id=teacher,
        host_institution_id="DE-UNI",
        visit_start=start,
        visit_end=end,
        sessions_per_week=sessions,
        buffer_days=buffer_days,
    )
    return service.start_preparation(COORD, app["id"])


def prepare_materials(service, app_id, kinds=("passport", "visa", "invitation_letter")):
    for kind in kinds:
        service.submit_material(HOME, app_id, kind, f"{kind}-扫描件-001")
        service.verify_material(COORD, app_id, kind)


def confirm_application(service, app_id):
    prepare_materials(service, app_id)
    service.request_hold(COORD, app_id)
    return service.confirm(COORD, app_id)
