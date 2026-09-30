"""领域模型：角色、状态机、资源类型与材料类别。

状态机与 domain/contract.json 对齐：邀请 -> 准备 -> 暂占 -> 确认 -> 访问，
另有终态 完成 / 取消 / 逾期。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    COORDINATOR = "coordinator"              # 国际交流专员（互访协调员）
    HOME_INSTITUTION = "home_institution"    # 派出院校
    HOST_INSTITUTION = "host_institution"    # 接收院校
    TEACHER = "teacher"                      # 访学教师本人


@dataclass(frozen=True)
class Actor:
    """一次操作的身份：角色 + 操作者 ID + 所属院校（院校角色必填）。"""

    role: Role
    actor_id: str
    institution_id: str | None = None


class ApplicationState(str, Enum):
    INVITED = "邀请"
    PREPARING = "准备"
    HELD = "暂占"
    CONFIRMED = "确认"
    VISITING = "访问"
    COMPLETED = "完成"
    CANCELLED = "取消"
    EXPIRED = "逾期"


TERMINAL_STATES = (
    ApplicationState.COMPLETED.value,
    ApplicationState.CANCELLED.value,
    ApplicationState.EXPIRED.value,
)


class HoldState(str, Enum):
    TENTATIVE = "tentative"    # 暂占中，带过期时间
    CONFIRMED = "confirmed"    # 材料齐备后确认
    RELEASED = "released"      # 主动释放（改期/取消/完成）
    EXPIRED = "expired"        # 逾期释放


ACTIVE_HOLD_STATES = (HoldState.TENTATIVE.value, HoldState.CONFIRMED.value)


class ResourceType(str, Enum):
    COURSE_SLOT = "course_slot"            # 课程时段（接收院校教学日历）
    DORM_UNIT = "dorm_unit"                # 接待宿舍（含行程缓冲）
    RECEPTION_QUOTA = "reception_quota"    # 接待配额（同时在访人数上限）


class MaterialKind(str, Enum):
    PASSPORT = "passport"                  # 护照
    VISA = "visa"                          # 签证
    INVITATION_LETTER = "invitation_letter"  # 邀请函
    HEALTH_CERT = "health_cert"            # 健康证明


# 确认前必须核验齐备的材料（签证正是场景中的卡点）
REQUIRED_MATERIALS = (
    MaterialKind.PASSPORT.value,
    MaterialKind.VISA.value,
    MaterialKind.INVITATION_LETTER.value,
)
