"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    party_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_cards (
    rate_card_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    tiers_json TEXT NOT NULL,
    deduction_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    licensor_party_id TEXT NOT NULL,
    rate_card_id TEXT NOT NULL REFERENCES rate_cards(rate_card_id),
    shares_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);

CREATE TABLE IF NOT EXISTS licensees (
    licensee_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    controlling_party_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES work_versions(version_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    regions_json TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    uses_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    guarantee_cents INTEGER NOT NULL CHECK(guarantee_cents >= 0),
    reporting_cycle TEXT NOT NULL DEFAULT 'monthly' CHECK(reporting_cycle IN ('monthly','quarterly')),
    status TEXT NOT NULL CHECK(status IN ('active','expired','revoked')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_imports (
    import_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    period_key TEXT,
    row_count INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    imported_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_records (
    usage_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES usage_imports(import_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    dedup_key TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    region TEXT NOT NULL,
    channel TEXT NOT NULL,
    use_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    occurred_on TEXT NOT NULL,
    period_key TEXT NOT NULL,
    original_period TEXT,
    corrects_usage_id TEXT,
    matched_license_id TEXT,
    in_scope INTEGER NOT NULL CHECK(in_scope IN (0,1)),
    scope_reason TEXT,
    bill_status TEXT NOT NULL CHECK(bill_status IN ('pending','billed','claim','superseded')),
    created_at TEXT NOT NULL,
    UNIQUE(licensee_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS periods (
    period_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS billing_entries (
    entry_id TEXT PRIMARY KEY,
    period_key TEXT NOT NULL,
    closes_period TEXT,
    usage_id TEXT REFERENCES usage_records(usage_id),
    license_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    licensee_id TEXT NOT NULL,
    controlling_party_id TEXT NOT NULL,
    region TEXT NOT NULL,
    channel TEXT NOT NULL,
    use_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    cursor_before INTEGER NOT NULL,
    unit_rate_micro_cents INTEGER NOT NULL,
    gross_cents INTEGER NOT NULL,
    deduction_cents INTEGER NOT NULL,
    net_cents INTEGER NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('regular','guarantee','supplement','reversal')),
    rate_snapshot_json TEXT NOT NULL,
    reversed_entry_id TEXT,
    confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    usage_id TEXT REFERENCES usage_records(usage_id),
    licensee_id TEXT NOT NULL,
    controlling_party_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    region TEXT NOT NULL,
    channel TEXT NOT NULL,
    use_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    occurred_on TEXT NOT NULL,
    reason TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    status TEXT NOT NULL CHECK(status IN ('open','assessed','recovered','written_off')),
    created_at TEXT NOT NULL,
    assessed_at TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    period_key TEXT NOT NULL,
    license_id TEXT NOT NULL,
    licensee_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    reason TEXT NOT NULL,
    funded_cents INTEGER NOT NULL DEFAULT 0 CHECK(funded_cents >= 0),
    status TEXT NOT NULL CHECK(status IN ('open','released','rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS settlement_payables (
    period_key TEXT NOT NULL,
    license_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    disputed_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(period_key, license_id, party_id)
);

CREATE TABLE IF NOT EXISTS distributions (
    distribution_id TEXT PRIMARY KEY,
    ref_type TEXT NOT NULL CHECK(ref_type IN ('entry','claim')),
    ref_id TEXT NOT NULL,
    period_key TEXT,
    license_id TEXT,
    party_id TEXT NOT NULL,
    basis_points INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_distributions_ref ON distributions(ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_distributions_period ON distributions(period_key);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    period_key TEXT,
    license_id TEXT,
    payee_party_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    kind TEXT NOT NULL CHECK(kind IN ('distribution','escrow_release','licensee_refund','claim_recovery')),
    status TEXT NOT NULL CHECK(status IN ('queued','processing','paid','failed','cancelled')),
    ref_id TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS cash_receipts (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    period_key TEXT,
    license_id TEXT,
    claim_id TEXT,
    licensee_id TEXT,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    received_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_transactions (
    txn_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    period_key TEXT,
    memo TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_entries (
    txn_id TEXT NOT NULL REFERENCES accounting_transactions(txn_id),
    account TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('dr','cr')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    PRIMARY KEY(txn_id, account, direction)
);

CREATE TABLE IF NOT EXISTS process_queues (
    queue_type TEXT NOT NULL CHECK(queue_type IN ('close','dispute','payment')),
    seq INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','processing','done','failed')),
    enqueued_at TEXT NOT NULL,
    processed_at TEXT,
    PRIMARY KEY(queue_type, seq)
);

CREATE TABLE IF NOT EXISTS dispute_decisions (
    dispute_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL CHECK(outcome IN ('release','reject'))
);

CREATE INDEX IF NOT EXISTS idx_entries_period ON billing_entries(period_key);
CREATE INDEX IF NOT EXISTS idx_entries_license_period ON billing_entries(license_id, period_key);
CREATE INDEX IF NOT EXISTS idx_usage_status ON usage_records(period_key, bill_status);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_queue_status ON process_queues(queue_type, status);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self.resume_queues()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """异常回滚、成功提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def resume_queues(self) -> None:
        """崩溃恢复：重启时把处理中的队列项与付款退回待处理，严格保持原顺序。"""

        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE process_queues SET status='queued', processed_at=NULL WHERE status='processing'"
            )
            connection.execute(
                "UPDATE payments SET status='queued', paid_at=NULL WHERE status='processing'"
            )

    def close(self) -> None:
        self.connection.close()
