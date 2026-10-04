"""纹样授权计费结算服务的离线端到端验收。

场景：「西城纹韵」获奖纹样被同一实际控制方旗下两家被许可方拆分报送，
验收覆盖合并阶梯、幂等去重、超范围追偿、关账冻结、迟到冲销、争议托管、
回款付款、重启保序与期间重算。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .service import SettlementService
from .storage import Database


def _seed(service: SettlementService) -> None:
    service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="运营管理员", role="admin", party_id="org")
    service.register_actor(request_id="a-op", actor_id="admin", new_actor_id="op",
                           display_name="版权运营", role="operator", party_id="org")
    service.register_actor(request_id="a-partner", actor_id="admin", new_actor_id="partner",
                           display_name="合作方经办人", role="partner", party_id="cp-1")
    service.register_actor(request_id="a-h1", actor_id="admin", new_actor_id="h1",
                           display_name="权利人甲", role="rights_holder", party_id="p-1")
    service.register_actor(request_id="a-h2", actor_id="admin", new_actor_id="h2",
                           display_name="权利人乙", role="rights_holder", party_id="p-2")
    service.register_actor(request_id="a-au", actor_id="admin", new_actor_id="au",
                           display_name="审计人员", role="auditor", party_id="org")

    service.register_work(request_id="w-1", actor_id="op", work_id="xicheng",
                          title="西城纹韵")
    # 阶梯：100 件以内每件 100 分，达到 100 件后每件 80 分；税前扣减 10%。
    service.register_rate_card(
        request_id="rc-1", actor_id="op", rate_card_id="rc-standard", currency="CNY",
        tiers_by_use={"packaging": [{"from_qty": 0, "rate_cents": 100},
                                    {"from_qty": 100, "rate_cents": 80}]},
        deduction={"basis_points": 1000})
    service.register_work_version(
        request_id="v-1", actor_id="op", version_id="v-1", work_id="xicheng",
        version_no=1, licensor_party_id="p-1", rate_card_id="rc-standard",
        shares={"p-1": 6000, "p-2": 4000},
        effective_from="2026-01-01", effective_to=None)

    # 两家被许可方受同一实际控制方 cp-1 控制。
    service.register_licensee(request_id="lic-a", actor_id="op", licensee_id="lic-a",
                              name="城西包装公司", controlling_party_id="cp-1")
    service.register_licensee(request_id="lic-b", actor_id="op", licensee_id="lic-b",
                              name="城西陈列公司", controlling_party_id="cp-1")
    service.register_license(
        request_id="l-1", actor_id="op", license_id="l-1", version_id="v-1",
        licensee_id="lic-a", regions=["CN"], channels=["store", "online"],
        uses=["packaging"], valid_from="2026-01-01", guarantee_cents=0,
        reporting_cycle="quarterly")
    service.register_license(
        request_id="l-2", actor_id="op", license_id="l-2", version_id="v-1",
        licensee_id="lic-b", regions=["CN"], channels=["store", "online"],
        uses=["packaging"], valid_from="2026-01-01", guarantee_cents=0,
        reporting_cycle="quarterly")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "settlement.sqlite3"
        clock = FixedClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        database = Database(db_path)
        service = SettlementService(database, clock)
        _seed(service)

        # 1) 两家被许可方各报 60 件，试图把 120 件拆成两单压在 100 件阶梯临界点下；
        #    另有一条 billboard 渠道使用不在授权范围内。
        first = service.import_usage(
            request_id="imp-q3-a", actor_id="partner", licensee_id="lic-a",
            period_key="2026-Q3",
            records=[{"dedup_key": "a-0001", "work_id": "xicheng", "region": "CN",
                      "channel": "store", "use_type": "packaging", "quantity": 60,
                      "occurred_on": "2026-07-01"}])
        service.import_usage(
            request_id="imp-q3-b", actor_id="partner", licensee_id="lic-b",
            period_key="2026-Q3",
            records=[{"dedup_key": "b-0001", "work_id": "xicheng", "region": "CN",
                      "channel": "online", "use_type": "packaging", "quantity": 60,
                      "occurred_on": "2026-07-02"},
                     {"dedup_key": "a-billboard", "work_id": "xicheng", "region": "CN",
                      "channel": "billboard", "use_type": "packaging", "quantity": 5,
                      "occurred_on": "2026-07-03"}])
        replay = service.import_usage(
            request_id="imp-q3-a", actor_id="partner", licensee_id="lic-a",
            period_key="2026-Q3",
            records=[{"dedup_key": "a-0001", "work_id": "xicheng", "region": "CN",
                      "channel": "store", "use_type": "packaging", "quantity": 60,
                      "occurred_on": "2026-07-01"}])
        billed = service.bill_period(actor_id="op", period_key="2026-Q3")

        entries = {e.license_id: e for e in service.list_entries("op", "2026-Q3")}
        gross_a = entries["l-1"].gross_cents
        gross_b = entries["l-2"].gross_cents
        # 合并后 120 件沿同一游标落档：1..99 件每件 100 分，100..120 件每件 80 分，
        # lic-a 取 1..60 件=6000，lic-b 取 61..120 件=39*100+21*80=5580；
        # 若按拆分视角各自从 0 起算则两家都会是 6000。
        merged_gross_ok = (gross_a, gross_b) == (6000, 5580)
        net_total = sum(e.net_cents for e in entries.values())  # 11580 - 1158 = 10422

        # 2) 争议只托管相关金额，不阻断其他付款。
        service.open_dispute(request_id="d-1", actor_id="partner", period_key="2026-Q3",
                             license_id="l-1", amount_cents=1000, reason="陈列数量存疑")
        service.close_period(request_id="close-q3", actor_id="op", period_key="2026-Q3")
        service.process_close_queue(actor_id="op")
        q3 = service.settlement_summary("au", "2026-Q3")
        escrow_ok = q3.escrow_cents == 1000
        frozen_ok = all(e.confirmed == 1 for e in service.list_entries("op", "2026-Q3"))

        # 3) Q3 回款：l-1 净额 5400、l-2 净额 5022；争议金额随回款进入托管。
        service.receive_cash(request_id="cash-l1", actor_id="op", period_key="2026-Q3",
                             license_id="l-1", amount_cents=5400)
        service.receive_cash(request_id="cash-l2", actor_id="op", period_key="2026-Q3",
                             license_id="l-2", amount_cents=5022)
        service.enqueue_payouts(actor_id="op", period_key="2026-Q3")
        service.process_payment_queue(actor_id="op", max_items=2)

        # 4) 关账后迟到的退货更正进入 Q4 红冲，同时 Q4 有新增用量自然结平。
        service.import_usage(
            request_id="imp-q4", actor_id="partner", licensee_id="lic-a",
            period_key="2026-Q4",
            records=[{"dedup_key": "ret-001", "work_id": "xicheng", "region": "CN",
                      "channel": "store", "use_type": "packaging", "quantity": -10,
                      "occurred_on": "2026-10-05", "correction_of": "a-0001"},
                     {"dedup_key": "q4-new", "work_id": "xicheng", "region": "CN",
                      "channel": "store", "use_type": "packaging", "quantity": 30,
                      "occurred_on": "2026-10-10"}])
        service.bill_period(actor_id="op", period_key="2026-Q4")
        service.close_period(request_id="close-q4", actor_id="op", period_key="2026-Q4")
        service.process_close_queue(actor_id="op")
        q4_entries = service.list_entries("op", "2026-Q4")
        reversal = [e for e in q4_entries if e.entry_type == "reversal"]
        reversal_ok = len(reversal) == 1 and reversal[0].net_cents == -900
        q4_receivable = sum(e.net_cents for e in q4_entries)  # 2700 - 900 = 1800
        service.receive_cash(request_id="cash-q4", actor_id="op", period_key="2026-Q4",
                             license_id="l-1", amount_cents=q4_receivable)
        service.enqueue_payouts(actor_id="op", period_key="2026-Q4")

        # 5) 超范围使用形成追偿，核定后追回。
        claim = service.list_claims("op")[0]
        claim_reason_ok = claim.reason == "out_of_scope"
        service.assess_claim(request_id="claim-assess", actor_id="op",
                             claim_id=claim.claim_id, amount_cents=2000)

        # 6) 模拟服务重启：processing 自动回到 queued，关账与付款顺序保持不变。
        paid_before = len([p for p in service.list_payments("op") if p.status == "paid"])
        database.close()
        database = Database(db_path)
        service = SettlementService(database, clock)
        service.process_payment_queue(actor_id="op", max_items=10)

        # 7) 争议裁定放行：托管金额按份额付给权利人，不影响其他付款。
        open_dispute = service.database.connection.execute(
            "SELECT dispute_id FROM disputes WHERE status='open'").fetchall()
        service.resolve_dispute(request_id="d-1-release", actor_id="op",
                                dispute_id=open_dispute[0]["dispute_id"], outcome="release")
        service.process_dispute_queue(actor_id="op")
        service.process_payment_queue(actor_id="op", max_items=10)

        # 8) 追偿款到账后按份额付出。
        service.recover_claim(request_id="claim-recover", actor_id="op",
                              claim_id=claim.claim_id, amount_cents=2000)
        service.process_payment_queue(actor_id="op", max_items=10)

        payments = service.list_payments("op")
        order = [(p.kind, p.amount_cents) for p in payments]
        expected_order = [
            ("distribution", 2640), ("distribution", 1760),
            ("distribution", 3013), ("distribution", 2009),
            ("distribution", 1080), ("distribution", 720),
            ("escrow_release", 600), ("escrow_release", 400),
            ("claim_recovery", 1200), ("claim_recovery", 800),
        ]
        order_ok = order == expected_order
        all_paid_ok = all(p.status == "paid" for p in payments)
        restart_progress_ok = paid_before == 2

        # 9) 权限视图：权利人甲只能看到自己的分配行；合作方只能看本方数据。
        h1_rows = service.list_distributions("h1", "2026-Q3")
        visibility_ok = {row.party_id for row in h1_rows} == {"p-1"}
        partner_usage = service.list_usage("partner")
        partner_scope_ok = {u.licensee_id for u in partner_usage} <= {"lic-a", "lic-b"}

        # 10) 审计重算两个期间，账户全部归零且审计链完整。
        q3_check = service.recompute_period("au", "2026-Q3")
        q4_check = service.recompute_period("au", "2026-Q4")
        recompute_ok = not q3_check["mismatches"] and not q4_check["mismatches"] \
            and q3_check["confirmed_ok"] and q4_check["confirmed_ok"]
        balanced, balances = service.accounting_balances()
        zero_ok = all(value == 0 for value in balances.values())
        audit_ok, event_count = service.verify_audit()

        result = {
            "status": "ok",
            "billed_entries": len(billed["entries"]),
            "claims_opened": len(billed["claims"]),
            "import_accepted": not first.replayed,
            "import_replayed": replay.replayed,
            "merged_tier_pricing": merged_gross_ok,
            "net_total_cents": net_total,
            "escrow_isolated_without_blocking": escrow_ok,
            "entries_frozen_on_close": frozen_ok,
            "late_reversal_in_next_period": reversal_ok,
            "claim_reason_classified": claim_reason_ok,
            "restart_resumed_in_order": restart_progress_ok,
            "payment_order_preserved": order_ok,
            "all_payments_paid": all_paid_ok,
            "holder_visibility_scoped": visibility_ok,
            "partner_visibility_scoped": partner_scope_ok,
            "recompute_ok": recompute_ok,
            "ledger_balanced": balanced,
            "ledger_zero": zero_ok,
            "final_balances": balances,
            "audit_valid": audit_ok,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] \
        and result["ledger_balanced"] and result["recompute_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
