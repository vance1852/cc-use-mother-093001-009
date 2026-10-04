import unittest

from pattern_license_billing.billing import apply_deductions, integrate, select_rate_version, tiered_fee
from pattern_license_billing.models import Tier


TIERS = [Tier(1, 0, 1000), Tier(2, 100000, 2000)]


class DeductionTest(unittest.TestCase):
    def test_percent_then_fixed_apply_in_sequence(self):
        rules = [
            {"deduction_id": "p", "name": "比例", "kind": "percent", "basis_points": 500,
             "amount_cents": 0, "sequence_no": 1, "active": 1},
            {"deduction_id": "f", "name": "定额", "kind": "fixed_per_fact", "basis_points": 0,
             "amount_cents": 200, "sequence_no": 2, "active": 1},
        ]
        base, trace = apply_deductions(10000, rules, 1)
        self.assertEqual(base, 9300)  # 10000 - 500 - 200
        self.assertEqual([t["amount_cents"] for t in trace], [500, 200])

    def test_deduction_cannot_go_negative(self):
        rules = [{"deduction_id": "f", "name": "定额", "kind": "fixed_per_fact",
                  "basis_points": 0, "amount_cents": 999, "sequence_no": 1, "active": 1}]
        base, _ = apply_deductions(100, rules, 1)
        self.assertEqual(base, 0)


class TieredFeeTest(unittest.TestCase):
    def test_first_tier(self):
        fee, tier_no, _ = tiered_fee(TIERS, 0, 80000)
        self.assertEqual(fee, 8000)
        self.assertEqual(tier_no, 1)

    def test_marginal_rate_when_crossing_threshold(self):
        fee, tier_no, segments = tiered_fee(TIERS, 95000, 20000)
        # 5000 @ 10% + 15000 @ 20%
        self.assertEqual(fee, 3500)
        self.assertEqual(tier_no, 2)
        self.assertEqual([s["tier_no"] for s in segments], [1, 2])

    def test_split_reports_equal_merged_report(self):
        # 临界前拆成三笔报送，费用必须等于一次性合并报送
        split = sum(tiered_fee(TIERS, before, chunk)[0]
                    for before, chunk in [(0, 40000), (40000, 40000), (80000, 40000)])
        merged, _, _ = tiered_fee(TIERS, 0, 120000)
        self.assertEqual(split, merged)
        self.assertEqual(merged, 10000 + 4000)

    def test_integrate_is_odd_symmetric(self):
        self.assertEqual(-integrate(TIERS, 120000), integrate(TIERS, -120000))

    def test_partial_refund_fee_is_delta(self):
        # 100000 -> 80000 的退货，基数扣 5% 后 95000 -> 76000，按位置差冲减
        fee_delta = integrate(TIERS, 76000) - integrate(TIERS, 95000)
        self.assertEqual(fee_delta, -1900)

    def test_select_rate_version_picks_latest_effective(self):
        versions = [
            {"rate_version_id": "rv1", "version_no": 1, "status": "active",
             "effective_from": "2026-01-01"},
            {"rate_version_id": "rv2", "version_no": 2, "status": "active",
             "effective_from": "2026-07-01"},
            {"rate_version_id": "rv3", "version_no": 3, "status": "retired",
             "effective_from": "2026-10-01"},
        ]
        self.assertEqual(select_rate_version(versions, "2026-08-01")["rate_version_id"], "rv2")
        self.assertEqual(select_rate_version(versions, "2026-06-01")["rate_version_id"], "rv1")
        self.assertIsNone(select_rate_version(versions[:0], "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
