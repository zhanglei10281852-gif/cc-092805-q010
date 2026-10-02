from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository
from app.preneed.repository import PreneedRepository

# 岗位角色：敏感动作必须由指定岗位执行
ROLE_CONTRACT_CLERK = "preneed_clerk"            # 生前契约经办员
ROLE_CUSTOMER_LEAD = "customer_service_lead"     # 客户服务主管
ROLE_FINANCE_CASHIER = "finance_cashier"         # 财务出纳
ROLE_FINANCE_MANAGER = "finance_manager"         # 财务主管
ROLE_SYSTEM_BATCH = "system_batch"               # 定时批处理

# 会计科目
ACC_RECEIVABLE = "应收契约款"
ACC_LIABILITY = "合同负债"
ACC_CASH = "银行存款"
ACC_REFUND_PAYABLE = "应退履约款"
ACC_CLEARING = "转让待清算"
ACC_INCOME = "违约金收入"
ACC_FULFILLMENT = "待履约服务款"

OPEN_STATUSES = {"active", "overdue"}


class PreneedService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = PreneedRepository(self.connection)
        self.repository.ensure_schema()
        MortuaryRepository(self.connection).ensure_schema()

    def now(self) -> datetime:
        return self.clock.now()

    def now_text(self) -> str:
        return to_storage(self.now())

    def today(self) -> date:
        return self.now().date()

    @staticmethod
    def _require_role(role: str, allowed: set[str], action: str) -> None:
        if role not in allowed:
            raise PermissionDeniedError(f"无权执行{action}", context={"required_roles": sorted(allowed), "role": role})

    # ------------------------------------------------------------------ 签约
    def create_contract(self, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK}, "生前契约签约")
        now = self.now_text()
        signed_on = payload.get("signed_at") or self.today()
        items = [dict(item) for item in payload["items"]]
        total = sum(int(item["quantity"]) * int(item["unit_price_cents"]) for item in items)
        installments = sorted(payload["installments"], key=lambda item: item["period_no"])
        planned_total = sum(int(item["amount_cents"]) for item in installments)
        if planned_total != total:
            raise ValidationError("分期计划金额之和必须等于契约总价",
                                  context={"total_cents": total, "planned_cents": planned_total})
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            if repo.contract_no(payload["contract_no"]):
                raise ConflictError("契约编号已存在")
            cursor = connection.execute(
                "INSERT INTO preneed_contracts(contract_no,plan_code,plan_name,customer_name,customer_phone,"
                "customer_identity,customer_address,beneficiary_name,beneficiary_identity,beneficiary_phone,"
                "relationship,total_cents,signed_on,refund_rule_json,status,current_version,next_action,"
                "next_action_due,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["contract_no"], payload["plan_code"], payload["plan_name"], payload["customer_name"],
                 payload["customer_phone"], payload["customer_identity"], payload["customer_address"],
                 payload["beneficiary_name"], payload["beneficiary_identity"], payload["beneficiary_phone"],
                 payload["relationship"], total, signed_on.isoformat(),
                 json.dumps(payload["refund_rule"], ensure_ascii=False, sort_keys=True),
                 "active", 1, f"缴纳第 1 期款项", installments[0]["due_date"].isoformat(), now, now),
            )
            contract_id = int(cursor.lastrowid)
            plan_json = json.dumps([{
                "period_no": p["period_no"], "due_date": p["due_date"].isoformat(),
                "amount_cents": p["amount_cents"],
            } for p in installments], ensure_ascii=False)
            connection.execute(
                "INSERT INTO preneed_versions(contract_id,version_no,change_type,items_json,price_basis_json,"
                "installment_plan_json,total_cents,change_reason,proposed_by,confirmed_by_customer_name,"
                "confirmed_by,confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, 1, "sign", json.dumps(items, ensure_ascii=False),
                 json.dumps(payload["price_basis"], ensure_ascii=False, sort_keys=True), plan_json, total,
                 "签约冻结清单与价格", actor, payload["customer_name"], actor, now, now),
            )
            for plan in installments:
                connection.execute(
                    "INSERT INTO preneed_installments(contract_id,period_no,due_date,amount_cents,version_no,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (contract_id, plan["period_no"], plan["due_date"].isoformat(), plan["amount_cents"], 1,
                     "scheduled", now, now),
                )
            # 签约即确认未来服务义务与应收对价
            self._entry(connection, contract_id, "contract.signed", ACC_RECEIVABLE, ACC_LIABILITY, total,
                        None, 1, actor, "签约确认全额合同义务", now)
            event_id = repo.event("contract", contract_id, "contract.signed", actor, {
                "contract_no": payload["contract_no"], "total_cents": total, "items": items,
                "price_basis": payload["price_basis"], "installments": len(installments),
                "signed_on": signed_on.isoformat(),
            }, now)
            connection.execute("UPDATE preneed_contracts SET last_event_id=? WHERE id=?", (event_id, contract_id))
            return self.get_contract(contract_id, repo)

    # ------------------------------------------------------------------ 查询
    def list_contracts(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM preneed_contracts"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["paid_cents"] = self._paid_total(item["id"])
            item["outstanding_cents"] = int(item["total_cents"]) - item["paid_cents"]
            result.append(item)
        return result

    def get_contract(self, contract_id: int, repo: PreneedRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        contract = repo.contract(contract_id)
        if contract is None:
            raise NotFoundError("生前契约不存在")
        return self._hydrate(contract, repo)

    def _hydrate(self, contract: dict[str, Any], repo: PreneedRepository) -> dict[str, Any]:
        contract_id = int(contract["id"])
        contract["refund_rule"] = json.loads(contract.pop("refund_rule_json"))
        versions = []
        for row in repo.versions(contract_id):
            versions.append(self._version_view(dict(row)))
        contract["versions"] = versions
        contract["installments"] = [dict(row) for row in repo.installments(contract_id)]
        contract["receipts"] = [dict(row) for row in repo.receipts(contract_id)]
        contract["refunds"] = [dict(row) for row in repo.refunds(contract_id)]
        contract["transfers"] = [dict(row) for row in repo.transfers(contract_id)]
        contract["suspensions"] = [dict(row) for row in repo.suspensions(contract_id)]
        contract["conversion"] = repo.conversion(contract_id)
        contract["accounting_entries"] = [dict(row) for row in repo.accounting_entries(contract_id)]
        contract["account_balances"] = self._balances(contract["accounting_entries"])
        paid = sum(int(r["amount_cents"]) for r in contract["receipts"] if r["status"] == "posted")
        contract["paid_cents"] = paid
        contract["outstanding_cents"] = int(contract["total_cents"]) - paid
        contract["refund_estimate_cents"] = self._refund_quote_rule(
            contract["refund_rule"], contract["created_at"], contract["status"], paid, self.today()) \
            if contract["status"] in OPEN_STATUSES | {"suspended"} else None
        action = self._next_action(contract)
        contract["next_action"] = action["action"]
        contract["next_action_due"] = action["due"]
        contract["timeline"] = repo.timeline("contract", contract_id)
        return contract

    @staticmethod
    def _version_view(row: dict[str, Any]) -> dict[str, Any]:
        row["items"] = json.loads(row.pop("items_json"))
        row["price_basis"] = json.loads(row.pop("price_basis_json"))
        plan_raw = row.pop("installment_plan_json") or ""
        row["installment_plan"] = json.loads(plan_raw) if plan_raw else None
        row["confirmed"] = bool(row.get("confirmed_at"))
        return row

    def get_versions(self, contract_id: int) -> list[dict[str, Any]]:
        if self.repository.contract(contract_id) is None:
            raise NotFoundError("生前契约不存在")
        return [self._version_view(dict(row)) for row in self.repository.versions(contract_id)]

    def snapshot_at(self, contract_id: int, as_of: datetime) -> dict[str, Any]:
        """重放截至某一时点的事件、版本、收据与分录，回答当时的合同责任。"""
        contract = self.repository.contract(contract_id)
        if contract is None:
            raise NotFoundError("生前契约不存在")
        moment = to_storage(as_of)
        if contract["created_at"] > moment:
            raise NotFoundError("该时点契约尚未签订")
        events = [e for e in self.repository.timeline("contract", contract_id) if e["created_at"] <= moment]
        status = "active"
        for event in events:
            if event["event_type"] == "receipt.posted":
                status = event["payload"].get("status", status)
                continue
            if event["event_type"] == "contract.resumed":
                status = event["payload"].get("new_status", "active")
                continue
            status = {
                "contract.signed": "active",
                "contract.overdue_marked": "overdue",
                "contract.suspended": "suspended",
                "contract.terminated": "terminated",
                "contract.transferred": "transferred",
                "contract.converted": "converted",
            }.get(event["event_type"], status)
        version = None
        for row in self.repository.versions(contract_id):
            if row["created_at"] <= moment and row["confirmed_at"] and row["confirmed_at"] <= moment:
                version = row
        if version is None:
            raise NotFoundError("该时点没有已确认的契约版本")
        paid = 0
        for row in self.repository.receipts(contract_id):
            posted = row["status"] == "posted" and row["received_at"] <= moment
            undone = (row["status"] == "reversed" and row["reversed_at"] and row["reversed_at"] <= moment)
            if posted and not undone:
                paid += int(row["amount_cents"])
        balances = self._balances([
            dict(row) for row in self.repository.accounting_entries(contract_id) if row["created_at"] <= moment
        ])
        installments = [dict(row) for row in self.repository.installments(contract_id) if row["created_at"] <= moment]
        next_due = None
        for plan in installments:
            if plan["status"] == "scheduled" and plan["paid_amount_cents"] < plan["amount_cents"]:
                next_due = plan
                break
        total = int(version["total_cents"])
        return {
            "as_of": moment,
            "contract_no": contract["contract_no"],
            "status": status,
            "version_no": version["version_no"],
            "total_cents": total,
            "paid_cents": paid,
            "outstanding_cents": total - paid,
            "liability_cents": max(-balances.get(ACC_LIABILITY, 0), 0),
            "next_action": self._status_action(status, next_due),
            "next_action_due": next_due["due_date"] if next_due else "",
            "events_so_far": [event["event_type"] for event in events],
        }

    # ------------------------------------------------------------------ 收款
    def record_receipt(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_FINANCE_CASHIER}, "生前契约收款")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] == "suspended":
                raise ConflictError("契约已暂停，暂停期间不得收款，请先恢复")
            if contract["status"] in {"terminated", "transferred"}:
                raise ConflictError("当前契约状态不能收款")
            duplicate = repo.receipt_ref(payload["external_reference"])
            if duplicate:
                if duplicate["contract_id"] != contract_id or duplicate["amount_cents"] != payload["amount_cents"]:
                    raise ConflictError("收款流水号已用于其他款项")
                return self.get_contract(contract_id, repo)
            installments = repo.installments(contract_id)
            open_periods = [p for p in installments
                            if p["status"] == "scheduled" and p["paid_amount_cents"] < p["amount_cents"]]
            amount = int(payload["amount_cents"])
            total_open = sum(int(p["amount_cents"]) - int(p["paid_amount_cents"]) for p in open_periods)
            if amount > total_open:
                raise ValidationError("收款金额超过未缴分期合计", context={"open_cents": total_open})
            allocations = self._allocate(open_periods, amount, payload.get("installment_id"))
            cursor = connection.execute(
                "INSERT INTO preneed_receipts(contract_id,period_id,amount_cents,channel,external_reference,"
                "received_by,received_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (contract_id, payload.get("installment_id"), amount, payload["channel"],
                 payload["external_reference"], payload["received_by"], now, now),
            )
            receipt_id = int(cursor.lastrowid)
            entry_id = self._entry(connection, contract_id, "receipt.posted", ACC_CASH, ACC_RECEIVABLE, amount,
                                   receipt_id, int(contract["current_version"]), payload["received_by"],
                                   f"分期收款 {payload['external_reference']}", now)
            connection.execute("UPDATE preneed_receipts SET account_event_id=? WHERE id=?", (entry_id, receipt_id))
            for period_id, applied in allocations:
                row = repo.installment(period_id)
                new_paid = int(row["paid_amount_cents"]) + applied
                new_status = "paid" if new_paid >= int(row["amount_cents"]) else "scheduled"
                connection.execute(
                    "UPDATE preneed_installments SET paid_amount_cents=?,status=?,"
                    "paid_at=COALESCE(paid_at,?),updated_at=? WHERE id=?",
                    (new_paid, new_status, now, now, period_id),
                )
            new_status = contract["status"]
            if contract["status"] == "overdue" and not self._has_late_open(repo, contract_id, self.today()):
                new_status = "active"
            self._refresh_contract_state(connection, repo, contract_id, new_status, now)
            repo.event("contract", contract_id, "receipt.posted", payload["received_by"], {
                "receipt_id": receipt_id, "amount_cents": amount,
                "external_reference": payload["external_reference"],
                "allocations": [{"period_id": pid, "amount_cents": amt} for pid, amt in allocations],
                "status": new_status,
            }, now)
            return self.get_contract(contract_id, repo)

    @staticmethod
    def _allocate(periods: list[dict[str, Any]], amount: int,
                  installment_id: int | None) -> list[tuple[int, int]]:
        if installment_id is not None:
            target = next((p for p in periods if p["id"] == installment_id), None)
            if target is None:
                raise ValidationError("指定分期不存在或已结清")
            remaining = int(target["amount_cents"]) - int(target["paid_amount_cents"])
            if amount > remaining:
                raise ValidationError("本期收款超过该期未缴金额")
            return [(installment_id, amount)]
        result: list[tuple[int, int]] = []
        rest = amount
        for period in sorted(periods, key=lambda p: (p["due_date"], p["period_no"])):
            if rest <= 0:
                break
            due = int(period["amount_cents"]) - int(period["paid_amount_cents"])
            applied = min(rest, due)
            result.append((int(period["id"]), applied))
            rest -= applied
        return result

    # ------------------------------------------------------------------ 逾期
    def mark_overdue(self, contract_id: int, actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK, ROLE_CUSTOMER_LEAD, ROLE_SYSTEM_BATCH}, "标记逾期")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("仅有效契约可以标记逾期")
            late = [p for p in repo.installments(contract_id)
                    if p["status"] == "scheduled" and p["paid_amount_cents"] < p["amount_cents"]
                    and p["due_date"] < self.today().isoformat()]
            if not late:
                raise ConflictError("没有逾期未缴的分期")
            if contract["status"] == "overdue":
                return self.get_contract(contract_id, repo)
            self._refresh_contract_state(connection, repo, contract_id, "overdue", now)
            event_id = repo.event("contract", contract_id, "contract.overdue_marked", actor, {
                "periods": [p["period_no"] for p in late],
            }, now)
            connection.execute("UPDATE preneed_contracts SET last_event_id=? WHERE id=?", (event_id, contract_id))
            return self.get_contract(contract_id, repo)

    @staticmethod
    def _has_late_open(repo: PreneedRepository, contract_id: int, today_: date) -> bool:
        return any(
            p["status"] == "scheduled" and p["paid_amount_cents"] < p["amount_cents"]
            and p["due_date"] < today_.isoformat()
            for p in repo.installments(contract_id)
        )

    # ------------------------------------------------------------------ 变更
    def propose_amendment(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK}, "提出契约变更")
        now = self.now_text()
        items = [dict(item) for item in payload["items"]]
        new_total = sum(int(item["quantity"]) * int(item["unit_price_cents"]) for item in items)
        planned = payload.get("installments")
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("仅有效契约可以提出变更")
            latest = repo.latest_version(contract_id)
            if latest and not latest["confirmed_at"]:
                raise ConflictError("存在尚未经客户确认的变更版本")
            paid_total = self._paid_total(contract_id, repo)
            existing = repo.installments(contract_id)
            if planned is not None:
                ordered = sorted(planned, key=lambda p: p["period_no"])
                if [p["period_no"] for p in ordered] != list(range(1, len(ordered) + 1)):
                    raise ValidationError("分期期号必须从 1 开始连续编号")
                for prev, cur in zip(ordered, ordered[1:]):
                    if cur["due_date"] <= prev["due_date"]:
                        raise ValidationError("分期到期日必须严格递增")
                if sum(int(p["amount_cents"]) for p in planned) != new_total:
                    raise ValidationError("调整后分期金额之和必须等于新总价")
                by_no = {p["period_no"]: p for p in existing}
                for plan in planned:
                    old = by_no.get(plan["period_no"])
                    if old is None:
                        continue
                    if old["status"] == "paid" and int(plan["amount_cents"]) != int(old["amount_cents"]):
                        raise ValidationError("已缴清期次的金额不能改动",
                                              context={"period_no": plan["period_no"]})
                    if int(plan["amount_cents"]) < int(old["paid_amount_cents"]):
                        raise ValidationError("期次金额不能低于已收金额",
                                              context={"period_no": plan["period_no"]})
                for old in existing:
                    if old["status"] == "scheduled" and int(old["paid_amount_cents"]) > 0 \
                            and old["period_no"] not in {p["period_no"] for p in planned}:
                        raise ValidationError("存在部分收款的期次，调整计划时必须保留",
                                              context={"period_no": old["period_no"]})
            else:
                scheduled = [p for p in existing if p["status"] == "scheduled"]
                if not scheduled and new_total != int(contract["total_cents"]):
                    raise ValidationError("契约已全部缴清，调整总价必须显式提供新的分期计划")
            version_no = int(contract["current_version"]) + 1
            plan_json = ""
            if planned is not None:
                plan_json = json.dumps([{
                    "period_no": p["period_no"], "due_date": p["due_date"].isoformat(),
                    "amount_cents": p["amount_cents"],
                } for p in sorted(planned, key=lambda x: x["period_no"])], ensure_ascii=False)
            cursor = connection.execute(
                "INSERT INTO preneed_versions(contract_id,version_no,change_type,items_json,price_basis_json,"
                "installment_plan_json,total_cents,change_reason,proposed_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (contract_id, version_no, "amend", json.dumps(items, ensure_ascii=False),
                 json.dumps(payload["price_basis"], ensure_ascii=False, sort_keys=True), plan_json, new_total,
                 payload["reason"], payload["proposed_by"], now),
            )
            version_id = int(cursor.lastrowid)
            repo.event("contract", contract_id, "amendment.proposed", payload["proposed_by"], {
                "version_no": version_no, "new_total_cents": new_total,
                "old_total_cents": int(contract["total_cents"]), "reason": payload["reason"],
            }, now)
            return {"id": version_id, "contract_id": contract_id, "version_no": version_no,
                    "status": "pending_customer_confirmation", "total_cents": new_total, "items": items}

    def confirm_amendment(self, version_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK}, "确认契约变更")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            version = repo.one(connection.execute(
                "SELECT * FROM preneed_versions WHERE id=?", (version_id,)).fetchone())
            if version is None:
                raise NotFoundError("变更版本不存在")
            if version["change_type"] != "amend":
                raise ConflictError("该版本不是服务变更")
            if version["confirmed_at"]:
                return self.get_contract(int(version["contract_id"]), repo)
            contract_id = int(version["contract_id"])
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("契约当前状态不能确认变更")
            connection.execute(
                "UPDATE preneed_versions SET confirmed_by_customer_name=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                (payload["customer_name"], actor, now, version_id),
            )
            old_total = int(contract["total_cents"])
            new_total = int(version["total_cents"])
            self._apply_installment_plan(connection, repo, contract_id, version, new_total,
                                         version["version_no"], now)
            delta = new_total - old_total
            if delta > 0:
                self._entry(connection, contract_id, "amendment.confirmed", ACC_RECEIVABLE, ACC_LIABILITY, delta,
                            None, version["version_no"], actor, f"变更加价 {version['version_no']} 版", now)
            elif delta < 0:
                self._entry(connection, contract_id, "amendment.confirmed", ACC_LIABILITY, ACC_RECEIVABLE, -delta,
                            None, version["version_no"], actor, f"变更减价 {version['version_no']} 版", now)
            connection.execute(
                "UPDATE preneed_contracts SET total_cents=?,current_version=?,version=version+1,updated_at=? WHERE id=?",
                (new_total, version["version_no"], now, contract_id),
            )
            self._refresh_contract_state(connection, repo, contract_id, contract["status"], now)
            repo.event("contract", contract_id, "amendment.confirmed", actor, {
                "version_no": version["version_no"], "old_total_cents": old_total,
                "new_total_cents": new_total, "customer_name": payload["customer_name"],
                "confirmation_ref": payload["confirmation_ref"],
            }, now)
            return self.get_contract(contract_id, repo)

    def discard_amendment(self, version_id: int, actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK, ROLE_CUSTOMER_LEAD}, "废弃变更草案")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            version = repo.one(connection.execute(
                "SELECT * FROM preneed_versions WHERE id=?", (version_id,)).fetchone())
            if version is None:
                raise NotFoundError("变更版本不存在")
            if version["confirmed_at"]:
                raise ConflictError("已客户确认的版本不能废弃，旧承诺已保留为历史版本")
            connection.execute("DELETE FROM preneed_versions WHERE id=?", (version_id,))
            repo.event("contract", version["contract_id"], "amendment.discarded", actor, {
                "version_no": version["version_no"],
            }, now)
            return {"discarded_version_no": version["version_no"], "contract_id": version["contract_id"]}

    @staticmethod
    def _apply_installment_plan(connection: sqlite3.Connection, repo: PreneedRepository, contract_id: int,
                                version: dict[str, Any], new_total: int, version_no: int, now: str) -> None:
        existing = repo.installments(contract_id)
        paid_total = sum(int(p["paid_amount_cents"]) for p in existing)
        plan_raw = version["installment_plan_json"] or ""
        if plan_raw:
            planned = json.loads(plan_raw)
            by_no = {p["period_no"]: p for p in existing}
            planned_nos = {p["period_no"] for p in planned}
            for plan in planned:
                old = by_no.get(plan["period_no"])
                if old is None:
                    connection.execute(
                        "INSERT INTO preneed_installments(contract_id,period_no,due_date,amount_cents,"
                        "version_no,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (contract_id, plan["period_no"], plan["due_date"], plan["amount_cents"],
                         version_no, "scheduled", now, now),
                    )
                elif old["status"] == "scheduled":
                    connection.execute(
                        "UPDATE preneed_installments SET due_date=?,amount_cents=?,version_no=?,updated_at=? WHERE id=?",
                        (plan["due_date"], plan["amount_cents"], version_no, now, old["id"]),
                    )
            for old in existing:
                if old["period_no"] not in planned_nos and old["status"] == "scheduled":
                    # propose 阶段已保证部分收款期次不会消失
                    connection.execute(
                        "UPDATE preneed_installments SET status='cancelled',version_no=?,updated_at=? WHERE id=?",
                        (version_no, now, old["id"]),
                    )
            return
        # 未显式给计划：把新旧总价差额并入最后一个未缴期
        scheduled = [p for p in existing if p["status"] == "scheduled"]
        if not scheduled:
            if new_total != paid_total:
                raise ValidationError("没有可调整的未缴期次，请显式提供分期计划")
            return
        open_total = sum(int(p["amount_cents"]) - int(p["paid_amount_cents"]) for p in scheduled)
        target_open = new_total - paid_total
        diff = target_open - open_total
        last = sorted(scheduled, key=lambda p: p["period_no"])[-1]
        new_last_amount = int(last["amount_cents"]) + diff
        if new_last_amount < int(last["paid_amount_cents"]):
            raise ValidationError("减价金额超过末期可调整范围，请显式提供分期计划")
        connection.execute(
            "UPDATE preneed_installments SET amount_cents=?,version_no=?,updated_at=? WHERE id=?",
            (new_last_amount, version_no, now, last["id"]),
        )
        for period in scheduled:
            if period["id"] != last["id"]:
                connection.execute(
                    "UPDATE preneed_installments SET version_no=?,updated_at=? WHERE id=?",
                    (version_no, now, period["id"]),
                )

    # ------------------------------------------------------------------ 暂停/恢复
    def suspend(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CUSTOMER_LEAD}, "暂停生前契约")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("仅有效或逾期契约可以暂停")
            connection.execute(
                "INSERT INTO preneed_suspensions(contract_id,kind,reason,reason_detail,operated_by,"
                "customer_confirmed_name,customer_confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (contract_id, "suspend", payload["reason"], payload.get("reason_detail", ""),
                 payload["operated_by"], payload["customer_confirmed_name"], now, now),
            )
            self._refresh_contract_state(connection, repo, contract_id, "suspended", now)
            repo.event("contract", contract_id, "contract.suspended", payload["operated_by"], {
                "reason": payload["reason"], "reason_detail": payload.get("reason_detail", ""),
                "customer_confirmed_name": payload["customer_confirmed_name"],
            }, now)
            return self.get_contract(contract_id, repo)

    def resume(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CUSTOMER_LEAD, ROLE_CONTRACT_CLERK}, "恢复生前契约")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] != "suspended":
                raise ConflictError("仅暂停中的契约可以恢复")
            connection.execute(
                "INSERT INTO preneed_suspensions(contract_id,kind,reason,operated_by,customer_confirmed_name,"
                "customer_confirmed_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (contract_id, "resume", payload["reason"], payload["operated_by"],
                 payload["customer_confirmed_name"], now, now),
            )
            new_status = "overdue" if self._has_late_open(repo, contract_id, self.today()) else "active"
            self._refresh_contract_state(connection, repo, contract_id, new_status, now)
            repo.event("contract", contract_id, "contract.resumed", payload["operated_by"], {
                "reason": payload["reason"], "new_status": new_status,
                "customer_confirmed_name": payload["customer_confirmed_name"],
            }, now)
            return self.get_contract(contract_id, repo)

    # ------------------------------------------------------------------ 解除
    def terminate(self, contract_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_FINANCE_MANAGER}, "解除生前契约")
        now = self.now_text()
        if payload["approved_by"] != actor:
            raise PermissionDeniedError("审批人必须与登录经办人一致")
        if payload["approved_by"] == payload["applied_by"]:
            raise PermissionDeniedError("契约解除必须经办与审批分离")
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES | {"suspended"}:
                raise ConflictError("当前契约状态不能解除")
            latest = repo.latest_version(contract_id)
            if latest and not latest["confirmed_at"]:
                raise ConflictError("存在未确认的变更版本，不能解除")
            total = int(contract["total_cents"])
            paid = self._paid_total(contract_id, repo)
            payable, penalty, rate_permille = self._refund_quote(contract, paid, self.today())
            outstanding = total - paid
            cursor = connection.execute(
                "INSERT INTO preneed_refunds(contract_id,payable_cents,penalty_cents,status,approved_by,"
                "applied_by,approved_at,applied_at,version_no,reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, payable, penalty, "pending", payload["approved_by"], payload["applied_by"],
                 now, now, int(contract["current_version"]), payload["reason"], now, now),
            )
            refund_id = int(cursor.lastrowid)
            # 复式分录：全额冲销合同负债，分别计入应退款、违约金收入与应收冲销
            if payable:
                self._entry(connection, contract_id, "contract.terminated", ACC_LIABILITY, ACC_REFUND_PAYABLE,
                            payable, None, int(contract["current_version"]), payload["approved_by"],
                            "解除确认应退本金", now)
            if penalty:
                self._entry(connection, contract_id, "contract.terminated", ACC_LIABILITY, ACC_INCOME, penalty,
                            None, int(contract["current_version"]), payload["approved_by"],
                            "解除扣款/违约金", now)
            if outstanding:
                self._entry(connection, contract_id, "contract.terminated", ACC_LIABILITY, ACC_RECEIVABLE,
                            outstanding, None, int(contract["current_version"]), payload["approved_by"],
                            "解除冲销未收应收", now)
            connection.execute(
                "UPDATE preneed_installments SET status='cancelled',updated_at=? "
                "WHERE contract_id=? AND status='scheduled'",
                (now, contract_id),
            )
            self._append_terminal_version(connection, repo, contract_id, "terminate", payload["reason"],
                                          payload["approved_by"], now)
            new_version_no = int(contract["current_version"]) + 1
            connection.execute(
                "UPDATE preneed_contracts SET status='terminated',current_version=?,total_cents=?,"
                "next_action='等待财务支付退款',next_action_due='',version=version+1,updated_at=? WHERE id=?",
                (new_version_no, paid, now, contract_id),
            )
            event_id = repo.event("contract", contract_id, "contract.terminated", payload["approved_by"], {
                "refund_id": refund_id, "paid_cents": paid, "payable_cents": payable,
                "penalty_cents": penalty, "rate_permille": rate_permille, "reason": payload["reason"],
            }, now)
            connection.execute("UPDATE preneed_contracts SET last_event_id=? WHERE id=?", (event_id, contract_id))
            return self.get_contract(contract_id, repo)

    @staticmethod
    def _refund_quote(contract: dict[str, Any], paid: int, today_: date) -> tuple[int, int, int]:
        rule = json.loads(contract["refund_rule_json"])
        return PreneedService._refund_quote_rule(
            rule, contract.get("signed_on") or contract["created_at"], contract["status"], paid, today_)

    @staticmethod
    def _refund_quote_rule(rule: dict[str, Any], signed_on: str, status: str,
                           paid: int, today_: date) -> tuple[int, int, int]:
        signed = date.fromisoformat(signed_on[:10])
        if (today_ - signed).days <= int(rule["cooling_days"]):
            rate = int(rule["cooling_rate_permille"])
        elif status == "overdue":
            rate = int(rule["after_overdue_rate_permille"])
        else:
            rate = int(rule["after_cooling_rate_permille"])
        payable = paid * rate // 1000
        return payable, paid - payable, rate

    def pay_refund(self, refund_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_FINANCE_CASHIER}, "支付解除退款")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            refund = repo.refund(refund_id)
            if refund is None:
                raise NotFoundError("退款单不存在")
            if refund["status"] == "paid":
                return refund
            if refund["status"] != "pending":
                raise ConflictError("该退款单不能支付")
            duplicate = connection.execute(
                "SELECT 1 FROM preneed_refunds WHERE payment_reference=? AND id<>?",
                (payload["payment_reference"], refund_id),
            ).fetchone()
            if duplicate:
                raise ConflictError("退款流水号已被使用")
            payable = int(refund["payable_cents"])
            if payable:
                self._entry(connection, int(refund["contract_id"]), "refund.paid", ACC_REFUND_PAYABLE, ACC_CASH,
                            payable, None, refund["version_no"], actor,
                            f"解除退款 {payload['payment_reference']}", now)
            connection.execute(
                "UPDATE preneed_refunds SET status='paid',paid_cents=payable_cents,paid_by=?,"
                "payment_reference=?,updated_at=? WHERE id=?",
                (payload["paid_by"], payload["payment_reference"], now, refund_id),
            )
            connection.execute(
                "UPDATE preneed_contracts SET next_action='契约已解除并结清',next_action_due='',updated_at=? WHERE id=?",
                (now, refund["contract_id"]),
            )
            repo.event("contract", refund["contract_id"], "refund.paid", payload["paid_by"], {
                "refund_id": refund_id, "amount_cents": payable,
                "payment_reference": payload["payment_reference"],
            }, now)
            return repo.refund(refund_id) or {}

    # ------------------------------------------------------------------ 转让
    def apply_transfer(self, contract_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CONTRACT_CLERK}, "申请受益人转让")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("仅有效契约可以申请转让")
            pending = connection.execute(
                "SELECT * FROM preneed_transfers WHERE contract_id=? AND status='pending'", (contract_id,)
            ).fetchone()
            if pending:
                raise ConflictError("已有待审核的转让申请")
            cursor = connection.execute(
                "INSERT INTO preneed_transfers(contract_id,old_beneficiary_name,old_beneficiary_identity,"
                "new_beneficiary_name,new_beneficiary_identity,new_beneficiary_phone,relationship,reason,"
                "applied_by,customer_confirmed_by,customer_confirmed_at,customer_confirmation_ref,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, contract["beneficiary_name"], contract["beneficiary_identity"],
                 payload["new_beneficiary_name"], payload["new_beneficiary_identity"],
                 payload["new_beneficiary_phone"], payload["relationship"], payload["reason"],
                 payload["applied_by"], payload["customer_confirmed_by"], now,
                 payload["customer_confirmation_ref"], "pending", now),
            )
            transfer_id = int(cursor.lastrowid)
            repo.event("contract", contract_id, "transfer.applied", payload["applied_by"], {
                "transfer_id": transfer_id,
                "old_beneficiary": contract["beneficiary_name"],
                "new_beneficiary": payload["new_beneficiary_name"],
                "customer_confirmation_ref": payload["customer_confirmation_ref"],
            }, now)
            return repo.transfer(transfer_id) or {}

    def approve_transfer(self, transfer_id: int, actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CUSTOMER_LEAD}, "批准受益人转让")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            transfer = repo.transfer(transfer_id)
            if transfer is None:
                raise NotFoundError("转让申请不存在")
            if transfer["status"] != "pending":
                raise ConflictError("转让申请已处理")
            old = self._require_contract(repo, int(transfer["contract_id"]))
            if old["status"] not in OPEN_STATUSES:
                raise ConflictError("原契约当前状态不能转让")
            old_version = repo.latest_version(int(old["id"]))
            items = json.loads(old_version["items_json"])
            price_basis = json.loads(old_version["price_basis_json"])
            total = int(old_version["total_cents"])
            paid = self._paid_total(int(old["id"]), repo)
            new_no = f"{old['contract_no']}-T{transfer_id}"
            if repo.contract_no(new_no):
                raise ConflictError("转让后契约编号冲突")
            cursor = connection.execute(
                "INSERT INTO preneed_contracts(contract_no,plan_code,plan_name,customer_name,customer_phone,"
                "customer_identity,customer_address,beneficiary_name,beneficiary_identity,beneficiary_phone,"
                "relationship,total_cents,signed_on,refund_rule_json,status,current_version,next_action,"
                "next_action_due,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_no, old["plan_code"], old["plan_name"], old["customer_name"], old["customer_phone"],
                 old["customer_identity"], old["customer_address"], transfer["new_beneficiary_name"],
                 transfer["new_beneficiary_identity"], transfer["new_beneficiary_phone"],
                 transfer["relationship"], total, self.today().isoformat(), old["refund_rule_json"], "active", 1,
                 "等待分期收缴", "", now, now),
            )
            new_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO preneed_versions(contract_id,version_no,change_type,items_json,price_basis_json,"
                "installment_plan_json,total_cents,change_reason,proposed_by,confirmed_by_customer_name,"
                "confirmed_by,confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, 1, "sign", json.dumps(items, ensure_ascii=False),
                 json.dumps(price_basis, ensure_ascii=False), old_version["installment_plan_json"], total,
                 f"由契约 {old['contract_no']} 转让继承，价格与清单冻结不变", actor,
                 old["customer_name"], actor, now, now),
            )
            for plan in repo.installments(int(old["id"])):
                connection.execute(
                    "INSERT INTO preneed_installments(contract_id,period_no,due_date,amount_cents,version_no,"
                    "status,paid_at,paid_amount_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id, plan["period_no"], plan["due_date"], plan["amount_cents"], 1,
                     plan["status"], plan["paid_at"], plan["paid_amount_cents"], now, now),
                )
            # 资金与义务通过“转让待清算”科目在两份契约间平移
            if paid:
                self._entry(connection, int(old["id"]), "contract.transferred", ACC_LIABILITY, ACC_CLEARING,
                            paid, None, int(old["current_version"]), actor,
                            f"转让至 {new_no}：已缴资金划出", now)
                self._entry(connection, new_id, "contract.transferred", ACC_CLEARING, ACC_LIABILITY, paid,
                            None, 1, actor, f"自 {old['contract_no']} 转让：继承已缴资金", now)
            outstanding = total - paid
            if outstanding:
                self._entry(connection, int(old["id"]), "contract.transferred", ACC_LIABILITY, ACC_RECEIVABLE,
                            outstanding, None, int(old["current_version"]), actor,
                            f"转让至 {new_no}：未收余额冲销", now)
                self._entry(connection, new_id, "contract.transferred", ACC_RECEIVABLE, ACC_LIABILITY,
                            outstanding, None, 1, actor,
                            f"自 {old['contract_no']} 转让：继承未收余额", now)
            if paid:
                connection.execute(
                    "INSERT INTO preneed_receipts(contract_id,period_id,amount_cents,channel,external_reference,"
                    "status,received_by,received_at,created_at) VALUES(?,?,?,?,?, 'posted',?,?,?)",
                    (new_id, None, paid, "transfer", f"TRANSFER-IN-{transfer_id}", actor, now, now),
                )
            connection.execute(
                "UPDATE preneed_transfers SET new_contract_id=?,status='confirmed',confirmed_at=? WHERE id=?",
                (new_id, now, transfer_id),
            )
            # 原契约保留全部版本与资金历史，仅状态转为已转让
            self._append_terminal_version(connection, repo, int(old["id"]), "transfer",
                                          f"受益人转让，新契约 {new_no}", actor, now)
            new_version_no = int(old["current_version"]) + 1
            connection.execute(
                "UPDATE preneed_contracts SET status='transferred',current_version=?,next_action=?,"
                "next_action_due='',version=version+1,updated_at=? WHERE id=?",
                (new_version_no, f"已转让至 {new_no}", now, int(old["id"])),
            )
            connection.execute(
                "UPDATE preneed_installments SET status='cancelled',updated_at=? "
                "WHERE contract_id=? AND status='scheduled'",
                (now, int(old["id"])),
            )
            repo.event("contract", int(old["id"]), "contract.transferred", actor, {
                "transfer_id": transfer_id, "new_contract_id": new_id, "new_contract_no": new_no,
                "transferred_paid_cents": paid,
            }, now)
            repo.event("contract", new_id, "contract.transferred_in", actor, {
                "transfer_id": transfer_id, "source_contract_no": old["contract_no"],
                "inherited_paid_cents": paid, "total_cents": total,
            }, now)
            self._refresh_contract_state(connection, repo, new_id, "active", now)
            return {"transfer": repo.transfer(transfer_id), "new_contract": self.get_contract(new_id, repo)}

    def reject_transfer(self, transfer_id: int, payload: dict[str, Any], role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CUSTOMER_LEAD, ROLE_CONTRACT_CLERK}, "驳回受益人转让")
        now = self.now_text()
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            transfer = repo.transfer(transfer_id)
            if transfer is None:
                raise NotFoundError("转让申请不存在")
            if transfer["status"] != "pending":
                raise ConflictError("转让申请已处理")
            connection.execute("UPDATE preneed_transfers SET status='rejected' WHERE id=?", (transfer_id,))
            repo.event("contract", transfer["contract_id"], "transfer.rejected", payload["rejected_by"], {
                "transfer_id": transfer_id, "reason": payload["reason"],
            }, now)
            return repo.transfer(transfer_id) or {}

    @staticmethod
    def _append_terminal_version(connection: sqlite3.Connection, repo: PreneedRepository, contract_id: int,
                                 change_type: str, reason: str, actor: str, now: str) -> None:
        latest = repo.latest_version(contract_id)
        connection.execute(
            "INSERT INTO preneed_versions(contract_id,version_no,change_type,items_json,price_basis_json,"
            "installment_plan_json,total_cents,change_reason,proposed_by,confirmed_by_customer_name,"
            "confirmed_by,confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contract_id, int(latest["version_no"]) + 1, change_type, latest["items_json"],
             latest["price_basis_json"], latest["installment_plan_json"], int(latest["total_cents"]),
             reason, "", actor, actor, now, now),
        )

    # ------------------------------------------------------------------ 死亡转换
    def convert_to_case(self, contract_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require_role(role, {ROLE_CUSTOMER_LEAD}, "转换生前契约为业务档案")
        now = self.now_text()
        if payload["approved_by"] != actor:
            raise PermissionDeniedError("复核人必须与登录经办人一致")
        if payload["approved_by"] == payload["handled_by"]:
            raise PermissionDeniedError("转换必须经办与复核分离")
        with transaction(immediate=True) as connection:
            repo = PreneedRepository(connection)
            contract = self._require_contract(repo, contract_id)
            if contract["status"] not in OPEN_STATUSES:
                raise ConflictError("仅有效契约可以转换为业务档案")
            if repo.conversion(contract_id):
                raise ConflictError("该契约已经转换，禁止重复转换",
                                    context={"case_id": repo.conversion(contract_id)["case_id"]})
            if repo.case_ref(payload["external_ref"]):
                raise ConflictError("业务档案外部编号已存在")
            try:
                death_time = datetime.fromisoformat(payload["death_time"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError("死亡时间格式无效") from exc
            if death_time > self.now():
                raise ValidationError("受益人尚未死亡，不能转换契约")
            version = repo.latest_version(contract_id)
            items = json.loads(version["items_json"])
            paid = self._paid_total(contract_id, repo)
            total = int(version["total_cents"])
            cursor = connection.execute(
                "INSERT INTO mortuary_cases(external_ref,decedent_name,identity_number,death_time,received_from,"
                "family_contact,family_phone,special_notes,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (payload["external_ref"], payload["decedent_name"], contract["beneficiary_identity"],
                 to_storage(death_time), payload["received_from"], payload["family_contact"],
                 payload["family_phone"], payload.get("special_notes", ""), "registered", now, now),
            )
            case_id = int(cursor.lastrowid)
            # 严格按签约冻结版本的清单与价格生成已确认服务订单
            order_ids = []
            for item in items:
                amount = int(item["quantity"]) * int(item["unit_price_cents"])
                cursor = connection.execute(
                    "INSERT INTO funeral_service_orders(case_id,service_code,quantity,unit_price_cents,"
                    "amount_cents,status,requested_by,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (case_id, item["service_code"], item["quantity"], item["unit_price_cents"], amount,
                     "confirmed", payload["handled_by"],
                     f"生前契约 {contract['contract_no']} 第 {version['version_no']} 版冻结清单转换", now, now),
                )
                order_ids.append(int(cursor.lastrowid))
            unfulfilled = total - paid
            connection.execute(
                "INSERT INTO preneed_conversions(contract_id,case_id,death_cert_ref,unfulfilled_cents,"
                "delivered_value_cents,handled_by,approved_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (contract_id, case_id, payload["death_cert_ref"], unfulfilled, paid,
                 payload["handled_by"], payload["approved_by"], now),
            )
            # 合同负债重分类至待履约服务款，运营接手履行；未缴余额继续收缴
            self._entry(connection, contract_id, "contract.converted", ACC_LIABILITY, ACC_FULFILLMENT, total,
                        None, version["version_no"], actor, f"受益人死亡转换，业务档案 #{case_id}", now)
            action = "继续收缴未缴余额并由运营履约" if unfulfilled else "业务档案已转换，进入履约"
            connection.execute(
                "UPDATE preneed_contracts SET status='converted',converted_case_id=?,"
                "next_action=?,next_action_due='',updated_at=? WHERE id=?",
                (case_id, action, now, contract_id),
            )
            event_id = repo.event("contract", contract_id, "contract.converted", actor, {
                "case_id": case_id, "external_ref": payload["external_ref"],
                "death_cert_ref": payload["death_cert_ref"], "order_ids": order_ids,
                "unfulfilled_cents": unfulfilled, "paid_cents": paid,
                "handled_by": payload["handled_by"],
            }, now)
            connection.execute("UPDATE preneed_contracts SET last_event_id=? WHERE id=?", (event_id, contract_id))
            MortuaryRepository(connection).event("case", case_id, "case.converted_from_preneed", actor, {
                "contract_id": contract_id, "contract_no": contract["contract_no"],
                "version_no": version["version_no"], "order_ids": order_ids,
                "unfulfilled_cents": unfulfilled,
            }, now)
            result = self.get_contract(contract_id, repo)
            result["case_id"] = case_id
            result["order_ids"] = order_ids
            return result

    # ------------------------------------------------------------------ 辅助
    def _require_contract(self, repo: PreneedRepository, contract_id: int) -> dict[str, Any]:
        contract = repo.contract(contract_id)
        if contract is None:
            raise NotFoundError("生前契约不存在")
        return contract

    @staticmethod
    def _paid_total(contract_id: int, repo: PreneedRepository | None = None) -> int:
        repo = repo or PreneedRepository(get_connection())
        return sum(int(row["amount_cents"]) for row in repo.receipts(contract_id) if row["status"] == "posted")

    @staticmethod
    def _entry(connection: sqlite3.Connection, contract_id: int, event_type: str, debit: str, credit: str,
               amount: int, receipt_id: int | None, version_no: int | None, operator_id: str, note: str,
               now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO preneed_accounting_entries(contract_id,event_type,debit_account,credit_account,"
            "amount_cents,receipt_id,version_no,operator_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (contract_id, event_type, debit, credit, amount, receipt_id, version_no, operator_id, note, now),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _balances(entries: list[dict[str, Any]]) -> dict[str, int]:
        balances: dict[str, int] = {}
        for entry in entries:
            balances[entry["debit_account"]] = balances.get(entry["debit_account"], 0) + int(entry["amount_cents"])
            balances[entry["credit_account"]] = balances.get(entry["credit_account"], 0) - int(entry["amount_cents"])
        return balances

    def _refresh_contract_state(self, connection: sqlite3.Connection, repo: PreneedRepository,
                                contract_id: int, status: str, now: str) -> None:
        contract = repo.contract(contract_id)
        hydrated = self._hydrate(dict(contract), PreneedRepository(connection))
        action = self._next_action(hydrated)
        connection.execute(
            "UPDATE preneed_contracts SET status=?,next_action=?,next_action_due=?,updated_at=? WHERE id=?",
            (status, action["action"], action["due"], now, contract_id),
        )

    def _next_action(self, contract: dict[str, Any]) -> dict[str, str]:
        status = contract.get("status")
        if status in OPEN_STATUSES or status == "converted":
            for plan in contract.get("installments", []):
                if plan["status"] == "scheduled" and plan["paid_amount_cents"] < plan["amount_cents"]:
                    return {"action": f"缴纳第 {plan['period_no']} 期款项", "due": plan["due_date"]}
            if status == "converted":
                return {"action": "业务档案已转换，进入履约", "due": ""}
            return {"action": "等待受益人身故后转换为业务档案", "due": ""}
        if status == "suspended":
            return {"action": "处理暂停原因后恢复契约", "due": ""}
        if status == "terminated":
            pending = [r for r in contract.get("refunds", []) if r["status"] == "pending"]
            if pending:
                return {"action": "等待财务支付退款", "due": ""}
            return {"action": "契约已解除并结清", "due": ""}
        if status == "transferred":
            confirmed = [t for t in contract.get("transfers", []) if t["status"] == "confirmed"]
            if confirmed:
                return {"action": f"已转让至新契约 #{confirmed[-1]['new_contract_id']}", "due": ""}
            return {"action": "等待转让审核", "due": ""}
        return {"action": contract.get("next_action", ""), "due": contract.get("next_action_due", "")}

    def _status_action(self, status: str, next_due: dict[str, Any] | None) -> str:
        if status in OPEN_STATUSES and next_due:
            return f"缴纳第 {next_due['period_no']} 期款项"
        if status in OPEN_STATUSES:
            return "等待受益人身故后转换为业务档案"
        if status == "suspended":
            return "处理暂停原因后恢复契约"
        if status == "terminated":
            return "等待解除审批与退款"
        if status == "transferred":
            return "契约已转让"
        if status == "converted":
            return "业务档案已转换，进入履约"
        return ""
