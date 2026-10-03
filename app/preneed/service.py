from __future__ import annotations

import calendar
import json
import sqlite3
import uuid
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository
from app.preneed.repository import PreneedRepository

# 权限矩阵：每个生命周期动作允许的岗位角色
PERMISSIONS: dict[str, set[str]] = {
    "contract.create": {"consultant", "customer_service", "finance_manager"},
    "contract.read": {"consultant", "customer_service", "cashier", "finance_manager", "auditor", "system"},
    "contract.amend_propose": {"consultant", "customer_service"},
    "contract.amend_confirm": {"customer_service", "finance_manager"},
    "contract.amend_reject": {"customer_service", "finance_manager"},
    "payment.receive": {"cashier", "finance_manager"},
    "refund.approve": {"finance_manager"},
    "contract.suspend": {"customer_service", "finance_manager"},
    "contract.resume": {"customer_service", "finance_manager"},
    "contract.terminate": {"finance_manager"},
    "contract.transfer": {"customer_service", "finance_manager"},
    "overdue.sweep": {"system", "finance_manager"},
    "contract.convert": {"customer_service", "finance_manager"},
}

OPEN_STATUSES = {"active", "overdue", "suspended"}
MEMO_ACCOUNTS = {
    "contract.overdue": "memo.overdue",
    "contract.overdue_resolved": "memo.overdue_resolved",
    "contract.suspended": "memo.suspended",
    "contract.resumed": "memo.resumed",
    "contract.transferred": "memo.beneficiary_change",
}


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def build_schedule(total_cents: int, number: int, first_due: date, interval_months: int) -> list[dict[str, Any]]:
    base, extra = divmod(total_cents, number)
    due = first_due
    rows: list[dict[str, Any]] = []
    for seq in range(1, number + 1):
        rows.append({"seq": seq, "due_on": due.isoformat(), "amount_cents": base + (extra if seq == number else 0)})
        due = add_months(due, interval_months)
    return rows


def refundable_amount(paid_cents: int, rule: dict[str, Any]) -> int:
    fee_bps = int(rule.get("admin_fee_bps", 0)) + int(rule.get("performance_penalty_bps", 0))
    fee = round(paid_cents * min(fee_bps, 10_000) / 10_000)
    return max(0, paid_cents - fee)


