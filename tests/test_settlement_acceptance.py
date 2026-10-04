import unittest

from pattern_license_settlement.acceptance import run


class SettlementAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["ledger_balanced"])
        self.assertTrue(result["recompute_ok"])
        self.assertTrue(result["payment_order_preserved"])
        self.assertTrue(result["restart_resumed_in_order"])
        self.assertTrue(result["holder_visibility_scoped"])


if __name__ == "__main__":
    unittest.main()
