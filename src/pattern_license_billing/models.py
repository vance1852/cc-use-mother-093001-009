"""授权计费服务在模块边界使用的不可变数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    """运营方、审计人员、合作方或权利人侧的 API 身份。"""

    principal_id: str
    kind: str
    display_name: str
    licensee_id: str | None
    holder_id: str | None
    active: bool


@dataclass(frozen=True)
class WriteReceipt:
    """幂等写入的稳定回执。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Tier:
    """一档阶梯费率：含税销售额达到 lower_bound_cents 后适用的基点费率。"""

    tier_no: int
    lower_bound_cents: int
    rate_basis_points: int


@dataclass(frozen=True)
class BillingEntryView:
    """计费分录的完整视图，含分账明细与金额来源。"""

    entry_id: str
    entry_type: str
    period_key: str
    origin_period_key: str
    license_id: str | None
    licensee_id: str | None
    work_id: str | None
    work_version_id: str | None
    rate_version_id: str | None
    ent_version_id: str | None
    fact_id: str | None
    source_entry_id: str | None
    claim_id: str | None
    guarantee_id: str | None
    gross_cents: int
    deduction_cents: int
    base_cents: int
    rate_basis_points: int
    amount_cents: int
    status: str
    created_by: str
    created_at: str
    allocations: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class UsageFactView:
    """使用事实及其匹配结果。"""

    fact_id: str
    batch_id: str
    licensee_id: str
    period_key: str
    source_record_key: str
    work_id: str | None
    work_version_id: str | None
    license_id: str | None
    rate_version_id: str | None
    territory: str
    channel: str
    usage_purpose: str
    quantity: int
    gross_revenue_cents: int
    occurred_at: str
    status: str
    match_note: str
