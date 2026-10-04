"""阶梯计价纯逻辑测试。"""

import unittest

from pattern_license_settlement.pricing import (MICRO_PER_CENT, PricingRow, apply_deduction,
                                               normalize_tiers, price_rows, rate_at)


def row(usage_id, licensee_id, controller, quantity, use_type="packaging",
        version_id="v-1", occurred_on="2026-07-01"):
    license_key = f"l-{licensee_id.rsplit('-', 1)[-1]}"
    return PricingRow(usage_id, license_key, "xicheng", version_id, licensee_id,
                      controller, "CN", "store", use_type, quantity, occurred_on, "2026-Q3")


class PricingTest(unittest.TestCase):
    def setUp(self):
        self.tiers = normalize_tiers({"packaging": [
            {"from_qty": 0, "rate_cents": 100},
            {"from_qty": 100, "rate_cents": 80},
            {"from_qty": 200, "rate_cents": 50},
        ]})

    def test_single_row_spans_multiple_tiers(self):
        priced = price_rows([row("u1", "lic-a", "cp-1", 250)], self.tiers)[0]
        # 1..99 @100, 100..199 @80, 200..250 @50
        self.assertEqual(99 * 100_0000 + 100 * 80_0000 + 51 * 50_0000,
                         priced.gross_micro_cents)
        self.assertEqual(99 * 100 + 100 * 80 + 51 * 50, priced.gross_cents)
        self.assertEqual(0, priced.cursor_before)

    def test_split_reports_under_common_controller_share_one_cursor(self):
        priced = price_rows([row("u1", "lic-a", "cp-1", 60, occurred_on="2026-07-01"),
                             row("u2", "lic-b", "cp-1", 60, occurred_on="2026-07-02")],
                            self.tiers)
        by_id = {p.row.usage_id: p for p in priced}
        self.assertEqual(6000, by_id["u1"].gross_cents)
        # 第二批从游标 60 起：39 件@100 + 21 件@80
        self.assertEqual(39 * 100 + 21 * 80, by_id["u2"].gross_cents)
        self.assertEqual(60, by_id["u2"].cursor_before)

    def test_different_controllers_have_independent_cursors(self):
        priced = price_rows([row("u1", "lic-a", "cp-1", 60),
                             row("u2", "lic-c", "cp-2", 60)], self.tiers)
        self.assertEqual([6000, 6000], [p.gross_cents for p in priced])

    def test_different_uses_have_independent_cursors(self):
        tiers = normalize_tiers({
            "packaging": [{"from_qty": 0, "rate_cents": 100}],
            "display": [{"from_qty": 0, "rate_cents": 200}],
        })
        priced = price_rows([row("u1", "lic-a", "cp-1", 3, "packaging"),
                             row("u2", "lic-a", "cp-1", 3, "display")], tiers)
        self.assertEqual({300, 600}, {p.gross_cents for p in priced})

    def test_rate_at_floor_semantics(self):
        tiers = self.tiers["packaging"]
        self.assertEqual(100 * MICRO_PER_CENT, rate_at(tiers, 1))
        self.assertEqual(100 * MICRO_PER_CENT, rate_at(tiers, 99))
        self.assertEqual(80 * MICRO_PER_CENT, rate_at(tiers, 100))
        self.assertEqual(50 * MICRO_PER_CENT, rate_at(tiers, 500))

    def test_tier_must_start_at_zero_and_be_continuous_floors(self):
        with self.assertRaises(ValueError):
            normalize_tiers({"packaging": [{"from_qty": 1, "rate_cents": 100}]})
        with self.assertRaises(ValueError):
            normalize_tiers({"packaging": [
                {"from_qty": 0, "rate_cents": 100},
                {"from_qty": 0, "rate_cents": 90}]})

    def test_deduction_fixed_and_rate_capped_at_gross(self):
        self.assertEqual(150, apply_deduction(1000, {"fixed_cents": 50, "basis_points": 1000}))
        self.assertEqual(1000, apply_deduction(1000, {"fixed_cents": 9000}))
        self.assertEqual(0, apply_deduction(1000, None))

    def test_fractional_rate_rounds_half_even(self):
        tiers = normalize_tiers({"packaging": [{"from_qty": 0, "rate_cents": 0.0015}]})
        # 15 微分/件 * 3 件 = 45 微分，不足 1 分，按银行家舍入到 0 分。
        priced = price_rows([row("u1", "lic-a", "cp-1", 3)], tiers)[0]
        self.assertEqual(45, priced.gross_micro_cents)
        self.assertEqual(0, priced.gross_cents)


if __name__ == "__main__":
    unittest.main()
