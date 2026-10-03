from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import get_connection
from app.preneed.service import PreneedService


ITEMS = [
    {"service_code": "farewell-ceremony", "service_name": "告别仪式", "quantity": 1, "unit_price_cents": 120000},
    {"service_code": "cremation-basic", "service_name": "基础火化", "quantity": 1, "unit_price_cents": 30000},
]


def contract_payload(contract_no: str = "PN-001", **overrides) -> dict:
    payload = {
        "contract_no": contract_no,
        "plan_code": "peace-2026",
        "plan_name": "安宁十年守护计划",
        "customer_name": "王秀兰",
        "customer_identity": "ID-CUST-001",
        "customer_phone": "13900000001",
        "beneficiary_name": "陈德海",
        "beneficiary_identity": "ID-BEN-001",
        "beneficiary_relation": "配偶",
        "price_list_code": "PL-2026-Q4",
        "price_list_effective_on": "2026-09-01",
        "service_items": ITEMS,
        "sales_discount_cents": 10000,
        "refund_rule": {"admin_fee_bps": 1000, "performance_penalty_bps": 0, "note": "解约扣手续费10%"},
        "installment_plan": {"number": 3, "first_due_on": "2026-10-01", "interval_months": 1},
        "grace_days": 5,
        "created_by": "consultant-zhao",
        "customer_confirmer": "王秀兰(本人签字)",
    }
    payload.update(overrides)
    return payload


