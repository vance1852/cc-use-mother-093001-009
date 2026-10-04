"""纹样授权计费结算服务的业务异常。"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号、业务唯一键或处理顺序与既有状态冲突。"""

    code = "conflict"
    status = 409


class ClosedPeriodError(ConflictError):
    """目标期间已经关账，不允许直接改写。"""

    code = "period_closed"


class OutOfOrderError(ConflictError):
    """队列要求按顺序推进，前序项目尚未完成。"""

    code = "out_of_order"
