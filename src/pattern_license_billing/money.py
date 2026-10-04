"""金额以最小货币单位（分）的整数表示，杜绝浮点误差。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def yuan_to_cents(value: int | float | str) -> int:
    """把元为单位的金额转换为整数分，四舍五入到分。"""

    cents = (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def cents_to_yuan(cents: int) -> str:
    """把整数分格式化为两位小数字符串，仅用于展示。"""

    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def share_of(amount: int, basis_points: int, total_basis_points: int) -> int:
    """按份额比例（基点）分配金额，零头截掉由调用方再平衡。"""

    if total_basis_points <= 0:
        raise ValueError("份额基数必须为正数")
    return amount * basis_points // total_basis_points


def prorate(amount: int, weights: list[int]) -> list[int]:
    """按整数权重比例分配金额，末份承担尾差，保证合计不变。"""

    total = sum(weights)
    if total <= 0:
        raise ValueError("权重合计必须为正数")
    if not weights:
        return []
    allocated = [amount * w // total for w in weights[:-1]]
    allocated.append(amount - sum(allocated))
    return allocated
