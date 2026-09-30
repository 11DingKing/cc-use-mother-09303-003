"""服务端领域错误：API 层据此映射 HTTP 状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类，携带稳定的错误码与 HTTP 状态。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ForbiddenError(DomainError):
    code = "forbidden"
    http_status = 403


class StateError(DomainError):
    """状态机不允许的迁移（如材料未齐就确认）。"""

    code = "invalid_state"
    http_status = 409


class EligibilityError(DomainError):
    """邀请资格或替代教师资格不满足。"""

    code = "ineligible"
    http_status = 422


class ConflictError(DomainError):
    """资源冲突：携带冲突明细与可选方案。"""

    code = "resource_conflict"
    http_status = 409

    def __init__(self, message: str, *, conflicts: list[dict], alternatives: list[dict]):
        super().__init__(message, detail={"conflicts": conflicts, "alternatives": alternatives})
        self.conflicts = conflicts
        self.alternatives = alternatives
