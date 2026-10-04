import unittest
from datetime import datetime, timezone

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import ConflictError, PermissionDenied, ValidationError

from pattern_license_billing.service import LicensingService
from pattern_license_billing.storage import LicensingDatabase


TIERS = [{"lower_bound_cents": 0, "rate_basis_points": 1000},
         {"lower_bound_cents": 100000, "rate_basis_points": 2000}]


def seed(service) -> None:
    service.register_principal(request_id="r-op", actor_id="bootstrap", principal_id="op",
                               kind="operator", display_name="运营")
    service.register_principal(request_id="r-aud", actor_id="op", principal_id="aud",
                               kind="auditor", display_name="审计")
    service.register_holder(request_id="r-h1", actor_id="op", holder_id="h1", name="甲")
    service.register_holder(request_id="r-h2", actor_id="op", holder_id="h2", name="乙")
    service.register_work(request_id="r-w", actor_id="op", work_id="w1", title="纹韵")
    service.register_work_version(request_id="r-wv", actor_id="op", work_id="w1", version_no=1,
                                  content_hash="x")
    service.register_entitlement_version(
        request_id="r-ev", actor_id="op", work_id="w1", version_no=1,
        effective_from="2026-01-01",
        shares=[{"holder_id": "h1", "basis_points": 7000},
                {"holder_id": "h2", "basis_points": 3000}])
    service.register_control_group(request_id="r-g", actor_id="op", group_id="g1", name="集团")
    service.register_licensee(request_id="r-l1", actor_id="op", licensee_id="p1",
                              control_group_id="g1", name="店一")
    service.register_licensee(request_id="r-l2", actor_id="op", licensee_id="p2",
                              control_group_id="g1", name="店二")
    service.register_principal(request_id="r-partner", actor_id="op", principal_id="partner1",
                               kind="partner", licensee_id="p1", display_name="店一报送")
    service.register_license(request_id="r-li1", actor_id="op", license_id="lic1",
                             licensee_id="p1", work_id="w1", work_version_no=1, territory="CN",
                             channel="packaging", usage_purpose="*", valid_from="2026-01-01")
    service.register_license(request_id="r-li2", actor_id="op", license_id="lic2",
                             licensee_id="p2", work_id="w1", work_version_no=1, territory="*",
                             channel="*", usage_purpose="*", valid_from="2026-01-01")
    service.register_rate_version(request_id="r-rv1", actor_id="op", license_id="lic1",
                                  version_no=1, effective_from="2026-01-01", tiers=TIERS)
    service.register_rate_version(request_id="r-rv2", actor_id="op", license_id="lic2",
                                  version_no=1, effective_from="2026-01-01", tiers=TIERS)
    service.register_reporting_cycle(request_id="r-c1", actor_id="op", license_id="lic1",
                                     period_key="2026-Q3", kind="quarterly",
                                     deadline="2026-10-10")
    service.register_reporting_cycle(request_id="r-c2", actor_id="op", license_id="lic2",
                                     period_key="2026-Q3", kind="quarterly",
                                     deadline="2026-10-10")


def item(key, gross, **kw):
    base = {"source_record_key": key, "work_id": "w1", "territory": "CN",
            "channel": "packaging", "usage_purpose": "box", "occurred_at": "2026-08-01",
            "quantity": 1, "gross_revenue_cents": gross}
    base.update(kw)
    return base


class LicensingServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = LicensingDatabase(":memory:")
        self.service = LicensingService(self.database,
                                        FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        seed(self.service)

    def tearDown(self):
        self.database.close()

    def _receive_p2_cycle(self, request_id: str = "b-zero"):
        """为 p2 报送一条零销售额事实，满足其 Q3 报送周期。"""

        self.service.import_usage_batch(
            request_id=request_id, actor_id="op", licensee_id="p2", period_key="2026-Q3",
            source_ref="zero", items=[item("zero-1", 0, territory="US", channel="digital",
                                           occurred_at="2026-08-02")])

    # --------------------------------------------------------------- 导入与匹配

    def test_batch_import_is_idempotent_and_deduplicates(self):
        first = self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 50000)])
        second = self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 50000)])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        facts = self.service.list_facts("op", "2026-Q3")
        self.assertEqual(1, len(facts))

    def test_changed_payload_with_same_request_id_conflicts(self):
        kwargs = dict(actor_id="op", licensee_id="p1", period_key="2026-Q3", source_ref="f1")
        self.service.import_usage_batch(request_id="b2", items=[item("s1", 1000)], **kwargs)
        with self.assertRaises(ConflictError):
            self.service.import_usage_batch(request_id="b2", items=[item("s1", 2000)], **kwargs)

    def test_partner_cannot_report_other_licensee(self):
        with self.assertRaises(PermissionDenied):
            self.service.import_usage_batch(
                request_id="b3", actor_id="partner1", licensee_id="p2", period_key="2026-Q3",
                source_ref="f", items=[item("s1", 1000)])

    def test_control_group_split_is_merged_for_tier(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 100000)])
        self.service.import_usage_batch(
            request_id="b2", actor_id="op", licensee_id="p2", period_key="2026-Q3",
            source_ref="f2", items=[item("s2", 20000, territory="US", channel="digital",
                                         occurred_at="2026-08-02")])
        fees = {e.licensee_id: e.amount_cents
                for e in self.service.list_entries("op", "2026-Q3")}
        self.assertEqual(10000, fees["p1"])
        self.assertEqual(4000, fees["p2"])  # 20000 全部落入 20% 档

    def test_expired_license_becomes_claim(self):
        self.service.terminate_license(request_id="t1", actor_id="op", license_id="lic1",
                                       terminated_on="2026-07-31")
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 5000, occurred_at="2026-08-15")])
        claims = self.service.list_claims("op")
        self.assertEqual("expired", claims[0]["reason"])
        self.assertEqual(0, len(self.service.list_entries("op", "2026-Q3")))

    def test_out_of_scope_becomes_claim_and_recovers_later(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 7000, territory="JP", channel="tv")])
        claim = self.service.list_claims("op")[0]
        self.assertEqual("out_of_scope", claim["reason"])
        self.service.register_reporting_cycle(request_id="c4", actor_id="op", license_id="lic1",
                                              period_key="2026-Q4", kind="quarterly",
                                              deadline="2027-01-10")
        self.service.register_reporting_cycle(request_id="c4b", actor_id="op", license_id="lic2",
                                              period_key="2026-Q4", kind="quarterly",
                                              deadline="2027-01-10")
        self.service.resolve_claim(request_id="rc1", actor_id="op", claim_id=claim["claim_id"],
                                   resolution="recovered", recovered_cents=4000,
                                   period_key="2026-Q4")
        entries = self.service.list_entries("op", "2026-Q4")
        self.assertEqual("recovery", entries[0].entry_type)
        self.assertEqual(4000, entries[0].amount_cents)
        # 追偿金额按份额分配
        self.assertEqual(4000, sum(a for _, a in entries[0].allocations))

    def test_unknown_work_is_unmatched_claim(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 1000, work_id=None)])
        self.assertEqual("unmatched", self.service.list_claims("op")[0]["reason"])

    # --------------------------------------------------------------- 关账与保底

    def test_close_freezes_entries_and_guarantees_shortfall(self):
        self.service.register_guarantee(request_id="g1", actor_id="op", license_id="lic1",
                                        period_key="2026-Q3", amount_cents=30000)
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 50000)])
        self._receive_p2_cycle()
        self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q3")
        entries = self.service.list_entries("op", "2026-Q3")
        self.assertTrue(all(e.status == "frozen" for e in entries))
        shortfall = [e for e in entries if e.entry_type == "guarantee_shortfall"]
        self.assertEqual(1, len(shortfall))
        # 50000 基数 * 10% = 5000，保底 30000，补差 25000
        self.assertEqual(25000, shortfall[0].amount_cents)

    def test_close_blocked_when_cycle_not_received(self):
        self.service.register_reporting_cycle(request_id="extra", actor_id="op", license_id="lic1",
                                              period_key="2026-Q4", kind="quarterly",
                                              deadline="2027-01-10")
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q4")

    def test_frozen_entries_are_immutable(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 1000)])
        self._receive_p2_cycle()
        self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q3")
        with self.assertRaises(Exception):
            self.database.connection.execute(
                "UPDATE billing_entries SET amount_cents=999 WHERE period_key='2026-Q3'")

    def test_late_correction_enters_next_period_as_reversal(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 100000)])
        self._receive_p2_cycle()
        self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q3")
        for lic in ("lic1", "lic2"):
            self.service.register_reporting_cycle(
                request_id=f"c4-{lic}", actor_id="op", license_id=lic, period_key="2026-Q4",
                kind="quarterly", deadline="2027-01-10")
        self.service.import_usage_batch(
            request_id="b2", actor_id="op", licensee_id="p1", period_key="2026-Q4",
            source_ref="late", items=[item("s2", 80000, correction_of="s1")])
        correction = [e for e in self.service.list_entries("op", "2026-Q4")
                      if e.source_entry_id][0]
        self.assertEqual("reversal", correction.entry_type)
        self.assertEqual("2026-Q3", correction.origin_period_key)
        self.assertEqual(-2000, correction.amount_cents)

    # --------------------------------------------------------------- 争议与付款

    def _closed_with_one_entry(self, gross=100000):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", gross)])
        self._receive_p2_cycle()
        self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q3")
        return [e for e in self.service.list_entries("op", "2026-Q3")
                if e.entry_type == "accrual"][0]

    def test_dispute_escrows_without_blocking_other_payment(self):
        entry = self._closed_with_one_entry()
        self.service.open_dispute(request_id="d1", actor_id="op", entry_id=entry.entry_id,
                                  amount_cents=3000, reason="存疑")
        totals = self.service.totals("op", "2026-Q3")
        self.assertEqual(3000, totals["escrow_cents"])
        queue = self.service.order_queue("op")
        # 应收 10000 中 3000 托管，剩 7000 可收
        receivable = [o for o in queue if o["direction"] == "receivable"][0]
        self.assertEqual(7000, receivable["payable_now_cents"])
        self.service.mark_order_paid(request_id="pay1", actor_id="op",
                                     order_id=receivable["order_id"])
        payables = [o for o in self.service.order_queue("op") if o["direction"] == "payable"]
        self.service.mark_order_paid(request_id="pay2", actor_id="op",
                                     order_id=payables[0]["order_id"])
        self.assertTrue(self.service.totals("op", "2026-Q3")["balanced"])

    def test_dispute_cannot_exceed_unpaid_amount(self):
        entry = self._closed_with_one_entry()
        order = [o for o in self.service.order_queue("op") if o["direction"] == "receivable"][0]
        self.service.mark_order_paid(request_id="pay", actor_id="op", order_id=order["order_id"])
        with self.assertRaises(ConflictError):
            self.service.open_dispute(request_id="d1", actor_id="op", entry_id=entry.entry_id,
                                      amount_cents=1, reason="太晚了")

    def test_reversed_dispute_cancels_both_orders_and_keeps_balance(self):
        entry = self._closed_with_one_entry()
        self.service.open_dispute(request_id="d1", actor_id="op", entry_id=entry.entry_id,
                                  amount_cents=4000, reason="退货")
        for order in self.service.order_queue("op"):
            if order["payable_now_cents"] > 0:
                self.service.mark_order_paid(request_id=f"pay-{order['sequence']}", actor_id="op",
                                             order_id=order["order_id"])
        dispute_id = self.service.list_disputes("op")[0]["dispute_id"]
        self.service.resolve_dispute(request_id="rd", actor_id="op", dispute_id=dispute_id,
                                     outcome="reversed", next_period_key="2026-Q4")
        totals = self.service.totals("op", "2026-Q3")
        self.assertTrue(totals["balanced"])
        self.assertEqual(0, totals["escrow_cents"])
        # 原应收 10000：6000 已收 + 4000 核销
        self.assertEqual(6000, totals["received_cents"])
        self.assertEqual(6000, totals["receivable_cents"])

    def test_payment_must_follow_queue_sequence(self):
        self._closed_with_one_entry()
        queue = self.service.order_queue("op")
        last = queue[-1]
        with self.assertRaises(ConflictError):
            self.service.mark_order_paid(request_id="px", actor_id="op",
                                         order_id=last["order_id"], amount_cents=1)

    # --------------------------------------------------------------- 权限与审计

    def test_auditor_can_recalculate_but_not_write(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_work(request_id="w", actor_id="aud", work_id="w2", title="x")
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 100000)])
        report = self.service.recalculate_period("aud", "2026-Q3")
        self.assertTrue(report["consistent"])

    def test_holder_sees_only_own_allocations(self):
        service_register = self.service
        service_register.register_principal(request_id="r-h1p", actor_id="op",
                                            principal_id="hp1", kind="holder", holder_id="h1",
                                            display_name="甲")
        service_register.register_principal(request_id="r-h2p", actor_id="op",
                                            principal_id="hp2", kind="holder", holder_id="h2",
                                            display_name="乙")
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 100000)])
        entries = self.service.list_entries("hp1", "2026-Q3")
        self.assertEqual(1, len(entries))
        self.assertEqual({("h1", 7000)}, set(entries[0].allocations))
        explain = self.service.explain_entry("hp1", entries[0].entry_id)
        self.assertEqual(7000, sum(a for _, a in explain["entry"]["allocations"]))

    def test_entitlement_shares_must_sum_to_full(self):
        with self.assertRaises(ValidationError):
            self.service.register_entitlement_version(
                request_id="ev2", actor_id="op", work_id="w1", version_no=2,
                effective_from="2026-01-01",
                shares=[{"holder_id": "h1", "basis_points": 9000}])

    def test_audit_chain_stays_valid(self):
        self.service.import_usage_batch(
            request_id="b1", actor_id="op", licensee_id="p1", period_key="2026-Q3",
            source_ref="f1", items=[item("s1", 1000)])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
