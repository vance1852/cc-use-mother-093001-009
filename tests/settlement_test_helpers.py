"""结算服务测试共用的建档辅助。"""

from __future__ import annotations

from datetime import datetime, timezone

from pattern_license_settlement.clock import FixedClock
from pattern_license_settlement.service import SettlementService
from pattern_license_settlement.storage import Database


def build_service(tiers=None, deduction=None, guarantee=0) -> SettlementService:
    """建立带有一套标准主数据的内存服务：作品 xicheng、两家同一控制方的被许可方。"""

    database = Database()
    service = SettlementService(database, FixedClock(datetime(2026, 10, 4, tzinfo=timezone.utc)))
    service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="管理员", role="admin", party_id="org")
    for actor_id, name, role, party in (
            ("op", "运营", "operator", "org"),
            ("partner", "合作方", "partner", "cp-1"),
            ("h1", "权利人甲", "rights_holder", "p-1"),
            ("h2", "权利人乙", "rights_holder", "p-2"),
            ("au", "审计", "auditor", "org")):
        service.register_actor(request_id=f"a-{actor_id}", actor_id="admin",
                               new_actor_id=actor_id, display_name=name, role=role, party_id=party)
    service.register_work(request_id="req-work", actor_id="op", work_id="xicheng",
                          title="西城纹韵")
    tiers = tiers or {"packaging": [{"from_qty": 0, "rate_cents": 100},
                                    {"from_qty": 100, "rate_cents": 80}]}
    service.register_rate_card(request_id="req-rc", actor_id="op", rate_card_id="rc",
                               currency="CNY", tiers_by_use=tiers, deduction=deduction)
    service.register_work_version(
        request_id="req-ver", actor_id="op", version_id="v-1", work_id="xicheng", version_no=1,
        licensor_party_id="p-1", rate_card_id="rc",
        shares={"p-1": 6000, "p-2": 4000}, effective_from="2026-01-01")
    service.register_licensee(request_id="req-la", actor_id="op", licensee_id="lic-a",
                              name="甲公司", controlling_party_id="cp-1")
    service.register_licensee(request_id="req-lb", actor_id="op", licensee_id="lic-b",
                              name="乙公司", controlling_party_id="cp-1")
    service.register_license(
        request_id="req-l1", actor_id="op", license_id="l-1", version_id="v-1",
        licensee_id="lic-a", regions=["CN"], channels=["store", "online"],
        uses=["packaging"], valid_from="2026-01-01", guarantee_cents=guarantee,
        reporting_cycle="quarterly")
    service.register_license(
        request_id="req-l2", actor_id="op", license_id="l-2", version_id="v-1",
        licensee_id="lic-b", regions=["CN"], channels=["store", "online"],
        uses=["packaging"], valid_from="2026-01-01",
        reporting_cycle="quarterly")
    return service


def usage_row(dedup_key, *, quantity=1, channel="store", licensee_ref="a",
              occurred_on="2026-07-01", correction_of=None):
    row = {"dedup_key": dedup_key, "work_id": "xicheng", "region": "CN",
           "channel": channel, "use_type": "packaging", "quantity": quantity,
           "occurred_on": occurred_on}
    if correction_of:
        row["correction_of"] = correction_of
    return row
