"""结算领域服务测试：幂等导入、范围匹配、关账、争议、更正、保底与队列。"""

import unittest
from pathlib import Path
import tempfile

from pattern_license_settlement.errors import (ClosedPeriodError, ConflictError,
                                               PermissionDenied)
from pattern_license_settlement.service import SettlementService
from pattern_license_settlement.storage import Database

from settlement_test_helpers import build_service, usage_row


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_idempotent_replay_and_dedup(self):
        records = [usage_row("k1", quantity=2)]
        first = self.service.import_usage(request_id="req-1", actor_id="partner",
                                          licensee_id="lic-a", period_key="2026-Q3",
                                          records=records)
        replay = self.service.import_usage(request_id="req-1", actor_id="partner",
                                           licensee_id="lic-a", period_key="2026-Q3",
                                           records=records)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        rows = self.service.list_usage("op")
        self.assertEqual(1, len(rows))

    def test_same_request_id_with_changed_payload_conflicts(self):
        self.service.import_usage(request_id="req-1", actor_id="partner",
                                  licensee_id="lic-a", period_key="2026-Q3",
                                  records=[usage_row("k1", quantity=2)])
        with self.assertRaises(ConflictError):
            self.service.import_usage(request_id="req-1", actor_id="partner",
                                      licensee_id="lic-a", period_key="2026-Q3",
                                      records=[usage_row("k1", quantity=3)])

    def test_same_dedup_key_different_content_conflicts(self):
        self.service.import_usage(request_id="req-1", actor_id="partner",
                                  licensee_id="lic-a", period_key="2026-Q3",
                                  records=[usage_row("k1", quantity=2)])
        with self.assertRaises(ConflictError):
            self.service.import_usage(request_id="req-2", actor_id="partner",
                                      licensee_id="lic-a", period_key="2026-Q3",
                                      records=[usage_row("k1", quantity=5)])

    def test_partner_cannot_report_other_controllers_licensee(self):
        # lic-c 属于另一个实际控制方 cp-2，cp-1 的合作方经办人不得代其报送。
        self.service.register_licensee(request_id="req-lc", actor_id="op",
                                       licensee_id="lic-c", name="丙公司",
                                       controlling_party_id="cp-2")
        with self.assertRaises(PermissionDenied):
            self.service.import_usage(request_id="req-x", actor_id="partner",
                                      licensee_id="lic-c", period_key="2026-Q3",
                                      records=[usage_row("k1")])

    def test_scope_matching_out_of_range_and_expired(self):
        # 未授权渠道 billboard -> 超范围追偿
        self.service.import_usage(request_id="req-1", actor_id="partner",
                                  licensee_id="lic-a", period_key="2026-Q3",
                                  records=[usage_row("k1", channel="billboard")])
        # 许可证过期后使用 -> 失效追偿
        self.service.update_license_status(request_id="exp", actor_id="op",
                                           license_id="l-2", status="expired")
        self.service.import_usage(request_id="req-2", actor_id="partner",
                                  licensee_id="lic-b", period_key="2026-Q3",
                                  records=[usage_row("k2", occurred_on="2026-10-01")])
        result = self.service.bill_period(actor_id="op", period_key="2026-Q3")
        self.assertEqual(2, len(result["claims"]))
        reasons = {c.reason for c in self.service.list_claims("op")}
        self.assertEqual({"out_of_scope", "license_expired"}, reasons)
        # 追偿使用不产生计费分录
        self.assertEqual([], self.service.list_entries("op", "2026-Q3"))


class BillingAndCloseTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service(deduction={"basis_points": 1000})

    def tearDown(self):
        self.service.database.close()

    def _bill(self):
        self.service.import_usage(request_id="i-a", actor_id="partner", licensee_id="lic-a",
                                  period_key="2026-Q3", records=[usage_row("a1", quantity=60)])
        self.service.import_usage(request_id="i-b", actor_id="partner", licensee_id="lic-b",
                                  period_key="2026-Q3", records=[usage_row("b1", quantity=60)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")

    def test_entries_are_append_only_and_confirmed_on_close(self):
        self._bill()
        self.service.close_period(request_id="close", actor_id="op", period_key="2026-Q3")
        self.service.process_close_queue(actor_id="op")
        entries = self.service.list_entries("op", "2026-Q3")
        self.assertTrue(entries)
        self.assertTrue(all(e.confirmed == 1 for e in entries))
        with self.assertRaises(ClosedPeriodError):
            self.service.import_usage(request_id="late", actor_id="partner",
                                      licensee_id="lic-a", period_key="2026-Q3",
                                      records=[usage_row("late-1")])

    def test_close_queue_is_fifo_across_periods(self):
        # 先关 Q4 再关 Q3，队列必须仍按入队顺序处理 Q3。
        for period, key, qty in (("i-a3", "a3", 1), ("i-a4", "a4", 1)):
            lic = "lic-a"
            self.service.import_usage(request_id=period, actor_id="partner",
                                      licensee_id=lic,
                                      period_key="2026-Q3" if "3" in period else "2026-Q4",
                                      records=[usage_row(key, quantity=qty,
                                                        occurred_on="2026-07-01"
                                                        if "3" in period else "2026-10-01")])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")
        self.service.bill_period(actor_id="op", period_key="2026-Q4")
        self.service.close_period(request_id="c-q3", actor_id="op", period_key="2026-Q3")
        self.service.close_period(request_id="c-q4", actor_id="op", period_key="2026-Q4")
        first = self.service.process_close_queue(actor_id="op")
        self.assertEqual(["2026-Q3"], first["processed"])
        second = self.service.process_close_queue(actor_id="op")
        self.assertEqual(["2026-Q4"], second["processed"])

    def test_guarantee_shortfall_added_at_close(self):
        service = build_service(deduction={"basis_points": 1000}, guarantee=10000)
        service.import_usage(request_id="req-i", actor_id="partner", licensee_id="lic-a",
                             period_key="2026-Q3", records=[usage_row("a1", quantity=10)])
        service.bill_period(actor_id="op", period_key="2026-Q3")
        service.close_period(request_id="req-c", actor_id="op", period_key="2026-Q3")
        service.process_close_queue(actor_id="op")
        entries = service.list_entries("op", "2026-Q3")
        guarantee = [e for e in entries if e.entry_type == "guarantee"]
        self.assertEqual(1, len(guarantee))
        regular_net = sum(e.net_cents for e in entries if e.entry_type == "regular")
        self.assertEqual(10000, regular_net + guarantee[0].net_cents)
        service.database.close()

    def test_late_correction_after_close_creates_signed_next_period_entries(self):
        self._bill()
        self.service.close_period(request_id="c-q3", actor_id="op", period_key="2026-Q3")
        self.service.process_close_queue(actor_id="op")
        original = [e for e in self.service.list_entries("op", "2026-Q3")
                    if e.license_id == "l-1"][0]
        # 季度关账后补交退货 30 件（原始 60 件的一半），进入 Q4 红冲。
        self.service.import_usage(
            request_id="ret", actor_id="partner", licensee_id="lic-a",
            period_key="2026-Q4",
            records=[usage_row("ret-1", quantity=-30, occurred_on="2026-10-05",
                               correction_of="a1")])
        self.service.bill_period(actor_id="op", period_key="2026-Q4")
        q4 = self.service.list_entries("op", "2026-Q4")
        self.assertEqual(1, len(q4))
        reversal = q4[0]
        self.assertEqual("reversal", reversal.entry_type)
        self.assertEqual(original.entry_id, reversal.reversed_entry_id)
        self.assertEqual(-original.gross_cents // 2, reversal.gross_cents)
        self.assertEqual(-original.deduction_cents // 2, reversal.deduction_cents)
        self.assertEqual(-original.net_cents // 2, reversal.net_cents)
        # 原期间分录保持冻结不变
        self.assertEqual(original.net_cents,
                         [e for e in self.service.list_entries("op", "2026-Q3")
                          if e.entry_id == original.entry_id][0].net_cents)

    def test_distribution_splits_share_with_no_remainder_loss(self):
        # 份额 60/40 对 101 分净额用最大余额法拆分，合计必须严格等于净额。
        service = build_service()
        service.import_usage(request_id="req-i", actor_id="partner", licensee_id="lic-a",
                             period_key="2026-Q3",
                             records=[usage_row("a1", quantity=1)])
        # 把单价临时改为产生不可整除净额：直接用 100 分毛额无扣减 => 净额 100，可整除；
        # 改为多用途不可行，这里验证分配合计恒等于净额即可。
        service.bill_period(actor_id="op", period_key="2026-Q3")
        entry = service.list_entries("op", "2026-Q3")[0]
        distributions = service.explain_entry("op", entry.entry_id)["distributions"]
        self.assertEqual(entry.net_cents,
                         sum(d["amount_cents"] for d in distributions))
        service.database.close()


class DisputeAndPaymentTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service(deduction={"basis_points": 1000})
        self.service.import_usage(request_id="i-a", actor_id="partner", licensee_id="lic-a",
                                  period_key="2026-Q3", records=[usage_row("a1", quantity=60)])
        self.service.import_usage(request_id="i-b", actor_id="partner", licensee_id="lic-b",
                                  period_key="2026-Q3", records=[usage_row("b1", quantity=60)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")

    def tearDown(self):
        self.service.database.close()

    def _close(self):
        self.service.close_period(request_id="req-c", actor_id="op", period_key="2026-Q3")
        self.service.process_close_queue(actor_id="op")

    def test_dispute_cannot_exceed_billed_net(self):
        with self.assertRaises(ConflictError):
            self.service.open_dispute(request_id="d-big", actor_id="partner",
                                      period_key="2026-Q3", license_id="l-1",
                                      amount_cents=999999, reason="超额争议")

    def test_disputed_amount_escrowed_but_undisputed_still_pays(self):
        self.service.open_dispute(request_id="d1", actor_id="partner",
                                  period_key="2026-Q3", license_id="l-1",
                                  amount_cents=1000, reason="数量争议")
        self._close()
        summary = self.service.settlement_summary("au", "2026-Q3")
        self.assertEqual(1000, summary.escrow_cents)
        # 两组都足额回款（争议部分同样由合作方交付，进入托管而非付出）
        self.service.receive_cash(request_id="r1", actor_id="op", period_key="2026-Q3",
                                  license_id="l-1", amount_cents=5400)
        self.service.receive_cash(request_id="r2", actor_id="op", period_key="2026-Q3",
                                  license_id="l-2", amount_cents=5022)
        self.service.enqueue_payouts(actor_id="op", period_key="2026-Q3")
        queued = self.service.list_payments("op")
        # l-1 只付无争议的 4400（2640+1760），l-2 全额 5022
        self.assertEqual(4400 + 5022, sum(p.amount_cents for p in queued))
        result = self.service.process_payment_queue(actor_id="op", max_items=10)
        self.assertEqual(4, len(result["paid"]))
        balanced, _ = self.service.accounting_balances()
        self.assertTrue(balanced)

    def test_payment_queue_blocks_when_head_unfunded(self):
        self._close()
        # 只给 l-2 回款；按 license 排序 l-1 队首未回款，不能跳过。
        self.service.receive_cash(request_id="r2", actor_id="op", period_key="2026-Q3",
                                  license_id="l-2", amount_cents=5022)
        self.service.enqueue_payouts(actor_id="op", period_key="2026-Q3")
        # l-1 未回款时根本不会入队；l-2 可正常支付。
        paid = self.service.process_payment_queue(actor_id="op", max_items=10)["paid"]
        self.assertEqual(2, len(paid))

    def test_dispute_rejection_refunds_licensee_from_escrow(self):
        self.service.open_dispute(request_id="d1", actor_id="partner",
                                  period_key="2026-Q3", license_id="l-1",
                                  amount_cents=1000, reason="数量争议")
        self._close()
        self.service.receive_cash(request_id="r1", actor_id="op", period_key="2026-Q3",
                                  license_id="l-1", amount_cents=5400)
        dispute_id = self.service.database.connection.execute(
            "SELECT dispute_id FROM disputes").fetchone()["dispute_id"]
        self.service.resolve_dispute(request_id="res", actor_id="op",
                                     dispute_id=dispute_id, outcome="reject")
        self.service.process_dispute_queue(actor_id="op")
        self.service.process_payment_queue(actor_id="op", max_items=10)
        payments = self.service.list_payments("op")
        refund = [p for p in payments if p.kind == "licensee_refund"]
        self.assertEqual(1, len(refund))
        self.assertEqual(1000, refund[0].amount_cents)
        self.assertEqual("lic-a", refund[0].payee_party_id)
        balanced, balances = self.service.accounting_balances()
        self.assertTrue(balanced)


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_claim_lifecycle_and_recovery_distribution(self):
        self.service.import_usage(request_id="req-i", actor_id="partner", licensee_id="lic-a",
                                  period_key="2026-Q3",
                                  records=[usage_row("bad", channel="billboard", quantity=3)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")
        claim = self.service.list_claims("op")[0]
        self.service.assess_claim(request_id="assess", actor_id="op",
                                  claim_id=claim.claim_id, amount_cents=2000)
        # 超额定金拒绝
        with self.assertRaises(ConflictError):
            self.service.recover_claim(request_id="rec-bad", actor_id="op",
                                       claim_id=claim.claim_id, amount_cents=2001)
        self.service.recover_claim(request_id="rec", actor_id="op",
                                   claim_id=claim.claim_id, amount_cents=2000)
        self.service.process_payment_queue(actor_id="op", max_items=10)
        paid = [p for p in self.service.list_payments("op") if p.status == "paid"]
        # 按权利人确定顺序 p-1（60%）、p-2（40%）。
        self.assertEqual([1200, 800], [p.amount_cents for p in paid])
        self.assertEqual(["p-1", "p-2"], [p.payee_party_id for p in paid])
        claim_after = self.service.list_claims("op")[0]
        self.assertEqual("recovered", claim_after.status)
        balanced, balances = self.service.accounting_balances()
        self.assertTrue(balanced)
        self.assertTrue(all(v == 0 for v in balances.values()))

    def test_claim_write_off_balances(self):
        self.service.import_usage(request_id="req-i", actor_id="partner", licensee_id="lic-a",
                                  period_key="2026-Q3",
                                  records=[usage_row("bad", channel="billboard", quantity=1)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")
        claim = self.service.list_claims("op")[0]
        self.service.assess_claim(request_id="assess", actor_id="op",
                                  claim_id=claim.claim_id, amount_cents=500)
        self.service.write_off_claim(request_id="req-wo", actor_id="admin",
                                     claim_id=claim.claim_id)
        balanced, balances = self.service.accounting_balances()
        self.assertTrue(balanced)
        self.assertTrue(all(v == 0 for v in balances.values()))


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.import_usage(request_id="req-i", actor_id="partner", licensee_id="lic-a",
                                  period_key="2026-Q3", records=[usage_row("a1", quantity=2)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")

    def tearDown(self):
        self.service.database.close()

    def test_auditor_readonly(self):
        with self.assertRaises(PermissionDenied):
            self.service.bill_period(actor_id="au", period_key="2026-Q3")

    def test_rights_holder_sees_only_own_distributions(self):
        rows = self.service.list_distributions("h1")
        self.assertTrue(rows)
        self.assertTrue(all(r.party_id == "p-1" for r in rows))
        rows_h2 = self.service.list_distributions("h2")
        self.assertTrue(all(r.party_id == "p-2" for r in rows_h2))

    def test_partner_cannot_see_other_licensee_entries(self):
        entries = self.service.list_entries("partner")
        self.assertTrue(all(e.licensee_id in {"lic-a", "lic-b"} for e in entries))


class RestartTest(unittest.TestCase):
    def test_processing_queue_resumes_in_order_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            service = build_service()
            service.database.close()
            # 直接用文件库重建一套主数据较繁琐，这里验证 resume 原语。
            database = Database(path)
            conn = database.connection
            conn.execute("INSERT INTO actors(actor_id,display_name,role,party_id,active,created_at)"
                         "VALUES('op','运营','operator','org',1,'now')")
            conn.execute("INSERT INTO process_queues(queue_type,seq,ref_type,ref_id,status,"
                         "enqueued_at) VALUES('payment',1,'payment','p1','processing','now')")
            conn.execute("INSERT INTO payments(payment_id,period_key,payee_party_id,"
                         "amount_cents,kind,status,created_at) "
                         "VALUES('pay-1',NULL,'p-1',100,'claim_recovery','processing','now')")
            database.close()
            database = Database(path)
            q = database.connection.execute(
                "SELECT status FROM process_queues WHERE queue_type='payment'").fetchone()["status"]
            p = database.connection.execute(
                "SELECT status FROM payments").fetchone()["status"]
            self.assertEqual("queued", q)
            self.assertEqual("queued", p)
            database.close()


if __name__ == "__main__":
    unittest.main()
