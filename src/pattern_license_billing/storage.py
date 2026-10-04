"""纹样授权计费服务的 SQLite 表结构。

复用 creative_program_foundation 的连接、事务、request_receipts 与
audit_events 表，只追加本领域自己的表。计费分录表通过触发器保证只追加、
不可覆盖：唯一允许的变更是关账时 confirmed -> frozen。
"""

from __future__ import annotations

from creative_program_foundation.storage import Database

LICENSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lh_principals (
    principal_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('operator','auditor','partner','holder')),
    display_name TEXT NOT NULL,
    licensee_id TEXT,
    holder_id TEXT,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS right_holders (
    holder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entitlement_versions (
    ent_version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS entitlement_shares (
    ent_version_id TEXT NOT NULL REFERENCES entitlement_versions(ent_version_id),
    holder_id TEXT NOT NULL REFERENCES right_holders(holder_id),
    basis_points INTEGER NOT NULL CHECK(basis_points > 0 AND basis_points <= 10000),
    PRIMARY KEY(ent_version_id, holder_id)
);
CREATE TABLE IF NOT EXISTS control_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS licensees (
    licensee_id TEXT PRIMARY KEY,
    control_group_id TEXT NOT NULL REFERENCES control_groups(group_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    work_version_id TEXT NOT NULL REFERENCES work_versions(version_id),
    territory TEXT NOT NULL,
    channel TEXT NOT NULL,
    usage_purpose TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','terminated')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_versions (
    rate_version_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(license_id, version_no)
);
CREATE TABLE IF NOT EXISTS rate_tiers (
    rate_version_id TEXT NOT NULL REFERENCES rate_versions(rate_version_id),
    tier_no INTEGER NOT NULL CHECK(tier_no >= 1),
    lower_bound_cents INTEGER NOT NULL CHECK(lower_bound_cents >= 0),
    rate_basis_points INTEGER NOT NULL CHECK(rate_basis_points >= 0 AND rate_basis_points <= 10000),
    PRIMARY KEY(rate_version_id, tier_no)
);
CREATE TABLE IF NOT EXISTS deduction_rules (
    deduction_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('percent','fixed_per_fact')),
    basis_points INTEGER NOT NULL DEFAULT 0 CHECK(basis_points >= 0 AND basis_points <= 10000),
    amount_cents INTEGER NOT NULL DEFAULT 0 CHECK(amount_cents >= 0),
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS periods (
    period_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS guarantees (
    guarantee_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    period_key TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    status TEXT NOT NULL CHECK(status IN ('applicable','applied')),
    shortfall_entry_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(license_id, period_key)
);
CREATE TABLE IF NOT EXISTS reporting_cycles (
    cycle_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    period_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('registered','received')),
    batch_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(license_id, period_key)
);
CREATE TABLE IF NOT EXISTS usage_batches (
    batch_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    period_key TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    claim_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('processed')),
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_facts (
    fact_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES usage_batches(batch_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    period_key TEXT NOT NULL,
    source_record_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    work_id TEXT,
    work_version_id TEXT,
    license_id TEXT,
    rate_version_id TEXT,
    territory TEXT NOT NULL,
    channel TEXT NOT NULL,
    usage_purpose TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    gross_revenue_cents INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('matched','expired','out_of_scope','unmatched')),
    match_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_entries (
    entry_id TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('accrual','supplement','reversal',
                                                  'guarantee_shortfall','recovery')),
    period_key TEXT NOT NULL,
    origin_period_key TEXT NOT NULL,
    license_id TEXT,
    licensee_id TEXT,
    work_id TEXT,
    work_version_id TEXT,
    rate_version_id TEXT,
    ent_version_id TEXT,
    fact_id TEXT REFERENCES usage_facts(fact_id),
    source_entry_id TEXT REFERENCES billing_entries(entry_id),
    claim_id TEXT,
    guarantee_id TEXT,
    gross_cents INTEGER NOT NULL DEFAULT 0,
    deduction_cents INTEGER NOT NULL DEFAULT 0,
    base_cents INTEGER NOT NULL DEFAULT 0,
    rate_basis_points INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL,
    cumulative_base_before INTEGER NOT NULL DEFAULT 0,
    cumulative_base_after INTEGER NOT NULL DEFAULT 0,
    settled_via_cancel INTEGER NOT NULL DEFAULT 0 CHECK(settled_via_cancel IN (0, 1)),
    trace_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('confirmed','frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_fact_accrual
    ON billing_entries(fact_id) WHERE entry_type IN ('accrual','supplement','reversal')
    AND fact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entries_period ON billing_entries(period_key);
CREATE INDEX IF NOT EXISTS idx_entries_licensee ON billing_entries(licensee_id);
CREATE INDEX IF NOT EXISTS idx_entries_group_work ON billing_entries(work_id, license_id);
CREATE TABLE IF NOT EXISTS entry_allocations (
    entry_id TEXT NOT NULL REFERENCES billing_entries(entry_id),
    holder_id TEXT,
    amount_cents INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_allocations_unique
    ON entry_allocations(entry_id, COALESCE(holder_id, ''));
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES billing_entries(entry_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','released','reversed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS dispute_parts (
    dispute_id TEXT NOT NULL REFERENCES disputes(dispute_id),
    holder_id TEXT NOT NULL REFERENCES right_holders(holder_id),
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY(dispute_id, holder_id)
);
CREATE TABLE IF NOT EXISTS claim_items (
    claim_id TEXT PRIMARY KEY,
    fact_id TEXT REFERENCES usage_facts(fact_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    work_id TEXT,
    period_key TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('expired','out_of_scope','unmatched')),
    gross_cents INTEGER NOT NULL CHECK(gross_cents >= 0),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('open','recovered','waived')),
    recovery_entry_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_orders (
    order_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    direction TEXT NOT NULL CHECK(direction IN ('receivable','payable')),
    counterparty_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    paid_cents INTEGER NOT NULL DEFAULT 0 CHECK(paid_cents >= 0),
    cancelled_cents INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_cents >= 0),
    status TEXT NOT NULL CHECK(status IN ('queued','paid')),
    created_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_queue ON payment_orders(status, sequence);
CREATE TABLE IF NOT EXISTS payment_parts (
    order_id TEXT NOT NULL REFERENCES payment_orders(order_id),
    entry_id TEXT NOT NULL REFERENCES billing_entries(entry_id),
    holder_id TEXT,
    amount_cents INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_parts_unique
    ON payment_parts(order_id, entry_id, COALESCE(holder_id, ''));
CREATE TABLE IF NOT EXISTS entry_settlements (
    settlement_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES billing_entries(entry_id),
    holder_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('receivable','payable')),
    reason TEXT NOT NULL CHECK(reason IN ('dispute_reversed')),
    ref_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_entry ON entry_settlements(entry_id);

-- 计费分录不可覆盖：除关账冻结外，任何更新都被数据库拒绝。
CREATE TRIGGER IF NOT EXISTS billing_entries_block_update
BEFORE UPDATE ON billing_entries
BEGIN
    SELECT CASE
        WHEN NEW.status = 'frozen' AND OLD.status = 'confirmed'
             AND NEW.entry_id = OLD.entry_id
             AND NEW.entry_type = OLD.entry_type
             AND NEW.period_key = OLD.period_key
             AND NEW.origin_period_key = OLD.origin_period_key
             AND IFNULL(NEW.license_id,'') = IFNULL(OLD.license_id,'')
             AND IFNULL(NEW.licensee_id,'') = IFNULL(OLD.licensee_id,'')
             AND IFNULL(NEW.work_id,'') = IFNULL(OLD.work_id,'')
             AND IFNULL(NEW.fact_id,'') = IFNULL(OLD.fact_id,'')
             AND IFNULL(NEW.source_entry_id,'') = IFNULL(OLD.source_entry_id,'')
             AND IFNULL(NEW.claim_id,'') = IFNULL(OLD.claim_id,'')
             AND IFNULL(NEW.guarantee_id,'') = IFNULL(OLD.guarantee_id,'')
             AND NEW.gross_cents = OLD.gross_cents
             AND NEW.deduction_cents = OLD.deduction_cents
             AND NEW.base_cents = OLD.base_cents
             AND NEW.rate_basis_points = OLD.rate_basis_points
             AND NEW.amount_cents = OLD.amount_cents
             AND NEW.cumulative_base_before = OLD.cumulative_base_before
             AND NEW.cumulative_base_after = OLD.cumulative_base_after
             AND NEW.settled_via_cancel = OLD.settled_via_cancel
             AND NEW.trace_json = OLD.trace_json
             AND NEW.created_by = OLD.created_by
             AND NEW.created_at = OLD.created_at
        THEN 1
        ELSE RAISE(ABORT, '计费分录不可覆盖，更正只能追加追补或冲销分录')
    END;
END;
CREATE TRIGGER IF NOT EXISTS billing_entries_block_delete
BEFORE DELETE ON billing_entries
BEGIN
    SELECT RAISE(ABORT, '计费分录只能追加，不能删除');
END;
CREATE TRIGGER IF NOT EXISTS entry_allocations_block_update
BEFORE UPDATE ON entry_allocations
BEGIN
    SELECT RAISE(ABORT, '分账明细不可更新');
END;
CREATE TRIGGER IF NOT EXISTS entry_allocations_block_delete
BEFORE DELETE ON entry_allocations
BEGIN
    SELECT RAISE(ABORT, '分账明细不能删除');
END;
"""


class LicensingDatabase(Database):
    """在基础库之上建出授权计费领域表。"""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(LICENSE_SCHEMA)