def create_contract(client, no: str = "PN-001", role: str = "consultant", **overrides) -> dict:
    response = client.post(f"/api/preneed/contracts?role={role}", json=contract_payload(no, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def pay(client, contract_id: int, ref: str, amount: int, role: str = "cashier") -> dict:
    response = client.post(
        f"/api/preneed/contracts/{contract_id}/receipts?role={role}",
        json={"amount_cents": amount, "channel": "bank", "external_reference": ref, "received_by": "cashier-qian"},
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()


def convert_payload(key: str = "conv-pn-001", ref: str = "CASE-PN-001") -> dict:
    return {
        "external_ref": ref,
        "death_time": "2026-10-02T06:00:00Z",
        "received_from": "市第一医院",
        "family_contact": "王秀兰",
        "family_phone": "13900000001",
        "special_notes": "凭生前契约办理",
        "actor": "customer-service-sun",
        "idempotency_key": key,
    }


def assert_ledger_balanced(detail: dict) -> None:
    debit = sum(entry["debit_cents"] for entry in detail["ledger"])
    credit = sum(entry["credit_cents"] for entry in detail["ledger"])
    assert debit == credit


def test_signing_freezes_price_list_and_installments(client):
    detail = create_contract(client)
    assert detail["status"] == "active"
    assert detail["installment_total_cents"] == 140000
    assert len(detail["installments"]) == 3
    assert [row["amount_cents"] for row in detail["installments"]] == [46666, 46666, 46668]
    version = detail["current_version_detail"]
    assert version["price_list_code"] == "PL-2026-Q4"
    assert version["status"] == "confirmed"
    assert version["service_items"][0]["unit_price_cents"] == 120000
    liability = detail["liability"]
    assert liability["receivable_gap_cents"] == 140000
    assert liability["refundable_cents"] == 0
    signed = [e for e in detail["ledger"] if e["event_type"] == "contract.signed"]
    assert len(signed) == 2 and sum(e["debit_cents"] for e in signed) == 140000
    assert detail["next_action"]["code"] in {"collect_installment", "in_force"}
    assert [e["event_type"] for e in detail["timeline"]] == ["contract.created"]


def test_role_permissions_are_enforced(client):
    denied = client.post("/api/preneed/contracts?role=cashier", json=contract_payload("PN-DENY"))
    assert denied.status_code == 403
    detail = create_contract(client, "PN-PERM")
    terminate = client.post(
        f"/api/preneed/contracts/{detail['id']}/terminate?role=customer_service",
        json={"reason": "客户迁居外地申请解除", "actor": "customer-service-sun"},
    )
    assert terminate.status_code == 403
    assert client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").status_code == 200


def test_payments_idempotent_and_capped(client):
    detail = create_contract(client, "PN-PAY")
    payment = {"amount_cents": 50000, "channel": "bank", "external_reference": "R-001", "received_by": "cashier-qian"}
    first = client.post(f"/api/preneed/contracts/{detail['id']}/receipts?role=cashier", json=payment)
    repeated = client.post(f"/api/preneed/contracts/{detail['id']}/receipts?role=cashier", json=payment)
    assert first.status_code == repeated.status_code == 201
    assert repeated.json()["paid_cents"] == 50000
    over = client.post(
        f"/api/preneed/contracts/{detail['id']}/receipts?role=cashier",
        json={"amount_cents": 100000, "channel": "bank", "external_reference": "R-002", "received_by": "cashier-qian"},
    )
    assert over.status_code == 422
    detail = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()
    assert detail["liability"]["receivable_gap_cents"] == 90000
    assert_ledger_balanced(detail)


def test_amendment_creates_version_requiring_confirmation_and_keeps_history(client):
    detail = create_contract(client, "PN-AMD")
    pay(client, detail["id"], "AR-1", 50000)
    amend = {
        "change_reason": "家属升级告别厅堂型",
        "price_list_code": "PL-2027-Q1",
        "price_list_effective_on": "2027-01-01",
        "service_items": [
            {"service_code": "farewell-ceremony", "service_name": "告别仪式尊享厅", "quantity": 1, "unit_price_cents": 150000},
            {"service_code": "cremation-basic", "service_name": "基础火化", "quantity": 1, "unit_price_cents": 30000},
        ],
        "proposed_by": "consultant-zhao",
    }
    proposed = client.post(f"/api/preneed/contracts/{detail['id']}/amendments?role=consultant", json=amend)
    assert proposed.status_code == 201
    version_id = proposed.json()["id"]
    # 待确认期间当前承诺不变
    mid = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()
    assert mid["current_version"] == 1
    assert mid["installment_total_cents"] == 140000
    second = client.post(f"/api/preneed/contracts/{detail['id']}/amendments?role=consultant", json=amend)
    assert second.status_code == 409
    # 客户确认后才生效，旧版本保留为 superseded
    confirmed = client.post(
        f"/api/preneed/contracts/{detail['id']}/versions/{version_id}/confirm?role=customer_service",
        json={"customer_confirmer": "王秀兰(电话回访确认)", "confirmed_by": "customer-service-sun"},
    )
    assert confirmed.status_code == 200
    final = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()
    assert final["current_version"] == 2
    assert final["installment_total_cents"] == 170000
    assert final["liability"]["receivable_gap_cents"] == 120000
    statuses = {v["version_no"]: v["status"] for v in final["versions"]}
    assert statuses == {1: "superseded", 2: "confirmed"}
    assert final["versions"][0]["service_items"][0]["unit_price_cents"] == 120000  # 旧承诺未被抹去
    assert [e["event_type"] for e in final["timeline"]][-1] == "contract.amendment_confirmed"
    assert_ledger_balanced(final)


def test_rejected_amendment_leaves_commitment_untouched(client):
    detail = create_contract(client, "PN-REJ")
    proposed = client.post(
        f"/api/preneed/contracts/{detail['id']}/amendments?role=consultant",
        json={"change_reason": "尝试增加纪念品项目", "sales_discount_cents": 0, "proposed_by": "consultant-zhao",
              "service_items": ITEMS},
    )
    version_id = proposed.json()["id"]
    rejected = client.post(
        f"/api/preneed/contracts/{detail['id']}/versions/{version_id}/reject?role=finance_manager",
        json={"rejection_reason": "客户不同意取消折让", "rejected_by": "finance-manager-li"},
    )
    assert rejected.status_code == 200
    final = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()
    assert final["current_version"] == 1
    assert final["installment_total_cents"] == 140000
    assert final["versions"][1]["status"] == "rejected"


def test_overdue_sweep_respects_grace_and_auto_resolves(client):
    del client  # 仅用于初始化临时数据库
    clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=UTC))
    service = PreneedService(get_connection(), clock)
    detail = service.create_contract(contract_payload(
        "PN-OD", installment_plan={"number": 2, "first_due_on": "2026-09-01", "interval_months": 1}, grace_days=5,
    ), "consultant")
    contract_id = detail["id"]
    clock.advance(days=3)
    assert service.sweep_overdue("cron", "system")["marked_overdue"] == []  # 宽限期内
    clock.advance(days=4)
    marked = service.sweep_overdue("cron", "system")["marked_overdue"]
    assert marked == [contract_id]
    assert service.get_contract(contract_id)["status"] == "overdue"
    # 补缴后自动解除逾期
    detail = service.receive_payment(contract_id, {
        "amount_cents": 70000, "channel": "bank", "external_reference": "OD-1", "received_by": "cashier-qian",
    }, "cashier")
    assert detail["status"] == "active"
    events = [e["event_type"] for e in detail["timeline"]]
    assert "contract.overdue" in events and "contract.overdue_resolved" in events


def test_suspend_blocks_transfer_and_resume_restores(client):
    detail = create_contract(client, "PN-SUS")
    suspended = client.post(
        f"/api/preneed/contracts/{detail['id']}/suspend?role=customer_service",
        json={"reason": "客户申请缓交三个月", "actor": "customer-service-sun"},
    )
    assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
    transfer = client.post(
        f"/api/preneed/contracts/{detail['id']}/transfer?role=customer_service",
        json={"to_beneficiary_name": "陈晓", "to_beneficiary_identity": "ID-BEN-002", "to_relation": "子女",
              "reason": "家庭安排变更", "transferred_by": "customer-service-sun", "customer_confirmer": "王秀兰确认"},
    )
    assert transfer.status_code == 409
    resumed = client.post(
        f"/api/preneed/contracts/{detail['id']}/resume?role=customer_service", json={"actor": "customer-service-sun"},
    )
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"


def test_terminate_refunds_per_rule_and_closes_obligation(client):
    detail = create_contract(client, "PN-TERM")
    pay(client, detail["id"], "TR-1", 100000)
    too_much = client.post(
        f"/api/preneed/contracts/{detail['id']}/terminate?role=finance_manager",
        json={"reason": "全家迁居境外", "actor": "finance-manager-li",
              "refund": {"amount_cents": 95000, "reason": "迁居解除", "external_reference": "RF-BAD", "handled_by": "finance-manager-li"}},
    )
    assert too_much.status_code == 422
    terminated = client.post(
        f"/api/preneed/contracts/{detail['id']}/terminate?role=finance_manager",
        json={"reason": "全家迁居境外", "actor": "finance-manager-li",
              "refund": {"amount_cents": 90000, "reason": "迁居解除", "external_reference": "RF-1", "handled_by": "finance-manager-li"}},
    )
    assert terminated.status_code == 200
    body = terminated.json()
    assert body["status"] == "terminated"
    assert body["refunded_cents"] == 90000
    assert all(row["status"] == "cancelled" for row in body["installments"])
    assert body["refunds"][0]["external_reference"] == "RF-1"
    assert_ledger_balanced(body)
    # 终局后不可再收款
    blocked = client.post(
        f"/api/preneed/contracts/{detail['id']}/receipts?role=cashier",
        json={"amount_cents": 1000, "channel": "bank", "external_reference": "TR-X", "received_by": "cashier-qian"},
    )
    assert blocked.status_code == 409


def test_beneficiary_transfer_recorded_as_memo_event(client):
    detail = create_contract(client, "PN-TX")
    transferred = client.post(
        f"/api/preneed/contracts/{detail['id']}/transfer?role=customer_service",
        json={"to_beneficiary_name": "陈晓", "to_beneficiary_identity": "ID-BEN-002", "to_relation": "子女",
              "reason": "原受益人年迈改由子女承接", "transferred_by": "customer-service-sun",
              "customer_confirmer": "王秀兰与陈德海共同签字"},
    )
    assert transferred.status_code == 200
    body = transferred.json()
    assert body["beneficiary_identity"] == "ID-BEN-002"
    assert body["transfers"][0]["from_beneficiary_identity"] == "ID-BEN-001"
    assert any(e["event_type"] == "contract.transferred" for e in body["ledger"])


def test_convert_after_death_creates_case_orders_and_is_idempotent(client):
    detail = create_contract(client, "PN-CONV")
    pay(client, detail["id"], "CV-1", 140000)
    converted = client.post(
        f"/api/preneed/contracts/{detail['id']}/convert?role=customer_service", json=convert_payload()
    )
    assert converted.status_code == 200, converted.text
    body = converted.json()
    assert body["case"]["external_ref"] == "CASE-PN-001"
    assert body["case"]["decedent_name"] == "陈德海"
    assert len(body["orders"]) == 3  # 两项冻结服务 + 一条销售折让
    assert {o["status"] for o in body["orders"]} == {"invoiced"}
    assert sum(o["amount_cents"] for o in body["orders"]) == 140000
    assert body["invoice"]["amount_cents"] == 140000
    assert body["invoice"]["status"] == "paid"
    assert body["invoice"]["paid_cents"] == 140000
    # 幂等：重复提交不重复建档
    replay = client.post(
        f"/api/preneed/contracts/{detail['id']}/convert?role=customer_service", json=convert_payload()
    )
    assert replay.status_code == 200 and replay.json()["case"]["id"] == body["case"]["id"]
    # 换键再来则被防重拒绝
    again = client.post(
        f"/api/preneed/contracts/{detail['id']}/convert?role=customer_service", json=convert_payload(key="conv-again", ref="CASE-PN-002")
    )
    assert again.status_code == 409
    case_detail = client.get(f"/api/mortuary/cases/{body['case']['id']}").json()
    assert case_detail["service_orders"][0]["notes"].startswith("生前契约 PN-CONV V1")
    contract = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()
    assert contract["status"] == "converted"
    assert_ledger_balanced(contract)


def test_convert_preserves_unpaid_balance_as_receivable(client):
    detail = create_contract(client, "PN-GAP")
    pay(client, detail["id"], "GP-1", 100000)
    converted = client.post(
        f"/api/preneed/contracts/{detail['id']}/convert?role=customer_service",
        json=convert_payload(key="conv-gap", ref="CASE-GAP-001"),
    )
    assert converted.status_code == 200
    invoice = converted.json()["invoice"]
    assert invoice["status"] == "partially_paid"
    assert invoice["amount_cents"] == 140000 and invoice["paid_cents"] == 100000
    ledger = client.get(f"/api/preneed/contracts/{detail['id']}?role=auditor").json()["ledger"]
    accounts = {e["account"] for e in ledger if e["event_type"] == "contract.converted"}
    assert "accounts_receivable" in accounts


def test_convert_requires_active_contract(client):
    detail = create_contract(client, "PN-BLOCK")
    client.post(
        f"/api/preneed/contracts/{detail['id']}/suspend?role=customer_service",
        json={"reason": "等待材料", "actor": "customer-service-sun"},
    )
    blocked = client.post(
        f"/api/preneed/contracts/{detail['id']}/convert?role=customer_service",
        json=convert_payload(key="conv-block", ref="CASE-BLOCK-001"),
    )
    assert blocked.status_code == 409


def test_point_in_time_liability_and_version_history(client):
    del client  # 仅用于初始化临时数据库
    clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=UTC))
    service = PreneedService(get_connection(), clock)
    created = service.create_contract(contract_payload("PN-ASOF"), "consultant")
    cid = created["id"]
    clock.advance(days=9)
    service.receive_payment(cid, {
        "amount_cents": 70000, "channel": "bank", "external_reference": "AF-1", "received_by": "cashier-qian",
    }, "cashier")
    clock.advance(days=5)
    proposed = service.propose_amendment(cid, {
        "change_reason": "增补骨灰暂存服务", "proposed_by": "consultant-zhao",
        "service_items": ITEMS + [
            {"service_code": "ash-storage", "service_name": "骨灰暂存一年", "quantity": 1, "unit_price_cents": 20000,
             "frozen": True}],
    }, "consultant")
    clock.advance(days=5)
    service.confirm_version(cid, proposed["id"], {
        "customer_confirmer": "王秀兰确认增补", "confirmed_by": "customer-service-sun",
    }, "customer_service")

    before_paid = service.contract_as_of(cid, datetime(2026, 9, 5, 0, 0, tzinfo=UTC), "auditor")
    assert before_paid["effective_version_no"] == 1
    assert before_paid["liability"]["package_total_cents"] == 140000
    assert before_paid["paid_cents"] == 0
    snapshot = service.contract_as_of(cid, datetime(2026, 9, 15, 0, 0, tzinfo=UTC), "auditor")
    assert snapshot["effective_version_no"] == 1
    assert snapshot["paid_cents"] == 70000
    after = service.contract_as_of(cid, datetime(2030, 1, 1, tzinfo=UTC), "auditor")
    assert after["effective_version_no"] == 2
    assert after["liability"]["package_total_cents"] == 160000
    assert after["paid_cents"] == 70000
    assert after["status"] == "active"
    full = service.get_contract(cid, role="auditor")
    assert [v["version_no"] for v in full["versions"]] == [1, 2]
    assert full["versions"][0]["service_items"][0]["unit_price_cents"] == 120000  # 旧价格永久留存
    try:
        service.contract_as_of(cid, datetime(2026, 8, 1, tzinfo=UTC), "auditor")
        assert False, "签约前查询应当失败"
    except Exception as exc:
        assert exc.status_code == 404