def as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class PreneedService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = PreneedRepository(self.connection)
        self.repository.ensure_schema()
        # 转换依赖殡葬档案表结构
        MortuaryRepository(self.connection).ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    def today(self) -> date:
        return self.clock.now().date()

    @staticmethod
    def _require(role: str, action: str) -> None:
        if role not in PERMISSIONS[action]:
            raise PermissionDeniedError(f"角色 {role} 无权执行 {action}", context={"action": action, "role": role})

    # ---- 会计 ----
    def _post(self, connection: sqlite3.Connection, contract_id: int, event_type: str, lines: list[tuple[str, int, int]], now: str, actor: str, *, occurrence: str = "", ref_type: str = "", ref_id: int = 0, memo: str = "") -> None:
        debit = sum(line[1] for line in lines)
        credit = sum(line[2] for line in lines)
        if debit != credit:
            raise ValidationError("会计分录借贷不平衡", context={"debit": debit, "credit": credit})
        if occurrence:
            duplicate = connection.execute(
                "SELECT 1 FROM preneed_accounting_entries WHERE contract_id=? AND event_type=? AND occurrence=?",
                (contract_id, event_type, occurrence),
            ).fetchone()
            if duplicate:
                raise ConflictError("同一会计事件不能重复记账", context={"event_type": event_type, "occurrence": occurrence})
        for entry_no, (account, debit_cents, credit_cents) in enumerate(lines, start=1):
            connection.execute(
                "INSERT INTO preneed_accounting_entries(contract_id,event_type,occurrence,entry_no,account,debit_cents,credit_cents,ref_type,ref_id,memo,created_at,actor)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, event_type, occurrence, entry_no, account, debit_cents, credit_cents, ref_type, ref_id, memo, now, actor),
            )

    def _memo(self, connection: sqlite3.Connection, contract_id: int, event_type: str, now: str, actor: str, memo: str, *, occurrence: str = "", ref_type: str = "", ref_id: int = 0) -> None:
        self._post(connection, contract_id, event_type, [(MEMO_ACCOUNTS[event_type], 0, 0)], now, actor, occurrence=occurrence, ref_type=ref_type, ref_id=ref_id, memo=memo)

    # ---- 签约 ----
    def create_contract(self, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.create")
        now = self.now()
        items = payload["service_items"]
        package_total = sum(int(item["quantity"]) * int(item["unit_price_cents"]) for item in items)
        discount = int(payload["sales_discount_cents"])
        net_total = package_total - discount
        rule = payload["refund_rule"]
        plan = payload["installment_plan"]
        plan_start = as_date(plan["first_due_on"]) if plan else None
        price_effective_on = as_date(payload["price_list_effective_on"])
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            if repo.contract_no(payload["contract_no"]):
                raise ConflictError("生前契约编号已存在")
            first_due = plan_start.isoformat() if plan else now[:10]
            cursor = connection.execute(
                "INSERT INTO preneed_contracts(contract_no,plan_code,plan_name,customer_name,customer_identity,customer_phone,"
                "beneficiary_name,beneficiary_identity,beneficiary_relation,status,current_version,installment_total_cents,"
                "next_due_on,grace_days,created_at,created_by,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?, 'active',1,?,?,?,?,?,?)",
                (payload["contract_no"], payload["plan_code"], payload["plan_name"], payload["customer_name"], payload["customer_identity"],
                 payload["customer_phone"], payload["beneficiary_name"], payload["beneficiary_identity"], payload["beneficiary_relation"],
                 net_total, first_due, int(payload["grace_days"]), now, payload["created_by"], now),
            )
            contract_id = int(cursor.lastrowid)
            plan_json = json.dumps({
                "number": plan["number"], "first_due_on": plan_start.isoformat(),
                "interval_months": plan["interval_months"],
            }, ensure_ascii=False, sort_keys=True) if plan else "{}"
            connection.execute(
                "INSERT INTO preneed_contract_versions(contract_id,version_no,change_reason,price_list_code,price_list_effective_on,"
                "service_items_json,package_total_cents,sales_discount_cents,refund_rule_json,installment_plan_json,grace_days,"
                "status,proposed_by,proposed_at,confirmed_by,confirmed_at,customer_confirmer,customer_confirmed_at,created_at)"
                " VALUES(?,1,'初次签约',?,?,?,?,?,?,?,?, 'confirmed',?,?,?,?,?,?,?)",
                (contract_id, payload["price_list_code"], price_effective_on.isoformat(),
                 json.dumps(items, ensure_ascii=False, sort_keys=True), package_total, discount,
                 json.dumps(rule, ensure_ascii=False, sort_keys=True), plan_json, int(payload["grace_days"]),
                 payload["created_by"], now, payload["created_by"], now, payload["customer_confirmer"], now, now),
            )
            schedule = build_schedule(net_total, plan["number"], plan_start, plan["interval_months"]) if plan else [
                {"seq": 1, "due_on": first_due, "amount_cents": net_total}
            ]
            for row in schedule:
                connection.execute(
                    "INSERT INTO preneed_installments(contract_id,version_no,seq,due_on,amount_cents,status) VALUES(?,1,?,?,?,'scheduled')",
                    (contract_id, row["seq"], row["due_on"], row["amount_cents"]),
                )
            # 签约即形成未来服务义务与对应应收
            self._post(connection, contract_id, "contract.signed", [
                ("installments_receivable", net_total, 0),
                ("contract_liability", 0, net_total),
            ], now, payload["created_by"], occurrence=str(contract_id), memo=f"{payload['plan_code']} 冻结价目 {payload['price_list_code']}")
            repo.event("contract", contract_id, "contract.created", payload["created_by"], {
                "contract_no": payload["contract_no"], "version_no": 1, "package_total_cents": net_total,
                "price_list_code": payload["price_list_code"], "customer_confirmer": payload["customer_confirmer"],
                "installments": len(schedule),
            }, now)
            return self.get_contract(contract_id, repo)

    # ---- 变更：新版本 + 客户确认 ----
    def propose_amendment(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.amend_propose")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            if contract["status"] == "suspended":
                raise ConflictError("暂停中的契约须先恢复才能提出变更")
            pending = connection.execute(
                "SELECT id FROM preneed_contract_versions WHERE contract_id=? AND status='pending'",
                (contract_id,),
            ).fetchone()
            if pending:
                raise ConflictError("已有待客户确认的变更版本", context={"pending_version_id": pending[0]})
            current = repo.version(contract_id, contract["current_version"])
            if current is None:
                raise NotFoundError("当前合同版本缺失")
            items = payload.get("service_items") or current["service_items"]
            package_total = sum(int(item["quantity"]) * int(item["unit_price_cents"]) for item in items)
            discount = int(payload["sales_discount_cents"]) if payload.get("sales_discount_cents") is not None else int(current["sales_discount_cents"])
            if discount > package_total:
                raise ValidationError("销售折让不能超过服务清单合计金额")
            net_total = package_total - discount
            rule = payload.get("refund_rule") or current["refund_rule"]
            grace_days = int(payload["grace_days"]) if payload.get("grace_days") is not None else (int(current["grace_days"]) if current["grace_days"] is not None else 0)
            new_plan = payload.get("installment_plan")
            if new_plan is not None:
                plan_store = {
                    "number": int(new_plan["number"]),
                    "first_due_on": as_date(new_plan["first_due_on"]).isoformat(),
                    "interval_months": int(new_plan["interval_months"]),
                }
            else:
                plan_store = current["installment_plan"] or {}
            price_list_code = payload.get("price_list_code") or current["price_list_code"]
            if payload.get("price_list_code"):
                price_effective = as_date(payload.get("price_list_effective_on") or self.today()).isoformat()
            else:
                price_effective = current["price_list_effective_on"]
            new_version_no = int(contract["current_version"]) + 1
            cursor = connection.execute(
                "INSERT INTO preneed_contract_versions(contract_id,version_no,change_reason,base_version_no,price_list_code,"
                "price_list_effective_on,service_items_json,package_total_cents,sales_discount_cents,refund_rule_json,"
                "installment_plan_json,grace_days,status,proposed_by,proposed_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (contract_id, new_version_no, payload["change_reason"], int(current["version_no"]),
                 price_list_code, price_effective,
                 json.dumps(items, ensure_ascii=False, sort_keys=True), package_total, discount,
                 json.dumps(rule, ensure_ascii=False, sort_keys=True),
                 json.dumps(plan_store, ensure_ascii=False, sort_keys=True), grace_days,
                 payload["proposed_by"], now, now),
            )
            version_id = int(cursor.lastrowid)
            repo.event("contract", contract_id, "contract.amendment_proposed", payload["proposed_by"], {
                "version_no": new_version_no, "version_id": version_id, "change_reason": payload["change_reason"],
                "package_total_cents": net_total,
            }, now)
            return repo.version_row(version_id) or {}

    def confirm_version(self, contract_id: int, version_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.amend_confirm")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            version = repo.version_row(version_id)
            if version is None or version["contract_id"] != contract_id:
                raise NotFoundError("合同版本不存在")
            if version["status"] == "confirmed":
                return version
            if version["status"] != "pending":
                raise ConflictError("该版本已处理，不能确认")
            current = repo.version(contract_id, contract["current_version"])
            if current is None:
                raise NotFoundError("当前合同版本缺失")
            new_total = int(version["package_total_cents"]) - int(version["sales_discount_cents"])
            old_total = int(current["package_total_cents"]) - int(current["sales_discount_cents"])
            net_paid = int(contract["paid_cents"]) - int(contract["refunded_cents"])
            connection.execute(
                "UPDATE preneed_contract_versions SET status='superseded',superseded_at=? WHERE contract_id=? AND id!=? AND status='confirmed'",
                (now, contract_id, version_id),
            )
            connection.execute(
                "UPDATE preneed_contract_versions SET status='confirmed',confirmed_by=?,confirmed_at=?,"
                "customer_confirmer=?,customer_confirmed_at=? WHERE id=?",
                (payload["confirmed_by"], now, payload["customer_confirmer"], now, version_id),
            )
            # 旧分期计划保留已收款流水；未结清的旧分期作废，剩余义务按新版本重建
            connection.execute(
                "UPDATE preneed_installments SET status='cancelled' WHERE contract_id=? AND status!='cancelled' AND paid_cents<amount_cents",
                (contract_id,),
            )
            outstanding = max(0, new_total - net_paid)
            plan = version["installment_plan"] or {}
            if outstanding and plan:
                schedule = build_schedule(outstanding, int(plan["number"]), date.fromisoformat(plan["first_due_on"]), int(plan["interval_months"]))
            elif outstanding:
                schedule = [{"seq": 1, "due_on": contract["next_due_on"] or now[:10], "amount_cents": outstanding}]
            else:
                schedule = []
            for row in schedule:
                connection.execute(
                    "INSERT INTO preneed_installments(contract_id,version_no,seq,due_on,amount_cents,status) VALUES(?,?,?,?,?,'scheduled')",
                    (contract_id, version["version_no"], row["seq"], row["due_on"], row["amount_cents"]),
                )
            next_due = schedule[0]["due_on"] if schedule else None
            new_status = contract["status"]
            if new_status == "overdue" and not self._unpaid_due(connection, contract_id, self.today(), version["grace_days"] if version["grace_days"] is not None else 0):
                new_status = "active"
            connection.execute(
                "UPDATE preneed_contracts SET current_version=?,installment_total_cents=?,grace_days=?,next_due_on=?,status=?,updated_at=? WHERE id=?",
                (version["version_no"], new_total, version["grace_days"] if version["grace_days"] is not None else 0, next_due, new_status, now, contract_id),
            )
            delta = new_total - old_total
            if delta:
                if delta > 0:
                    lines = [("installments_receivable", delta, 0), ("contract_liability", 0, delta)]
                else:
                    lines = [("contract_liability", -delta, 0), ("installments_receivable", 0, -delta)]
                self._post(connection, contract_id, "contract.amendment_confirmed", lines, now, payload["confirmed_by"],
                           occurrence=str(version_id), ref_type="contract_version", ref_id=version_id, memo=version["change_reason"])
            repo.event("contract", contract_id, "contract.amendment_confirmed", payload["confirmed_by"], {
                "version_no": version["version_no"], "old_total_cents": old_total, "new_total_cents": new_total,
                "customer_confirmer": payload["customer_confirmer"], "outstanding_cents": outstanding,
            }, now)
            return repo.version_row(version_id) or {}

    def reject_version(self, contract_id: int, version_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.amend_reject")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            self._must_contract(repo, contract_id)
            version = repo.version_row(version_id)
            if version is None or version["contract_id"] != contract_id:
                raise NotFoundError("合同版本不存在")
            if version["status"] != "pending":
                raise ConflictError("只有待确认版本可以拒绝")
            connection.execute(
                "UPDATE preneed_contract_versions SET status='rejected',rejection_reason=? WHERE id=?",
                (payload["rejection_reason"], version_id),
            )
            repo.event("contract", contract_id, "contract.amendment_rejected", payload["rejected_by"], {
                "version_no": version["version_no"], "reason": payload["rejection_reason"],
            }, now)
            return repo.version_row(version_id) or {}

    # ---- 收款 ----
    def receive_payment(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "payment.receive")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            if contract["status"] == "suspended":
                raise ConflictError("暂停中的契约暂不收款，请先办理恢复")
            duplicate = repo.receipt_ref(payload["external_reference"])
            if duplicate:
                if duplicate["contract_id"] != contract_id or duplicate["amount_cents"] != payload["amount_cents"]:
                    raise ConflictError("收款流水号已用于其他款项")
                return self.get_contract(contract_id, repo)
            total = int(contract["installment_total_cents"])
            paid = int(contract["paid_cents"])
            if paid >= total:
                raise ConflictError("契约款项已收齐，不能继续收款")
            amount = int(payload["amount_cents"])
            if amount > total - paid:
                raise ValidationError("收款金额超过剩余应收", context={"remaining_cents": total - paid})
            installments = repo.open_installments(contract_id)
            if payload.get("installment_seq") is not None:
                chosen = [item for item in installments if item["seq"] == payload["installment_seq"] and item["version_no"] == contract["current_version"]]
                if not chosen:
                    raise NotFoundError("指定的分期不存在")
                ordered = chosen + [item for item in installments if item["id"] != chosen[0]["id"]]
            else:
                ordered = installments
            remaining = amount
            allocated: list[dict[str, Any]] = []
            for item in ordered:
                unpaid = int(item["amount_cents"]) - int(item["paid_cents"])
                if unpaid <= 0 or remaining <= 0:
                    continue
                applied = min(unpaid, remaining)
                new_paid = int(item["paid_cents"]) + applied
                item_status = "paid" if new_paid == int(item["amount_cents"]) else "partial"
                connection.execute(
                    "UPDATE preneed_installments SET paid_cents=?,status=?,paid_at=COALESCE(paid_at,?) WHERE id=?",
                    (new_paid, item_status, now, item["id"]),
                )
                allocated.append({"installment_id": item["id"], "seq": item["seq"], "amount_cents": applied})
                remaining -= applied
            if remaining:
                raise ValidationError("没有足够的待收分期承接该笔款项")
            cursor = connection.execute(
                "INSERT INTO preneed_receipts(contract_id,installment_id,amount_cents,channel,external_reference,received_by,received_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (contract_id, allocated[0]["installment_id"], amount, payload["channel"], payload["external_reference"], payload["received_by"], now),
            )
            receipt_id = int(cursor.lastrowid)
            new_paid = paid + amount
            connection.execute("UPDATE preneed_contracts SET paid_cents=?,next_due_on=?,updated_at=? WHERE id=?", (new_paid, self._next_unpaid_due(connection, contract_id), now, contract_id))
            self._post(connection, contract_id, "payment.received", [
                ("cash", amount, 0),
                ("installments_receivable", 0, amount),
            ], now, payload["received_by"], occurrence=payload["external_reference"], ref_type="preneed_receipt", ref_id=receipt_id)
            repo.event("contract", contract_id, "payment.received", payload["received_by"], {
                "receipt_id": receipt_id, "amount_cents": amount, "external_reference": payload["external_reference"],
                "allocation": allocated,
            }, now)
            # 补齐欠款（含宽限期）后自动解除逾期
            if contract["status"] == "overdue":
                due_unpaid = self._unpaid_due(connection, contract_id, self.today(), int(contract["grace_days"]))
                if not due_unpaid:
                    connection.execute("UPDATE preneed_contracts SET status='active',updated_at=? WHERE id=?", (now, contract_id))
                    connection.execute(
                        "INSERT INTO preneed_overdue_log(contract_id,action,due_on,overdue_days,actor,created_at) VALUES(?, 'resolved','',0,?,?)",
                        (contract_id, payload["received_by"], now),
                    )
                    self._memo(connection, contract_id, "contract.overdue_resolved", now, payload["received_by"], "欠款补齐", occurrence=payload["external_reference"])
                    repo.event("contract", contract_id, "contract.overdue_resolved", payload["received_by"], {"receipt_id": receipt_id}, now)
            return self.get_contract(contract_id, repo)

    @staticmethod
    def _next_unpaid_due(connection: sqlite3.Connection, contract_id: int) -> str | None:
        row = connection.execute(
            "SELECT MIN(due_on) FROM preneed_installments WHERE contract_id=? AND status!='cancelled' AND (amount_cents-paid_cents)>0",
            (contract_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    # ---- 逾期巡检 ----
    def sweep_overdue(self, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "overdue.sweep")
        now = self.now()
        today = self.today()
        marked: list[int] = []
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            for contract in repo.overdue_candidates(today.isoformat()):
                unpaid = self._unpaid_due(connection, contract["id"], today, int(contract["grace_days"]))
                if not unpaid:
                    continue
                earliest = unpaid[0]
                due_on = date.fromisoformat(earliest["due_on"])
                overdue_days = (today - due_on).days
                update_cursor = connection.execute("UPDATE preneed_contracts SET status='overdue',updated_at=? WHERE id=? AND status='active'", (now, contract["id"]))
                if update_cursor.rowcount:
                    cursor = connection.execute(
                        "INSERT INTO preneed_overdue_log(contract_id,action,due_on,overdue_days,actor,created_at) VALUES(?, 'marked_overdue',?,?,?,?)",
                        (contract["id"], earliest["due_on"], overdue_days, actor, now),
                    )
                    log_id = int(cursor.lastrowid)
                    self._memo(connection, contract["id"], "contract.overdue", now, actor,
                               f"分期 seq={earliest['seq']} 逾期 {overdue_days} 天", occurrence=str(log_id),
                               ref_type="preneed_installment", ref_id=earliest["id"])
                    repo.event("contract", contract["id"], "contract.overdue", actor, {
                        "installment_id": earliest["id"], "due_on": earliest["due_on"], "overdue_days": overdue_days,
                    }, now)
                    marked.append(contract["id"])
        return {"marked_overdue": marked, "swept_at": now}

    @staticmethod
    def _unpaid_due(connection: sqlite3.Connection, contract_id: int, today: date, grace_days: int = 0) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT i.* FROM preneed_installments i"
            " WHERE i.contract_id=? AND i.status!='cancelled' AND (i.amount_cents-i.paid_cents)>0"
            " AND date(i.due_on,'+'||?||' days') < date(?) ORDER BY i.due_on",
            (contract_id, grace_days, today.isoformat()),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 暂停 / 恢复 ----
    def suspend(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.suspend")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            if contract["status"] == "suspended":
                return self.get_contract(contract_id, repo)
            connection.execute(
                "UPDATE preneed_contracts SET status='suspended',suspended_at=?,suspended_reason=?,updated_at=? WHERE id=?",
                (now, payload["reason"], now, contract_id),
            )
            self._memo(connection, contract_id, "contract.suspended", now, payload["actor"], payload["reason"], occurrence=uuid.uuid4().hex)
            repo.event("contract", contract_id, "contract.suspended", payload["actor"], {"reason": payload["reason"]}, now)
            return self.get_contract(contract_id, repo)

    def resume(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.resume")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            if contract["status"] != "suspended":
                raise ConflictError("只有暂停中的契约可以恢复")
            still_overdue = bool(self._unpaid_due(connection, contract_id, self.today(), int(contract["grace_days"])))
            connection.execute(
                "UPDATE preneed_contracts SET status=?,suspended_at=NULL,suspended_reason='',updated_at=? WHERE id=?",
                ("overdue" if still_overdue else "active", now, contract_id),
            )
            self._memo(connection, contract_id, "contract.resumed", now, payload["actor"],
                       "恢复后仍处逾期" if still_overdue else "恢复履约", occurrence=uuid.uuid4().hex)
            repo.event("contract", contract_id, "contract.resumed", payload["actor"], {"status": "overdue" if still_overdue else "active"}, now)
            return self.get_contract(contract_id, repo)

    # ---- 解除（含退款） ----
    def terminate(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.terminate")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError(f"契约处于 {contract['status']} 状态，不能解除")
            pending = connection.execute(
                "SELECT id FROM preneed_contract_versions WHERE contract_id=? AND status='pending'", (contract_id,),
            ).fetchone()
            if pending:
                raise ConflictError("存在待确认的变更版本，不能解除")
            current = repo.version(contract_id, contract["current_version"])
            rule = current["refund_rule"] if current else {}
            paid = int(contract["paid_cents"])
            already_refunded = int(contract["refunded_cents"])
            net_paid = paid - already_refunded
            refundable = refundable_amount(net_paid, rule)
            refund_payload = payload.get("refund")
            if refund_payload is not None:
                if repo.refund_ref(refund_payload["external_reference"]):
                    raise ConflictError("退款流水号已存在")
                amount = refund_payload["amount_cents"] if refund_payload.get("amount_cents") is not None else refundable
                if amount > refundable:
                    raise ValidationError("退款金额超过按约定可退金额", context={"refundable_cents": refundable})
            else:
                amount = 0
            total = int(contract["installment_total_cents"])
            outstanding_receivable = max(0, total - paid)
            fee = net_paid - amount
            lines: list[tuple[str, int, int]] = []
            if outstanding_receivable:
                lines.append(("contract_liability", outstanding_receivable, 0))
                lines.append(("installments_receivable", 0, outstanding_receivable))
            if fee:
                lines.append(("contract_liability", fee, 0))
                lines.append(("service_revenue", 0, fee))
            if amount:
                lines.append(("contract_liability", amount, 0))
                lines.append(("cash", 0, amount))
            actor = payload.get("actor") or (refund_payload["handled_by"] if refund_payload is not None else "finance_manager")
            self._post(connection, contract_id, "contract.terminated", lines, now, actor, occurrence=str(contract_id), memo=payload["reason"])
            connection.execute(
                "UPDATE preneed_installments SET status='cancelled' WHERE contract_id=? AND status!='cancelled'",
                (contract_id,),
            )
            if amount and refund_payload is not None:
                connection.execute(
                    "INSERT INTO preneed_refunds(contract_id,amount_cents,reason,external_reference,handled_by,handled_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (contract_id, amount, refund_payload["reason"], refund_payload["external_reference"], refund_payload["handled_by"], now),
                )
            connection.execute(
                "UPDATE preneed_contracts SET status='terminated',terminated_at=?,terminate_reason=?,"
                "refunded_cents=refunded_cents+?,updated_at=? WHERE id=?",
                (now, payload["reason"], amount, now, contract_id),
            )
            repo.event("contract", contract_id, "contract.terminated", actor, {
                "reason": payload["reason"], "refund_cents": amount, "forfeited_cents": fee,
                "receivable_written_off_cents": outstanding_receivable,
            }, now)
            return self.get_contract(contract_id, repo)

    # ---- 受益人转让 ----
    def transfer_beneficiary(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.transfer")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_open(repo, contract_id)
            if contract["status"] == "suspended":
                raise ConflictError("暂停中的契约不能办理转让")
            pending = connection.execute(
                "SELECT id FROM preneed_contract_versions WHERE contract_id=? AND status='pending'", (contract_id,),
            ).fetchone()
            if pending:
                raise ConflictError("存在待确认的变更版本，不能转让")
            if payload["to_beneficiary_identity"] == contract["beneficiary_identity"]:
                raise ValidationError("新受益人与当前受益人相同")
            cursor = connection.execute(
                "INSERT INTO preneed_transfers(contract_id,from_beneficiary_name,from_beneficiary_identity,"
                "to_beneficiary_name,to_beneficiary_identity,to_relation,reason,transferred_by,customer_confirmer,transferred_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (contract_id, contract["beneficiary_name"], contract["beneficiary_identity"],
                 payload["to_beneficiary_name"], payload["to_beneficiary_identity"], payload["to_relation"],
                 payload["reason"], payload["transferred_by"], payload["customer_confirmer"], now),
            )
            transfer_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE preneed_contracts SET beneficiary_name=?,beneficiary_identity=?,beneficiary_relation=?,updated_at=? WHERE id=?",
                (payload["to_beneficiary_name"], payload["to_beneficiary_identity"], payload["to_relation"], now, contract_id),
            )
            self._memo(connection, contract_id, "contract.transferred", now, payload["transferred_by"],
                       f"{contract['beneficiary_name']} → {payload['to_beneficiary_name']}：{payload['reason']}",
                       occurrence=str(transfer_id), ref_type="preneed_transfer", ref_id=transfer_id)
            repo.event("contract", contract_id, "contract.transferred", payload["transferred_by"], {
                "transfer_id": transfer_id, "from_identity": contract["beneficiary_identity"],
                "to_name": payload["to_beneficiary_name"], "to_identity": payload["to_beneficiary_identity"],
                "customer_confirmer": payload["customer_confirmer"],
            }, now)
            return self.get_contract(contract_id, repo)

    # ---- 受益人死亡：转换为业务档案与服务订单（防重复） ----
    def convert(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require(role, "contract.convert")
        now = self.now()
        death_time = to_storage(payload["death_time"])
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._must_contract(repo, contract_id)
            idem = repo.conversion_key(payload["idempotency_key"])
            if idem is not None:
                if idem["id"] != contract_id:
                    raise ConflictError("转换幂等键已用于其他契约", context={"contract_id": idem["id"]})
                return self._conversion_summary(repo, idem)
            if contract["status"] == "converted":
                raise ConflictError("契约已转换，不能重复转换", context={"case_id": contract["converted_case_id"]})
            if contract["status"] not in {"active", "overdue"}:
                raise ConflictError(f"契约处于 {contract['status']} 状态，不能转换")
            pending = connection.execute(
                "SELECT id FROM preneed_contract_versions WHERE contract_id=? AND status='pending'", (contract_id,),
            ).fetchone()
            if pending:
                raise ConflictError("存在待确认的变更版本，不能转换")
            mortuary = MortuaryRepository(connection)
            existing_case = mortuary.case_ref(payload["external_ref"])
            if existing_case is not None:
                raise ConflictError("业务编号已被其他档案占用", context={"case_id": existing_case["id"]})
            if death_time > now:
                raise ValidationError("死亡时间不能晚于当前时间")
            current = repo.version(contract_id, contract["current_version"])
            if current is None:
                raise NotFoundError("当前合同版本缺失")
            case_cursor = connection.execute(
                "INSERT INTO mortuary_cases(external_ref,decedent_name,identity_number,death_time,received_from,family_contact,family_phone,special_notes,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,'registered',?,?)",
                (payload["external_ref"], contract["beneficiary_name"], contract["beneficiary_identity"], death_time,
                 payload["received_from"], payload["family_contact"], payload["family_phone"], payload["special_notes"], now, now),
            )
            case_id = int(case_cursor.lastrowid)
            order_ids: list[int] = []
            gross_total = 0
            # 按签约时冻结的服务清单与价格逐项生成订单
            for item in current["service_items"]:
                amount = int(item["quantity"]) * int(item["unit_price_cents"])
                gross_total += amount
                order_cursor = connection.execute(
                    "INSERT INTO funeral_service_orders(case_id,service_code,quantity,unit_price_cents,amount_cents,status,requested_by,notes,created_at,updated_at)"
                    " VALUES(?,?,?,?,?, 'invoiced',?,?,?,?)",
                    (case_id, item["service_code"], item["quantity"], item["unit_price_cents"], amount, payload["actor"],
                     f"生前契约 {contract['contract_no']} V{contract['current_version']} 冻结价格转换", now, now),
                )
                order_ids.append(int(order_cursor.lastrowid))
            discount = int(current["sales_discount_cents"])
            if discount:
                discount_cursor = connection.execute(
                    "INSERT INTO funeral_service_orders(case_id,service_code,quantity,unit_price_cents,amount_cents,status,requested_by,notes,created_at,updated_at)"
                    " VALUES(?,?,1,?,?,'invoiced',?,?,?,?)",
                    (case_id, "contract_discount", -discount, -discount, payload["actor"],
                     f"生前契约 {contract['contract_no']} V{contract['current_version']} 销售折让", now, now),
                )
                order_ids.append(int(discount_cursor.lastrowid))
            net_total = int(contract["installment_total_cents"])
            paid = int(contract["paid_cents"]) - int(contract["refunded_cents"])
            paid_applied = min(paid, net_total)
            gap = max(0, net_total - paid_applied)
            invoice_status = "paid" if gap == 0 else "partially_paid"
            invoice_cursor = connection.execute(
                "INSERT INTO invoices(case_id,amount_cents,paid_cents,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, net_total, paid_applied, invoice_status, payload["actor"], now, now),
            )
            invoice_id = int(invoice_cursor.lastrowid)
            for order_id in order_ids:
                connection.execute("INSERT INTO invoice_items(invoice_id,order_id,amount_cents) SELECT ?,id,amount_cents FROM funeral_service_orders WHERE id=?", (invoice_id, order_id))
            # 合同义务（净价）结算为服务收入；未收分期余额转入业务账单应收
            lines = [
                ("contract_liability", net_total, 0),
                ("service_revenue", 0, net_total),
            ]
            if gap:
                lines += [("accounts_receivable", gap, 0), ("installments_receivable", 0, gap)]
            self._post(connection, contract_id, "contract.converted", lines, now, payload["actor"],
                       occurrence=payload["idempotency_key"], ref_type="mortuary_case", ref_id=case_id,
                       memo=f"转换为档案 {payload['external_ref']}")
            connection.execute(
                "UPDATE preneed_installments SET status='cancelled' WHERE contract_id=? AND status!='cancelled'",
                (contract_id,),
            )
            connection.execute(
                "UPDATE preneed_contracts SET status='converted',converted_at=?,converted_case_id=?,"
                "converted_order_ids_json=?,conversion_key=?,next_due_on=NULL,updated_at=? WHERE id=?",
                (now, case_id, json.dumps(order_ids), payload["idempotency_key"], now, contract_id),
            )
            repo.event("contract", contract_id, "contract.converted", payload["actor"], {
                "case_id": case_id, "invoice_id": invoice_id, "order_ids": order_ids,
                "external_ref": payload["external_ref"], "unperformed_balance_cents": net_total,
                "gross_total_cents": gross_total, "discount_cents": discount,
                "paid_applied_cents": paid_applied, "receivable_gap_cents": gap,
            }, now)
            mortuary.event("case", case_id, "case.from_preneed", payload["actor"], {
                "contract_id": contract_id, "contract_no": contract["contract_no"],
                "version_no": contract["current_version"], "invoice_id": invoice_id, "order_ids": order_ids,
            }, now)
            contract = repo.contract(contract_id) or {}
            return self._conversion_summary(repo, contract)

    def _conversion_summary(self, repo: PreneedRepository, contract: dict[str, Any]) -> dict[str, Any]:
        case = MortuaryRepository(repo.connection).case(contract["converted_case_id"])
        orders = [dict(row) for row in repo.connection.execute(
            "SELECT * FROM funeral_service_orders WHERE case_id=? ORDER BY id", (contract["converted_case_id"],),
        ).fetchall()]
        invoice = repo.connection.execute(
            "SELECT * FROM invoices WHERE case_id=? ORDER BY id DESC LIMIT 1", (contract["converted_case_id"],),
        ).fetchone()
        return {"contract": contract, "case": case, "orders": orders, "invoice": dict(invoice) if invoice else None}

    # ---- 查询 ----
    def list_contracts(self, status: str | None, limit: int, role: str) -> list[dict[str, Any]]:
        self._require(role, "contract.read")
        statuses = [status] if status else None
        return self.repository.contracts_by_status(statuses, limit)

    def get_contract(self, contract_id: int, repo: PreneedRepository | None = None, role: str | None = None) -> dict[str, Any]:
        if role is not None:
            self._require(role, "contract.read")
        repo = repo or self.repository
        contract = self._must_contract(repo, contract_id)
        contract["current_version_detail"] = repo.version(contract_id, contract["current_version"])
        contract["versions"] = repo.versions(contract_id)
        contract["installments"] = repo.installments(contract_id)
        contract["receipts"] = repo.receipts(contract_id)
        contract["refunds"] = repo.refunds(contract_id)
        contract["transfers"] = repo.transfers(contract_id)
        contract["ledger"] = repo.entries(contract_id)
        contract["timeline"] = repo.timeline("contract", contract_id)
        contract["liability"] = self._liability_snapshot(repo, contract)
        contract["next_action"] = self._next_action(repo, contract, self.today())
        return contract

    def _liability_snapshot(self, repo: PreneedRepository, contract: dict[str, Any], *, at: str | None = None, paid_override: int | None = None, refunded_override: int | None = None, total_override: int | None = None) -> dict[str, Any]:
        paid = int(contract["paid_cents"]) if paid_override is None else paid_override
        refunded = int(contract["refunded_cents"]) if refunded_override is None else refunded_override
        total = int(contract["installment_total_cents"]) if total_override is None else total_override
        net_paid = paid - refunded
        current = repo.version(contract["id"], contract["current_version"])
        refundable = refundable_amount(max(0, net_paid), current["refund_rule"] if current else {})
        return {
            "package_total_cents": total,
            "paid_cents": paid,
            "refunded_cents": refunded,
            "refundable_cents": refundable if contract["status"] in OPEN_STATUSES else 0,
            "unperformed_balance_cents": total if contract["status"] != "converted" else 0,
            "receivable_gap_cents": max(0, total - paid),
            "held_fund_cents": max(0, net_paid),
        }

    def _next_action(self, repo: PreneedRepository, contract: dict[str, Any], today: date) -> dict[str, Any]:
        status = contract["status"]
        if status in {"terminated", "converted", "void"}:
            return {"code": "none", "detail": "契约已终局", "due_on": None, "overdue_days": 0}
        open_items = [item for item in repo.open_installments(contract["id"]) if int(item["amount_cents"]) - int(item["paid_cents"]) > 0]
        if not open_items:
            return {"code": "awaiting_beneficiary_event", "detail": "款项已收齐，等待受益人身后服务触发", "due_on": None, "overdue_days": 0}
        earliest = open_items[0]
        due_on = date.fromisoformat(earliest["due_on"])
        overdue_days = max(0, (today - due_on).days)
        if status == "suspended":
            return {"code": "resume_or_review", "detail": "契约暂停中，需恢复或评估解除", "due_on": earliest["due_on"], "overdue_days": overdue_days}
        if overdue_days > int(contract["grace_days"]):
            return {"code": "collect_overdue_installment", "detail": f"分期 seq={earliest['seq']} 已逾期", "due_on": earliest["due_on"], "overdue_days": overdue_days}
        if (due_on - today).days <= 7:
            return {"code": "collect_installment", "detail": f"分期 seq={earliest['seq']} 临近到期", "due_on": earliest["due_on"], "overdue_days": 0}
        return {"code": "in_force", "detail": "契约正常履约中", "due_on": earliest["due_on"], "overdue_days": 0}

    def contract_as_of(self, contract_id: int, at: datetime, role: str) -> dict[str, Any]:
        self._require(role, "contract.read")
        at_text = to_storage(at)
        repo = self.repository
        contract = self._must_contract(repo, contract_id)
        if at_text < contract["created_at"]:
            raise NotFoundError("该时点契约尚未签订")
        versions = [v for v in repo.versions(contract_id) if v["status"] in {"confirmed", "superseded"} and (v["confirmed_at"] or "") <= at_text]
        if not versions:
            raise NotFoundError("该时点尚无经客户确认的合同版本")
        effective = max(versions, key=lambda v: v["confirmed_at"])
        paid = int(repo.connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM preneed_receipts WHERE contract_id=? AND received_at<=?",
            (contract_id, at_text),
        ).fetchone()[0])
        refunded = int(repo.connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM preneed_refunds WHERE contract_id=? AND handled_at<=?",
            (contract_id, at_text),
        ).fetchone()[0])
        status = self._status_as_of(repo, contract_id, at_text)
        total = int(effective["package_total_cents"]) - int(effective["sales_discount_cents"])
        installments = [dict(row) for row in repo.connection.execute(
            "SELECT * FROM preneed_installments WHERE contract_id=? AND version_no=? ORDER BY seq",
            (contract_id, effective["version_no"]),
        ).fetchall()]
        simulated = dict(contract)
        simulated["status"] = status
        simulated["current_version"] = effective["version_no"]
        simulated["installment_total_cents"] = total
        simulated["paid_cents"] = paid
        simulated["refunded_cents"] = refunded
        simulated["grace_days"] = int(contract["grace_days"])
        return {
            "as_of": at_text,
            "status": status,
            "effective_version_no": effective["version_no"],
            "effective_version": effective,
            "installments": installments,
            "paid_cents": paid,
            "refunded_cents": refunded,
            "liability": self._liability_snapshot(repo, simulated, paid_override=paid, refunded_override=refunded, total_override=total),
            "next_action": self._as_of_next_action(installments, status, at.date(), int(contract["grace_days"])),
        }

    @staticmethod
    def _status_as_of(repo: PreneedRepository, contract_id: int, at_text: str) -> str:
        rows = repo.connection.execute(
            "SELECT event_type FROM preneed_events WHERE aggregate_type='contract' AND aggregate_id=? AND created_at<=? ORDER BY id",
            (str(contract_id), at_text),
        ).fetchall()
        status = "active"
        for row in rows:
            event_type = row["event_type"]
            if event_type == "contract.overdue":
                status = "overdue"
            elif event_type == "contract.overdue_resolved":
                status = "active"
            elif event_type == "contract.suspended":
                status = "suspended"
            elif event_type == "contract.resumed":
                status = "active"
            elif event_type == "contract.terminated":
                status = "terminated"
            elif event_type == "contract.converted":
                status = "converted"
        return status

    @staticmethod
    def _as_of_next_action(installments: list[dict[str, Any]], status: str, day: date, grace_days: int) -> dict[str, Any]:
        if status in {"terminated", "converted", "void"}:
            return {"code": "none", "detail": "契约已终局", "due_on": None, "overdue_days": 0}
        unpaid = [item for item in installments if int(item["amount_cents"]) - int(item["paid_cents"]) > 0 and item["status"] != "cancelled"]
        if not unpaid:
            return {"code": "awaiting_beneficiary_event", "detail": "款项已收齐，等待受益人身后服务触发", "due_on": None, "overdue_days": 0}
        earliest = unpaid[0]
        due_on = date.fromisoformat(earliest["due_on"])
        overdue_days = max(0, (day - due_on).days)
        if status == "suspended":
            return {"code": "resume_or_review", "detail": "契约暂停中", "due_on": earliest["due_on"], "overdue_days": overdue_days}
        if overdue_days > grace_days:
            return {"code": "collect_overdue_installment", "detail": f"分期 seq={earliest['seq']} 已逾期", "due_on": earliest["due_on"], "overdue_days": overdue_days}
        return {"code": "in_force", "detail": "契约正常履约中", "due_on": earliest["due_on"], "overdue_days": 0}

    @staticmethod
    def _must_contract(repo: PreneedRepository, contract_id: int) -> dict[str, Any]:
        contract = repo.contract(contract_id)
        if contract is None:
            raise NotFoundError("生前契约不存在")
        return contract

    def _must_open(self, repo: PreneedRepository, contract_id: int) -> dict[str, Any]:
        contract = self._must_contract(repo, contract_id)
        if contract["status"] not in OPEN_STATUSES:
            raise ConflictError(f"契约处于 {contract['status']} 状态，该操作不允许")
        return contract
