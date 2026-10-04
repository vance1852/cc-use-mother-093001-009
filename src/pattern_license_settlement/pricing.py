"""阶梯费率计价、实际控制方合并与税前扣减的纯逻辑。

金额全程使用整数：
- 业务金额单位为「分」；
- 费率单价单位为「微分」（1 分 = 10_000 微分），用于精确表达不足 1 分的单价；
- 每条用量先按合并数量游标落档计算微分总额，再用银行家舍入折算为分，
  折算误差只可能出现在单条分录内部，可由 rate_snapshot 逐单位解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

MICRO_PER_CENT = 10_000


@dataclass(frozen=True)
class PricingRow:
    """等待计价的一条用量（已完成范围匹配）。"""

    usage_id: str
    license_id: str
    work_id: str
    version_id: str
    licensee_id: str
    controlling_party_id: str
    region: str
    channel: str
    use_type: str
    quantity: int
    occurred_on: str
    sort_key: str


@dataclass(frozen=True)
class PricedRow:
    """一条用量的计价结果。"""

    row: PricingRow
    cursor_before: int
    unit_rate_micro_cents: int
    gross_micro_cents: int
    gross_cents: int
    segments: tuple[tuple[int, int, int], ...]  # (数量, 区间起点件序, 该档单价微分)


def _to_micro_rate(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("费率不能是布尔值")
    micro = int(Decimal(str(value)) * MICRO_PER_CENT)
    if micro < 0:
        raise ValueError("费率不能为负")
    return micro


def normalize_tiers(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[tuple[int, int]]]:
    """把 {用途: [{from_qty, rate_cents}]} 规整为升序 (下限, 微分单价)。"""

    tiers_by_use: dict[str, list[tuple[int, int]]] = {}
    for use_type, tiers in raw.items():
        normalized = sorted((int(t["from_qty"]), _to_micro_rate(t["rate_cents"])) for t in tiers)
        if not normalized or normalized[0][0] != 0:
            raise ValueError(f"{use_type} 的阶梯必须从 from_qty=0 开始")
        floors = [item[0] for item in normalized]
        if len(set(floors)) != len(floors):
            raise ValueError(f"{use_type} 的阶梯下限不能重复")
        tiers_by_use[use_type] = normalized
    return tiers_by_use


def rate_at(tiers: list[tuple[int, int]], qty: int) -> int:
    """返回累计数量 qty（第 qty 件，qty>=1）所处档位的微分单价。"""

    rate = tiers[0][1]
    for floor, candidate in tiers:
        if qty >= floor:
            rate = candidate
        else:
            break
    return rate


def _price_segment(tiers: list[tuple[int, int]], cursor: int, quantity: int
                   ) -> tuple[int, tuple[tuple[int, int, int], ...]]:
    """计算开区间 (cursor, cursor+quantity] 的微分总额与分段说明。"""

    total_micro = 0
    segments: list[tuple[int, int, int]] = []
    remaining = quantity
    qty = cursor
    while remaining > 0:
        qty += 1
        unit = rate_at(tiers, qty)
        span = remaining
        for floor, _ in tiers:
            if floor > qty:
                span = min(span, floor - qty)
                break
        total_micro += unit * span
        segments.append((span, qty, unit))
        qty += span - 1
        remaining -= span
    return total_micro, tuple(segments)


def price_rows(rows: list[PricingRow], tiers_by_use: dict[str, list[tuple[int, int]]]
               ) -> list[PricedRow]:
    """按 (作品版本, 实际控制方, 用途) 合并数量游标，逐行计价。

    同一实际控制合作方即使通过多个被许可方主体拆分报送，数量也沿同一条
    游标前进，阶梯档位无法靠拆分报送规避。游标内按业务发生时间确定顺序。
    """

    groups: dict[tuple[str, str, str], list[PricingRow]] = {}
    for row in rows:
        groups.setdefault((row.version_id, row.controlling_party_id, row.use_type), []).append(row)

    result: list[PricedRow] = []
    for _, members in groups.items():
        use_type = members[0].use_type
        tiers = tiers_by_use.get(use_type)
        if tiers is None:
            raise ValueError(f"费率卡缺少用途 {use_type} 的阶梯")
        members.sort(key=lambda item: (item.sort_key, item.occurred_on, item.licensee_id, item.usage_id))
        cursor = 0
        for row in members:
            total_micro, segments = _price_segment(tiers, cursor, row.quantity)
            blended = total_micro // row.quantity if row.quantity else 0
            gross_cents = int((Decimal(total_micro) / MICRO_PER_CENT).quantize(
                Decimal("1"), rounding=ROUND_HALF_EVEN))
            result.append(PricedRow(row, cursor, blended, total_micro, gross_cents, segments))
            cursor += row.quantity
    result.sort(key=lambda priced: (priced.row.sort_key, priced.row.occurred_on, priced.row.licensee_id, priced.row.usage_id))
    return result


def apply_deduction(gross_cents: int, deduction: dict[str, Any] | None) -> int:
    """应用税前扣减：fixed_cents 固定额与 basis_points 比例可叠加，以毛额为上限。"""

    if not deduction or gross_cents <= 0:
        return 0
    amount = 0
    if deduction.get("fixed_cents") is not None:
        amount += max(0, int(deduction["fixed_cents"]))
    if deduction.get("basis_points") is not None:
        bp = int(deduction["basis_points"])
        if not 0 <= bp <= 10_000:
            raise ValueError("扣减比例必须在 0..10000 个基点之间")
        amount += int((Decimal(gross_cents) * bp / 10_000).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN))
    return min(amount, gross_cents)
