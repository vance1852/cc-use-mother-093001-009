import unittest
from datetime import datetime, timezone

from creative_program_foundation.clock import FixedClock

from pattern_license_billing.api import route
from pattern_license_billing.service import LicensingService
from pattern_license_billing.storage import LicensingDatabase


TIERS = [{"lower_bound_cents": 0, "rate_basis_points": 1000},
         {"lower_bound_cents": 100000, "rate_basis_points": 2000}]


class LicensingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = LicensingDatabase(":memory:")
        self.service = LicensingService(self.database,
                                        FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        r = self.service.register_principal(request_id="r-op", actor_id="bootstrap",
                                            principal_id="op", kind="operator",
                                            display_name="运营")
        self.assertFalse(r.replayed)
        self.service.register_holder(request_id="r-h", actor_id="op", holder_id="h1", name="甲")
        self.service.register_work(request_id="r-w", actor_id="op", work_id="w1", title="纹韵")
        self.service.register_work_version(request_id="r-wv", actor_id="op", work_id="w1",
                                           version_no=1, content_hash="x")
        self.service.register_entitlement_version(
            request_id="r-ev", actor_id="op", work_id="w1", version_no=1,
            effective_from="2026-01-01",
            shares=[{"holder_id": "h1", "basis_points": 10000}])
        self.service.register_control_group(request_id="r-g", actor_id="op", group_id="g1",
                                            name="集团")
        self.service.register_licensee(request_id="r-l", actor_id="op", licensee_id="p1",
                                       control_group_id="g1", name="店")
        self.service.register_license(request_id="r-li", actor_id="op", license_id="lic1",
                                      licensee_id="p1", work_id="w1", work_version_no=1,
                                      territory="*", channel="*", usage_purpose="*",
                                      valid_from="2026-01-01")
        self.service.register_rate_version(request_id="r-rv", actor_id="op", license_id="lic1",
                                           version_no=1, effective_from="2026-01-01", tiers=TIERS)
        self.service.register_reporting_cycle(request_id="r-c", actor_id="op", license_id="lic1",
                                              period_key="2026-Q3", kind="quarterly",
                                              deadline="2026-10-10")

    def tearDown(self):
        self.database.close()

    def _headers(self, actor="op"):
        return {"X-Actor-Id": actor}

    def test_health(self):
        status, body = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_import_batch_and_read_entries(self):
        status, body = route(self.service, "POST", "/usage-batches", {
            "request_id": "b1", "licensee_id": "p1", "period_key": "2026-Q3",
            "source_ref": "f1",
            "items": [{"source_record_key": "s1", "work_id": "w1", "territory": "CN",
                       "channel": "packaging", "usage_purpose": "box",
                       "occurred_at": "2026-08-01", "quantity": 1,
                       "gross_revenue_cents": 50000}]}, self._headers())
        self.assertEqual(201, status)
        self.assertEqual("usage_batch", body["resource_type"])

        status, body = route(self.service, "GET", "/entries?period_key=2026-Q3", None,
                             self._headers())
        self.assertEqual(200, status)
        self.assertEqual(5000, body["items"][0]["amount_cents"])
        self.assertEqual([{"holder_id": "h1", "amount_cents": 5000}],
                         body["items"][0]["allocations"])

    def test_explain_entry_describes_amount(self):
        route(self.service, "POST", "/usage-batches", {
            "request_id": "b1", "licensee_id": "p1", "period_key": "2026-Q3", "source_ref": "f1",
            "items": [{"source_record_key": "s1", "work_id": "w1", "territory": "CN",
                       "channel": "packaging", "usage_purpose": "box",
                       "occurred_at": "2026-08-01", "gross_revenue_cents": 50000}]},
            self._headers())
        entry = self.service.list_entries("op")[0]
        status, body = route(self.service, "GET", f"/entries/{entry.entry_id}", None,
                             self._headers())
        self.assertEqual(200, status)
        self.assertEqual(50000, body["trace"]["gross_cents"])
        self.assertEqual(1, body["trace"]["landing_tier_no"])

    def test_close_then_totals_balance(self):
        route(self.service, "POST", "/usage-batches", {
            "request_id": "b1", "licensee_id": "p1", "period_key": "2026-Q3", "source_ref": "f1",
            "items": [{"source_record_key": "s1", "work_id": "w1", "territory": "CN",
                       "channel": "packaging", "usage_purpose": "box",
                       "occurred_at": "2026-08-01", "gross_revenue_cents": 50000}]},
            self._headers())
        status, body = route(self.service, "POST", "/periods/close", {
            "request_id": "close", "period_key": "2026-Q3"}, self._headers())
        self.assertEqual(201, status)
        status, body = route(self.service, "GET", "/totals?period_key=2026-Q3", None,
                             self._headers())
        self.assertEqual(200, status)
        self.assertTrue(body["balanced"])
        self.assertEqual(5000, body["receivable_cents"])

    def test_missing_actor_is_rejected(self):
        status, body = route(self.service, "POST", "/works",
                             {"request_id": "w2", "work_id": "w2", "title": "x"}, {})
        self.assertEqual(404, status)

    def test_unknown_route(self):
        status, body = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
