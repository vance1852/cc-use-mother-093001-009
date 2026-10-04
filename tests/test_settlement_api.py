"""纹样授权结算服务的 HTTP 路由测试。"""

import unittest

from pattern_license_settlement.api import route

from settlement_test_helpers import build_service, usage_row


class SettlementApiTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_health_reports_ledger_and_audit(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["ledger_balanced"])
        self.assertEqual(0, payload["balances"]["ar"])

    def test_unknown_route_404(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_actor_header_drives_permissions(self):
        body = {"request_id": "req-bad", "licensee_id": "lic-a", "period_key": "2026-Q3",
                "records": [usage_row("k1")]}
        # 审计员只读。
        status, payload = route(self.service, "POST", "/usage-imports", body,
                                {"X-Actor-Id": "au"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_end_to_end_route_flow(self):
        status, payload = route(self.service, "POST", "/usage-imports",
                                {"request_id": "imp-1", "licensee_id": "lic-a",
                                 "period_key": "2026-Q3",
                                 "records": [usage_row("a1", quantity=60)]},
                                {"X-Actor-Id": "partner"})
        self.assertEqual(201, status)
        # 重放返回 200。
        status, payload = route(self.service, "POST", "/usage-imports",
                                {"request_id": "imp-1", "licensee_id": "lic-a",
                                 "period_key": "2026-Q3",
                                 "records": [usage_row("a1", quantity=60)]},
                                {"X-Actor-Id": "partner"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

        status, payload = route(self.service, "POST", "/billing",
                                {"period_key": "2026-Q3"}, {"X-Actor-Id": "op"})
        self.assertEqual(201, status)
        self.assertEqual(1, len(payload["entries"]))

        status, payload = route(self.service, "GET", "/entries?period_key=2026-Q3", None,
                                {"X-Actor-Id": "h1"})
        self.assertEqual(200, status)
        # 权利人甲只看到自己享有份额的作品版本分录。
        self.assertTrue(payload["items"])
        self.assertTrue(all(item["version_id"] == "v-1" for item in payload["items"]))

    def test_explain_entry_route(self):
        self.service.import_usage(request_id="imp-1", actor_id="partner",
                                  licensee_id="lic-a", period_key="2026-Q3",
                                  records=[usage_row("a1", quantity=5)])
        self.service.bill_period(actor_id="op", period_key="2026-Q3")
        entry_id = self.service.list_entries("op", "2026-Q3")[0].entry_id
        status, payload = route(self.service, "GET", f"/entries/{entry_id}/explain", None,
                                {"X-Actor-Id": "au"})
        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["distributions"]))
        self.assertEqual(1, len(payload["transactions"]))

    def test_recompute_route(self):
        status, payload = route(self.service, "GET", "/recompute?period_key=2026-Q3", None,
                                {"X-Actor-Id": "au"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ledger_balanced"])
        self.assertEqual([], payload["mismatches"])

    def test_settlement_summary_forbidden_for_partner(self):
        status, payload = route(self.service, "GET", "/settlements/2026-Q3", None,
                                {"X-Actor-Id": "partner"})
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
