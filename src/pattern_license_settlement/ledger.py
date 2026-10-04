"""复式记账原语与平衡校验。

账户：
- ar       应收账款（总应收的累计借方）
- claim    追偿应收（超范围/失效核定后借记，追回或核销时贷记）
- cash     资金（合作方回款计入借方，付给权利人/退款计入贷方）
- clearing 清算（计费时贷记，分配付款时借记，最终结平）
- escrow   争议托管（托管贷记，放行/退回时借记）

每个事务借贷必相等。试算平衡：全部账户带符号余额（借正贷负）之和恒为零，
即 应收 + 追偿 + 现金 + 清算 + 托管 = 0；任一中间状态都必须满足。
"""

from __future__ import annotations

import uuid
from typing import Any

ACCOUNT_RECEIVABLE = "ar"
ACCOUNT_CLAIM = "claim"
ACCOUNT_CASH = "cash"
ACCOUNT_CLEARING = "clearing"
ACCOUNT_ESCROW = "escrow"


def post(connection, *, event_type: str, ref_type: str, ref_id: str,
         lines: dict[str, int], memo: str, period_key: str | None = None,
         created_at: str) -> str:
    """登记一笔借贷平衡的事务；lines 为 {账户: 带符号金额}，正借负贷。

    金额以分为单位，必须为非零整数；借贷合计必须为零。
    """

    positives = {account: amount for account, amount in lines.items() if amount > 0}
    negatives = {account: amount for account, amount in lines.items() if amount < 0}
    if not positives or not negatives:
        raise ValueError("入账事务必须同时包含借方与贷方")
    if sum(positives.values()) != -sum(negatives.values()):
        raise ValueError("借贷不平衡")
    txn_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO accounting_transactions(txn_id,event_type,ref_type,ref_id,period_key,memo,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (txn_id, event_type, ref_type, ref_id, period_key, memo, created_at),
    )
    for account, amount in positives.items():
        connection.execute(
            "INSERT INTO accounting_entries(txn_id,account,direction,amount_cents) VALUES(?,?, 'dr',?)",
            (txn_id, account, amount),
        )
    for account, amount in negatives.items():
        connection.execute(
            "INSERT INTO accounting_entries(txn_id,account,direction,amount_cents) VALUES(?,?, 'cr',?)",
            (txn_id, account, -amount),
        )
    return txn_id


def account_balances(connection) -> dict[str, int]:
    """返回各账户带符号余额（借方正）。"""

    balances = {ACCOUNT_RECEIVABLE: 0, ACCOUNT_CLAIM: 0, ACCOUNT_CASH: 0,
                ACCOUNT_CLEARING: 0, ACCOUNT_ESCROW: 0}
    rows = connection.execute(
        "SELECT account, direction, SUM(amount_cents) AS total FROM accounting_entries GROUP BY account, direction"
    ).fetchall()
    for row in rows:
        sign = 1 if row["direction"] == "dr" else -1
        balances[row["account"]] = balances.get(row["account"], 0) + sign * row["total"]
    return balances


def transactions_for_ref(connection, ref_type: str, ref_id: str) -> list[dict[str, Any]]:
    """列出某业务对象的全部入账事务，用于逐笔解释金额来源。"""

    rows = connection.execute(
        "SELECT * FROM accounting_transactions WHERE ref_type=? AND ref_id=? ORDER BY created_at, txn_id",
        (ref_type, ref_id),
    ).fetchall()
    result = []
    for row in rows:
        lines = [
            {"account": item["account"], "direction": item["direction"], "amount_cents": item["amount_cents"]}
            for item in connection.execute(
                "SELECT * FROM accounting_entries WHERE txn_id=? ORDER BY account, direction", (row["txn_id"],)
            )
        ]
        result.append({"txn_id": row["txn_id"], "event_type": row["event_type"],
                       "ref_type": row["ref_type"], "ref_id": row["ref_id"],
                       "period_key": row["period_key"], "memo": row["memo"],
                       "created_at": row["created_at"], "lines": lines})
    return result
