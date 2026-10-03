from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS preneed_contracts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_no TEXT NOT NULL UNIQUE,
 plan_code TEXT NOT NULL, plan_name TEXT NOT NULL,
 customer_name TEXT NOT NULL, customer_identity TEXT NOT NULL, customer_phone TEXT NOT NULL,
 beneficiary_name TEXT NOT NULL, beneficiary_identity TEXT NOT NULL,
 beneficiary_relation TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active'
  CHECK(status IN ('active','overdue','suspended','terminated','transferred','converted','void')),
 current_version INTEGER NOT NULL DEFAULT 1,
 paid_cents INTEGER NOT NULL DEFAULT 0, refunded_cents INTEGER NOT NULL DEFAULT 0,
 installment_total_cents INTEGER NOT NULL DEFAULT 0,
 next_due_on TEXT, grace_days INTEGER NOT NULL DEFAULT 0,
 suspended_at TEXT, suspended_reason TEXT NOT NULL DEFAULT '',
 terminated_at TEXT, terminate_reason TEXT NOT NULL DEFAULT '',
 converted_at TEXT, converted_case_id INTEGER,
 converted_order_ids_json TEXT NOT NULL DEFAULT '[]',
 conversion_key TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_preneed_conversion_key ON preneed_contracts(conversion_key) WHERE conversion_key!='';
CREATE INDEX IF NOT EXISTS idx_preneed_status ON preneed_contracts(status,next_due_on);
CREATE TABLE IF NOT EXISTS preneed_contract_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 version_no INTEGER NOT NULL,
 change_reason TEXT NOT NULL, base_version_no INTEGER,
 price_list_code TEXT NOT NULL, price_list_effective_on TEXT NOT NULL,
 service_items_json TEXT NOT NULL,
 package_total_cents INTEGER NOT NULL,
 sales_discount_cents INTEGER NOT NULL DEFAULT 0, refund_rule_json TEXT NOT NULL DEFAULT '{}',
 installment_plan_json TEXT NOT NULL DEFAULT '{}', grace_days INTEGER,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','superseded','rejected')),
 superseded_at TEXT,
 proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL,
 confirmed_by TEXT NOT NULL DEFAULT '', confirmed_at TEXT,
 customer_confirmer TEXT NOT NULL DEFAULT '', customer_confirmed_at TEXT,
 rejection_reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(contract_id,version_no)
);
CREATE TABLE IF NOT EXISTS preneed_installments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 version_no INTEGER NOT NULL, seq INTEGER NOT NULL,
 due_on TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 paid_cents INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled'
  CHECK(status IN ('scheduled','paid','partial','cancelled')),
 paid_at TEXT,
 UNIQUE(contract_id,version_no,seq)
);
CREATE INDEX IF NOT EXISTS idx_installment_due ON preneed_installments(contract_id,due_on);
CREATE TABLE IF NOT EXISTS preneed_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 installment_id INTEGER REFERENCES preneed_installments(id),
 amount_cents INTEGER NOT NULL, channel TEXT NOT NULL,
 external_reference TEXT NOT NULL UNIQUE,
 received_by TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_contract ON preneed_receipts(contract_id,id);
