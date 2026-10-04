"""计费规则的纯函数实现：扣减、阶梯费率选档与金额来源解释。

阶梯费率按“同一实际控制合作方（控制集团）在同一期间内、同一作品维度”的
累计计费基数边际选档，使临界前拆分报送无法降低费率档；退货/冲销等负数基数
按对称规则处理，F(-x)=-F(x)，追补与冲销统一用“累计位置差”计费。
"""

from __future__ import annotations

from typing import Any, Sequence

from .models import Tier


def apply_deductions(gross_cents: int, rules: Sequence[dict[str, Any]],
                     quantity: int) -> tuple[int, list[dict[str, Any]]]:
    """按规则顺序在含税销售额上计算税前扣减，返回扣减后基数与逐项说明。"""

    breakdown: list[dict[str, Any]] = []
    base = gross_cents
    if gross_cents < 0:  # 纯退货负销售额不适用税前扣减
        return base, breakdown
    for rule in sorted(rules, key=lambda r: r["sequence_no"]):
        if not rule.get("active", 1):
            continue
        if rule["kind"] == "percent":
            amount = base * rule["basis_points"] // 10000
        elif rule["kind"] == "fixed_per_fact":
            amount = rule["amount_cents"] * max(quantity, 1)
        else:  # pragma: no cover - 由数据库约束拦截
            raise ValueError("未知扣减类型")
        amount = min(amount, base)
        base -= amount
        breakdown.append({
            "deduction_id": rule["deduction_id"],
            "name": rule["name"],
            "kind": rule["kind"],
            "amount_cents": amount,
        })
    return base, breakdown


def select_rate_version(rate_versions: Sequence[dict[str, Any]], on_date: str) -> dict[str, Any] | None:
    """选择 on_date 当天生效的最新费率版本。"""

    candidates = [rv for rv in rate_versions
                  if rv["status"] == "active" and rv["effective_from"] <= on_date]
    if not candidates:
        return None
    return max(candidates, key=lambda rv: (rv["effective_from"], rv["version_no"]))


def integrate(tiers: Sequence[Tier], position_cents: int) -> int:
    """累计基数落到 position_cents 时的整体费用。

    对正数按边际费率逐档积分；对负数（退货/冲销把累计基数拉低时）按同样的
    档级边界对称取费，保证 F(-x) = -F(x)。
    """

    ordered = sorted(tiers, key=lambda t: t.lower_bound_cents)
    sign = -1 if position_cents < 0 else 1
    x = abs(position_cents)
    total = 0
    for index, tier in enumerate(ordered):
        upper = ordered[index + 1].lower_bound_cents if index + 1 < len(ordered) else None
        low = tier.lower_bound_cents
        if x <= low:
            break
        span = x - low if upper is None else min(x, upper) - low
        total += span * tier.rate_basis_points // 10000
    return sign * total


def tiered_fee(tiers: Sequence[Tier], cumulative_before_cents: int,
               base_cents: int) -> tuple[int, int, list[dict[str, Any]]]:
    """按累计基数的“位置差”把一笔（可为负的）基数映射到费用。

    返回费用额、末段落档档级与跨档明细。只要同一控制集团维度的累计位置相同，
    拆分报送与合并报送的费用必然相同。
    """

    ordered = sorted(tiers, key=lambda t: t.lower_bound_cents)
    before = cumulative_before_cents
    after = cumulative_before_cents + base_cents
    fee = integrate(ordered, after) - integrate(ordered, before)

    lo, hi = sorted((before, after))
    segments: list[dict[str, Any]] = []
    for index, tier in enumerate(ordered):
        upper = ordered[index + 1].lower_bound_cents if index + 1 < len(ordered) else None
        window_lo = max(lo, tier.lower_bound_cents)
        window_hi = min(hi, upper) if upper is not None else hi
        span = window_hi - window_lo
        if span > 0:
            part_fee = span * tier.rate_basis_points // 10000
            segments.append({
                "tier_no": tier.tier_no,
                "lower_bound_cents": tier.lower_bound_cents,
                "rate_basis_points": tier.rate_basis_points,
                "base_cents": -span if base_cents < 0 else span,
                "fee_cents": -part_fee if base_cents < 0 else part_fee,
            })
    landing = ordered[0]
    for tier in ordered:
        if after >= tier.lower_bound_cents:
            landing = tier
    return fee, landing.tier_no, segments
