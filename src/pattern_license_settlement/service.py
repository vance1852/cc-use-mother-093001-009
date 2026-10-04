"""纹样授权计费与结算领域服务。

职责链：
  主数据登记（作品/版本/费率卡/份额/被许可方/许可证/保底金/报送周期）
  -> 用量幂等导入、去重、范围匹配
  -> 按授权与费率版本生成不可覆盖的计费分录（同一实际控制方合并阶梯）
  -> 争议托管 -> 关账队列（冻结已确认分录、保底补差）
  -> 回款 -> 按原顺序推进的付款队列
  -> 迟到更正以追补/冲销进入后续期间；超范围/失效形成追偿项目

所有金额为整数分；复式账保证：
    应收(ar) + 追偿(claim) + 现金(cash) = 清算(clearing) + 托管(escrow)
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Callable

from . import ledger
from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import (ClosedPeriodError, ConflictError, NotFoundError, OutOfOrderError,
                     PermissionDenied, ValidationError)
from .models import (Actor, BillingEntry, Claim, Dispute, Distribution, License, Licensee,
                     Payment, RateCard, RateTier, Settlement, UsageRecord, Work, WorkVersion,
                     WriteReceipt)
from .pricing import MICRO_PER_CENT, PricingRow, apply_deduction, normalize_tiers, price_rows
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ROLES = frozenset({"admin", "operator", "partner", "rights_holder", "auditor"})
PERIOD = re.compile(r"^\d{4}-(?:[01]\d|Q[1-4])$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SettlementService:
    """协调权限、幂等、计价、复式账、持久队列与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ================================================================ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _cents(self, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"{field} 不能是布尔值")
        value = int(value)
        if value < 0:
            raise ValidationError(f"{field} 不能为负")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["party_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def register_actor(self, *, request_id: str, actor_id: str, new_actor_id: str,
                       display_name: str, role: str, party_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "new_actor_id": new_actor_id, "role": role,
                   "party_id": party_id, "display_name": display_name}
        with self.database.transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM actors").fetchone()["c"]
            if count:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin")
            elif actor_id != "bootstrap":
                raise PermissionDenied("首位管理员必须由 bootstrap 创建")
            new_actor_id = self._id(new_actor_id, "new_actor_id")
            party_id = self._id(party_id, "party_id")
            display_name = self._text(display_name, "display_name")
            if role not in ROLES:
                raise ValidationError("role 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO actors(actor_id,display_name,role,party_id,active,created_at) "
                        "VALUES(?,?,?,?,1,?)",
                        (new_actor_id, display_name, role, party_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("操作者编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="actor.registered",
                            resource_type="actor", resource_id=new_actor_id,
                            detail={"role": role, "party_id": party_id})
                return "actor", new_actor_id, {"actor_id": new_actor_id}

            return self._idempotent(connection, request_id=request_id, action="register_actor",
                                    payload=payload, create=create)

    # ============================================================== 主数据

    def register_work(self, *, request_id: str, actor_id: str, work_id: str, title: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work_id = self._id(work_id, "work_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,title,active,created_at) VALUES(?,?,1,?)",
                        (work_id, title, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="work.registered",
                            resource_type="work", resource_id=work_id, detail={"title": title})
                return "work", work_id, {"work_id": work_id}

            return self._idempotent(connection, request_id=request_id, action="register_work",
                                    payload=payload, create=create)

    def register_rate_card(self, *, request_id: str, actor_id: str, rate_card_id: str,
                           currency: str, tiers_by_use: dict[str, list[dict[str, Any]]],
                           deduction: dict[str, Any] | None = None) -> WriteReceipt:
        if not isinstance(tiers_by_use, dict) or not tiers_by_use:
            raise ValidationError("tiers_by_use 必须是非空对象")
        try:
            normalize_tiers(tiers_by_use)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        deduction = deduction or {}
        if deduction.get("basis_points") is not None and not 0 <= int(deduction["basis_points"]) <= 10_000:
            raise ValidationError("扣减比例必须在 0..10000 个基点之间")
        if deduction.get("fixed_cents") is not None and int(deduction["fixed_cents"]) < 0:
            raise ValidationError("固定扣减不能为负")
        payload = {"actor_id": actor_id, "rate_card_id": rate_card_id, "currency": currency,
                   "tiers_by_use": tiers_by_use, "deduction": deduction}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            rate_card_id = self._id(rate_card_id, "rate_card_id")
            currency = self._text(currency, "currency", 3).upper()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO rate_cards(rate_card_id,currency,tiers_json,deduction_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (rate_card_id, currency, canonical_json(tiers_by_use),
                         canonical_json(deduction), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("费率卡编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="rate_card.registered",
                            resource_type="rate_card", resource_id=rate_card_id,
                            detail={"currency": currency, "uses": sorted(tiers_by_use)})
                return "rate_card", rate_card_id, {"rate_card_id": rate_card_id}

            return self._idempotent(connection, request_id=request_id, action="register_rate_card",
                                    payload=payload, create=create)

    def register_work_version(self, *, request_id: str, actor_id: str, version_id: str,
                              work_id: str, version_no: int, licensor_party_id: str,
                              rate_card_id: str, shares: dict[str, int],
                              effective_from: str, effective_to: str | None = None) -> WriteReceipt:
        if not isinstance(shares, dict) or not shares:
            raise ValidationError("shares 必须是非空的权利人份额映射")
        shares = {self._id(str(k), "shares.party"): int(v) for k, v in shares.items()}
        if any(v <= 0 for v in shares.values()) or sum(shares.values()) != 10_000:
            raise ValidationError("权利人份额必须为正且基点合计为 10000")
        if not DATE.fullmatch(effective_from) or (effective_to and not DATE.fullmatch(effective_to)):
            raise ValidationError("生效日期必须是 YYYY-MM-DD")
        if effective_to and effective_to < effective_from:
            raise ValidationError("生效结束不能早于开始")
        payload = {"actor_id": actor_id, "version_id": version_id, "work_id": work_id,
                   "version_no": version_no, "licensor_party_id": licensor_party_id,
                   "rate_card_id": rate_card_id, "shares": shares,
                   "effective_from": effective_from, "effective_to": effective_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version_id = self._id(version_id, "version_id")
            licensor_party_id = self._id(licensor_party_id, "licensor_party_id")
            if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            if connection.execute("SELECT 1 FROM rate_cards WHERE rate_card_id=?",
                                  (rate_card_id,)).fetchone() is None:
                raise NotFoundError("费率卡不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO work_versions(version_id,work_id,version_no,licensor_party_id,"
                        "rate_card_id,shares_json,effective_from,effective_to,active,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,1,?)",
                        (version_id, work_id, int(version_no), licensor_party_id, rate_card_id,
                         canonical_json(shares), effective_from, effective_to, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("版本编号已经存在或版本号重复") from exc
                self._audit(connection, actor_id=actor_id, action="work_version.registered",
                            resource_type="work_version", resource_id=version_id,
                            detail={"work_id": work_id, "version_no": int(version_no),
                                    "shares": shares})
                return "work_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_work_version", payload=payload, create=create)

    def register_licensee(self, *, request_id: str, actor_id: str, licensee_id: str,
                          name: str, controlling_party_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "licensee_id": licensee_id, "name": name,
                   "controlling_party_id": controlling_party_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            licensee_id = self._id(licensee_id, "licensee_id")
            controlling_party_id = self._id(controlling_party_id, "controlling_party_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO licensees(licensee_id,name,controlling_party_id,active,created_at) "
                        "VALUES(?,?,?,1,?)",
                        (licensee_id, name, controlling_party_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("被许可方编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="licensee.registered",
                            resource_type="licensee", resource_id=licensee_id,
                            detail={"controlling_party_id": controlling_party_id})
                return "licensee", licensee_id, {"licensee_id": licensee_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_licensee", payload=payload, create=create)

    def register_license(self, *, request_id: str, actor_id: str, license_id: str,
                         version_id: str, licensee_id: str, regions: list[str],
                         channels: list[str], uses: list[str], valid_from: str,
                         valid_to: str | None = None, guarantee_cents: int = 0,
                         reporting_cycle: str = "monthly") -> WriteReceipt:
        if not regions or not channels or not uses:
            raise ValidationError("地域、渠道、用途均不能为空")
        if reporting_cycle not in ("monthly", "quarterly"):
            raise ValidationError("报送周期只能是 monthly 或 quarterly")
        if not DATE.fullmatch(valid_from) or (valid_to and not DATE.fullmatch(valid_to)):
            raise ValidationError("有效期必须是 YYYY-MM-DD")
        guarantee_cents = self._cents(guarantee_cents, "guarantee_cents")
        payload = {"actor_id": actor_id, "license_id": license_id, "version_id": version_id,
                   "licensee_id": licensee_id, "regions": sorted(regions),
                   "channels": sorted(channels), "uses": sorted(uses),
                   "valid_from": valid_from, "valid_to": valid_to,
                   "guarantee_cents": guarantee_cents, "reporting_cycle": reporting_cycle}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            license_id = self._id(license_id, "license_id")
            version = connection.execute("SELECT * FROM work_versions WHERE version_id=?",
                                         (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("作品版本不存在")
            if connection.execute("SELECT 1 FROM licensees WHERE licensee_id=?",
                                  (licensee_id,)).fetchone() is None:
                raise NotFoundError("被许可方不存在")
            card = connection.execute("SELECT * FROM rate_cards WHERE rate_card_id=?",
                                      (version["rate_card_id"],)).fetchone()
            allowed_uses = set(json.loads(card["tiers_json"]))
            if not set(uses) <= allowed_uses:
                raise ValidationError(f"用途超出费率卡范围: {sorted(set(uses) - allowed_uses)}")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO licenses(license_id,version_id,licensee_id,regions_json,"
                        "channels_json,uses_json,valid_from,valid_to,guarantee_cents,"
                        "reporting_cycle,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'active',?)",
                        (license_id, version_id, licensee_id, canonical_json(sorted(regions)),
                         canonical_json(sorted(channels)), canonical_json(sorted(uses)),
                         valid_from, valid_to, guarantee_cents, reporting_cycle, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("许可证编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="license.registered",
                            resource_type="license", resource_id=license_id,
                            detail={"version_id": version_id, "licensee_id": licensee_id,
                                    "guarantee_cents": guarantee_cents})
                return "license", license_id, {"license_id": license_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_license", payload=payload, create=create)

    def update_license_status(self, *, request_id: str, actor_id: str,
                              license_id: str, status: str) -> WriteReceipt:
        if status not in ("active", "expired", "revoked"):
            raise ValidationError("许可证状态只能是 active、expired 或 revoked")
        payload = {"actor_id": actor_id, "license_id": license_id, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM licenses WHERE license_id=?",
                                  (license_id,)).fetchone() is None:
                raise NotFoundError("许可证不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE licenses SET status=? WHERE license_id=?",
                                   (status, license_id))
                self._audit(connection, actor_id=actor_id, action="license.status_changed",
                            resource_type="license", resource_id=license_id,
                            detail={"status": status})
                return "license", license_id, {"license_id": license_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_license_status", payload=payload, create=create)

    # ========================================================== 用量导入

    def import_usage(self, *, request_id: str, actor_id: str, licensee_id: str,
                     period_key: str, records: list[dict[str, Any]]) -> WriteReceipt:
        if not PERIOD.fullmatch(period_key or ""):
            raise ValidationError("period_key 必须是 YYYY-MM 或 YYYY-Qn")
        if not isinstance(records, list) or not records:
            raise ValidationError("records 必须是非空数组")
        payload = {"actor_id": actor_id, "licensee_id": licensee_id,
                   "period_key": period_key, "records": records}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            licensee = connection.execute("SELECT * FROM licensees WHERE licensee_id=?",
                                          (licensee_id,)).fetchone()
            if licensee is None:
                raise NotFoundError("被许可方不存在")
            if actor.role == "partner" and actor.party_id != licensee["controlling_party_id"]:
                raise PermissionDenied("只能报送本方实际控制的被许可方用量")
            self._require(actor, "admin", "operator", "partner")
            period = connection.execute("SELECT * FROM periods WHERE period_key=?",
                                        (period_key,)).fetchone()
            if period is not None and period["status"] == "closed":
                raise ClosedPeriodError("该期间已关账，更正请报送至当前开放期间并使用 correction_of")
            normalized = [self._normalize_usage_row(connection, licensee_id, raw) for raw in records]

            def create() -> tuple[str, str, dict[str, Any]]:
                import_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO usage_imports(import_id,request_id,licensee_id,period_key,"
                    "row_count,new_count,duplicate_count,imported_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (import_id, request_id, licensee_id, period_key, len(normalized), 0, 0,
                     actor_id, self._now()),
                )
                new_count = duplicate_count = 0
                for item in normalized:
                    # 去重：同一被许可方同一业务键只接受一次；内容不同即冲突。
                    existing = connection.execute(
                        "SELECT * FROM usage_records WHERE licensee_id=? AND dedup_key=?",
                        (licensee_id, item["dedup_key"]),
                    ).fetchone()
                    if existing:
                        if existing["quantity"] != item["quantity"] or \
                                existing["work_id"] != item["work_id"]:
                            raise ConflictError(f"业务键 {item['dedup_key']} 已用于不同内容")
                        duplicate_count += 1
                        continue
                    if item["corrects_usage_id"]:
                        # 更正记录沿用原始用量的授权匹配，避免因当前许可证失效误入追偿。
                        original_usage = connection.execute(
                            "SELECT matched_license_id, scope_reason FROM usage_records "
                            "WHERE usage_id=?", (item["corrects_usage_id"],)).fetchone()
                        matched_id = original_usage["matched_license_id"]
                        scope_reason = original_usage["scope_reason"]
                    else:
                        matched_id, scope_reason = self._match_license(
                            connection, licensee_id, item["work_id"], item["region"],
                            item["channel"], item["use_type"], item["occurred_on"])
                    usage_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO usage_records(usage_id,import_id,licensee_id,dedup_key,work_id,"
                        "region,channel,use_type,quantity,occurred_on,period_key,original_period,"
                        "corrects_usage_id,matched_license_id,in_scope,scope_reason,bill_status,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                        (usage_id, import_id, licensee_id, item["dedup_key"], item["work_id"],
                         item["region"], item["channel"], item["use_type"], item["quantity"],
                         item["occurred_on"], period_key, item["original_period"],
                         item["corrects_usage_id"], matched_id,
                         1 if matched_id else 0, scope_reason, self._now()),
                    )
                    new_count += 1
                connection.execute("UPDATE usage_imports SET new_count=?, duplicate_count=? WHERE import_id=?",
                                   (new_count, duplicate_count, import_id))
                self._audit(connection, actor_id=actor_id, action="usage.imported",
                            resource_type="usage_import", resource_id=import_id,
                            detail={"licensee_id": licensee_id, "period_key": period_key,
                                    "new": new_count, "duplicate": duplicate_count})
                return "usage_import", import_id, {"import_id": import_id,
                                                   "new_count": new_count,
                                                   "duplicate_count": duplicate_count}

            return self._idempotent(connection, request_id=request_id, action="import_usage",
                                    payload=payload, create=create)

    def _normalize_usage_row(self, connection, licensee_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        for field in ("dedup_key", "work_id", "region", "channel", "use_type", "occurred_on"):
            if not str(raw.get(field, "")).strip():
                raise ValidationError(f"用量记录缺少字段 {field}")
        dedup_key = self._id(raw["dedup_key"], "dedup_key")
        work_id = self._id(raw["work_id"], "work_id")
        region = self._text(raw["region"], "region", 64)
        channel = self._text(raw["channel"], "channel", 64)
        use_type = self._text(raw["use_type"], "use_type", 64)
        occurred_on = self._text(raw["occurred_on"], "occurred_on", 10)
        if not DATE.fullmatch(occurred_on):
            raise ValidationError("occurred_on 必须是 YYYY-MM-DD")
        if isinstance(raw.get("quantity"), bool) or not int(raw["quantity"]):
            raise ValidationError("quantity 必须是非零整数（退货/冲销用负数）")
        quantity = int(raw["quantity"])
        if abs(quantity) > 1_000_000_000:
            raise ValidationError("quantity 超出允许范围")
        if connection.execute("SELECT 1 FROM works WHERE work_id=? AND active=1",
                              (work_id,)).fetchone() is None:
            raise NotFoundError(f"作品 {work_id} 不存在或已停用")
        corrects_usage_id = None
        original_period = None
        correction_of = raw.get("correction_of")
        if correction_of:
            correction_of = self._id(correction_of, "correction_of")
            original = connection.execute(
                "SELECT * FROM usage_records WHERE licensee_id=? AND dedup_key=?",
                (licensee_id, correction_of),
            ).fetchone()
            if original is None:
                raise NotFoundError(f"被更正的原始用量 {correction_of} 不存在")
            if original["work_id"] != work_id:
                raise ValidationError("更正记录的作品必须与原始用量一致")
            if quantity * original["quantity"] < 0 and abs(quantity) > abs(original["quantity"]):
                raise ValidationError("退货数量不能超过原始用量")
            if quantity < 0:
                already = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS total FROM usage_records "
                    "WHERE corrects_usage_id=?", (original["usage_id"],)).fetchone()["total"]
                if abs(already) + abs(quantity) > abs(original["quantity"]):
                    raise ValidationError("累计退货数量不能超过原始用量")
            corrects_usage_id = original["usage_id"]
            original_period = original["period_key"]
        return {"dedup_key": dedup_key, "work_id": work_id, "region": region, "channel": channel,
                "use_type": use_type, "quantity": quantity, "occurred_on": occurred_on,
                "corrects_usage_id": corrects_usage_id, "original_period": original_period}

    def _match_license(self, connection, licensee_id: str, work_id: str, region: str,
                       channel: str, use_type: str, occurred_on: str) -> tuple[str | None, str | None]:
        """返回 (license_id, 未命中原因)；有效且范围命中时返回许可证主键。"""

        rows = connection.execute(
            "SELECT l.*, wv.work_id AS wv_work, wv.version_no AS version_no "
            "FROM licenses l JOIN work_versions wv ON l.version_id=wv.version_id "
            "WHERE l.licensee_id=? AND wv.work_id=? "
            "AND l.valid_from<=? AND (l.valid_to IS NULL OR l.valid_to>=?) "
            "ORDER BY wv.version_no DESC, l.valid_from DESC, l.license_id",
            (licensee_id, work_id, occurred_on, occurred_on),
        ).fetchall()

        def covers(row) -> bool:
            return region in set(json.loads(row["regions_json"])) and \
                channel in set(json.loads(row["channels_json"])) and \
                use_type in set(json.loads(row["uses_json"]))

        for row in rows:
            if row["status"] == "active" and covers(row):
                return row["license_id"], None
        # 存在日期内、范围匹配但已失效的许可证 -> 失效；否则视为超范围使用。
        for row in rows:
            if covers(row):
                return None, "license_expired"
        return None, "out_of_scope"

    # ============================================================ 计费分录

    def _ensure_period(self, connection, period_key: str) -> None:
        if not PERIOD.fullmatch(period_key):
            raise ValidationError("period_key 必须是 YYYY-MM 或 YYYY-Qn")
        connection.execute(
            "INSERT INTO periods(period_key,status) VALUES(?, 'open') "
            "ON CONFLICT(period_key) DO NOTHING", (period_key,),
        )

    def _period_status(self, connection, period_key: str) -> str | None:
        row = connection.execute("SELECT status FROM periods WHERE period_key=?",
                                 (period_key,)).fetchone()
        return row["status"] if row else None

    def _license_context(self, connection, license_id: str):
        return connection.execute(
            "SELECT l.*, lz.controlling_party_id AS controlling_party_id, "
            "wv.work_id AS wv_work, wv.shares_json AS shares_json, "
            "rc.tiers_json AS tiers_json, rc.deduction_json AS deduction_json "
            "FROM licenses l "
            "JOIN licensees lz ON l.licensee_id=lz.licensee_id "
            "JOIN work_versions wv ON l.version_id=wv.version_id "
            "JOIN rate_cards rc ON wv.rate_card_id=rc.rate_card_id "
            "WHERE l.license_id=?", (license_id,),
        ).fetchone()

    def bill_period(self, *, actor_id: str, period_key: str) -> dict[str, Any]:
        """为开放期间的待计费用量生成分录；同一控制方数量沿持久游标合并。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._ensure_period(connection, period_key)
            if self._period_status(connection, period_key) == "closed":
                raise ClosedPeriodError("期间已关账")
            pending = connection.execute(
                "SELECT * FROM usage_records WHERE period_key=? AND bill_status='pending' "
                "ORDER BY occurred_on, created_at, usage_id", (period_key,),
            ).fetchall()

            positive_rows: list[PricingRow] = []
            contexts: dict[str, Any] = {}
            created: list[str] = []
            claims: list[str] = []
            claimed: set[str] = set()

            # 超范围/失效使用不形成应收，直接进入待追偿项目；
            # 对已入追偿的原始用量再做退货更正时不重复立项（由核定金额净额体现）。
            for usage in pending:
                if not usage["in_scope"]:
                    if usage["corrects_usage_id"]:
                        connection.execute(
                            "UPDATE usage_records SET bill_status='superseded' WHERE usage_id=?",
                            (usage["usage_id"],))
                        continue
                    reason = usage["scope_reason"] or "out_of_scope"
                    claim_id = self._create_claim(connection, usage, reason=reason)
                    connection.execute(
                        "UPDATE usage_records SET bill_status='claim' WHERE usage_id=?",
                        (usage["usage_id"],))
                    claimed.add(usage["usage_id"])
                    claims.append(claim_id)
                    continue
                ctx = self._license_context(connection, usage["matched_license_id"])
                contexts[usage["usage_id"]] = ctx
                if usage["quantity"] > 0:
                    positive_rows.append(PricingRow(
                        usage_id=usage["usage_id"], license_id=ctx["license_id"],
                        work_id=ctx["wv_work"], version_id=ctx["version_id"],
                        licensee_id=usage["licensee_id"],
                        controlling_party_id=ctx["controlling_party_id"],
                        region=usage["region"], channel=usage["channel"],
                        use_type=usage["use_type"], quantity=usage["quantity"],
                        occurred_on=usage["occurred_on"], sort_key=period_key))

            # 每个 (版本,实际控制方,用途) 组沿此前批次留下的游标继续计价，
            # 因此分批报送、多个被许可方主体拆分报送都无法规避阶梯。
            start_cursors = self._load_cursors(connection, period_key)
            priced_map = self._price_continuously(positive_rows, contexts, start_cursors)

            for usage in pending:
                if usage["usage_id"] in claimed or not usage["in_scope"]:
                    continue
                ctx = contexts[usage["usage_id"]]
                correction_of = usage["corrects_usage_id"]
                if usage["quantity"] > 0:
                    priced = priced_map[usage["usage_id"]]
                    gross = priced.gross_cents
                    deduction = apply_deduction(gross, json.loads(ctx["deduction_json"]))
                    entry_type = "supplement" if correction_of else "regular"
                    reversed_entry_id = None
                    cursor_before = priced.cursor_before
                    unit_rate = priced.unit_rate_micro_cents
                    segments = priced.segments
                else:
                    # 退货/冲销：按原始分录的毛额、扣减额成比例红冲，不占用阶梯游标。
                    original = connection.execute(
                        "SELECT * FROM billing_entries WHERE usage_id=? "
                        "ORDER BY created_at, entry_id LIMIT 1", (correction_of,),
                    ).fetchone()
                    if original is None:
                        raise ConflictError("被冲销的原始用量尚未计费，无法红冲")
                    ratio = (-usage["quantity"]) / original["quantity"]
                    gross = -self._ratio_amount(original["gross_cents"], ratio)
                    deduction = -self._ratio_amount(original["deduction_cents"], ratio)
                    entry_type = "reversal"
                    reversed_entry_id = original["entry_id"]
                    cursor_before = original["cursor_before"]
                    unit_rate = original["unit_rate_micro_cents"]
                    segments = tuple(json.loads(original["rate_snapshot_json"]).get("segments", []))
                net = gross - deduction
                entry_id = self._insert_entry(
                    connection, period_key=period_key, usage=usage, ctx=ctx,
                    cursor_before=cursor_before, unit_rate=unit_rate,
                    gross_cents=gross, deduction_cents=deduction, net_cents=net,
                    entry_type=entry_type, reversed_entry_id=reversed_entry_id,
                    rate_snapshot={"tiers": json.loads(ctx["tiers_json"]),
                                   "deduction": json.loads(ctx["deduction_json"]),
                                   "segments": [list(s) for s in segments],
                                   "cursor_before": cursor_before})
                self._distribute(connection, ref_type="entry", ref_id=entry_id,
                                 shares_json=ctx["shares_json"], net_cents=net,
                                 period_key=period_key, license_id=ctx["license_id"])
                ledger.post(connection, event_type="billing", ref_type="billing_entry",
                            ref_id=entry_id, period_key=period_key,
                            memo=f"{entry_type} {usage['use_type']} x{usage['quantity']}",
                            lines={ledger.ACCOUNT_RECEIVABLE: net,
                                   ledger.ACCOUNT_CLEARING: -net},
                            created_at=self._now())
                connection.execute(
                    "UPDATE usage_records SET bill_status='billed' WHERE usage_id=?",
                    (usage["usage_id"],))
                created.append(entry_id)

            self._audit(connection, actor_id=actor_id, action="period.billed",
                        resource_type="period", resource_id=period_key,
                        detail={"entries": len(created), "claims": len(claims)})
            return {"period_key": period_key, "entries": created, "claims": claims}

    def _price_continuously(self, rows: list[PricingRow], contexts: dict[str, Any],
                            start_cursors: dict[tuple[str, str, str], int]) -> dict[str, Any]:
        """组内按发生时间排序，从持久游标起步逐行计价。"""

        from .pricing import _price_segment

        groups: dict[tuple[str, str, str], list[PricingRow]] = {}
        for row in rows:
            groups.setdefault((row.version_id, row.controlling_party_id, row.use_type),
                              []).append(row)
        priced_map: dict[str, Any] = {}
        for key, members in groups.items():
            members.sort(key=lambda item: (item.occurred_on, item.licensee_id, item.usage_id))
            tiers = normalize_tiers(json.loads(contexts[members[0].usage_id]["tiers_json"]))[key[2]]
            cursor = start_cursors.get(key, 0)
            for row in members:
                total_micro, segments = _price_segment(tiers, cursor, row.quantity)
                blended = total_micro // row.quantity
                gross_cents = int((Decimal(total_micro) / MICRO_PER_CENT).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN))
                priced_map[row.usage_id] = _Priced(cursor, blended, total_micro,
                                                   gross_cents, segments)
                cursor += row.quantity
        return priced_map

    def _load_cursors(self, connection, period_key: str) -> dict[tuple[str, str, str], int]:
        """读取各组本期间已计费的正向累计数量（红冲不占用游标）。"""

        cursors: dict[tuple[str, str, str], int] = {}
        rows = connection.execute(
            "SELECT version_id, controlling_party_id, use_type, "
            "MAX(cursor_before + quantity) AS cursor_end "
            "FROM billing_entries WHERE period_key=? AND quantity > 0 "
            "GROUP BY version_id, controlling_party_id, use_type",
            (period_key,),
        ).fetchall()
        for row in rows:
            cursors[(row["version_id"], row["controlling_party_id"], row["use_type"])] = \
                row["cursor_end"]
        return cursors

    @staticmethod
    def _ratio_amount(amount: int, ratio: float) -> int:
        return int((Decimal(abs(amount)) * Decimal(str(ratio))).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN))

    def _create_claim(self, connection, usage, *, reason: str) -> str:
        licensee = connection.execute(
            "SELECT controlling_party_id FROM licensees WHERE licensee_id=?",
            (usage["licensee_id"],),
        ).fetchone()
        claim_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO claims(claim_id,usage_id,licensee_id,controlling_party_id,work_id,"
            "region,channel,use_type,quantity,occurred_on,reason,amount_cents,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,0, 'open',?)",
            (claim_id, usage["usage_id"], usage["licensee_id"],
             licensee["controlling_party_id"], usage["work_id"], usage["region"],
             usage["channel"], usage["use_type"], usage["quantity"],
             usage["occurred_on"], reason, self._now()),
        )
        self._audit(connection, actor_id="system", action="claim.opened",
                    resource_type="claim", resource_id=claim_id,
                    detail={"reason": reason, "usage_id": usage["usage_id"],
                            "licensee_id": usage["licensee_id"]})
        return claim_id

    def assess_claim(self, *, request_id: str, actor_id: str, claim_id: str,
                     amount_cents: int) -> WriteReceipt:
        """核定追偿金额并计入追偿应收，按作品版本份额预先分配。"""

        amount_cents = self._cents(amount_cents, "amount_cents")
        if amount_cents <= 0:
            raise ValidationError("追偿金额必须为正")
        payload = {"actor_id": actor_id, "claim_id": claim_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            claim = connection.execute("SELECT * FROM claims WHERE claim_id=?",
                                       (claim_id,)).fetchone()
            if claim is None:
                raise NotFoundError("追偿项目不存在")
            if claim["status"] != "open":
                raise ConflictError("追偿项目已核定")
            usage = connection.execute("SELECT * FROM usage_records WHERE usage_id=?",
                                       (claim["usage_id"],)).fetchone()
            shares = self._shares_for_usage(connection, usage)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE claims SET amount_cents=?, status='assessed', assessed_at=? WHERE claim_id=?",
                    (amount_cents, self._now(), claim_id))
                self._distribute(connection, ref_type="claim", ref_id=claim_id,
                                 shares_json=canonical_json(shares), net_cents=amount_cents,
                                 period_key=None, license_id=None)
                ledger.post(connection, event_type="claim.assessed", ref_type="claim",
                            ref_id=claim_id,
                            memo=f"追偿核定 {claim['reason']}",
                            lines={ledger.ACCOUNT_CLAIM: amount_cents,
                                   ledger.ACCOUNT_CLEARING: -amount_cents},
                            created_at=self._now())
                self._audit(connection, actor_id=actor_id, action="claim.assessed",
                            resource_type="claim", resource_id=claim_id,
                            detail={"amount_cents": amount_cents})
                return "claim", claim_id, {"claim_id": claim_id, "amount_cents": amount_cents}

            return self._idempotent(connection, request_id=request_id, action="assess_claim",
                                    payload=payload, create=create)

    def _shares_for_usage(self, connection, usage) -> dict[str, int]:
        row = connection.execute(
            "SELECT wv.shares_json AS shares_json FROM works w "
            "JOIN work_versions wv ON wv.work_id=w.work_id "
            "WHERE w.work_id=? ORDER BY wv.version_no DESC LIMIT 1", (usage["work_id"],),
        ).fetchone()
        return json.loads(row["shares_json"])

    def recover_claim(self, *, request_id: str, actor_id: str, claim_id: str,
                      amount_cents: int) -> WriteReceipt:
        """登记追偿回款并把对权利人的付款排入付款队列。"""

        amount_cents = self._cents(amount_cents, "amount_cents")
        payload = {"actor_id": actor_id, "claim_id": claim_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            claim = connection.execute("SELECT * FROM claims WHERE claim_id=?",
                                       (claim_id,)).fetchone()
            if claim is None:
                raise NotFoundError("追偿项目不存在")
            recovered = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM cash_receipts WHERE claim_id=?",
                (claim_id,)).fetchone()["total"]
            if recovered + amount_cents > claim["amount_cents"]:
                raise ConflictError("追偿回款超过核定金额")

            def create() -> tuple[str, str, dict[str, Any]]:
                receipt_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cash_receipts(receipt_id,request_id,claim_id,licensee_id,"
                    "amount_cents,received_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (receipt_id, request_id, claim_id, claim["licensee_id"],
                     amount_cents, actor_id, self._now()))
                ledger.post(connection, event_type="cash.recovered", ref_type="cash_receipt",
                            ref_id=receipt_id, memo=f"追偿回款 {claim_id}",
                            lines={ledger.ACCOUNT_CASH: amount_cents,
                                   ledger.ACCOUNT_CLAIM: -amount_cents},
                            created_at=self._now())
                if recovered + amount_cents == claim["amount_cents"]:
                    connection.execute(
                        "UPDATE claims SET status='recovered', resolved_at=? WHERE claim_id=?",
                        (self._now(), claim_id))
                    for dist in connection.execute(
                            "SELECT * FROM distributions WHERE ref_type='claim' AND ref_id=? "
                            "ORDER BY party_id", (claim_id,)).fetchall():
                        self._enqueue_payment(connection, period_key=None, license_id=None,
                                              party_id=dist["party_id"],
                                              amount_cents=dist["amount_cents"],
                                              kind="claim_recovery", ref_id=claim_id)
                self._audit(connection, actor_id=actor_id, action="claim.recovered",
                            resource_type="claim", resource_id=claim_id,
                            detail={"amount_cents": amount_cents})
                return "cash_receipt", receipt_id, {"receipt_id": receipt_id}

            return self._idempotent(connection, request_id=request_id, action="recover_claim",
                                    payload=payload, create=create)

    def write_off_claim(self, *, request_id: str, actor_id: str, claim_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "claim_id": claim_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            claim = connection.execute("SELECT * FROM claims WHERE claim_id=?",
                                       (claim_id,)).fetchone()
            if claim is None:
                raise NotFoundError("追偿项目不存在")
            if claim["status"] != "assessed":
                raise ConflictError("只有已核定未追回的项目可以核销")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE claims SET status='written_off', resolved_at=? WHERE claim_id=?",
                    (self._now(), claim_id))
                ledger.post(connection, event_type="claim.written_off", ref_type="claim",
                            ref_id=claim_id, memo=f"追偿核销 {claim_id}",
                            lines={ledger.ACCOUNT_CLAIM: -claim["amount_cents"],
                                   ledger.ACCOUNT_CLEARING: claim["amount_cents"]},
                            created_at=self._now())
                self._audit(connection, actor_id=actor_id, action="claim.written_off",
                            resource_type="claim", resource_id=claim_id, detail={})
                return "claim", claim_id, {"claim_id": claim_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="write_off_claim", payload=payload, create=create)

    def _insert_entry(self, connection, *, period_key, usage, ctx, cursor_before, unit_rate,
                      gross_cents, deduction_cents, net_cents, entry_type, reversed_entry_id,
                      rate_snapshot) -> str:
        entry_id = uuid.uuid4().hex
        placeholders = "?,?,NULL," + ",".join(["?"] * 18) + ",0,0,?"
        connection.execute(
            "INSERT INTO billing_entries(entry_id,period_key,closes_period,usage_id,license_id,"
            "work_id,version_id,licensee_id,controlling_party_id,region,channel,use_type,quantity,"
            "cursor_before,unit_rate_micro_cents,gross_cents,deduction_cents,net_cents,entry_type,"
            "rate_snapshot_json,reversed_entry_id,confirmed,superseded,created_at) "
            f"VALUES({placeholders})",
            (entry_id, period_key, usage["usage_id"], ctx["license_id"],
             ctx["wv_work"], ctx["version_id"], usage["licensee_id"],
             ctx["controlling_party_id"], usage["region"], usage["channel"], usage["use_type"],
             usage["quantity"], cursor_before, unit_rate, gross_cents, deduction_cents,
             net_cents, entry_type, canonical_json(rate_snapshot), reversed_entry_id,
             self._now()),
        )
        return entry_id

    def _distribute(self, connection, *, ref_type: str, ref_id: str, shares_json: str,
                    net_cents: int, period_key: str | None, license_id: str | None) -> None:
        """按基点用最大余额法拆分带符号净额，各权利人金额合计与净额严格一致。"""

        shares: dict[str, int] = json.loads(shares_json) if isinstance(shares_json, str) \
            else shares_json
        total_bp = sum(shares.values())
        sign = -1 if net_cents < 0 else 1
        amount = abs(net_cents)
        exact = {party: Decimal(amount) * bp / total_bp for party, bp in shares.items()}
        floored = {party: int(exact[party]) for party in shares}
        remainder = amount - sum(floored.values())
        order = sorted(shares, key=lambda party: (-(exact[party] - int(exact[party])), party))
        for party in order[:remainder]:
            floored[party] += 1
        for party, value in floored.items():
            connection.execute(
                "INSERT INTO distributions(distribution_id,ref_type,ref_id,period_key,license_id,"
                "party_id,basis_points,amount_cents) VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, ref_type, ref_id, period_key, license_id,
                 party, shares[party], sign * value),
            )

    # ================================================================ 争议

    def open_dispute(self, *, request_id: str, actor_id: str, period_key: str,
                     license_id: str, amount_cents: int, reason: str) -> WriteReceipt:
        """争议只登记托管意向；关账时把争议金额从可付金额隔离到托管。"""

        amount_cents = self._cents(amount_cents, "amount_cents")
        if amount_cents <= 0:
            raise ValidationError("争议金额必须为正")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "period_key": period_key, "license_id": license_id,
                   "amount_cents": amount_cents, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "partner")
            self._ensure_period(connection, period_key)
            if self._period_status(connection, period_key) == "closed":
                raise ClosedPeriodError("期间已关账，不能再登记争议")
            license_row = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                             (license_id,)).fetchone()
            if license_row is None:
                raise NotFoundError("许可证不存在")
            if actor.role == "partner":
                licensee = connection.execute(
                    "SELECT controlling_party_id FROM licensees WHERE licensee_id=?",
                    (license_row["licensee_id"],)).fetchone()
                if actor.party_id != licensee["controlling_party_id"]:
                    raise PermissionDenied("只能对本方许可证提出争议")
            billed = connection.execute(
                "SELECT COALESCE(SUM(net_cents),0) AS total FROM billing_entries "
                "WHERE period_key=? AND license_id=?", (period_key, license_id),
            ).fetchone()["total"]
            disputed = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM disputes "
                "WHERE period_key=? AND license_id=? AND status='open'",
                (period_key, license_id),
            ).fetchone()["total"]
            if disputed + amount_cents > max(billed, 0):
                raise ConflictError("争议金额合计不能超过该许可证本期净额")

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO disputes(dispute_id,period_key,license_id,licensee_id,"
                    "amount_cents,reason,funded_cents,status,created_at) "
                    "VALUES(?,?,?,?,?,?,0, 'open',?)",
                    (dispute_id, period_key, license_id, license_row["licensee_id"],
                     amount_cents, reason, self._now()))
                self._audit(connection, actor_id=actor_id, action="dispute.opened",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"period_key": period_key, "license_id": license_id,
                                    "amount_cents": amount_cents})
                return "dispute", dispute_id, {"dispute_id": dispute_id}

            return self._idempotent(connection, request_id=request_id, action="open_dispute",
                                    payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        outcome: str) -> WriteReceipt:
        """争议裁定请求入争议队列，按登记顺序处理：release 付权利人，reject 退合作方。"""

        if outcome not in ("release", "reject"):
            raise ValidationError("outcome 只能是 release 或 reject")
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "outcome": outcome}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            dispute = connection.execute("SELECT * FROM disputes WHERE dispute_id=?",
                                         (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已经裁定")

            def create() -> tuple[str, str, dict[str, Any]]:
                seq = self._enqueue(connection, "dispute", "dispute", dispute_id)
                connection.execute(
                    "INSERT INTO dispute_decisions(dispute_id,outcome) VALUES(?,?)",
                    (dispute_id, outcome))
                self._audit(connection, actor_id=actor_id, action="dispute.queued",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"outcome": outcome, "seq": seq})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "queue_seq": seq,
                                               "outcome": outcome}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_dispute", payload=payload, create=create)

    # ================================================================ 关账

    def close_period(self, *, request_id: str, actor_id: str, period_key: str) -> WriteReceipt:
        """关账请求进入关账队列，按请求顺序冻结已确认分录；调用前应先完成计费。"""

        payload = {"actor_id": actor_id, "period_key": period_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._ensure_period(connection, period_key)
            if self._period_status(connection, period_key) == "closed":
                raise ConflictError("期间已关账")
            queued = connection.execute(
                "SELECT 1 FROM process_queues WHERE queue_type='close' AND ref_id=? "
                "AND status IN ('queued','processing')", (period_key,)).fetchone()
            if queued:
                raise ConflictError("该期间已在关账队列中")

            def create() -> tuple[str, str, dict[str, Any]]:
                seq = self._enqueue(connection, "close", "period", period_key)
                self._audit(connection, actor_id=actor_id, action="period.close_queued",
                            resource_type="period", resource_id=period_key,
                            detail={"seq": seq})
                return "period", period_key, {"period_key": period_key, "queue_seq": seq}

            return self._idempotent(connection, request_id=request_id, action="close_period",
                                    payload=payload, create=create)

    def process_close_queue(self, *, actor_id: str, max_items: int = 1) -> dict[str, Any]:
        """按序处理关账队列；只处理队首，保证期间顺序。"""

        processed = []
        for _ in range(max(1, max_items)):
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin", "operator")
                head = connection.execute(
                    "SELECT * FROM process_queues WHERE queue_type='close' AND status IN "
                    "('queued','processing') ORDER BY seq LIMIT 1").fetchone()
                if head is None:
                    break
                connection.execute(
                    "UPDATE process_queues SET status='processing' WHERE queue_type='close' AND seq=?",
                    (head["seq"],))
                period_key = head["ref_id"]
                self._freeze_period(connection, actor_id, period_key)
                connection.execute(
                    "UPDATE process_queues SET status='done', processed_at=? "
                    "WHERE queue_type='close' AND seq=?", (self._now(), head["seq"]))
                processed.append(period_key)
        return {"processed": processed}

    def _freeze_period(self, connection, actor, period_key: str) -> None:
        # 保底金补差：对本期净额低于保底的许可证补足（红冲后的净额参与比较）。
        for lic in connection.execute(
                "SELECT * FROM billing_entries WHERE period_key=? GROUP BY license_id",
                (period_key,)).fetchall():
            license_row = self._license_context(connection, lic["license_id"])
            net_total = connection.execute(
                "SELECT COALESCE(SUM(net_cents),0) AS total FROM billing_entries "
                "WHERE period_key=? AND license_id=?", (period_key, lic["license_id"]),
            ).fetchone()["total"]
            gap = license_row["guarantee_cents"] - net_total
            if license_row["guarantee_cents"] > 0 and gap > 0:
                entry_id = uuid.uuid4().hex
                snapshot = {"kind": "guarantee", "guarantee_cents": license_row["guarantee_cents"],
                            "net_before": net_total}
                connection.execute(
                    "INSERT INTO billing_entries(entry_id,period_key,closes_period,usage_id,"
                    "license_id,work_id,version_id,licensee_id,controlling_party_id,region,channel,"
                    "use_type,quantity,cursor_before,unit_rate_micro_cents,gross_cents,"
                    "deduction_cents,net_cents,entry_type,rate_snapshot_json,reversed_entry_id,"
                    "confirmed,superseded,created_at) "
                    "VALUES(?,?,NULL,NULL,?,?,?,?,?, 'guarantee','guarantee','guarantee',"
                    "0,0,0,?,0,?, 'guarantee',?,NULL,0,0,?)",
                    (entry_id, period_key, license_row["license_id"], license_row["wv_work"],
                     license_row["version_id"], license_row["licensee_id"],
                     license_row["controlling_party_id"], gap, gap,
                     canonical_json(snapshot), self._now()))
                self._distribute(connection, ref_type="entry", ref_id=entry_id,
                                 shares_json=license_row["shares_json"], net_cents=gap,
                                 period_key=period_key, license_id=license_row["license_id"])
                ledger.post(connection, event_type="billing.guarantee", ref_type="billing_entry",
                            ref_id=entry_id, period_key=period_key,
                            memo=f"保底补差 {lic['license_id']}",
                            lines={ledger.ACCOUNT_RECEIVABLE: gap,
                                   ledger.ACCOUNT_CLEARING: -gap},
                            created_at=self._now())
        # 冻结：分录确认并归属本期间，此后不可改写；迟到数据只能进入后续期间。
        connection.execute(
            "UPDATE billing_entries SET confirmed=1, closes_period=? WHERE period_key=?",
            (period_key, period_key))
        # 汇总应付：按 (许可证, 权利人) 聚合分配明细。
        self._snapshot_payables(connection, period_key)
        # 争议金额隔离：清算 -> 托管（是否到账不影响账面隔离，付款仍受回款约束）。
        for row in connection.execute(
                "SELECT license_id, COALESCE(SUM(amount_cents),0) AS amount "
                "FROM disputes WHERE period_key=? AND status='open' GROUP BY license_id",
                (period_key,)).fetchall():
            if row["amount"] > 0:
                ledger.post(connection, event_type="dispute.escrowed", ref_type="dispute",
                            ref_id=f"{period_key}:{row['license_id']}", period_key=period_key,
                            memo=f"争议托管 {row['license_id']}",
                            lines={ledger.ACCOUNT_CLEARING: row["amount"],
                                   ledger.ACCOUNT_ESCROW: -row["amount"]},
                            created_at=self._now())
                connection.execute(
                    "UPDATE disputes SET funded_cents=amount_cents WHERE period_key=? "
                    "AND license_id=? AND status='open'", (period_key, row["license_id"]))
        connection.execute("UPDATE periods SET status='closed', closed_at=? WHERE period_key=?",
                           (self._now(), period_key))
        self._audit(connection, actor_id=actor, action="period.closed",
                    resource_type="period", resource_id=period_key,
                    detail={"entries": connection.execute(
                        "SELECT COUNT(*) AS c FROM billing_entries WHERE closes_period=?",
                        (period_key,)).fetchone()["c"]})

    def _snapshot_payables(self, connection, period_key: str) -> None:
        rows = connection.execute(
            "SELECT be.license_id, d.party_id, SUM(d.amount_cents) AS amount "
            "FROM distributions d JOIN billing_entries be ON d.ref_id=be.entry_id "
            "WHERE d.ref_type='entry' AND be.closes_period=? "
            "GROUP BY be.license_id, d.party_id", (period_key,)).fetchall()
        # 争议按各权利人应付比例（最大余额法）摊到 disputed_cents。
        disputes = {row["license_id"]: row["amount"] for row in connection.execute(
            "SELECT license_id, SUM(amount_cents) AS amount FROM disputes "
            "WHERE period_key=? AND status='open' GROUP BY license_id", (period_key,)).fetchall()}
        grouped: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            grouped.setdefault(row["license_id"], []).append((row["party_id"], row["amount"]))
        for license_id, members in grouped.items():
            total = sum(amount for _, amount in members)
            disputed_total = min(max(disputes.get(license_id, 0), 0), max(total, 0))
            disputed_map = self._apportion(members, total, disputed_total)
            for party_id, amount in members:
                connection.execute(
                    "INSERT INTO settlement_payables(period_key,license_id,party_id,"
                    "amount_cents,disputed_cents) VALUES(?,?,?,?,?)",
                    (period_key, license_id, party_id, amount, disputed_map.get(party_id, 0)))

    @staticmethod
    def _apportion(members: list[tuple[str, int]], total: int, target: int) -> dict[str, int]:
        if total <= 0 or target <= 0:
            return {party: 0 for party, _ in members}
        exact = {party: Decimal(target) * amount / total for party, amount in members}
        floored = {party: int(exact[party]) for party, _ in members}
        remainder = target - sum(floored.values())
        order = sorted((party for party, _ in members),
                       key=lambda party: (-(exact[party] - int(exact[party])), party))
        for party in order[:remainder]:
            floored[party] += 1
        return floored

    # ================================================================ 回款

    def receive_cash(self, *, request_id: str, actor_id: str, period_key: str,
                     license_id: str, amount_cents: int) -> WriteReceipt:
        """登记合作方回款：现金增加、应收减少；足额回款后才能排付款。"""

        amount_cents = self._cents(amount_cents, "amount_cents")
        if amount_cents <= 0:
            raise ValidationError("回款金额必须为正")
        payload = {"actor_id": actor_id, "period_key": period_key,
                   "license_id": license_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "partner")
            if self._period_status(connection, period_key) != "closed":
                raise ConflictError("只能对已关账期间登记回款")
            license_row = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                             (license_id,)).fetchone()
            if license_row is None:
                raise NotFoundError("许可证不存在")
            if actor.role == "partner":
                licensee = connection.execute(
                    "SELECT controlling_party_id FROM licensees WHERE licensee_id=?",
                    (license_row["licensee_id"],)).fetchone()
                if actor.party_id != licensee["controlling_party_id"]:
                    raise PermissionDenied("只能为本方许可证登记回款")
            receivable = connection.execute(
                "SELECT COALESCE(SUM(net_cents),0) AS total FROM billing_entries "
                "WHERE closes_period=? AND license_id=?", (period_key, license_id),
            ).fetchone()["total"]
            received = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM cash_receipts "
                "WHERE period_key=? AND license_id=?", (period_key, license_id),
            ).fetchone()["total"]
            if received + amount_cents > max(receivable, 0):
                raise ConflictError("回款超过该许可证本期应收")

            def create() -> tuple[str, str, dict[str, Any]]:
                receipt_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cash_receipts(receipt_id,request_id,period_key,license_id,"
                    "licensee_id,amount_cents,received_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (receipt_id, request_id, period_key, license_id,
                     license_row["licensee_id"], amount_cents, actor_id, self._now()))
                ledger.post(connection, event_type="cash.received", ref_type="cash_receipt",
                            ref_id=receipt_id, period_key=period_key,
                            memo=f"回款 {period_key} {license_id}",
                            lines={ledger.ACCOUNT_CASH: amount_cents,
                                   ledger.ACCOUNT_RECEIVABLE: -amount_cents},
                            created_at=self._now())
                self._audit(connection, actor_id=actor_id, action="cash.received",
                            resource_type="cash_receipt", resource_id=receipt_id,
                            detail={"period_key": period_key, "license_id": license_id,
                                    "amount_cents": amount_cents})
                return "cash_receipt", receipt_id, {"receipt_id": receipt_id}

            return self._idempotent(connection, request_id=request_id, action="receive_cash",
                                    payload=payload, create=create)

    def write_off_receivable(self, *, request_id: str, actor_id: str, period_key: str,
                             license_id: str) -> WriteReceipt:
        """核销无法收回的应收：冲回应收与清算，取消未付队列，余额仍平衡。"""

        payload = {"actor_id": actor_id, "period_key": period_key, "license_id": license_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            open_dispute = connection.execute(
                "SELECT 1 FROM disputes WHERE period_key=? AND license_id=? AND status='open' LIMIT 1",
                (period_key, license_id)).fetchone()
            if open_dispute:
                raise ConflictError("该组存在未决争议，不能核销应收")
            outstanding = self._group_outstanding(connection, period_key, license_id)
            if outstanding <= 0:
                raise ConflictError("该组没有可核销的应收余额")

            def create() -> tuple[str, str, dict[str, Any]]:
                ledger.post(connection, event_type="receivable.written_off",
                            ref_type="license", ref_id=f"{period_key}:{license_id}",
                            period_key=period_key, memo=f"应收核销 {license_id}",
                            lines={ledger.ACCOUNT_RECEIVABLE: -outstanding,
                                   ledger.ACCOUNT_CLEARING: outstanding},
                            created_at=self._now())
                connection.execute(
                    "UPDATE payments SET status='cancelled' WHERE period_key=? AND license_id=? "
                    "AND status IN ('queued','processing')", (period_key, license_id))
                self._audit(connection, actor_id=actor_id, action="receivable.written_off",
                            resource_type="license",
                            resource_id=f"{period_key}:{license_id}",
                            detail={"amount_cents": outstanding})
                return "license", license_id, {"license_id": license_id,
                                               "written_off_cents": outstanding}

            return self._idempotent(connection, request_id=request_id,
                                    action="write_off_receivable", payload=payload, create=create)

    def _group_outstanding(self, connection, period_key: str, license_id: str) -> int:
        """该许可证本期尚余的正应收：毛应收 - 已回款 - 已核销。"""

        receivable = connection.execute(
            "SELECT COALESCE(SUM(net_cents),0) AS total FROM billing_entries "
            "WHERE closes_period=? AND license_id=?", (period_key, license_id),
        ).fetchone()["total"]
        received = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM cash_receipts "
            "WHERE period_key=? AND license_id=?", (period_key, license_id),
        ).fetchone()["total"]
        written = connection.execute(
            "SELECT COALESCE(SUM(e.amount_cents),0) AS total "
            "FROM accounting_transactions t JOIN accounting_entries e ON t.txn_id=e.txn_id "
            "WHERE t.event_type='receivable.written_off' AND t.period_key=? "
            "AND t.ref_id=? AND e.account='ar' AND e.direction='cr'",
            (period_key, f"{period_key}:{license_id}")).fetchone()["total"]
        return max(receivable, 0) - received - written

    # ================================================================ 付款

    def enqueue_payouts(self, *, actor_id: str, period_key: str) -> dict[str, Any]:
        """为已足额回款的许可证组生成权利人付款，按许可证、权利人确定顺序入队。"""

        enqueued = []
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if self._period_status(connection, period_key) != "closed":
                raise ConflictError("期间未关账")
            for payable in connection.execute(
                    "SELECT * FROM settlement_payables WHERE period_key=? ORDER BY license_id, party_id",
                    (period_key,)).fetchall():
                amount = payable["amount_cents"] - payable["disputed_cents"]
                if amount <= 0:
                    continue
                ref_id = f"dist:{period_key}:{payable['license_id']}:{payable['party_id']}"
                if connection.execute("SELECT 1 FROM payments WHERE ref_id=?",
                                      (ref_id,)).fetchone():
                    continue
                received = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS total FROM cash_receipts "
                    "WHERE period_key=? AND license_id=?",
                    (period_key, payable["license_id"])).fetchone()["total"]
                group_total = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS total FROM settlement_payables "
                    "WHERE period_key=? AND license_id=?",
                    (period_key, payable["license_id"])).fetchone()["total"]
                open_disputed = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS total FROM disputes "
                    "WHERE period_key=? AND license_id=? AND status='open'",
                    (period_key, payable["license_id"])).fetchone()["total"]
                # 回款覆盖无争议部分即可排无争议付款；争议金额继续等待裁定。
                if received < group_total - open_disputed:
                    continue
                self._enqueue_payment(connection, period_key=period_key,
                                      license_id=payable["license_id"],
                                      party_id=payable["party_id"], amount_cents=amount,
                                      kind="distribution", ref_id=ref_id)
                enqueued.append(ref_id)
            self._audit(connection, actor_id=actor_id, action="payouts.enqueued",
                        resource_type="period", resource_id=period_key,
                        detail={"count": len(enqueued)})
        return {"period_key": period_key, "enqueued": enqueued}

    def _enqueue_payment(self, connection, *, period_key, license_id, party_id, amount_cents,
                         kind, ref_id) -> str:
        payment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO payments(payment_id,period_key,license_id,payee_party_id,amount_cents,"
            "kind,status,ref_id,created_at) VALUES(?,?,?,?,?,?, 'queued',?,?)",
            (payment_id, period_key, license_id, party_id, amount_cents, kind, ref_id,
             self._now()))
        return payment_id

    def process_payment_queue(self, *, actor_id: str, max_items: int = 1) -> dict[str, Any]:
        """严格按入队顺序付款；队首余额不足则报 OutOfOrder，绝不跳过。"""

        paid = []
        for _ in range(max(1, max_items)):
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin", "operator")
                head = connection.execute(
                    "SELECT * FROM payments WHERE status IN ('queued','processing') "
                    "ORDER BY rowid LIMIT 1").fetchone()
                if head is None:
                    break
                balances = ledger.account_balances(connection)
                if balances.get(ledger.ACCOUNT_CASH, 0) < head["amount_cents"]:
                    raise OutOfOrderError(
                        f"队首付款 {head['payment_id']} 回款未到位，不能处理后续付款")
                connection.execute("UPDATE payments SET status='processing' WHERE payment_id=?",
                                   (head["payment_id"],))
                self._pay_one(connection, head)
                connection.execute(
                    "UPDATE payments SET status='paid', paid_at=? WHERE payment_id=?",
                    (self._now(), head["payment_id"]))
                paid.append(head["payment_id"])
        return {"paid": paid}

    def _pay_one(self, connection, head) -> None:
        kind = head["kind"]
        if kind in ("escrow_release", "licensee_refund"):
            # 托管放行给权利人或退还给合作方，都来自关账时隔离的托管。
            lines = {ledger.ACCOUNT_ESCROW: head["amount_cents"],
                     ledger.ACCOUNT_CASH: -head["amount_cents"]}
        else:  # distribution / claim_recovery
            lines = {ledger.ACCOUNT_CLEARING: head["amount_cents"],
                     ledger.ACCOUNT_CASH: -head["amount_cents"]}
        ledger.post(connection, event_type=f"payment.{kind}", ref_type="payment",
                    ref_id=head["payment_id"], period_key=head["period_key"],
                    memo=f"{kind} -> {head['payee_party_id']}", lines=lines,
                    created_at=self._now())

    def process_dispute_queue(self, *, actor_id: str, max_items: int = 1) -> dict[str, Any]:
        """按争议登记顺序裁定；release 排托管放行付款，reject 排合作方退款。"""

        processed = []
        for _ in range(max(1, max_items)):
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin", "operator")
                head = connection.execute(
                    "SELECT q.* FROM process_queues q "
                    "WHERE q.queue_type='dispute' AND q.status IN ('queued','processing') "
                    "ORDER BY q.seq LIMIT 1").fetchone()
                if head is None:
                    break
                dispute_id = head["ref_id"]
                decision = connection.execute(
                    "SELECT * FROM dispute_decisions WHERE dispute_id=?", (dispute_id,),
                ).fetchone()
                dispute = connection.execute("SELECT * FROM disputes WHERE dispute_id=?",
                                             (dispute_id,)).fetchone()
                if self._period_status(connection, dispute["period_key"]) != "closed":
                    raise OutOfOrderError("争议所属期间尚未关账，不能裁定")
                balances = ledger.account_balances(connection)
                if balances.get(ledger.ACCOUNT_CASH, 0) < dispute["amount_cents"]:
                    raise OutOfOrderError("队首争议对应回款未到位，不能裁定后续争议")
                connection.execute(
                    "UPDATE process_queues SET status='processing' WHERE queue_type='dispute' AND seq=?",
                    (head["seq"],))
                if decision["outcome"] == "release":
                    self._distribute_escrow_release(connection, dispute)
                    new_status = "released"
                else:
                    self._enqueue_payment(connection, period_key=dispute["period_key"],
                                          license_id=dispute["license_id"],
                                          party_id=dispute["licensee_id"],
                                          amount_cents=dispute["amount_cents"],
                                          kind="licensee_refund",
                                          ref_id=f"refund:{dispute_id}")
                    new_status = "rejected"
                connection.execute(
                    "UPDATE disputes SET status=?, resolved_at=? WHERE dispute_id=?",
                    (new_status, self._now(), dispute_id))
                connection.execute(
                    "UPDATE process_queues SET status='done', processed_at=? "
                    "WHERE queue_type='dispute' AND seq=?", (self._now(), head["seq"]))
                processed.append({"dispute_id": dispute_id, "outcome": decision["outcome"]})
        return {"processed": processed}

    def _distribute_escrow_release(self, connection, dispute) -> None:
        """托管金额按该组各权利人的应付比例放行。"""

        rows = connection.execute(
            "SELECT party_id, disputed_cents FROM settlement_payables "
            "WHERE period_key=? AND license_id=? AND disputed_cents>0 ORDER BY party_id",
            (dispute["period_key"], dispute["license_id"])).fetchall()
        total = sum(row["disputed_cents"] for row in rows)
        if total == 0:
            return
        amounts = self._apportion([(row["party_id"], row["disputed_cents"]) for row in rows],
                                  total, dispute["amount_cents"])
        for party_id, amount in amounts.items():
            if amount > 0:
                self._enqueue_payment(connection, period_key=dispute["period_key"],
                                      license_id=dispute["license_id"], party_id=party_id,
                                      amount_cents=amount, kind="escrow_release",
                                      ref_id=f"escrow:{dispute['dispute_id']}:{party_id}")
        connection.execute(
            "UPDATE settlement_payables SET disputed_cents=0 WHERE period_key=? AND license_id=?",
            (dispute["period_key"], dispute["license_id"]))

    def _enqueue(self, connection, queue_type: str, ref_type: str, ref_id: str) -> int:
        seq_row = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM process_queues WHERE queue_type=?",
            (queue_type,)).fetchone()
        seq = seq_row["next_seq"]
        connection.execute(
            "INSERT INTO process_queues(queue_type,seq,ref_type,ref_id,status,enqueued_at) "
            "VALUES(?,?,?,?, 'queued',?)", (queue_type, seq, ref_type, ref_id, self._now()))
        return seq

    # ================================================================ 查询

    def _visible_licensee_ids(self, connection, actor: Actor) -> list[str] | None:
        if actor.role in ("admin", "operator", "auditor"):
            return None
        if actor.role == "partner":
            return [row["licensee_id"] for row in connection.execute(
                "SELECT licensee_id FROM licensees WHERE controlling_party_id=?",
                (actor.party_id,)).fetchall()]
        return None

    def _holder_version_ids(self, connection, party_id: str) -> list[str]:
        """返回某权利人持有份额的全部作品版本（解析 JSON 精确匹配）。"""

        version_ids = []
        for row in connection.execute("SELECT version_id, shares_json FROM work_versions"):
            if party_id in json.loads(row["shares_json"]):
                version_ids.append(row["version_id"])
        return version_ids

    def list_usage(self, actor_id: str, period_key: str | None = None) -> list[UsageRecord]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = "SELECT * FROM usage_records"
        clauses = []
        params: list[Any] = []
        if period_key:
            clauses.append("period_key=?")
            params.append(period_key)
        if actor.role == "partner":
            ids = self._visible_licensee_ids(connection, actor) or []
            clauses.append(f"licensee_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        elif actor.role == "rights_holder":
            version_ids = self._holder_version_ids(connection, actor.party_id)
            if not version_ids:
                return []
            placeholders = ",".join("?" * len(version_ids))
            clauses.append(
                f"(matched_license_id IN (SELECT license_id FROM licenses "
                f"WHERE version_id IN ({placeholders})) OR work_id IN "
                f"(SELECT DISTINCT work_id FROM work_versions WHERE version_id IN ({placeholders})))")
            params.extend(version_ids + version_ids)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY period_key, occurred_on, usage_id"
        result = []
        for row in connection.execute(sql, params):
            result.append(UsageRecord(
                row["usage_id"], row["work_id"], row["licensee_id"], row["region"],
                row["channel"], row["use_type"], row["quantity"], row["occurred_on"],
                row["period_key"], row["matched_license_id"], bool(row["in_scope"])))
        return result

    def list_entries(self, actor_id: str, period_key: str | None = None) -> list[BillingEntry]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = "SELECT * FROM billing_entries"
        clauses = []
        params: list[Any] = []
        if period_key:
            clauses.append("period_key=?")
            params.append(period_key)
        if actor.role == "partner":
            ids = self._visible_licensee_ids(connection, actor) or []
            if not ids:
                return []
            clauses.append(f"licensee_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        elif actor.role == "rights_holder":
            version_ids = self._holder_version_ids(connection, actor.party_id)
            if not version_ids:
                return []
            clauses.append(f"version_id IN ({','.join('?' * len(version_ids))})")
            params.extend(version_ids)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY period_key, created_at, entry_id"
        return [self._entry_from_row(row) for row in connection.execute(sql, params)]

    @staticmethod
    def _entry_from_row(row) -> BillingEntry:
        return BillingEntry(
            row["entry_id"], row["period_key"], row["closes_period"], row["usage_id"],
            row["license_id"], row["work_id"], row["version_id"], row["licensee_id"],
            row["controlling_party_id"], row["region"], row["channel"], row["use_type"],
            row["quantity"], row["cursor_before"], row["unit_rate_micro_cents"],
            row["gross_cents"], row["deduction_cents"], row["net_cents"], row["entry_type"],
            json.loads(row["rate_snapshot_json"]), row["reversed_entry_id"],
            row["confirmed"], row["created_at"])

    def list_distributions(self, actor_id: str, period_key: str | None = None) -> list[Distribution]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = ("SELECT d.* FROM distributions d "
               "LEFT JOIN billing_entries be ON d.ref_type='entry' AND d.ref_id=be.entry_id")
        clauses = []
        params: list[Any] = []
        if period_key:
            clauses.append("d.period_key=?")
            params.append(period_key)
        if actor.role == "partner":
            ids = self._visible_licensee_ids(connection, actor) or []
            if not ids:
                return []
            clauses.append(f"d.license_id IN (SELECT license_id FROM licenses WHERE licensee_id IN "
                           f"({','.join('?' * len(ids))}))")
            params.extend(ids)
        elif actor.role == "rights_holder":
            clauses.append("d.party_id=?")
            params.append(actor.party_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.period_key, d.ref_id, d.party_id"
        return [Distribution(row["distribution_id"], row["ref_type"], row["ref_id"],
                             row["period_key"], row["license_id"], row["party_id"],
                             row["basis_points"], row["amount_cents"])
                for row in connection.execute(sql, params)]

    def list_claims(self, actor_id: str) -> list[Claim]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = "SELECT * FROM claims"
        params: list[Any] = []
        if actor.role == "partner":
            ids = self._visible_licensee_ids(connection, actor) or []
            if not ids:
                return []
            sql += f" WHERE licensee_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        elif actor.role == "rights_holder":
            version_ids = self._holder_version_ids(connection, actor.party_id)
            if not version_ids:
                return []
            work_rows = connection.execute(
                f"SELECT DISTINCT work_id FROM work_versions WHERE version_id IN "
                f"({','.join('?' * len(version_ids))})", version_ids).fetchall()
            work_ids = [r["work_id"] for r in work_rows]
            if not work_ids:
                return []
            sql += f" WHERE work_id IN ({','.join('?' * len(work_ids))})"
            params.extend(work_ids)
        sql += " ORDER BY created_at, claim_id"
        return [Claim(row["claim_id"], row["usage_id"], row["licensee_id"],
                      row["controlling_party_id"], row["work_id"], row["region"],
                      row["channel"], row["use_type"], row["quantity"], row["occurred_on"],
                      row["reason"], row["status"], row["amount_cents"])
                for row in connection.execute(sql, params)]

    def list_payments(self, actor_id: str) -> list[Payment]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = "SELECT * FROM payments"
        params: list[Any] = []
        if actor.role == "rights_holder":
            sql += " WHERE payee_party_id=?"
            params.append(actor.party_id)
        elif actor.role == "partner":
            ids = self._visible_licensee_ids(connection, actor) or []
            if not ids:
                return []
            sql += (f" WHERE license_id IN (SELECT license_id FROM licenses WHERE licensee_id IN "
                    f"({','.join('?' * len(ids))}))")
            params.extend(ids)
        sql += " ORDER BY rowid"
        return [Payment(row["payment_id"], row["period_key"], row["payee_party_id"],
                        row["license_id"], row["amount_cents"], row["kind"], row["status"],
                        None) for row in connection.execute(sql, params)]

    def settlement_summary(self, actor_id: str, period_key: str) -> Settlement:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "auditor")
        receivable = connection.execute(
            "SELECT COALESCE(SUM(net_cents),0) AS total FROM billing_entries WHERE period_key=?",
            (period_key,)).fetchone()["total"]
        paid = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM payments "
            "WHERE period_key=? AND status='paid'", (period_key,)).fetchone()["total"]
        escrow_open = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM disputes "
            "WHERE period_key=? AND status='open'", (period_key,)).fetchone()["total"]
        written_off = 0
        for txn in connection.execute(
                "SELECT txn_id FROM accounting_transactions "
                "WHERE event_type='receivable.written_off' AND period_key=?",
                (period_key,)).fetchall():
            written_off += connection.execute(
                "SELECT amount_cents FROM accounting_entries WHERE txn_id=? AND account='ar'",
                (txn["txn_id"],)).fetchone()["amount_cents"]
        period = connection.execute("SELECT * FROM periods WHERE period_key=?",
                                    (period_key,)).fetchone()
        claims_total = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM claims c JOIN usage_records u "
            "ON c.usage_id=u.usage_id WHERE u.period_key=? AND c.status IN ('assessed','open')",
            (period_key,)).fetchone()["total"]
        status = period["status"] if period else "open"
        balance = receivable - paid - escrow_open - written_off
        return Settlement(period_key, status, receivable, paid, escrow_open, balance,
                          written_off, claims_total)

    def explain_entry(self, actor_id: str, entry_id: str) -> dict[str, Any]:
        """审计视角：给出分录的费率快照、逐段计价、分配与复式账来源。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "auditor", "rights_holder")
        row = connection.execute("SELECT * FROM billing_entries WHERE entry_id=?",
                                 (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError("分录不存在")
        if actor.role == "rights_holder":
            owns = connection.execute(
                "SELECT 1 FROM distributions WHERE ref_type='entry' AND ref_id=? AND party_id=?",
                (entry_id, actor.party_id)).fetchone()
            if not owns:
                raise PermissionDenied("只能查看与本方份额相关的分录")
        distributions = [
            {"party_id": item["party_id"], "basis_points": item["basis_points"],
             "amount_cents": item["amount_cents"]}
            for item in connection.execute(
                "SELECT * FROM distributions WHERE ref_type='entry' AND ref_id=? ORDER BY party_id",
                (entry_id,))]
        return {"entry": self._entry_from_row(row).__dict__,
                "distributions": distributions,
                "transactions": ledger.transactions_for_ref(connection, "billing_entry", entry_id)}

    def recompute_period(self, actor_id: str, period_key: str) -> dict[str, Any]:
        """重算期间：逐分录按快照分段复算毛额、核对扣减净额、分配合计与全局平衡。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "auditor", "admin", "operator")
        rows = connection.execute(
            "SELECT * FROM billing_entries WHERE period_key=? ORDER BY created_at, entry_id",
            (period_key,)).fetchall()
        mismatches: list[dict[str, Any]] = []
        for row in rows:
            snapshot = json.loads(row["rate_snapshot_json"])
            if row["entry_type"] == "guarantee":
                recomputed_gross = row["gross_cents"]
            elif row["entry_type"] == "reversal":
                # 红冲按原始毛额与退货比例复算。
                original = connection.execute(
                    "SELECT quantity, gross_cents FROM billing_entries WHERE entry_id=?",
                    (row["reversed_entry_id"],)).fetchone()
                ratio = Decimal(abs(row["quantity"])) / Decimal(original["quantity"])
                recomputed_gross = -int((Decimal(abs(original["gross_cents"])) * ratio).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN))
            else:
                micro = sum(int(span) * int(unit)
                            for span, _start, unit in snapshot.get("segments", []))
                recomputed_gross = int((Decimal(micro) / MICRO_PER_CENT).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN))
                if row["quantity"] < 0:
                    recomputed_gross = -recomputed_gross
            distributed = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM distributions "
                "WHERE ref_type='entry' AND ref_id=?", (row["entry_id"],)).fetchone()["total"]
            problems = []
            if recomputed_gross != row["gross_cents"]:
                problems.append("gross_mismatch")
            if row["gross_cents"] - row["deduction_cents"] != row["net_cents"]:
                problems.append("net_mismatch")
            if distributed != row["net_cents"]:
                problems.append("distribution_mismatch")
            if problems:
                mismatches.append({"entry_id": row["entry_id"], "problems": problems,
                                   "stored_gross": row["gross_cents"],
                                   "recomputed_gross": recomputed_gross})
        period = connection.execute("SELECT * FROM periods WHERE period_key=?",
                                    (period_key,)).fetchone()
        confirmed_ok = True
        if period is not None and period["status"] == "closed":
            confirmed_ok = connection.execute(
                "SELECT COUNT(*) AS c FROM billing_entries WHERE period_key=? AND confirmed=0",
                (period_key,)).fetchone()["c"] == 0
        balanced, balances = self.accounting_balances()
        return {"period_key": period_key, "entries_checked": len(rows),
                "mismatches": mismatches, "confirmed_ok": confirmed_ok,
                "ledger_balanced": balanced, "balances": balances}

    def accounting_balances(self) -> tuple[bool, dict[str, int]]:
        balances = ledger.account_balances(self.database.connection)
        # 带符号余额（借正贷负）之和恒为零。
        identity = sum(balances.values())
        return identity == 0, balances

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
        ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"],
                 "actor_id": row["actor_id"], "action": row["action"],
                 "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                 "detail": json.loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]} for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)


class _Priced:
    __slots__ = ("cursor_before", "unit_rate_micro_cents", "gross_micro_cents",
                 "gross_cents", "segments")

    def __init__(self, cursor_before, unit_rate, gross_micro, gross_cents, segments) -> None:
        self.cursor_before = cursor_before
        self.unit_rate_micro_cents = unit_rate
        self.gross_micro_cents = gross_micro
        self.gross_cents = gross_cents
        self.segments = segments
