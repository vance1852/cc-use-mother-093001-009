"""纹样授权计费与结算的领域服务。

关键不变量：
- 使用事实经幂等导入与去重后做范围匹配，计费分录只追加、不可覆盖；
- 阶梯费率按同一实际控制合作方（控制集团）合并后的累计基数选档；
- 关账只冻结已确认分录并生成应收/应付付款队列；迟到更正以追补/冲销进入后续期间；
- 争议只托管争议金额，无争议部分照常收款与付款；
- 许可证失效或超范围使用只形成待追偿项目，追偿达成后才计费；
- 两侧始终平衡：
  应收 = 已收 + 托管 + 应收余额；应付 = 已付 + 托管 + 应付余额。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any, Callable

from creative_program_foundation.audit import append_event, canonical_json, digest
from creative_program_foundation.clock import Clock, SystemClock
from creative_program_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError

from .billing import apply_deductions, select_rate_version, tiered_fee
from .models import BillingEntryView, UsageFactView, WriteReceipt
from .money import prorate
from .storage import LicensingDatabase


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PERIOD_RE = re.compile(r"^\d{4}-Q[1-4]$|^\d{4}-\d{2}$|^\d{4}$")
PRINCIPAL_KINDS = {"operator", "auditor", "partner", "holder"}


class LicensingService:
    """协调权限、幂等、事务、计费规则、关账队列与审计。"""

    def __init__(self, database: LicensingDatabase, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _period(self, value: Any) -> str:
        value = str(value).strip()
        if not PERIOD_RE.fullmatch(value):
            raise ValidationError("period_key 必须是 YYYY、YYYY-MM 或 YYYY-Qn 形式")
        return value

    def _period_start(self, period_key: str) -> str:
        if "Q" in period_key:
            year, quarter = period_key.split("-Q")
            return f"{year}-{int(quarter) * 3 - 2:02d}-01"
        if len(period_key) == 4:
            return f"{period_key}-01-01"
        return f"{period_key}-01"

    def _date(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        return value

    def _cents(self, value: Any, field: str, allow_negative: bool = False) -> int:
        try:
            amount = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是整数分") from exc
        if not allow_negative and amount < 0:
            raise ValidationError(f"{field} 不能为负数")
        return amount

    def _principal(self, connection, principal_id: str):
        row = connection.execute("SELECT * FROM lh_principals WHERE principal_id=?",
                                 (principal_id,)).fetchone()
        if row is None:
            raise NotFoundError("主体不存在或未登记")
        active = bool(row["active"])
        if not active:
            raise PermissionDenied("主体已停用")
        return row

    def _require(self, principal, *kinds: str) -> None:
        if principal["kind"] not in kinds:
            raise PermissionDenied("当前身份不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
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

    def _ensure_period(self, connection, period_key: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO periods(period_key,status,created_at) VALUES(?,'open',?)",
            (period_key, self._now()),
        )

    def _open_period(self, connection, period_key: str) -> None:
        self._ensure_period(connection, period_key)
        row = connection.execute("SELECT status FROM periods WHERE period_key=?",
                                 (period_key,)).fetchone()
        if row["status"] != "open":
            raise ConflictError(f"期间 {period_key} 已关账，数据请报送至后续期间")

    # ------------------------------------------------------------ 主体与作品

    def register_principal(self, *, request_id: str, actor_id: str, principal_id: str, kind: str,
                           display_name: str, licensee_id: str | None = None,
                           holder_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "principal_id": principal_id, "kind": kind,
                   "display_name": display_name, "licensee_id": licensee_id, "holder_id": holder_id}
        with self.database.transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM lh_principals").fetchone()["c"]
            if count:
                actor = self._principal(connection, actor_id)
                self._require(actor, "operator")
            elif actor_id != "bootstrap":
                raise PermissionDenied("首位主体必须由 bootstrap 登记")
            principal_id = self._id(principal_id, "principal_id")
            display_name = self._text(display_name, "display_name")
            if kind not in PRINCIPAL_KINDS:
                raise ValidationError("kind 必须是 operator/auditor/partner/holder")
            if kind == "partner" and not licensee_id:
                raise ValidationError("合作方主体必须绑定 licensee_id")
            if kind == "holder" and not holder_id:
                raise ValidationError("权利人主体必须绑定 holder_id")
            if licensee_id and connection.execute(
                    "SELECT 1 FROM licensees WHERE licensee_id=?", (licensee_id,)).fetchone() is None:
                raise NotFoundError("被许可方不存在")
            if holder_id and connection.execute(
                    "SELECT 1 FROM right_holders WHERE holder_id=?", (holder_id,)).fetchone() is None:
                raise NotFoundError("权利人不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO lh_principals(principal_id,kind,display_name,licensee_id,holder_id,"
                        "active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (principal_id, kind, display_name, licensee_id, holder_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("主体编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="principal.registered",
                            resource_type="principal", resource_id=principal_id,
                            detail={"kind": kind, "licensee_id": licensee_id, "holder_id": holder_id})
                return "principal", principal_id, {"principal_id": principal_id}

            return self._idempotent(connection, request_id=request_id, action="register_principal",
                                    payload=payload, create=create)

    def register_holder(self, *, request_id: str, actor_id: str, holder_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "holder_id": holder_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            holder_id = self._id(holder_id, "holder_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO right_holders(holder_id,name,active,created_at) VALUES(?,?,'1',?)",
                        (holder_id, name, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("权利人编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="holder.registered",
                            resource_type="holder", resource_id=holder_id, detail={"name": name})
                return "holder", holder_id, {"holder_id": holder_id}

            return self._idempotent(connection, request_id=request_id, action="register_holder",
                                    payload=payload, create=create)

    def register_work(self, *, request_id: str, actor_id: str, work_id: str, title: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            work_id = self._id(work_id, "work_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,title,active,created_at) VALUES(?,?,'1',?)",
                        (work_id, title, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("作品编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="work.registered",
                            resource_type="work", resource_id=work_id, detail={"title": title})
                return "work", work_id, {"work_id": work_id}

            return self._idempotent(connection, request_id=request_id, action="register_work",
                                    payload=payload, create=create)

    def register_work_version(self, *, request_id: str, actor_id: str, work_id: str,
                              version_no: int, content_hash: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "version_no": version_no,
                   "content_hash": content_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            work_id = self._id(work_id, "work_id")
            version_no = int(version_no)
            if version_no < 1:
                raise ValidationError("version_no 必须从 1 开始")
            content_hash = self._text(content_hash, "content_hash", 128)
            if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            version_id = f"{work_id}:wv{version_no}"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO work_versions(version_id,work_id,version_no,content_hash,status,"
                        "created_at) VALUES(?,?,?,?,'active',?)",
                        (version_id, work_id, version_no, content_hash, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("作品版本已存在") from exc
                self._audit(connection, actor_id=actor_id, action="work_version.registered",
                            resource_type="work_version", resource_id=version_id,
                            detail={"work_id": work_id, "version_no": version_no})
                return "work_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id, action="register_work_version",
                                    payload=payload, create=create)

    def register_entitlement_version(self, *, request_id: str, actor_id: str, work_id: str,
                                     version_no: int, effective_from: str,
                                     shares: list[dict[str, Any]]) -> WriteReceipt:
        """登记某作品一版权利人份额；所有份额基点合计必须恰为 10000。"""

        payload = {"actor_id": actor_id, "work_id": work_id, "version_no": version_no,
                   "effective_from": effective_from, "shares": shares}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            work_id = self._id(work_id, "work_id")
            version_no = int(version_no)
            effective_from = self._date(effective_from, "effective_from")
            if not isinstance(shares, list) or not shares:
                raise ValidationError("shares 必须是非空列表")
            total = 0
            normalized: list[tuple[str, int]] = []
            for item in shares:
                holder_id = self._id(item["holder_id"], "holder_id")
                points = int(item["basis_points"])
                if points <= 0:
                    raise ValidationError("份额基点必须为正数")
                total += points
                normalized.append((holder_id, points))
            if total != 10000:
                raise ValidationError(f"权利人份额基点合计必须为 10000，当前为 {total}")
            if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            for holder_id, _ in normalized:
                if connection.execute("SELECT 1 FROM right_holders WHERE holder_id=?",
                                      (holder_id,)).fetchone() is None:
                    raise NotFoundError(f"权利人 {holder_id} 不存在")
            ent_version_id = f"{work_id}:ev{version_no}"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO entitlement_versions(ent_version_id,work_id,version_no,status,"
                        "effective_from,created_at) VALUES(?,?,?,'active',?,?)",
                        (ent_version_id, work_id, version_no, effective_from, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("份额版本已存在") from exc
                for holder_id, points in normalized:
                    connection.execute(
                        "INSERT INTO entitlement_shares(ent_version_id,holder_id,basis_points) VALUES(?,?,?)",
                        (ent_version_id, holder_id, points),
                    )
                self._audit(connection, actor_id=actor_id, action="entitlement_version.registered",
                            resource_type="entitlement_version", resource_id=ent_version_id,
                            detail={"work_id": work_id, "version_no": version_no,
                                    "effective_from": effective_from, "shares": normalized})
                return "entitlement_version", ent_version_id, {"ent_version_id": ent_version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_entitlement_version", payload=payload, create=create)

    # ------------------------------------------------------------ 合作与许可

    def register_control_group(self, *, request_id: str, actor_id: str, group_id: str,
                               name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "group_id": group_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            group_id = self._id(group_id, "group_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO control_groups(group_id,name,created_at) VALUES(?,?,?)",
                        (group_id, name, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("控制集团编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="control_group.registered",
                            resource_type="control_group", resource_id=group_id, detail={"name": name})
                return "control_group", group_id, {"group_id": group_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_control_group", payload=payload, create=create)

    def register_licensee(self, *, request_id: str, actor_id: str, licensee_id: str,
                          control_group_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "licensee_id": licensee_id,
                   "control_group_id": control_group_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            licensee_id = self._id(licensee_id, "licensee_id")
            control_group_id = self._id(control_group_id, "control_group_id")
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM control_groups WHERE group_id=?",
                                  (control_group_id,)).fetchone() is None:
                raise NotFoundError("控制集团不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO licensees(licensee_id,control_group_id,name,active,created_at) "
                        "VALUES(?,?,?,'1',?)",
                        (licensee_id, control_group_id, name, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("被许可方编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="licensee.registered",
                            resource_type="licensee", resource_id=licensee_id,
                            detail={"control_group_id": control_group_id, "name": name})
                return "licensee", licensee_id, {"licensee_id": licensee_id}

            return self._idempotent(connection, request_id=request_id, action="register_licensee",
                                    payload=payload, create=create)

    def register_license(self, *, request_id: str, actor_id: str, license_id: str, licensee_id: str,
                         work_id: str, work_version_no: int, territory: str, channel: str,
                         usage_purpose: str, valid_from: str, valid_to: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "license_id": license_id, "licensee_id": licensee_id,
                   "work_id": work_id, "work_version_no": work_version_no, "territory": territory,
                   "channel": channel, "usage_purpose": usage_purpose, "valid_from": valid_from,
                   "valid_to": valid_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            licensee_id = self._id(licensee_id, "licensee_id")
            work_id = self._id(work_id, "work_id")
            work_version_no = int(work_version_no)
            territory = self._text(territory, "territory", 80)
            channel = self._text(channel, "channel", 80)
            usage_purpose = self._text(usage_purpose, "usage_purpose", 80)
            valid_from = self._date(valid_from, "valid_from")
            if valid_to is not None:
                valid_to = self._date(valid_to, "valid_to")
                if valid_to < valid_from:
                    raise ValidationError("valid_to 不能早于 valid_from")
            version_row = connection.execute(
                "SELECT * FROM work_versions WHERE work_id=? AND version_no=?",
                (work_id, work_version_no)).fetchone()
            if version_row is None:
                raise NotFoundError("作品版本不存在")
            if connection.execute("SELECT 1 FROM licensees WHERE licensee_id=?",
                                  (licensee_id,)).fetchone() is None:
                raise NotFoundError("被许可方不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO licenses(license_id,licensee_id,work_id,work_version_id,territory,"
                        "channel,usage_purpose,valid_from,valid_to,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
                        (license_id, licensee_id, work_id, version_row["version_id"], territory, channel,
                         usage_purpose, valid_from, valid_to, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("许可证编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="license.registered",
                            resource_type="license", resource_id=license_id,
                            detail={"licensee_id": licensee_id, "work_id": work_id,
                                    "territory": territory, "channel": channel,
                                    "usage_purpose": usage_purpose,
                                    "valid_from": valid_from, "valid_to": valid_to})
                return "license", license_id, {"license_id": license_id}

            return self._idempotent(connection, request_id=request_id, action="register_license",
                                    payload=payload, create=create)

    def terminate_license(self, *, request_id: str, actor_id: str, license_id: str,
                          terminated_on: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "license_id": license_id, "terminated_on": terminated_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            terminated_on = self._date(terminated_on, "terminated_on")
            row = connection.execute("SELECT * FROM licenses WHERE license_id=?", (license_id,)).fetchone()
            if row is None:
                raise NotFoundError("许可证不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE licenses SET status='terminated', valid_to=? WHERE license_id=?",
                    (terminated_on, license_id),
                )
                self._audit(connection, actor_id=actor_id, action="license.terminated",
                            resource_type="license", resource_id=license_id,
                            detail={"terminated_on": terminated_on})
                return "license", license_id, {"license_id": license_id, "status": "terminated"}

            return self._idempotent(connection, request_id=request_id, action="terminate_license",
                                    payload=payload, create=create)

    def register_rate_version(self, *, request_id: str, actor_id: str, license_id: str,
                              version_no: int, effective_from: str,
                              tiers: list[dict[str, Any]]) -> WriteReceipt:
        """登记阶梯费率版本：tiers=[{lower_bound_cents, rate_basis_points}]，低档到高档。"""

        payload = {"actor_id": actor_id, "license_id": license_id, "version_no": version_no,
                   "effective_from": effective_from, "tiers": tiers}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            version_no = int(version_no)
            effective_from = self._date(effective_from, "effective_from")
            if not isinstance(tiers, list) or not tiers:
                raise ValidationError("tiers 必须是非空列表")
            prepared: list[tuple[int, int]] = []
            last_bound = -1
            for item in tiers:
                bound = self._cents(item["lower_bound_cents"], "lower_bound_cents")
                rate = int(item["rate_basis_points"])
                if not 0 <= rate <= 10000:
                    raise ValidationError("费率基点必须在 0 到 10000 之间")
                if bound <= last_bound:
                    raise ValidationError("阶梯下限必须严格递增")
                prepared.append((bound, rate))
                last_bound = bound
            if prepared[0][0] != 0:
                raise ValidationError("第一档下限必须为 0")
            if connection.execute("SELECT 1 FROM licenses WHERE license_id=?",
                                  (license_id,)).fetchone() is None:
                raise NotFoundError("许可证不存在")
            rate_version_id = f"{license_id}:rv{version_no}"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO rate_versions(rate_version_id,license_id,version_no,status,"
                        "effective_from,created_at) VALUES(?,?,?,'active',?,?)",
                        (rate_version_id, license_id, version_no, effective_from, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("费率版本已存在") from exc
                for index, (bound, rate) in enumerate(prepared, start=1):
                    connection.execute(
                        "INSERT INTO rate_tiers(rate_version_id,tier_no,lower_bound_cents,"
                        "rate_basis_points) VALUES(?,?,?,?)",
                        (rate_version_id, index, bound, rate),
                    )
                self._audit(connection, actor_id=actor_id, action="rate_version.registered",
                            resource_type="rate_version", resource_id=rate_version_id,
                            detail={"license_id": license_id, "version_no": version_no,
                                    "effective_from": effective_from, "tiers": prepared})
                return "rate_version", rate_version_id, {"rate_version_id": rate_version_id}

            return self._idempotent(connection, request_id=request_id, action="register_rate_version",
                                    payload=payload, create=create)

    def register_deduction_rule(self, *, request_id: str, actor_id: str, license_id: str,
                                deduction_id: str, name: str, kind: str,
                                basis_points: int = 0, amount_cents: int = 0,
                                sequence_no: int = 1) -> WriteReceipt:
        payload = {"actor_id": actor_id, "license_id": license_id, "deduction_id": deduction_id,
                   "name": name, "kind": kind, "basis_points": basis_points,
                   "amount_cents": amount_cents, "sequence_no": sequence_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            deduction_id = self._id(deduction_id, "deduction_id")
            name = self._text(name, "name")
            sequence_no = int(sequence_no)
            if sequence_no < 1:
                raise ValidationError("sequence_no 必须从 1 开始")
            if kind == "percent":
                basis_points = int(basis_points)
                if not 0 < basis_points <= 10000:
                    raise ValidationError("百分比扣减基点必须在 1 到 10000 之间")
                amount_cents = 0
            elif kind == "fixed_per_fact":
                amount_cents = self._cents(amount_cents, "amount_cents")
                if amount_cents <= 0:
                    raise ValidationError("定额扣减必须大于 0")
                basis_points = 0
            else:
                raise ValidationError("kind 必须是 percent 或 fixed_per_fact")
            if connection.execute("SELECT 1 FROM licenses WHERE license_id=?",
                                  (license_id,)).fetchone() is None:
                raise NotFoundError("许可证不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO deduction_rules(deduction_id,license_id,name,kind,basis_points,"
                        "amount_cents,sequence_no,active,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                        (deduction_id, license_id, name, kind, basis_points, amount_cents,
                         sequence_no, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("扣减规则编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="deduction_rule.registered",
                            resource_type="deduction_rule", resource_id=deduction_id,
                            detail={"license_id": license_id, "kind": kind,
                                    "basis_points": basis_points, "amount_cents": amount_cents})
                return "deduction_rule", deduction_id, {"deduction_id": deduction_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_deduction_rule", payload=payload, create=create)

    def register_guarantee(self, *, request_id: str, actor_id: str, license_id: str,
                           period_key: str, amount_cents: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "license_id": license_id, "period_key": period_key,
                   "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            period_key = self._period(period_key)
            amount_cents = self._cents(amount_cents, "amount_cents")
            if connection.execute("SELECT 1 FROM licenses WHERE license_id=?",
                                  (license_id,)).fetchone() is None:
                raise NotFoundError("许可证不存在")
            self._ensure_period(connection, period_key)
            guarantee_id = f"{license_id}:g:{period_key}"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO guarantees(guarantee_id,license_id,period_key,amount_cents,status,"
                        "created_at) VALUES(?,?,?,?,'applicable',?)",
                        (guarantee_id, license_id, period_key, amount_cents, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该期间保底金已登记") from exc
                self._audit(connection, actor_id=actor_id, action="guarantee.registered",
                            resource_type="guarantee", resource_id=guarantee_id,
                            detail={"license_id": license_id, "period_key": period_key,
                                    "amount_cents": amount_cents})
                return "guarantee", guarantee_id, {"guarantee_id": guarantee_id}

            return self._idempotent(connection, request_id=request_id, action="register_guarantee",
                                    payload=payload, create=create)

    def register_reporting_cycle(self, *, request_id: str, actor_id: str, license_id: str,
                                 period_key: str, kind: str, deadline: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "license_id": license_id, "period_key": period_key,
                   "kind": kind, "deadline": deadline}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            license_id = self._id(license_id, "license_id")
            period_key = self._period(period_key)
            kind = self._text(kind, "kind", 40)
            deadline = self._date(deadline, "deadline")
            if connection.execute("SELECT 1 FROM licenses WHERE license_id=?",
                                  (license_id,)).fetchone() is None:
                raise NotFoundError("许可证不存在")
            self._ensure_period(connection, period_key)
            cycle_id = f"{license_id}:cyc:{period_key}"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO reporting_cycles(cycle_id,license_id,period_key,kind,deadline,"
                        "status,created_at) VALUES(?,?,?,?,?,'registered',?)",
                        (cycle_id, license_id, period_key, kind, deadline, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该报送周期已登记") from exc
                self._audit(connection, actor_id=actor_id, action="reporting_cycle.registered",
                            resource_type="reporting_cycle", resource_id=cycle_id,
                            detail={"license_id": license_id, "period_key": period_key,
                                    "kind": kind, "deadline": deadline})
                return "reporting_cycle", cycle_id, {"cycle_id": cycle_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_reporting_cycle", payload=payload, create=create)

    # ----------------------------------------------------------- 幂等事实导入

    def import_usage_batch(self, *, request_id: str, actor_id: str, licensee_id: str,
                           period_key: str, source_ref: str,
                           items: list[dict[str, Any]]) -> WriteReceipt:
        """幂等导入一批使用事实：去重、范围匹配、按控制集团合并计阶梯、生成分录。"""

        payload = {"actor_id": actor_id, "licensee_id": licensee_id, "period_key": period_key,
                   "source_ref": source_ref, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator", "partner")
            licensee_id = self._id(licensee_id, "licensee_id")
            period_key = self._period(period_key)
            source_ref = self._text(source_ref, "source_ref", 120)
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空列表")
            licensee = connection.execute(
                "SELECT * FROM licensees WHERE licensee_id=?", (licensee_id,)).fetchone()
            if licensee is None:
                raise NotFoundError("被许可方不存在")
            if actor["kind"] == "partner" and actor["licensee_id"] != licensee_id:
                raise PermissionDenied("合作方只能报送本主体的用量")
            self._open_period(connection, period_key)

            def create() -> tuple[str, str, dict[str, Any]]:
                batch_id = uuid.uuid4().hex
                counts = {"item_count": len(items), "new_count": 0, "duplicate_count": 0,
                          "matched_count": 0, "claim_count": 0}
                connection.execute(
                    "INSERT INTO usage_batches(batch_id,request_id,licensee_id,period_key,source_ref,"
                    "item_count,new_count,duplicate_count,matched_count,claim_count,status,submitted_by,"
                    "created_at,processed_at) VALUES(?,?,?,?,?,?,?,?,?,?,'processed',?,?,?)",
                    (batch_id, request_id, licensee_id, period_key, source_ref,
                     counts["item_count"], 0, 0, 0, 0, actor_id, self._now(), self._now()),
                )
                for raw in items:
                    self._process_fact(connection, raw_item=raw, batch_id=batch_id,
                                       licensee_id=licensee_id, period_key=period_key,
                                       actor_id=actor_id, counts=counts)
                connection.execute(
                    "UPDATE usage_batches SET new_count=?,duplicate_count=?,matched_count=?,"
                    "claim_count=? WHERE batch_id=?",
                    (counts["new_count"], counts["duplicate_count"], counts["matched_count"],
                     counts["claim_count"], batch_id),
                )
                connection.execute(
                    "UPDATE reporting_cycles SET status='received', batch_id=? "
                    "WHERE period_key=? AND status='registered' AND license_id IN "
                    "(SELECT license_id FROM licenses WHERE licensee_id=?)",
                    (batch_id, period_key, licensee_id),
                )
                self._audit(connection, actor_id=actor_id, action="usage_batch.imported",
                            resource_type="usage_batch", resource_id=batch_id,
                            detail={"licensee_id": licensee_id, "period_key": period_key,
                                    "source_ref": source_ref, **counts})
                return "usage_batch", batch_id, {"batch_id": batch_id, **counts}

            return self._idempotent(connection, request_id=request_id, action="import_usage_batch",
                                    payload=payload, create=create)

    def _load_tiers(self, connection, rate_version_id: str):
        rows = connection.execute(
            "SELECT * FROM rate_tiers WHERE rate_version_id=? ORDER BY tier_no",
            (rate_version_id,)).fetchall()
        from .models import Tier
        return [Tier(row["tier_no"], row["lower_bound_cents"], row["rate_basis_points"]) for row in rows]

    def _active_entitlement(self, connection, work_id: str, on_date: str):
        return connection.execute(
            "SELECT * FROM entitlement_versions WHERE work_id=? AND status='active' "
            "AND effective_from<=? ORDER BY version_no DESC LIMIT 1",
            (work_id, on_date)).fetchone()

    def _match_license(self, connection, *, licensee_id: str, work_id: str | None,
                       territory: str, channel: str, usage_purpose: str, occurred_at: str):
        """按被许可方、作品、地域/渠道/用途与有效期做范围匹配，返回 (许可证行, 原因)。

        先看是否存在覆盖该使用范围的许可证，再判断它是否处于有效期内，从而区分
        “许可证失效”（expired）与“地域/渠道/用途超范围”（out_of_scope）。
        """

        if work_id is None:
            return None, "unmatched"

        def covers(scope: str, value: str) -> bool:
            return scope == "*" or scope == value

        rows = connection.execute(
            "SELECT l.*, lv.control_group_id FROM licenses l "
            "JOIN licensees lv ON lv.licensee_id=l.licensee_id "
            "WHERE l.licensee_id=? AND l.work_id=?",
            (licensee_id, work_id)).fetchall()
        scoped = [row for row in rows
                  if covers(row["territory"], territory)
                  and covers(row["channel"], channel)
                  and covers(row["usage_purpose"], usage_purpose)]
        valid = [row for row in scoped
                 if row["status"] == "active" and row["valid_from"] <= occurred_at
                 and (row["valid_to"] is None or occurred_at < row["valid_to"])]
        if len(valid) > 1:
            raise ConflictError("范围匹配命中多个有效许可证，需要补充地域/渠道/用途")
        if valid:
            return valid[0], None
        if scoped:
            return None, "expired"
        return None, "out_of_scope"

    def _process_fact(self, connection, *, raw_item: dict[str, Any], batch_id: str,
                      licensee_id: str, period_key: str, actor_id: str,
                      counts: dict[str, int]) -> None:
        record_key = self._id(raw_item["source_record_key"], "source_record_key")
        work_id = raw_item.get("work_id")
        if work_id is not None:
            work_id = self._id(work_id, "work_id")
        territory = self._text(raw_item["territory"], "territory", 80)
        channel = self._text(raw_item["channel"], "channel", 80)
        usage_purpose = self._text(raw_item["usage_purpose"], "usage_purpose", 80)
        occurred_at = self._date(raw_item["occurred_at"], "occurred_at")
        quantity = int(raw_item.get("quantity", 1))
        if quantity < 0:
            raise ValidationError("quantity 不能为负")
        correction_of = raw_item.get("correction_of")
        if correction_of:
            correction_of = self._id(correction_of, "correction_of")
        original_fact = None
        if correction_of:
            original_fact = connection.execute(
                "SELECT * FROM usage_facts WHERE licensee_id=? AND source_record_key=? "
                "AND status='matched'",
                (licensee_id, correction_of)).fetchone()
            if original_fact is None:
                raise NotFoundError(f"更正引用的原始记录 {correction_of} 不存在或未匹配")
            if original_fact["period_key"] == period_key:
                raise ConflictError("当期数据请直接作废重报，跨期更正才使用 correction_of")
        gross = self._cents(raw_item["gross_revenue_cents"], "gross_revenue_cents",
                            allow_negative=bool(correction_of))
        dedup_key = digest([licensee_id, record_key, correction_of or ""])
        if connection.execute("SELECT 1 FROM usage_facts WHERE dedup_key=?",
                              (dedup_key,)).fetchone():
            counts["duplicate_count"] += 1
            return
        counts["new_count"] += 1

        if original_fact is not None:
            license_row = connection.execute(
                "SELECT l.*, lv.control_group_id FROM licenses l "
                "JOIN licensees lv ON lv.licensee_id=l.licensee_id WHERE l.license_id=?",
                (original_fact["license_id"],)).fetchone()
            rate_version_id = original_fact["rate_version_id"]
            match_status, match_note = "matched", f"correction_of={correction_of}"
            work_id = original_fact["work_id"]
        else:
            license_row, reason = self._match_license(
                connection, licensee_id=licensee_id, work_id=work_id, territory=territory,
                channel=channel, usage_purpose=usage_purpose, occurred_at=occurred_at)
            if license_row is None:
                note = {"expired": "许可证已失效或超出有效期",
                        "out_of_scope": "地域/渠道/用途超范围",
                        "unmatched": "作品无法识别，无有效许可证"}[reason]
                fact_id = self._insert_fact(
                    connection, batch_id=batch_id, licensee_id=licensee_id, period_key=period_key,
                    record_key=record_key, payload=raw_item, work_id=work_id, license_row=None,
                    rate_version_id=None, territory=territory, channel=channel,
                    usage_purpose=usage_purpose, quantity=quantity, gross=gross,
                    occurred_at=occurred_at, dedup_key=dedup_key, status=reason, match_note=note)
                connection.execute(
                    "INSERT INTO claim_items(claim_id,fact_id,licensee_id,work_id,period_key,reason,"
                    "gross_cents,note,status,created_at) VALUES(?,?,?,?,?,?,?,?,'open',?)",
                    (uuid.uuid4().hex, fact_id, licensee_id, work_id, period_key, reason,
                     max(gross, 0), note, self._now()),
                )
                counts["claim_count"] += 1
                return
            rate_versions = [dict(row) for row in connection.execute(
                "SELECT * FROM rate_versions WHERE license_id=? ORDER BY version_no",
                (license_row["license_id"],)).fetchall()]
            chosen = select_rate_version(rate_versions, occurred_at)
            if chosen is None:
                note = "生效日无可用费率版本"
                fact_id = self._insert_fact(
                    connection, batch_id=batch_id, licensee_id=licensee_id, period_key=period_key,
                    record_key=record_key, payload=raw_item, work_id=work_id, license_row=None,
                    rate_version_id=None, territory=territory, channel=channel,
                    usage_purpose=usage_purpose, quantity=quantity, gross=gross,
                    occurred_at=occurred_at, dedup_key=dedup_key, status="expired", match_note=note)
                connection.execute(
                    "INSERT INTO claim_items(claim_id,fact_id,licensee_id,work_id,period_key,reason,"
                    "gross_cents,note,status,created_at) VALUES(?,?,?,?,?,?,'expired',?,?, 'open',?)",
                    (uuid.uuid4().hex, fact_id, licensee_id, work_id, period_key, max(gross, 0),
                     note, self._now()),
                )
                counts["claim_count"] += 1
                return
            rate_version_id = chosen["rate_version_id"]
            match_status, match_note = "matched", license_row["license_id"]

        fact_id = self._insert_fact(
            connection, batch_id=batch_id, licensee_id=licensee_id, period_key=period_key,
            record_key=record_key, payload=raw_item, work_id=work_id, license_row=license_row,
            rate_version_id=rate_version_id, territory=territory, channel=channel,
            usage_purpose=usage_purpose, quantity=quantity, gross=gross, occurred_at=occurred_at,
            dedup_key=dedup_key, status=match_status, match_note=match_note)
        counts["matched_count"] += 1
        fact_row = connection.execute("SELECT * FROM usage_facts WHERE fact_id=?",
                                      (fact_id,)).fetchone()
        if fact_row["gross_revenue_cents"] == 0:
            return  # 零销售额合规报送不产生计费分录，但仍标记报送周期已收到
        self._bill_fact(connection, fact_row=fact_row, license_row=license_row,
                        rate_version_id=rate_version_id, original_fact=original_fact,
                        actor_id=actor_id)

    def _insert_fact(self, connection, *, batch_id: str, licensee_id: str, period_key: str,
                     record_key: str, payload: dict[str, Any], work_id: str | None, license_row,
                     rate_version_id: str | None, territory: str, channel: str, usage_purpose: str,
                     quantity: int, gross: int, occurred_at: str, dedup_key: str,
                     status: str, match_note: str) -> str:
        fact_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO usage_facts(fact_id,batch_id,licensee_id,period_key,source_record_key,"
            "payload_hash,work_id,work_version_id,license_id,rate_version_id,territory,channel,"
            "usage_purpose,quantity,gross_revenue_cents,occurred_at,dedup_key,status,match_note,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact_id, batch_id, licensee_id, period_key, record_key, digest(payload), work_id,
             license_row["work_version_id"] if license_row else None,
             license_row["license_id"] if license_row else None, rate_version_id,
             territory, channel, usage_purpose, quantity, gross, occurred_at, dedup_key,
             status, match_note, self._now()),
        )
        return fact_id

    def _bill_fact(self, connection, *, fact_row, license_row, rate_version_id: str,
                   original_fact, actor_id: str) -> None:
        """对匹配事实生成不可覆盖的计费分录与按份额的分账明细。

        普通事实按当期集团累计基数边际计费；跨期更正按“从原累计位置抽掉旧基数、
        插入更正后基数”的位置差计费，使退货或补报在阶梯临界两侧都对称可重算。
        """

        gross = fact_row["gross_revenue_cents"]
        rules = [dict(row) for row in connection.execute(
            "SELECT * FROM deduction_rules WHERE license_id=? AND active=1 ORDER BY sequence_no",
            (license_row["license_id"],)).fetchall()]
        tiers = self._load_tiers(connection, rate_version_id)
        group_id = license_row["control_group_id"]

        if original_fact is not None:
            origin_entry = connection.execute(
                "SELECT * FROM billing_entries WHERE fact_id=?",
                (original_fact["fact_id"],)).fetchone()
            new_base, deduction_trace = apply_deductions(gross, rules, fact_row["quantity"])
            old_base = origin_entry["base_cents"]
            base = new_base - old_base
            gross_delta = gross - original_fact["gross_revenue_cents"]
            deduction_total = gross_delta - base
            # 在原始累计位置上重放：位置 = 原分录之后位置 - 原基数，再插入新基数
            anchor_after = origin_entry["cumulative_base_after"]
            anchor_before = anchor_after - old_base
            fee = (self._integrate(tiers, anchor_before + new_base)
                   - self._integrate(tiers, anchor_before + old_base))
            cumulative_before = anchor_after
            cumulative_after = anchor_after + base
        else:
            base, deduction_trace = apply_deductions(gross, rules, fact_row["quantity"])
            deduction_total = gross - base
            cumulative_row = connection.execute(
                "SELECT COALESCE(SUM(base_cents),0) AS cum FROM billing_entries "
                "WHERE period_key=? AND work_id=? AND entry_type='accrual' AND license_id IN "
                "(SELECT license_id FROM licenses JOIN licensees "
                "ON licensees.licensee_id=licenses.licensee_id "
                "WHERE licensees.control_group_id=?)",
                (fact_row["period_key"], fact_row["work_id"], group_id)).fetchone()
            cumulative_before = cumulative_row["cum"]
            fee, _, _ = tiered_fee(tiers, cumulative_before, base)
            cumulative_after = cumulative_before + base

        ent = self._active_entitlement(connection, fact_row["work_id"], fact_row["occurred_at"])
        if ent is None:
            raise ConflictError(
                f"作品 {fact_row['work_id']} 在 {fact_row['occurred_at']} 无生效份额版本")
        share_rows = connection.execute(
            "SELECT * FROM entitlement_shares WHERE ent_version_id=? ORDER BY holder_id",
            (ent["ent_version_id"],)).fetchall()
        weights = [row["basis_points"] for row in share_rows]
        amounts = prorate(fee, weights)

        entry_type = "accrual"
        source_entry_id = None
        origin_period_key = fact_row["period_key"]
        if original_fact is not None:
            source_entry_id = origin_entry["entry_id"]
            origin_period_key = origin_entry["period_key"]
            entry_type = "supplement" if fee >= 0 else "reversal"
        landing_tier_no = self._landing_tier(tiers, cumulative_after)
        landing_rate = next((t.rate_basis_points for t in tiers
                             if t.tier_no == landing_tier_no), 0)
        trace = {
            "gross_cents": gross_delta if original_fact is not None else gross,
            "deductions": deduction_trace,
            "deduction_cents": deduction_total,
            "base_cents": base,
            "control_group_id": group_id,
            "cumulative_base_before": cumulative_before,
            "cumulative_base_after": cumulative_after,
            "rate_version_id": rate_version_id,
            "landing_tier_no": landing_tier_no,
            "landing_rate_basis_points": landing_rate,
            "entitlement_version_id": ent["ent_version_id"],
            "shares": [{"holder_id": r["holder_id"], "basis_points": r["basis_points"],
                        "amount_cents": part}
                       for r, part in zip(share_rows, amounts)],
            "correction_of": original_fact["source_record_key"] if original_fact else None,
        }
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO billing_entries(entry_id,entry_type,period_key,origin_period_key,license_id,"
            "licensee_id,work_id,work_version_id,rate_version_id,ent_version_id,fact_id,"
            "source_entry_id,gross_cents,deduction_cents,base_cents,rate_basis_points,amount_cents,"
            "cumulative_base_before,cumulative_base_after,settled_via_cancel,trace_json,status,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'confirmed',?,?)",
            (entry_id, entry_type, fact_row["period_key"], origin_period_key,
             license_row["license_id"], license_row["licensee_id"], fact_row["work_id"],
             fact_row["work_version_id"], rate_version_id, ent["ent_version_id"], fact_row["fact_id"],
             source_entry_id, trace["gross_cents"], deduction_total, base, landing_rate, fee,
             cumulative_before, cumulative_after, 0, canonical_json(trace),
             actor_id, self._now()),
        )
        for row, part in zip(share_rows, amounts):
            connection.execute(
                "INSERT INTO entry_allocations(entry_id,holder_id,amount_cents) VALUES(?,?,?)",
                (entry_id, row["holder_id"], part),
            )
        self._audit(connection, actor_id=actor_id, action="billing_entry.generated",
                    resource_type="billing_entry", resource_id=entry_id,
                    detail={"entry_type": entry_type, "period_key": fact_row["period_key"],
                            "fact_id": fact_row["fact_id"], "amount_cents": fee,
                            "cumulative_base_after": cumulative_after,
                            "landing_tier_no": landing_tier_no})

    @staticmethod
    def _integrate(tiers, position_cents: int) -> int:
        from .billing import integrate
        return integrate(tiers, position_cents)

    @staticmethod
    def _landing_tier(tiers, position_cents: int) -> int:
        landing = tiers[0]
        for tier in sorted(tiers, key=lambda t: t.lower_bound_cents):
            if position_cents >= tier.lower_bound_cents:
                landing = tier
        return landing.tier_no

    # ------------------------------------------------------------------ 关账

    def close_period(self, *, request_id: str, actor_id: str, period_key: str,
                     note: str = "") -> WriteReceipt:
        """关账：校验报送周期、保底补足、冻结已确认分录、按对手方生成付款队列。"""

        payload = {"actor_id": actor_id, "period_key": period_key, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            period_key = self._period(period_key)

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_period(connection, period_key)
                period = connection.execute("SELECT * FROM periods WHERE period_key=?",
                                            (period_key,)).fetchone()
                if period["status"] == "closed":
                    raise ConflictError(f"期间 {period_key} 已关账")
                missing = connection.execute(
                    "SELECT license_id FROM reporting_cycles WHERE period_key=? AND status!='received'",
                    (period_key,)).fetchall()
                if missing:
                    raise ConflictError("存在未收到数据的报送周期: "
                                        + ",".join(row["license_id"] for row in missing))
                self._apply_guarantees(connection, period_key=period_key, actor_id=actor_id)
                entries = connection.execute(
                    "SELECT * FROM billing_entries WHERE period_key=? AND status='confirmed' "
                    "ORDER BY created_at, entry_id",
                    (period_key,)).fetchall()
                for entry in entries:
                    connection.execute(
                        "UPDATE billing_entries SET status='frozen' WHERE entry_id=?",
                        (entry["entry_id"],))
                order_ids = self._queue_period_orders(connection, period_key=period_key,
                                                      entries=entries)
                connection.execute(
                    "UPDATE periods SET status='closed', closed_at=?, note=? WHERE period_key=?",
                    (self._now(), note, period_key),
                )
                self._audit(connection, actor_id=actor_id, action="period.closed",
                            resource_type="period", resource_id=period_key,
                            detail={"frozen_entries": len(entries), "orders": order_ids})
                return "period", period_key, {"period_key": period_key, "status": "closed",
                                              "frozen_entries": len(entries), "orders": order_ids}

            return self._idempotent(connection, request_id=request_id, action="close_period",
                                    payload=payload, create=create)

    def _apply_guarantees(self, connection, *, period_key: str, actor_id: str) -> None:
        guarantees = connection.execute(
            "SELECT * FROM guarantees WHERE period_key=? AND status='applicable'",
            (period_key,)).fetchall()
        for guarantee in guarantees:
            accrued = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM billing_entries "
                "WHERE period_key=? AND license_id=? AND entry_type IN ('accrual','supplement','reversal')",
                (period_key, guarantee["license_id"])).fetchone()["total"]
            shortfall = guarantee["amount_cents"] - accrued
            connection.execute("UPDATE guarantees SET status='applied' WHERE guarantee_id=?",
                               (guarantee["guarantee_id"],))
            if shortfall <= 0:
                continue
            license_row = connection.execute(
                "SELECT l.*, lv.control_group_id FROM licenses l "
                "JOIN licensees lv ON lv.licensee_id=l.licensee_id WHERE l.license_id=?",
                (guarantee["license_id"],)).fetchone()
            ent = self._active_entitlement(connection, license_row["work_id"],
                                           self._period_start(period_key))
            if ent is None:
                raise ConflictError("保底补足缺少生效份额版本")
            share_rows = connection.execute(
                "SELECT * FROM entitlement_shares WHERE ent_version_id=? ORDER BY holder_id",
                (ent["ent_version_id"],)).fetchall()
            amounts = prorate(shortfall, [row["basis_points"] for row in share_rows])
            entry_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO billing_entries(entry_id,entry_type,period_key,origin_period_key,license_id,"
                "licensee_id,work_id,work_version_id,rate_version_id,ent_version_id,guarantee_id,"
                "gross_cents,deduction_cents,base_cents,rate_basis_points,amount_cents,"
                "cumulative_base_before,cumulative_base_after,settled_via_cancel,trace_json,status,"
                "created_by,created_at) "
                "VALUES(?, 'guarantee_shortfall', ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0,0,0,0,?,"
                "0,0,0,?,'confirmed',?,?)",
                (entry_id, period_key, period_key, license_row["license_id"],
                 license_row["licensee_id"], license_row["work_id"], license_row["work_version_id"],
                 ent["ent_version_id"], guarantee["guarantee_id"], shortfall,
                 canonical_json({"guarantee_id": guarantee["guarantee_id"],
                                 "guarantee_cents": guarantee["amount_cents"],
                                 "accrued_cents": accrued, "shortfall_cents": shortfall}),
                 actor_id, self._now()),
            )
            for row, part in zip(share_rows, amounts):
                connection.execute(
                    "INSERT INTO entry_allocations(entry_id,holder_id,amount_cents) VALUES(?,?,?)",
                    (entry_id, row["holder_id"], part),
                )
            connection.execute(
                "UPDATE guarantees SET shortfall_entry_id=? WHERE guarantee_id=?",
                (entry_id, guarantee["guarantee_id"]),
            )
            self._audit(connection, actor_id=actor_id, action="billing_entry.guarantee_shortfall",
                        resource_type="billing_entry", resource_id=entry_id,
                        detail={"guarantee_id": guarantee["guarantee_id"],
                                "period_key": period_key, "shortfall_cents": shortfall})

    def _next_sequence(self, connection) -> int:
        return int(connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS s FROM payment_orders").fetchone()["s"])

    def _queue_period_orders(self, connection, *, period_key: str, entries) -> list[str]:
        """按对手方汇总额度生成应收/应付队列；争议冲销分录不再入队。"""

        order_ids: list[str] = []
        receivables: dict[str, int] = {}
        payables: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry["settled_via_cancel"]:
                continue
            if entry["amount_cents"] and entry["licensee_id"]:
                receivables[entry["licensee_id"]] = receivables.get(entry["licensee_id"], 0) \
                    + entry["amount_cents"]
            for part in connection.execute(
                    "SELECT * FROM entry_allocations WHERE entry_id=?",
                    (entry["entry_id"],)).fetchall():
                if not part["amount_cents"]:
                    continue
                bucket = payables.setdefault(part["holder_id"], {"total": 0, "parts": []})
                bucket["total"] += part["amount_cents"]
                bucket["parts"].append((entry["entry_id"], part["holder_id"], part["amount_cents"]))
        for licensee_id, amount in sorted(receivables.items()):
            if amount <= 0:
                continue
            order_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO payment_orders(order_id,sequence,direction,counterparty_id,period_key,"
                "amount_cents,status,created_at) VALUES(?,?,'receivable',?,?,?,'queued',?)",
                (order_id, self._next_sequence(connection), licensee_id, period_key, amount,
                 self._now()),
            )
            order_ids.append(order_id)
        for holder_id, bucket in sorted(payables.items()):
            if bucket["total"] <= 0:
                continue
            order_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO payment_orders(order_id,sequence,direction,counterparty_id,period_key,"
                "amount_cents,status,created_at) VALUES(?,?,'payable',?,?,?,'queued',?)",
                (order_id, self._next_sequence(connection), holder_id, period_key,
                 bucket["total"], self._now()),
            )
            for entry_id, hid, part_amount in bucket["parts"]:
                connection.execute(
                    "INSERT INTO payment_parts(order_id,entry_id,holder_id,amount_cents) "
                    "VALUES(?,?,?,?)",
                    (order_id, entry_id, hid, part_amount),
                )
            order_ids.append(order_id)
        return order_ids

    # ------------------------------------------------------------------ 争议

    def _receivable_order(self, connection, licensee_id: str, period_key: str):
        return connection.execute(
            "SELECT * FROM payment_orders WHERE direction='receivable' AND counterparty_id=? "
            "AND period_key=?", (licensee_id, period_key)).fetchone()

    def _receivable_held(self, connection, licensee_id: str, period_key: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(d.amount_cents),0) AS held FROM disputes d "
            "JOIN billing_entries e ON e.entry_id=d.entry_id "
            "WHERE d.status='open' AND e.licensee_id=? AND e.period_key=?",
            (licensee_id, period_key)).fetchone()["held"]

    def _payable_held(self, connection, order_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(dp.amount_cents),0) AS held FROM dispute_parts dp "
            "JOIN disputes d ON d.dispute_id=dp.dispute_id "
            "JOIN payment_parts pp ON pp.entry_id=d.entry_id AND pp.holder_id=dp.holder_id "
            "WHERE pp.order_id=? AND d.status='open'", (order_id,)).fetchone()["held"]

    def _entry_paid_and_cancelled(self, connection, entry) -> tuple[int, int]:
        """统计分录在应收侧与应付侧已经支付/核销的额度（取两侧较大值约束争议）。"""

        paid = 0
        cancelled = 0
        order = self._receivable_order(connection, entry["licensee_id"], entry["period_key"])
        if order is not None and order["amount_cents"]:
            settled = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM entry_settlements "
                "WHERE entry_id=? AND direction='receivable'", (entry["entry_id"],)).fetchone()["total"]
            ratio_paid = order["paid_cents"] * entry["amount_cents"] // order["amount_cents"]
            paid = max(paid, ratio_paid)
            cancelled = max(cancelled, settled)
        for part in connection.execute(
                "SELECT * FROM payment_parts WHERE entry_id=?", (entry["entry_id"],)).fetchall():
            po = connection.execute("SELECT * FROM payment_orders WHERE order_id=?",
                                    (part["order_id"],)).fetchone()
            if po["amount_cents"]:
                paid = max(paid, po["paid_cents"] * part["amount_cents"] // po["amount_cents"])
                settled = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS total FROM entry_settlements "
                    "WHERE entry_id=? AND holder_id IS ? AND direction='payable'",
                    (entry["entry_id"], part["holder_id"])).fetchone()["total"]
                cancelled = max(cancelled, settled)
        return paid, cancelled

    def open_dispute(self, *, request_id: str, actor_id: str, entry_id: str,
                     amount_cents: int, reason: str) -> WriteReceipt:
        """对已冻结分录提出争议：只托管争议金额，不阻断其余无争议金额。"""

        payload = {"actor_id": actor_id, "entry_id": entry_id, "amount_cents": amount_cents,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator", "partner")
            entry_id = self._id(entry_id, "entry_id")
            amount_cents = self._cents(amount_cents, "amount_cents")
            reason = self._text(reason, "reason", 500)
            entry = connection.execute("SELECT * FROM billing_entries WHERE entry_id=?",
                                       (entry_id,)).fetchone()
            if entry is None:
                raise NotFoundError("计费分录不存在")
            if entry["status"] != "frozen":
                raise ConflictError("只能对已关账冻结的分录提出争议")
            if actor["kind"] == "partner" and actor["licensee_id"] != entry["licensee_id"]:
                raise PermissionDenied("合作方只能对本主体的分录提出争议")
            if amount_cents > entry["amount_cents"]:
                raise ValidationError("争议金额不能超过分录金额")
            already = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS held FROM disputes "
                "WHERE entry_id=? AND status='open'", (entry_id,)).fetchone()["held"]
            if already + amount_cents > entry["amount_cents"]:
                raise ConflictError("争议托管金额合计不能超过分录金额")
            paid, cancelled = self._entry_paid_and_cancelled(connection, entry)
            if amount_cents > entry["amount_cents"] - paid - cancelled - already:
                raise ConflictError("争议金额超过该分录尚未支付且未托管的额度")
            alloc_rows = connection.execute(
                "SELECT * FROM entry_allocations WHERE entry_id=? ORDER BY holder_id",
                (entry_id,)).fetchall()
            weights = [max(row["amount_cents"], 0) for row in alloc_rows]
            if amount_cents > 0 and sum(weights) == 0:
                raise ConflictError("分录没有可托管的分账金额")
            held_parts = prorate(amount_cents, weights)
            dispute_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO disputes(dispute_id,entry_id,amount_cents,reason,status,created_by,"
                    "created_at) VALUES(?,?,?,?,'open',?,?)",
                    (dispute_id, entry_id, amount_cents, reason, actor_id, self._now()),
                )
                for row, held in zip(alloc_rows, held_parts):
                    connection.execute(
                        "INSERT INTO dispute_parts(dispute_id,holder_id,amount_cents) VALUES(?,?,?)",
                        (dispute_id, row["holder_id"], held),
                    )
                self._audit(connection, actor_id=actor_id, action="dispute.opened",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"entry_id": entry_id, "amount_cents": amount_cents})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "escrow_cents": amount_cents}

            return self._idempotent(connection, request_id=request_id, action="open_dispute",
                                    payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        outcome: str, next_period_key: str | None = None) -> WriteReceipt:
        """release 解除托管恢复可付；reversed 冲销进入后续期间并核销两侧队列额度。"""

        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "outcome": outcome,
                   "next_period_key": next_period_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            dispute_id = self._id(dispute_id, "dispute_id")
            if outcome not in ("released", "reversed"):
                raise ValidationError("outcome 必须是 released 或 reversed")
            dispute = connection.execute("SELECT * FROM disputes WHERE dispute_id=?",
                                         (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已处理")
            entry = connection.execute("SELECT * FROM billing_entries WHERE entry_id=?",
                                       (dispute["entry_id"],)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                target_period = None
                if outcome == "reversed":
                    target_period = self._period(next_period_key)
                    self._open_period(connection, target_period)
                    self._write_reversal_for_dispute(connection, entry=entry, dispute=dispute,
                                                     period_key=target_period, actor_id=actor_id)
                connection.execute(
                    "UPDATE disputes SET status=?, resolved_at=? WHERE dispute_id=?",
                    (outcome, self._now(), dispute_id),
                )
                self._audit(connection, actor_id=actor_id, action=f"dispute.{outcome}",
                            resource_type="dispute", resource_id=dispute_id,
                            detail={"entry_id": entry["entry_id"],
                                    "amount_cents": dispute["amount_cents"],
                                    "next_period_key": target_period})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "status": outcome}

            return self._idempotent(connection, request_id=request_id, action="resolve_dispute",
                                    payload=payload, create=create)

    def _write_reversal_for_dispute(self, connection, *, entry, dispute, period_key: str,
                                    actor_id: str) -> None:
        """冲销争议：已冻结期间账目不动，只核销两侧队列中的托管额度。

        核销记入只追加的 entry_settlements，使该期间
        “应收=已收+托管+余额”“应付=已付+托管+余额”继续成立，
        且不向后续期间追加负数分录，避免对同一笔金额二次扣减。
        """

        parts = connection.execute("SELECT * FROM dispute_parts WHERE dispute_id=?",
                                   (dispute["dispute_id"],)).fetchall()
        for part in parts:
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE direction='payable' AND period_key=? "
                "AND counterparty_id=?",
                (entry["period_key"], part["holder_id"])).fetchone()
            if order is not None:
                self._cancel_order_amount(connection, order, part["amount_cents"])
            connection.execute(
                "INSERT INTO entry_settlements(settlement_id,entry_id,holder_id,direction,reason,"
                "ref_id,amount_cents,created_at) VALUES(?,?,?, 'payable', 'dispute_reversed', ?,?,?)",
                (uuid.uuid4().hex, entry["entry_id"], part["holder_id"], dispute["dispute_id"],
                 part["amount_cents"], self._now()),
            )
        receivable = self._receivable_order(connection, entry["licensee_id"], entry["period_key"])
        if receivable is not None:
            self._cancel_order_amount(connection, receivable, dispute["amount_cents"])
        connection.execute(
            "INSERT INTO entry_settlements(settlement_id,entry_id,holder_id,direction,reason,"
            "ref_id,amount_cents,created_at) VALUES(?,?,NULL, 'receivable', 'dispute_reversed', ?,?,?)",
            (uuid.uuid4().hex, entry["entry_id"], dispute["dispute_id"],
             dispute["amount_cents"], self._now()),
        )
        self._audit(connection, actor_id=actor_id, action="billing_entry.dispute_reversed",
                    resource_type="billing_entry", resource_id=entry["entry_id"],
                    detail={"dispute_id": dispute["dispute_id"],
                            "amount_cents": -dispute["amount_cents"],
                            "target_period_key": period_key})

    def _cancel_order_amount(self, connection, order, amount: int) -> None:
        usable = max(0, order["amount_cents"] - order["paid_cents"] - order["cancelled_cents"])
        cancel = min(amount, usable)
        if cancel <= 0:
            return
        cancelled = order["cancelled_cents"] + cancel
        paid = order["paid_cents"]
        finished = cancelled + paid >= order["amount_cents"]
        connection.execute(
            "UPDATE payment_orders SET cancelled_cents=?, status=CASE WHEN ?=1 THEN 'paid' "
            "ELSE status END, paid_at=CASE WHEN ?=1 AND paid_at IS NULL THEN ? ELSE paid_at END "
            "WHERE order_id=?",
            (cancelled, 1 if finished else 0, 1 if finished else 0, self._now(), order["order_id"]),
        )

    # ------------------------------------------------------------------ 付款

    def order_queue(self, actor_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """按入队顺序返回未完成队列，服务重启后顺序不变。"""

        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        rows = connection.execute(
            "SELECT * FROM payment_orders WHERE status='queued' ORDER BY sequence LIMIT ?",
            (int(limit),)).fetchall()
        items = []
        for row in rows:
            view = self._order_dict(connection, row)
            if principal["kind"] == "partner" and principal["licensee_id"] != row["counterparty_id"]:
                continue
            if principal["kind"] == "holder" and principal["holder_id"] != row["counterparty_id"]:
                continue
            items.append(view)
        return items

    def _order_dict(self, connection, row) -> dict[str, Any]:
        held = 0
        if row["direction"] == "payable":
            held = self._payable_held(connection, row["order_id"])
        else:
            held = self._receivable_held(connection, row["counterparty_id"], row["period_key"])
        settled = row["paid_cents"] + row["cancelled_cents"]
        return {
            "order_id": row["order_id"], "sequence": row["sequence"], "direction": row["direction"],
            "counterparty_id": row["counterparty_id"], "period_key": row["period_key"],
            "amount_cents": row["amount_cents"], "paid_cents": row["paid_cents"],
            "cancelled_cents": row["cancelled_cents"], "escrow_cents": held,
            "payable_now_cents": max(0, row["amount_cents"] - settled - held),
            "status": "paid" if settled >= row["amount_cents"] else row["status"],
        }

    def mark_order_paid(self, *, request_id: str, actor_id: str, order_id: str,
                        amount_cents: int | None = None) -> WriteReceipt:
        """推进队列付款；争议托管额度不可支付，必须按队列顺序。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            order_id = self._id(order_id, "order_id")
            order = connection.execute("SELECT * FROM payment_orders WHERE order_id=?",
                                       (order_id,)).fetchone()
            if order is None:
                raise NotFoundError("付款单不存在")
            settled = order["paid_cents"] + order["cancelled_cents"]
            if settled >= order["amount_cents"]:
                raise ConflictError("付款单已完成")
            earlier = connection.execute(
                "WITH held AS ("
                "SELECT po.order_id, CASE WHEN po.direction='payable' THEN "
                "COALESCE((SELECT SUM(dp.amount_cents) FROM dispute_parts dp "
                "JOIN disputes d ON d.dispute_id=dp.dispute_id "
                "JOIN payment_parts pp ON pp.entry_id=d.entry_id AND pp.holder_id=dp.holder_id "
                "WHERE pp.order_id=po.order_id AND d.status='open'),0) ELSE "
                "COALESCE((SELECT SUM(d2.amount_cents) FROM disputes d2 "
                "JOIN billing_entries e2 ON e2.entry_id=d2.entry_id "
                "WHERE d2.status='open' AND e2.licensee_id=po.counterparty_id "
                "AND e2.period_key=po.period_key),0) END AS held_cents "
                "FROM payment_orders po) "
                "SELECT COUNT(*) AS c FROM payment_orders JOIN held "
                "ON held.order_id=payment_orders.order_id "
                "WHERE payment_orders.status='queued' AND payment_orders.sequence<? "
                "AND payment_orders.amount_cents - payment_orders.paid_cents "
                "- payment_orders.cancelled_cents > held.held_cents",
                (order["sequence"],)).fetchone()["c"]
            if earlier:
                raise ConflictError("必须按队列顺序付款，前面仍有可支付的未完成付款单")
            if order["direction"] == "receivable":
                held = self._receivable_held(connection, order["counterparty_id"],
                                             order["period_key"])
            else:
                held = self._payable_held(connection, order_id)
            available = order["amount_cents"] - settled - held
            pay_amount = available if amount_cents is None else self._cents(amount_cents,
                                                                            "amount_cents")
            if pay_amount <= 0:
                raise ConflictError("当前没有可支付额度（可能处于争议托管中）")
            if pay_amount > available:
                raise ConflictError("支付金额超过可用额度，争议托管金额必须等待争议处理")

            def create() -> tuple[str, str, dict[str, Any]]:
                new_paid = order["paid_cents"] + pay_amount
                finished = new_paid + order["cancelled_cents"] >= order["amount_cents"]
                connection.execute(
                    "UPDATE payment_orders SET paid_cents=?, status=CASE WHEN ?=1 THEN 'paid' "
                    "ELSE 'queued' END, paid_at=CASE WHEN ?=1 THEN ? ELSE paid_at END "
                    "WHERE order_id=?",
                    (new_paid, 1 if finished else 0, 1 if finished else 0, self._now(), order_id),
                )
                self._audit(connection, actor_id=actor_id, action="payment_order.paid",
                            resource_type="payment_order", resource_id=order_id,
                            detail={"amount_cents": pay_amount, "finished": bool(finished),
                                    "direction": order["direction"]})
                return "payment_order", order_id, {"order_id": order_id, "paid_cents": new_paid,
                                                    "status": "paid" if finished else "queued"}

            return self._idempotent(connection, request_id=request_id, action="mark_order_paid",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 追偿

    def resolve_claim(self, *, request_id: str, actor_id: str, claim_id: str, resolution: str,
                      recovered_cents: int | None = None, period_key: str | None = None,
                      fallback_holder_id: str | None = None) -> WriteReceipt:
        """待追偿项目：recovered 达成交付并在指定期间计费；waived 核销。"""

        payload = {"actor_id": actor_id, "claim_id": claim_id, "resolution": resolution,
                   "recovered_cents": recovered_cents, "period_key": period_key,
                   "fallback_holder_id": fallback_holder_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._principal(connection, actor_id)
            self._require(actor, "operator")
            claim_id = self._id(claim_id, "claim_id")
            claim = connection.execute("SELECT * FROM claim_items WHERE claim_id=?",
                                       (claim_id,)).fetchone()
            if claim is None:
                raise NotFoundError("待追偿项目不存在")
            if claim["status"] != "open":
                raise ConflictError("待追偿项目已处理")
            if resolution not in ("recovered", "waived"):
                raise ValidationError("resolution 必须是 recovered 或 waived")

            def create() -> tuple[str, str, dict[str, Any]]:
                entry_id = None
                if resolution == "recovered":
                    amount = self._cents(recovered_cents, "recovered_cents")
                    if amount <= 0:
                        raise ValidationError("追偿金额必须大于 0")
                    target_period = self._period(period_key)
                    self._open_period(connection, target_period)
                    entry_id = self._write_recovery_entry(
                        connection, claim=claim, amount=amount, period_key=target_period,
                        fallback_holder_id=fallback_holder_id, actor_id=actor_id)
                    connection.execute(
                        "UPDATE claim_items SET status='recovered', recovery_entry_id=? WHERE claim_id=?",
                        (entry_id, claim_id),
                    )
                else:
                    connection.execute("UPDATE claim_items SET status='waived' WHERE claim_id=?",
                                       (claim_id,))
                self._audit(connection, actor_id=actor_id, action=f"claim.{resolution}",
                            resource_type="claim", resource_id=claim_id,
                            detail={"recovered_cents": recovered_cents, "period_key": period_key})
                result: dict[str, Any] = {"claim_id": claim_id, "status": resolution}
                if entry_id:
                    result["recovery_entry_id"] = entry_id
                return "claim", claim_id, result

            return self._idempotent(connection, request_id=request_id, action="resolve_claim",
                                    payload=payload, create=create)

    def _write_recovery_entry(self, connection, *, claim, amount: int, period_key: str,
                              fallback_holder_id: str | None, actor_id: str) -> str:
        work_id = claim["work_id"]
        share_rows = []
        ent_version_id = None
        if work_id:
            fact = connection.execute("SELECT * FROM usage_facts WHERE fact_id=?",
                                      (claim["fact_id"],)).fetchone()
            ent = self._active_entitlement(connection, work_id, fact["occurred_at"])
            if ent is not None:
                ent_version_id = ent["ent_version_id"]
                share_rows = connection.execute(
                    "SELECT * FROM entitlement_shares WHERE ent_version_id=? ORDER BY holder_id",
                    (ent_version_id,)).fetchall()
        if not share_rows:
            if not fallback_holder_id:
                raise ValidationError("无法确定份额版本，需要提供 fallback_holder_id")
            if connection.execute("SELECT 1 FROM right_holders WHERE holder_id=?",
                                  (fallback_holder_id,)).fetchone() is None:
                raise NotFoundError("兜底权利人不存在")
        work_version_id = None
        if work_id:
            wv = connection.execute(
                "SELECT version_id FROM work_versions WHERE work_id=? ORDER BY version_no DESC LIMIT 1",
                (work_id,)).fetchone()
            work_version_id = wv["version_id"] if wv else None
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO billing_entries(entry_id,entry_type,period_key,origin_period_key,license_id,"
            "licensee_id,work_id,work_version_id,rate_version_id,ent_version_id,fact_id,claim_id,"
            "gross_cents,deduction_cents,base_cents,rate_basis_points,amount_cents,"
            "cumulative_base_before,cumulative_base_after,settled_via_cancel,trace_json,status,"
            "created_by,created_at) "
            "VALUES(?, 'recovery', ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, 0,0,0,0,?, 0,0,0,?,"
            "'confirmed',?,?)",
            (entry_id, period_key, period_key, claim["licensee_id"], work_id, work_version_id,
             ent_version_id, claim["fact_id"], claim["claim_id"], amount,
             canonical_json({"reason": "claim_recovered", "claim_id": claim["claim_id"],
                             "recovery_cents": amount}), actor_id, self._now()),
        )
        if share_rows:
            for row, part in zip(share_rows, prorate(amount, [r["basis_points"] for r in share_rows])):
                connection.execute(
                    "INSERT INTO entry_allocations(entry_id,holder_id,amount_cents) VALUES(?,?,?)",
                    (entry_id, row["holder_id"], part),
                )
        else:
            connection.execute(
                "INSERT INTO entry_allocations(entry_id,holder_id,amount_cents) VALUES(?,?,?)",
                (entry_id, fallback_holder_id, amount),
            )
        self._audit(connection, actor_id=actor_id, action="billing_entry.recovery",
                    resource_type="billing_entry", resource_id=entry_id,
                    detail={"claim_id": claim["claim_id"], "amount_cents": amount})
        return entry_id

    # ------------------------------------------------------------------ 查询

    def _entry_view(self, connection, row) -> BillingEntryView:
        allocs = tuple((r["holder_id"], r["amount_cents"]) for r in connection.execute(
            "SELECT holder_id,amount_cents FROM entry_allocations WHERE entry_id=? ORDER BY holder_id",
            (row["entry_id"],)))
        return BillingEntryView(
            row["entry_id"], row["entry_type"], row["period_key"], row["origin_period_key"],
            row["license_id"], row["licensee_id"], row["work_id"], row["work_version_id"],
            row["rate_version_id"], row["ent_version_id"], row["fact_id"], row["source_entry_id"],
            row["claim_id"], row["guarantee_id"], row["gross_cents"], row["deduction_cents"],
            row["base_cents"], row["rate_basis_points"], row["amount_cents"], row["status"],
            row["created_by"], row["created_at"], allocs)

    def list_entries(self, actor_id: str, period_key: str | None = None,
                     licensee_id: str | None = None) -> list[BillingEntryView]:
        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        query = "SELECT * FROM billing_entries WHERE 1=1"
        parameters: list[Any] = []
        if principal["kind"] in ("operator", "auditor"):
            pass
        elif principal["kind"] == "partner":
            query += " AND licensee_id=?"
            parameters.append(principal["licensee_id"])
            if licensee_id and licensee_id != principal["licensee_id"]:
                raise PermissionDenied("只能查询本主体数据")
        elif principal["kind"] == "holder":
            query += (" AND entry_id IN (SELECT entry_id FROM entry_allocations WHERE holder_id=?)")
            parameters.append(principal["holder_id"])
        else:  # pragma: no cover - 由登记约束保证
            raise PermissionDenied("未知身份类型")
        if licensee_id and principal["kind"] in ("operator", "auditor"):
            query += " AND licensee_id=?"
            parameters.append(licensee_id)
        if period_key:
            query += " AND period_key=?"
            parameters.append(period_key)
        query += " ORDER BY period_key, created_at, entry_id"
        views = [self._entry_view(connection, row)
                 for row in connection.execute(query, parameters)]
        if principal["kind"] == "holder":
            holder_id = principal["holder_id"]
            from dataclasses import replace
            views = [replace(view, allocations=tuple(a for a in view.allocations
                                                     if a[0] == holder_id))
                     for view in views]
        return views

    def explain_entry(self, actor_id: str, entry_id: str) -> dict[str, Any]:
        """返回单笔分录金额来源：扣减、集团累计基数、落档分段与份额分配。"""

        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        row = connection.execute("SELECT * FROM billing_entries WHERE entry_id=?",
                                 (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError("计费分录不存在")
        view = self._entry_view(connection, row)
        if principal["kind"] == "partner" and view.licensee_id != principal["licensee_id"]:
            raise PermissionDenied("只能查看本主体分录")
        if principal["kind"] == "holder" and principal["holder_id"] not in dict(view.allocations):
            raise PermissionDenied("只能查看与本权利人相关的分录")
        data = view.__dict__
        if principal["kind"] == "holder":
            data["allocations"] = tuple(a for a in view.allocations
                                        if a[0] == principal["holder_id"])
        return {"entry": data, "trace": json.loads(row["trace_json"] or "{}")}

    def list_facts(self, actor_id: str, period_key: str | None = None) -> list[UsageFactView]:
        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        query = "SELECT f.* FROM usage_facts f WHERE 1=1"
        parameters: list[Any] = []
        if principal["kind"] == "partner":
            query += " AND f.licensee_id=?"
            parameters.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            query += (" AND f.work_id IN (SELECT ev.work_id FROM entitlement_versions ev "
                      "JOIN entitlement_shares es ON es.ent_version_id=ev.ent_version_id "
                      "WHERE es.holder_id=?)")
            parameters.append(principal["holder_id"])
        if period_key:
            query += " AND f.period_key=?"
            parameters.append(period_key)
        query += " ORDER BY f.created_at, f.fact_id"
        items = []
        for row in connection.execute(query, parameters):
            items.append(UsageFactView(
                row["fact_id"], row["batch_id"], row["licensee_id"], row["period_key"],
                row["source_record_key"], row["work_id"], row["work_version_id"], row["license_id"],
                row["rate_version_id"], row["territory"], row["channel"], row["usage_purpose"],
                row["quantity"], row["gross_revenue_cents"], row["occurred_at"],
                row["status"], row["match_note"]))
        return items

    def list_claims(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        if principal["kind"] == "holder":
            raise PermissionDenied("权利人不查看追偿明细")
        query = "SELECT * FROM claim_items WHERE 1=1"
        parameters: list[Any] = []
        if principal["kind"] == "partner":
            query += " AND licensee_id=?"
            parameters.append(principal["licensee_id"])
        if status:
            query += " AND status=?"
            parameters.append(status)
        return [dict(row) for row in connection.execute(query + " ORDER BY created_at, claim_id",
                                                        parameters)]

    def list_disputes(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        query = ("SELECT d.* FROM disputes d JOIN billing_entries b ON b.entry_id=d.entry_id WHERE 1=1")
        parameters: list[Any] = []
        if principal["kind"] == "partner":
            query += " AND b.licensee_id=?"
            parameters.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            query += " AND d.entry_id IN (SELECT entry_id FROM entry_allocations WHERE holder_id=?)"
            parameters.append(principal["holder_id"])
        if status:
            query += " AND d.status=?"
            parameters.append(status)
        return [dict(row) for row in connection.execute(query + " ORDER BY created_at", parameters)]

    def list_orders(self, actor_id: str, period_key: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        query = "SELECT * FROM payment_orders WHERE 1=1"
        parameters: list[Any] = []
        if principal["kind"] == "partner":
            query += " AND counterparty_id=? AND direction='receivable'"
            parameters.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            query += " AND counterparty_id=? AND direction='payable'"
            parameters.append(principal["holder_id"])
        if period_key:
            query += " AND period_key=?"
            parameters.append(period_key)
        rows = connection.execute(query + " ORDER BY sequence", parameters).fetchall()
        return [self._order_dict(connection, row) for row in rows]

    def totals(self, actor_id: str, period_key: str | None = None) -> dict[str, Any]:
        """两侧恒等：净应收=已收+托管+应收余额；净应付=已付+托管+应付余额。

        净应收=分录金额合计-争议冲销核销；净应付=分账合计-争议冲销核销。
        """

        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        ep = ["1=1"]
        eparams: list[Any] = []
        ap = ["1=1"]
        aparams: list[Any] = []
        if principal["kind"] == "partner":
            ep.append("licensee_id=?")
            eparams.append(principal["licensee_id"])
            ap.append("e.licensee_id=?")
            aparams.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            ep.append("entry_id IN (SELECT entry_id FROM entry_allocations WHERE holder_id=?)")
            eparams.append(principal["holder_id"])
            ap.append("a.holder_id=?")
            aparams.append(principal["holder_id"])
        if period_key:
            ep.append("period_key=?")
            eparams.append(period_key)
            ap.append("e.period_key=?")
            aparams.append(period_key)
        receivable_gross = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM billing_entries WHERE "
            + " AND ".join(ep), eparams).fetchone()["total"]
        payable_gross = connection.execute(
            "SELECT COALESCE(SUM(a.amount_cents),0) AS total FROM entry_allocations a "
            "JOIN billing_entries e ON e.entry_id=a.entry_id WHERE "
            + " AND ".join(ap), aparams).fetchone()["total"]

        cp = ["s.reason='dispute_reversed'"]
        cparams: list[Any] = []
        if principal["kind"] == "partner":
            cp.append("e.licensee_id=?")
            cparams.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            cp.append("s.holder_id=?")
            cparams.append(principal["holder_id"])
        if period_key:
            cp.append("e.period_key=?")
            cparams.append(period_key)
        cancellation_base = (
            "FROM entry_settlements s JOIN billing_entries e ON e.entry_id=s.entry_id WHERE "
            + " AND ".join(cp))
        receivable_cancelled = connection.execute(
            "SELECT COALESCE(SUM(s.amount_cents),0) AS total " + cancellation_base
            + " AND s.direction='receivable'", cparams).fetchone()["total"]
        payable_cancelled = connection.execute(
            "SELECT COALESCE(SUM(s.amount_cents),0) AS total " + cancellation_base
            + " AND s.direction='payable'", cparams).fetchone()["total"]
        receivable = receivable_gross - receivable_cancelled
        payable = payable_gross - payable_cancelled

        def order_sum(direction: str) -> int:
            where = ["direction=?"]
            params: list[Any] = [direction]
            if principal["kind"] == "partner":
                if direction != "receivable":
                    return 0
                where.append("counterparty_id=?")
                params.append(principal["licensee_id"])
            elif principal["kind"] == "holder":
                if direction != "payable":
                    return 0
                where.append("counterparty_id=?")
                params.append(principal["holder_id"])
            if period_key:
                where.append("period_key=?")
                params.append(period_key)
            return connection.execute(
                "SELECT COALESCE(SUM(paid_cents),0) AS total FROM payment_orders WHERE "
                + " AND ".join(where), params).fetchone()["total"]

        received = order_sum("receivable")
        paid = order_sum("payable")
        kind = principal["kind"]
        if kind == "holder":
            # 权利人视角只关心分账侧：其应收等于分账份额，回款等于已付分账款
            receivable = payable
            received = paid
        elif kind == "partner":
            # 合作方视角只关心应收侧，不展示运营方对权利人的应付
            payable = 0
            paid = 0

        dw = ["d.status='open'"]
        dparams: list[Any] = []
        if principal["kind"] == "partner":
            dw.append("e.licensee_id=?")
            dparams.append(principal["licensee_id"])
        elif principal["kind"] == "holder":
            dw.append("dp.holder_id=?")
            dparams.append(principal["holder_id"])
        if period_key:
            dw.append("e.period_key=?")
            dparams.append(period_key)
        escrow_join = ("FROM disputes d JOIN billing_entries e ON e.entry_id=d.entry_id "
                       + ("JOIN dispute_parts dp ON dp.dispute_id=d.dispute_id "
                          if principal["kind"] == "holder" else ""))
        escrow = connection.execute(
            "SELECT COALESCE(SUM("
            + ("dp.amount_cents" if principal["kind"] == "holder" else "d.amount_cents")
            + "),0) AS total " + escrow_join + "WHERE " + " AND ".join(dw),
            dparams).fetchone()["total"]
        receivable_balance = receivable - received - (escrow if kind != "holder" else 0)
        payable_balance = payable - paid - (escrow if kind != "partner" else 0)
        balanced = True
        if kind != "holder":
            balanced = balanced and receivable == received + escrow + receivable_balance
        if kind != "partner":
            balanced = balanced and payable == paid + escrow + payable_balance
        return {
            "receivable_cents": receivable,
            "received_cents": received,
            "payable_cents": payable,
            "paid_cents": paid,
            "escrow_cents": escrow,
            "receivable_balance_cents": receivable_balance,
            "payable_balance_cents": payable_balance,
            "balanced": balanced,
        }

    # ------------------------------------------------------------------ 审计

    def recalculate_period(self, actor_id: str, period_key: str) -> dict[str, Any]:
        """按事实流与版本规则重算期间内每笔应计/追补/冲销分录，逐笔比对并解释。"""

        connection = self.database.connection
        principal = self._principal(connection, actor_id)
        self._require(principal, "operator", "auditor")
        period_key = self._period(period_key)
        facts = connection.execute(
            "SELECT * FROM usage_facts WHERE period_key=? AND status='matched' "
            "ORDER BY rowid", (period_key,)).fetchall()
        cumulative: dict[tuple[str, str], int] = {}
        checks: list[dict[str, Any]] = []
        consistent = True
        for fact in facts:
            if fact["gross_revenue_cents"] == 0:
                continue  # 零销售额合规报送不生成分录
            license_row = connection.execute(
                "SELECT l.*, lv.control_group_id FROM licenses l JOIN licensees lv "
                "ON lv.licensee_id=l.licensee_id WHERE l.license_id=?",
                (fact["license_id"],)).fetchone()
            tiers = self._load_tiers(connection, fact["rate_version_id"])
            rules = [dict(row) for row in connection.execute(
                "SELECT * FROM deduction_rules WHERE license_id=? AND active=1 ORDER BY sequence_no",
                (fact["license_id"],)).fetchall()]
            origin = connection.execute(
                "SELECT * FROM billing_entries WHERE fact_id=? AND entry_type IN "
                "('supplement','reversal')", (fact["fact_id"],)).fetchone()
            new_base, _ = apply_deductions(fact["gross_revenue_cents"], rules, fact["quantity"])
            if origin is not None:
                old_entry = connection.execute(
                    "SELECT * FROM billing_entries WHERE entry_id=?",
                    (origin["source_entry_id"],)).fetchone()
                old_base = old_entry["base_cents"]
                base = new_base - old_base
                anchor_before = old_entry["cumulative_base_after"] - old_base
                before = anchor_before
                fee = (self._integrate(tiers, anchor_before + new_base)
                       - self._integrate(tiers, anchor_before + old_base))
                after = old_entry["cumulative_base_after"] + base
            else:
                base = new_base
                key = (license_row["control_group_id"], fact["work_id"])
                before = cumulative.get(key, 0)
                fee, _, _ = tiered_fee(tiers, before, base)
                after = before + base
                cumulative[key] = after
            entry = origin if origin is not None else connection.execute(
                "SELECT * FROM billing_entries WHERE fact_id=? AND entry_type='accrual'",
                (fact["fact_id"],)).fetchone()
            alloc_ok = False
            if entry is not None:
                ent = self._active_entitlement(connection, fact["work_id"], fact["occurred_at"])
                stored_alloc = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS total FROM entry_allocations WHERE entry_id=?",
                    (entry["entry_id"],)).fetchone()["total"]
                alloc_ok = stored_alloc == fee and ent["ent_version_id"] == entry["ent_version_id"]
            ok = entry is not None and entry["amount_cents"] == fee and entry["base_cents"] == base \
                and entry["cumulative_base_after"] == after and alloc_ok
            consistent = consistent and ok
            checks.append({
                "fact_id": fact["fact_id"], "source_record_key": fact["source_record_key"],
                "control_group_id": license_row["control_group_id"],
                "cumulative_before": before, "cumulative_after": after,
                "recomputed_fee_cents": fee,
                "stored_fee_cents": entry["amount_cents"] if entry else None,
                "matches": ok,
            })
        stored_total = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM billing_entries WHERE period_key=?",
            (period_key,)).fetchone()["total"]
        recomputed_usage = sum(item["recomputed_fee_cents"] for item in checks)
        other = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM billing_entries WHERE period_key=? "
            "AND entry_type IN ('guarantee_shortfall','recovery')", (period_key,)).fetchone()["total"]
        totals_match = stored_total == recomputed_usage + other
        return {
            "period_key": period_key,
            "consistent": consistent and totals_match,
            "entries_checked": len(checks),
            "checks": checks,
            "stored_total_cents": stored_total,
            "recomputed_usage_total_cents": recomputed_usage,
            "guarantee_and_recovery_total_cents": other,
        }

    def verify_audit(self) -> tuple[bool, int]:
        from creative_program_foundation.audit import verify_chain
        return verify_chain(self.database.connection)
