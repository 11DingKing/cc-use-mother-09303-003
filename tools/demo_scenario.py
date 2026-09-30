"""场景演示：访学日期临时提前引发的多资源冲突与协调处置。

复现：李老师访学提前，与接收院校既有安排、接待宿舍、接待配额冲突；
签证材料未齐但名额已被暂占；随后演示备选方案、改期、确认、
替代教师、逾期释放与进程恢复清理。
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from exchange_service.errors import ConflictError, StateError  # noqa: E402
from exchange_service.service import ExchangeService  # noqa: E402
from exchange_service.store import Store  # noqa: E402
from helpers import (  # noqa: E402
    COORD,
    HOME,
    FakeClock,
    confirm_application,
    make_application,
    make_service,
    prepare_materials,
)


def show(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    clock = FakeClock()
    service = make_service(hold_ttl=timedelta(hours=48), clock=clock)

    # 1) 王老师已确认 11-02 ~ 11-06 访学（占满宿舍与接待配额）
    first = make_application(service, teacher="T-WANG")
    confirm_application(service, first["id"])
    show("王老师访学已确认", service.get_application(COORD, first["id"]))

    # 2) 李老师行程临时提前到同一窗口：暂占冲突，返回可选方案
    second = make_application(service, teacher="T-LI")
    try:
        service.request_hold(COORD, second["id"])
    except ConflictError as exc:
        show("暂占冲突（宿舍 + 接待配额），返回可选方案", exc.detail)
        alt = exc.alternatives[0]
        # 3) 采纳备选方案改期，再暂占
        service.reschedule(COORD, second["id"],
                           visit_start=alt["visit_start"], visit_end=alt["visit_end"])
        show("改期到备选窗口后暂占成功", service.request_hold(COORD, second["id"]))

    # 4) 签证未齐：名额已暂占但无法确认
    prepare_materials(service, second["id"], kinds=("passport", "invitation_letter"))
    try:
        service.confirm(COORD, second["id"])
    except StateError as exc:
        show("签证未齐，确认被拒", exc.detail)

    # 5) 补齐签证后确认
    service.submit_material(HOME, second["id"], "visa", "签证页-2026")
    service.verify_material(COORD, second["id"], "visa")
    show("签证齐备后确认", service.confirm(COORD, second["id"]))

    # 6) 替代教师：换人、身份材料重置、占用回退暂占（同一事务）
    show("替代教师（李老师 -> 王老师，窗口不重叠）",
         service.substitute_teacher(COORD, second["id"], "T-WANG"))

    # 7) 逾期释放：暂占 TTL 到期未确认，资源自动回收
    clock.advance(days=3)
    show("逾期释放（暂占回退为准备）", service.sweep())

    # 8) 进程恢复：重启后继续清理上次遗留的暂占
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "exchange.db")
        clock2 = FakeClock()
        service_a = make_service(db, hold_ttl=timedelta(hours=1), clock=clock2)
        stale = make_application(service_a, teacher="T-WANG")
        service_a.request_hold(COORD, stale["id"])
        service_a.store.close()
        clock2.advance(hours=2)  # 暂占已过期，进程“重启”
        store_b = Store(db)
        recovered = ExchangeService(store_b, clock=clock2).recover()
        show("进程恢复后继续清理遗留暂占", recovered)
        store_b.close()


if __name__ == "__main__":
    main()
