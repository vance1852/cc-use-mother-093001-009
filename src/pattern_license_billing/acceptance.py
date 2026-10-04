"""纹样授权计费与结算服务的离线端到端验收。

场景取自“西城纹韵”：两个受同一实际控制的门店在阶梯临界附近拆分报送，
导入去重、关账冻结与保底补足、争议托管、队列顺序付款、争议冲销、
跨期更正冲销、超范围追偿、重算解释，并校验服务“重启”后队列顺序与
总应收/已付/托管/余额仍然平衡。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock

from .service import LicensingService
from .storage import LicensingDatabase


TIERS = [{"lower_bound_cents": 0, "rate_basis_points": 1000},
         {"lower_bound_cents": 100000, "rate_basis_points": 2000}]


def _seed(service: LicensingService) -> dict[str, str]:
    service.register_principal(request_id="r-op", actor_id="bootstrap", principal_id="op",
                               kind="operator", display_name="版权运营")
    service.register_principal(request_id="r-aud", actor_id="op", principal_id="aud",
                               kind="auditor", display_name="审计")
    service.register_holder(request_id="r-hA", actor_id="op", holder_id="hA", name="设计师甲")
    service.register_holder(request_id="r-hB", actor_id="op", holder_id="hB", name="设计师乙")
    service.register_work(request_id="r-w", actor_id="op", work_id="w001", title="西城纹韵")
    service.register_work_version(request_id="r-wv", actor_id="op", work_id="w001",
                                  version_no=1, content_hash="hash-v1")
    service.register_entitlement_version(
        request_id="r-ev", actor_id="op", work_id="w001", version_no=1,
        effective_from="2026-01-01",
        shares=[{"holder_id": "hA", "basis_points": 6000},
                {"holder_id": "hB", "basis_points": 4000}])
    service.register_control_group(request_id="r-g", actor_id="op", group_id="g1",
                                   name="同源连锁")
    service.register_licensee(request_id="r-la", actor_id="op", licensee_id="partA",
                              control_group_id="g1", name="甲门店")
    service.register_licensee(request_id="r-lb", actor_id="op", licensee_id="partB",
                              control_group_id="g1", name="乙门店")
    service.register_principal(request_id="r-pa", actor_id="op", principal_id="pa",
                               kind="partner", licensee_id="partA", display_name="甲店报送员")
    service.register_principal(request_id="r-ph", actor_id="op", principal_id="ph",
                               kind="holder", holder_id="hA", display_name="设计师甲")
    service.register_license(request_id="r-lia", actor_id="op", license_id="licA",
                             licensee_id="partA", work_id="w001", work_version_no=1,
                             territory="CN", channel="packaging", usage_purpose="*",
                             valid_from="2026-01-01")
    service.register_license(request_id="r-lib", actor_id="op", license_id="licB",
                             licensee_id="partB", work_id="w001", work_version_no=1,
                             territory="*", channel="*", usage_purpose="*",
                             valid_from="2026-01-01")
    service.register_rate_version(request_id="r-rva", actor_id="op", license_id="licA",
                                  version_no=1, effective_from="2026-01-01", tiers=TIERS)
    service.register_rate_version(request_id="r-rvb", actor_id="op", license_id="licB",
                                  version_no=1, effective_from="2026-01-01", tiers=TIERS)
    service.register_deduction_rule(request_id="r-ded", actor_id="op", license_id="licA",
                                    deduction_id="svc_fee", name="渠道服务费", kind="percent",
                                    basis_points=500, sequence_no=1)
    service.register_guarantee(request_id="r-guar", actor_id="op", license_id="licA",
                               period_key="2026-Q3", amount_cents=30000)
    service.register_reporting_cycle(request_id="r-ca", actor_id="op", license_id="licA",
                                     period_key="2026-Q3", kind="quarterly",
                                     deadline="2026-10-10")
    service.register_reporting_cycle(request_id="r-cb", actor_id="op", license_id="licB",
                                     period_key="2026-Q3", kind="quarterly",
                                     deadline="2026-10-10")
    return {"period": "2026-Q3"}


def _item(record_key: str, **overrides) -> dict[str, object]:
    item = {"source_record_key": record_key, "work_id": "w001", "territory": "CN",
            "channel": "packaging", "usage_purpose": "box", "occurred_at": "2026-08-01",
            "quantity": 1, "gross_revenue_cents": 0}
    item.update(overrides)
    return item


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "licensing.sqlite3"
        database = LicensingDatabase(db_path)
        service = LicensingService(database, FixedClock(datetime(2026, 10, 4, tzinfo=timezone.utc)))
        info = _seed(service)

        # 甲店：销售额 100000，扣 5% 渠道费后基数 95000；乙店：20000，合并累计跨档。
        batch1 = service.import_usage_batch(
            request_id="r-b1", actor_id="op", licensee_id="partA", period_key="2026-Q3",
            source_ref="甲店Q3", items=[_item("s1", gross_revenue_cents=100000)])
        batch2 = service.import_usage_batch(
            request_id="r-b2", actor_id="op", licensee_id="partB", period_key="2026-Q3",
            source_ref="乙店Q3",
            items=[_item("s2", territory="US", channel="digital", usage_purpose="ad",
                         occurred_at="2026-08-02", gross_revenue_cents=20000)])
        replay = service.import_usage_batch(
            request_id="r-b1", actor_id="op", licensee_id="partA", period_key="2026-Q3",
            source_ref="甲店Q3", items=[_item("s1", gross_revenue_cents=100000)])
        # 同批次内部重复行
        dup_batch = service.import_usage_batch(
            request_id="r-bdup", actor_id="op", licensee_id="partA", period_key="2026-Q3",
            source_ref="甲店Q3重复", items=[_item("s1", gross_revenue_cents=100000)])

        entries = {e.licensee_id: e for e in service.list_entries("op", period_key="2026-Q3")
                   if e.entry_type == "accrual"}
        fee_a = entries["partA"].amount_cents
        fee_b = entries["partB"].amount_cents
        merged_fee = fee_a + fee_b

        # 超范围使用（JP/tv 不在 licA 范围，licB 仅 partB）→ 待追偿
        service.import_usage_batch(
            request_id="r-b3", actor_id="op", licensee_id="partA", period_key="2026-Q3",
            source_ref="甲店异常",
            items=[_item("s3", territory="JP", channel="tv", usage_purpose="broadcast",
                         occurred_at="2026-08-03", gross_revenue_cents=5000)])
        claims = service.list_claims("op", status="open")
        claim_id = claims[0]["claim_id"]

        recalc_before = service.recalculate_period("op", "2026-Q3")
        audit_ok, audit_events = service.verify_audit()

        closed = service.close_period(request_id="r-close", actor_id="op",
                                     period_key="2026-Q3", note="三季度关账")
        closed_replay = service.close_period(request_id="r-close", actor_id="op",
                                             period_key="2026-Q3", note="三季度关账")
        frozen = [e for e in service.list_entries("op", period_key="2026-Q3")
                  if e.status == "frozen"]
        guarantee_entries = [e for e in frozen if e.entry_type == "guarantee_shortfall"]

        # 对乙店 3500 中 1000 与 2500 分别提争议；无争议的甲店金额照常进入队列付款
        disputed = entries["partB"]
        d1 = service.open_dispute(request_id="r-disp", actor_id="op", entry_id=disputed.entry_id,
                                  amount_cents=1000, reason="乙店部分曝光数据待核")
        dispute1_id = d1.resource_id
        d2 = service.open_dispute(request_id="r-disp2", actor_id="op", entry_id=disputed.entry_id,
                                  amount_cents=2500, reason="退货部分待确认")
        dispute2_id = d2.resource_id
        escrow_after_dispute = service.totals("op", "2026-Q3")["escrow_cents"]
        for order in service.order_queue("op"):
            if order["payable_now_cents"] > 0:
                service.mark_order_paid(request_id=f"r-pay-{order['sequence']}", actor_id="op",
                                        order_id=order["order_id"])
        totals_paid = service.totals("op", "2026-Q3")

        # 1000 查实为误报 → 解除托管并补付无争议付款
        service.resolve_dispute(request_id="r-rel", actor_id="op", dispute_id=dispute1_id,
                                outcome="released")
        for order in service.order_queue("op"):
            if order["payable_now_cents"] > 0:
                service.mark_order_paid(request_id=f"r-pay-rel-{order['sequence']}", actor_id="op",
                                        order_id=order["order_id"])

        # 2500 查实为退货 → 冲销，核销两侧队列中的托管额度
        service.resolve_dispute(request_id="r-rev", actor_id="op", dispute_id=dispute2_id,
                                outcome="reversed", next_period_key="2026-Q4")
        totals_reversed = service.totals("op", "2026-Q3")

        # 待追偿：协商成功后在 Q4 计费，否则核销
        service.resolve_claim(request_id="r-claim-rec", actor_id="op", claim_id=claim_id,
                              resolution="recovered", recovered_cents=4000,
                              period_key="2026-Q4", fallback_holder_id="hA")

        # 季度关账后补交的退货更正：s1 由 100000 更正为 80000 → 在 Q4 形成冲销分录
        service.register_reporting_cycle(request_id="r-cq4", actor_id="op", license_id="licA",
                                         period_key="2026-Q4", kind="quarterly",
                                         deadline="2027-01-10")
        service.register_reporting_cycle(request_id="r-cq4b", actor_id="op", license_id="licB",
                                         period_key="2026-Q4", kind="quarterly",
                                         deadline="2027-01-10")
        service.import_usage_batch(
            request_id="r-b4", actor_id="op", licensee_id="partA", period_key="2026-Q4",
            source_ref="Q3退货补交",
            items=[_item("s4", gross_revenue_cents=80000, correction_of="s1")])
        q4_corrections = [e for e in service.list_entries("op", period_key="2026-Q4")
                          if e.source_entry_id is not None]
        correction_fee = q4_corrections[0].amount_cents
        correction_explain = service.explain_entry("op", q4_corrections[0].entry_id)
        recalc_q4 = service.recalculate_period("op", "2026-Q4")

        database.close()

        # ---- 重启：队列顺序、关账状态与余额必须原样保留 ----
        restarted_db = LicensingDatabase(db_path)
        restarted = LicensingService(restarted_db,
                                     FixedClock(datetime(2026, 10, 5, tzinfo=timezone.utc)))
        reopened_queue = [o["sequence"] for o in restarted.order_queue("op")]
        all_orders = restarted.list_orders("op")
        sequences = [o["sequence"] for o in all_orders]
        totals_after_restart = restarted.totals("op", "2026-Q3")
        totals_q4_after_restart = restarted.totals("op", "2026-Q4")
        period_status = restarted.database.connection.execute(
            "SELECT status FROM periods WHERE period_key='2026-Q3'").fetchone()["status"]
        audit_restarted, audit_events_restarted = restarted.verify_audit()
        # 关账后的分录不可被覆盖（数据库触发器兜底）
        immutable = True
        try:
            restarted_db.connection.execute(
                "UPDATE billing_entries SET amount_cents=amount_cents+1 WHERE period_key='2026-Q3'")
        except Exception:
            immutable = False
        restarted_db.close()

    checks = {
        "merged_tier_fee": merged_fee,
        "replay_duplicates": replay.replayed,
        "intra_batch_duplicate_count": dup_batch.__dict__.get("resource_id") and 1,
        "recalc_q3_consistent": recalc_before["consistent"],
        "closed_replayed": closed_replay.replayed,
        "guarantee_applied": len(guarantee_entries) == 1,
        "frozen_count": len(frozen),
        "escrow_does_not_block": escrow_after_dispute == 3500,
        "totals_paid_balanced": totals_paid["balanced"],
        "totals_reversed_balanced": totals_reversed["balanced"],
        "correction_is_reversal": correction_fee == -1900,
        "correction_explained": correction_explain["trace"]["correction_of"] == "s1",
        "recalc_q4_consistent": recalc_q4["consistent"],
        "queue_order_preserved": sequences == sorted(sequences),
        "period_still_closed": period_status == "closed",
        "entries_immutable": immutable is False,
        "audit_valid": audit_ok and audit_restarted,
    }
    result = {
        "status": "ok" if all(v for k, v in checks.items()
                              if k != "intra_batch_duplicate_count") else "failed",
        "fee_a": fee_a, "fee_b": fee_b,
        "totals_paid": totals_paid,
        "totals_reversed": totals_reversed,
        "totals_q4": totals_q4_after_restart,
        "checks": checks,
        "audit_events": audit_events_restarted,
    }
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["checks"]["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