CREATE TABLE IF NOT EXISTS preneed_refunds (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 amount_cents INTEGER NOT NULL, reason TEXT NOT NULL,
 external_reference TEXT NOT NULL UNIQUE,
 handled_by TEXT NOT NULL, handled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_accounting_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 event_type TEXT NOT NULL, occurrence TEXT NOT NULL DEFAULT '', entry_no INTEGER NOT NULL,
 account TEXT NOT NULL, debit_cents INTEGER NOT NULL DEFAULT 0, credit_cents INTEGER NOT NULL DEFAULT 0,
 ref_type TEXT NOT NULL DEFAULT '', ref_id INTEGER NOT NULL DEFAULT 0,
 memo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, actor TEXT NOT NULL,
 UNIQUE(contract_id,event_type,occurrence,entry_no)
);
CREATE INDEX IF NOT EXISTS idx_accounting_contract ON preneed_accounting_entries(contract_id,id);
CREATE TABLE IF NOT EXISTS preneed_transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 from_beneficiary_name TEXT NOT NULL, from_beneficiary_identity TEXT NOT NULL,
 to_beneficiary_name TEXT NOT NULL, to_beneficiary_identity TEXT NOT NULL,
 to_relation TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
 transferred_by TEXT NOT NULL, customer_confirmer TEXT NOT NULL,
 transferred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preneed_overdue_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER NOT NULL REFERENCES preneed_contracts(id),
 action TEXT NOT NULL CHECK(action IN ('marked_overdue','resolved')),
 due_on TEXT NOT NULL, overdue_days INTEGER NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL
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

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO preneed_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def contract(self, contract_id: int) -> dict[str, Any] | None:
        row = self.one(self.connection.execute("SELECT * FROM preneed_contracts WHERE id=?", (contract_id,)).fetchone())
        if row is not None:
            row["converted_order_ids"] = json.loads(row.pop("converted_order_ids_json"))
        return row

    def contract_no(self, contract_no: str) -> dict[str, Any] | None:
        row = self.one(self.connection.execute("SELECT * FROM preneed_contracts WHERE contract_no=?", (contract_no,)).fetchone())
        if row is not None:
            row["converted_order_ids"] = json.loads(row.pop("converted_order_ids_json"))
        return row

    def conversion_key(self, key: str) -> dict[str, Any] | None:
        row = self.one(self.connection.execute("SELECT * FROM preneed_contracts WHERE conversion_key=?", (key,)).fetchone())
        if row is not None:
            row["converted_order_ids"] = json.loads(row.pop("converted_order_ids_json"))
        return row

    def contracts_by_status(self, statuses: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM preneed_contracts"
        params: list[Any] = []
        if statuses:
            sql += " WHERE status IN (%s)" % ",".join("?" for _ in statuses)
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        for row in rows:
            row["converted_order_ids"] = json.loads(row.pop("converted_order_ids_json"))
        return rows

    def version(self, contract_id: int, version_no: int) -> dict[str, Any] | None:
        row = self.one(self.connection.execute("SELECT * FROM preneed_contract_versions WHERE contract_id=? AND version_no=?", (contract_id, version_no)).fetchone())
        return self._hydrate_version(row)

    def version_row(self, version_id: int) -> dict[str, Any] | None:
        row = self.one(self.connection.execute("SELECT * FROM preneed_contract_versions WHERE id=?", (version_id,)).fetchone())
        return self._hydrate_version(row)

    def versions(self, contract_id: int) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.connection.execute("SELECT * FROM preneed_contract_versions WHERE contract_id=? ORDER BY version_no", (contract_id,)).fetchall()]
        return [self._hydrate_version(row) for row in rows]

    @staticmethod
    def _hydrate_version(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["service_items"] = json.loads(row.pop("service_items_json"))
        row["refund_rule"] = json.loads(row.pop("refund_rule_json"))
        plan_raw = row.pop("installment_plan_json")
        row["installment_plan"] = json.loads(plan_raw) if plan_raw else None
        return row

    def installments(self, contract_id: int, version_no: int | None = None) -> list[dict[str, Any]]:
        if version_no is None:
            rows = self.connection.execute("SELECT * FROM preneed_installments WHERE contract_id=? ORDER BY version_no,seq", (contract_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM preneed_installments WHERE contract_id=? AND version_no=? ORDER BY seq", (contract_id, version_no)).fetchall()
        return [dict(row) for row in rows]

    def installment(self, installment_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_installments WHERE id=?", (installment_id,)).fetchone())

    def open_installments(self, contract_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM preneed_installments WHERE contract_id=? AND status!='cancelled' ORDER BY seq",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def receipts(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM preneed_receipts WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def receipt_ref(self, external_reference: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_receipts WHERE external_reference=?", (external_reference,)).fetchone())

    def refunds(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM preneed_refunds WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def refund_ref(self, external_reference: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM preneed_refunds WHERE external_reference=?", (external_reference,)).fetchone())

    def transfers(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM preneed_transfers WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def entries(self, contract_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM preneed_accounting_entries WHERE contract_id=? ORDER BY id", (contract_id,)).fetchall()]

    def overdue_candidates(self, today: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM preneed_contracts WHERE status='active' AND next_due_on IS NOT NULL"
            " AND date(next_due_on,'+'||grace_days||' days') < ? ORDER BY next_due_on",
            (today,),
        ).fetchall()
        return [dict(row) for row in rows]

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM preneed_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id", (kind, str(aggregate_id))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
