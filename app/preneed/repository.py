from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS preneed_contracts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_no TEXT NOT NULL UNIQUE,
 plan_code TEXT NOT NULL, plan_name TEXT NOT NULL,
 customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL,
 customer_identity TEXT NOT NULL DEFAULT '', customer_address TEXT NOT NULL DEFAULT '',
 beneficiary_name TEXT NOT NULL, beneficiary_identity TEXT NOT NULL DEFAULT '',
 beneficiary_phone TEXT NOT NULL DEFAULT '', relationship TEXT NOT NULL DEFAULT '',
 total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
 signed_on TEXT NOT NULL,
 refund_rule_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'active'
   CHECK(status IN ('active','overdue','suspended','transferred','terminated','converted')),
 current_version INTEGER NOT NULL DEFAULT 1,
 converted_case_id INTEGER,
 next_action TEXT NOT NULL DEFAULT '等待分期收缴',
 next_action_due TEXT NOT NULL DEFAULT '',
 last_event_id INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 version_no INTEGER NOT NULL, change_type TEXT NOT NULL
   CHECK(change_type IN ('sign','amend','transfer','terminate','suspend','resume')),
 items_json TEXT NOT NULL, price_basis_json TEXT NOT NULL DEFAULT '{}',
 installment_plan_json TEXT NOT NULL DEFAULT '',
 total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
 change_reason TEXT NOT NULL DEFAULT '',
 proposed_by TEXT NOT NULL DEFAULT '', confirmed_by_customer_name TEXT NOT NULL DEFAULT '',
 confirmed_by TEXT NOT NULL DEFAULT '', confirmed_at TEXT,
 created_at TEXT NOT NULL,
 UNIQUE(contract_id,version_no)
);
CREATE TABLE IF NOT EXISTS preneed_installments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 period_no INTEGER NOT NULL, due_date TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
 version_no INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'scheduled'
   CHECK(status IN ('scheduled','paid','waived','refunded','cancelled')),
 paid_at TEXT, paid_amount_cents INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(contract_id,period_no)
);
CREATE TABLE IF NOT EXISTS preneed_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 period_id INTEGER REFERENCES preneed_installments(id),
 amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
 channel TEXT NOT NULL, external_reference TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN ('posted','reversed')),
 received_by TEXT NOT NULL, received_at TEXT NOT NULL, reversed_by TEXT NOT NULL DEFAULT '',
 reversed_reason TEXT NOT NULL DEFAULT '', reversed_at TEXT,
 account_event_id INTEGER,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_accounting_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 event_type TEXT NOT NULL, debit_account TEXT NOT NULL, credit_account TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
 receipt_id INTEGER REFERENCES preneed_receipts(id),
 version_no INTEGER, operator_id TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preneed_accounting_contract ON preneed_accounting_entries(contract_id,id);
CREATE TABLE IF NOT EXISTS preneed_refunds (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 payable_cents INTEGER NOT NULL CHECK(payable_cents >= 0),
 penalty_cents INTEGER NOT NULL DEFAULT 0 CHECK(penalty_cents >= 0),
 paid_cents INTEGER NOT NULL DEFAULT 0 CHECK(paid_cents >= 0),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','cancelled')),
 approved_by TEXT NOT NULL, applied_by TEXT NOT NULL,
 approved_at TEXT NOT NULL, applied_at TEXT NOT NULL,
 paid_by TEXT NOT NULL DEFAULT '', payment_reference TEXT NOT NULL DEFAULT '',
 version_no INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 new_contract_id INTEGER REFERENCES preneed_contracts(id),
 old_beneficiary_name TEXT NOT NULL, old_beneficiary_identity TEXT,
 new_beneficiary_name TEXT NOT NULL, new_beneficiary_identity TEXT NOT NULL DEFAULT '',
 new_beneficiary_phone TEXT NOT NULL DEFAULT '', relationship TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', applied_by TEXT NOT NULL,
 customer_confirmed_by TEXT NOT NULL DEFAULT '', customer_confirmed_at TEXT,
 customer_confirmation_ref TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected','cancelled')),
 created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS preneed_suspensions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 kind TEXT NOT NULL CHECK(kind IN ('suspend','resume')),
 reason TEXT NOT NULL, reason_detail TEXT NOT NULL DEFAULT '',
 operated_by TEXT NOT NULL, customer_confirmed_name TEXT NOT NULL DEFAULT '',
 customer_confirmed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_conversions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL UNIQUE REFERENCES preneed_contracts(id),
 case_id INTEGER NOT NULL UNIQUE REFERENCES mortuary_cases(id),
 death_cert_ref TEXT NOT NULL,
 unfulfilled_cents INTEGER NOT NULL CHECK(unfulfilled_cents >= 0),
 delivered_value_cents INTEGER NOT NULL CHECK(delivered_value_cents >= 0),
 handled_by TEXT NOT NULL, approved_by TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preneed_event ON preneed_events(aggregate_type,aggregate_id,id);
'''


class PreneedRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO preneed_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )
        return int(cursor.lastrowid)

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM preneed_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id",
            (kind, str(aggregate_id)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def contract(self, contract_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_contracts WHERE id=?", (contract_id,)).fetchone())

    def contract_no(self, contract_no: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_contracts WHERE contract_no=?", (contract_no,)).fetchone())

    def version(self, contract_id: int, version_no: int) -> dict[str, Any] | None:
        return self.one(
            self.connection.execute("SELECT * FROM preneed_versions WHERE contract_id=? AND version_no=?", (contract_id, version_no)).fetchone()
        )

    def latest_version(self, contract_id: int) -> dict[str, Any] | None:
        return self.one(
            self.connection.execute("SELECT * FROM preneed_versions WHERE contract_id=? ORDER BY version_no DESC LIMIT 1", (contract_id,)).fetchone()
        )

    def versions(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_versions WHERE contract_id=? ORDER BY version_no", (contract_id,)).fetchall()]

    def installment(self, installment_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_installments WHERE id=?", (installment_id,)).fetchone())

    def installments(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_installments WHERE contract_id=? ORDER BY period_no", (contract_id,)).fetchall()]

    def receipt_ref(self, external_reference: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_receipts WHERE external_reference=?", (external_reference,)).fetchone())

    def receipts(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_receipts WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def accounting_entries(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_accounting_entries WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def refunds(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_refunds WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def refund(self, refund_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_refunds WHERE id=?", (refund_id,)).fetchone())

    def transfer(self, transfer_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_transfers WHERE id=?", (transfer_id,)).fetchone())

    def transfers(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_transfers WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def suspensions(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM preneed_suspensions WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def conversion(self, contract_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_conversions WHERE contract_id=?", (contract_id,)).fetchone())

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def case_ref(self, ref: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE external_ref=?", (ref,)).fetchone())

    def order(self, order_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM funeral_service_orders WHERE id=?", (order_id,)).fetchone())
