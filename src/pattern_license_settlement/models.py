"""定义纹样授权计费结算服务的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """具有明确角色的操作者；合作方与权利人通过 party_id 与组织绑定。"""

    actor_id: str
    display_name: str
    role: str
    party_id: str
    active: bool


@dataclass(frozen=True)
class WriteReceipt:
    """一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Work:
    """授权纹样作品。"""

    work_id: str
    title: str
    active: bool


@dataclass(frozen=True)
class WorkVersion:
    """作品的授权条款版本；费率与份额按版本冻结，旧版本继续用于历史重算。"""

    version_id: str
    work_id: str
    version_no: int
    licensor_party_id: str
    rate_card_id: str
    shares: dict[str, int]
    effective_from: str
    effective_to: str | None
    active: bool


@dataclass(frozen=True)
class RateTier:
    """阶梯费率中的一档：达到 from_qty 起适用每单位 rate_cents 分。"""

    from_qty: int
    rate_cents: int


@dataclass(frozen=True)
class RateCard:
    """费率卡：按用途给出阶梯费率与税前扣减规则（固定额或比例基点）。"""

    rate_card_id: str
    currency: str
    tiers_by_use: dict[str, list[RateTier]]
    deduction: dict[str, Any]


@dataclass(frozen=True)
class Licensee:
    """被许可方；同一 controlling_party_id 下的报送在阶梯判断时合并。"""

    licensee_id: str
    name: str
    controlling_party_id: str
    active: bool


@dataclass(frozen=True)
class License:
    """许可证：作品版本、被许可方、地域渠道用途范围、有效期与保底金。"""

    license_id: str
    version_id: str
    licensee_id: str
    regions: frozenset[str]
    channels: frozenset[str]
    uses: frozenset[str]
    valid_from: str
    valid_to: str | None
    guarantee_cents: int
    status: str


@dataclass(frozen=True)
class UsageRecord:
    """去重并完成范围匹配后的使用事实。"""

    usage_id: str
    work_id: str
    licensee_id: str
    region: str
    channel: str
    use_type: str
    quantity: int
    occurred_on: str
    period_key: str
    license_id: str | None
    in_scope: bool


@dataclass(frozen=True)
class BillingEntry:
    """不可覆盖的计费分录；supplement 追补(+)、reversal 冲销(-) 进入后续期间。"""

    entry_id: str
    period_key: str
    closes_period: str | None
    usage_id: str | None
    license_id: str
    work_id: str
    version_id: str
    licensee_id: str
    controlling_party_id: str
    region: str
    channel: str
    use_type: str
    quantity: int
    cursor_before: int
    unit_rate_micro_cents: int
    gross_cents: int
    deduction_cents: int
    net_cents: int
    entry_type: str
    rate_snapshot: dict[str, Any]
    reversed_entry_id: str | None
    confirmed: int
    created_at: str


@dataclass(frozen=True)
class Distribution:
    """计费分录或追偿项目按权利人份额（基点）展开的分配明细。"""

    distribution_id: str
    ref_type: str
    ref_id: str
    period_key: str | None
    license_id: str | None
    party_id: str
    basis_points: int
    amount_cents: int


@dataclass(frozen=True)
class Claim:
    """许可证失效或超范围使用形成的待追偿项目。"""

    claim_id: str
    usage_id: str | None
    licensee_id: str
    controlling_party_id: str
    work_id: str
    region: str
    channel: str
    use_type: str
    quantity: int
    occurred_on: str
    reason: str
    status: str
    amount_cents: int


@dataclass(frozen=True)
class Dispute:
    """争议只托管相关金额，无争议部分照常支付。"""

    dispute_id: str
    period_key: str
    license_id: str
    licensee_id: str
    amount_cents: int
    reason: str
    status: str


@dataclass(frozen=True)
class Payment:
    """付款指令：支付对象与金额来自结算单，严格按队列顺序执行。"""

    payment_id: str
    period_key: str
    payee_party_id: str
    license_id: str | None
    amount_cents: int
    kind: str
    status: str
    queue_seq: int | None


@dataclass(frozen=True)
class Settlement:
    """期间结算汇总视图。"""

    period_key: str
    status: str
    receivable_cents: int
    paid_cents: int
    escrow_cents: int
    balance_cents: int
    written_off_cents: int
    claims_cents: int
