from __future__ import annotations

CLERK = "preneed_clerk"
LEAD = "customer_service_lead"
CASHIER = "finance_cashier"
MANAGER = "finance_manager"


def make_contract(client, no: str = "PN-001", total: int = 300000, refund_rule: dict | None = None,
                  actor: str = "sales-zhao", due_dates: tuple[str, str, str] = ("2026-09-01", "2026-11-01", "2027-01-01"),
                  signed_at: str | None = None) -> dict:
    third = total // 3
    amounts = [third, third, total - 2 * third]
    payload = {
        "contract_no": no,
        "plan_code": "PEACE-A",
        "plan_name": "安宁套餐甲",
        "customer_name": "客户王福海",
        "customer_phone": "13900000001",
        "customer_identity": "ID-1101-0001",
        "customer_address": "幸福路 1 号",
        "beneficiary_name": "王福海本人",
        "beneficiary_identity": "ID-1101-0001",
        "beneficiary_phone": "13900000001",
        "relationship": "本人",
        "items": [
            {"service_code": "body-care", "service_name": "遗体护理", "quantity": 1, "unit_price_cents": total - 120000},
            {"service_code": "farewell-hall", "service_name": "送别厅", "quantity": 1, "unit_price_cents": 120000},
        ],
        "price_basis": {"price_list": "2026版", "locked_at": "2026-09-01", "currency": "CNY"},
        "refund_rule": refund_rule or {"cooling_days": 7, "cooling_rate_permille": 1000,
                                       "after_cooling_rate_permille": 700,
                                       "after_overdue_rate_permille": 300},
        "installments": [
            {"period_no": 1, "due_date": due_dates[0], "amount_cents": amounts[0]},
            {"period_no": 2, "due_date": due_dates[1], "amount_cents": amounts[1]},
            {"period_no": 3, "due_date": due_dates[2], "amount_cents": amounts[2]},
        ],
        "signed_by": actor,
    }
    if signed_at:
        payload["signed_at"] = signed_at
    response = client.post(f"/api/preneed/contracts?actor={actor}&role={CLERK}", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_sign_freezes_items_price_and_installments(client):
    contract = make_contract(client)
    assert contract["status"] == "active"
    assert contract["total_cents"] == 300000
    assert contract["outstanding_cents"] == 300000
    version = contract["versions"][0]
    assert version["version_no"] == 1 and version["confirmed"] is True
    assert version["price_basis"]["price_list"] == "2026版"
    assert len(contract["installments"]) == 3
    # 签约即确认全额合同义务与应收
    entries = contract["accounting_entries"]
    assert entries[0]["debit_account"] == "应收契约款"
    assert entries[0]["credit_account"] == "合同负债"
    assert entries[0]["amount_cents"] == 300000
    assert contract["next_action"] == "缴纳第 1 期款项"


def test_installment_plan_must_equal_total(client):
    payload = {
        "contract_no": "PN-BAD-1", "plan_code": "PLAN-BAD", "plan_name": "套餐",
        "customer_name": "客户钱某", "customer_phone": "13900000002",
        "beneficiary_name": "受益人钱某",
        "items": [{"service_code": "s1", "service_name": "项目一", "quantity": 1, "unit_price_cents": 100000}],
        "installments": [{"period_no": 1, "due_date": "2026-11-01", "amount_cents": 90000}],
        "signed_by": "sales-zhao",
    }
    response = client.post(f"/api/preneed/contracts?actor=sales-zhao&role={CLERK}", json=payload)
    assert response.status_code == 422


def test_role_based_permissions(client):
    contract = make_contract(client, "PN-PERM-1")
    # 非契约经办员不能签约：契约号换新
    payload = {
        "contract_no": "PN-PERM-2", "plan_code": "PLAN-X", "plan_name": "套餐",
        "customer_name": "客户孙某", "customer_phone": "13900000003",
        "beneficiary_name": "受益人孙某",
        "items": [{"service_code": "s1", "service_name": "项目一", "quantity": 1, "unit_price_cents": 100000}],
        "installments": [{"period_no": 1, "due_date": "2026-11-01", "amount_cents": 100000}],
        "signed_by": "cashier-li",
    }
    denied = client.post(f"/api/preneed/contracts?actor=cashier-li&role={CASHIER}", json=payload)
    assert denied.status_code == 403
    # 出纳以外不能收款
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-PERM-1", "received_by": "cashier-li"}
    assert client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CLERK}", json=receipt).status_code == 403
    # 只有主管能暂停
    suspend = {"reason": "客户申请暂缓", "operated_by": "sales-zhao", "customer_confirmed_name": "客户王福海"}
    assert client.post(f"/api/preneed/contracts/{contract['id']}/suspend?role={CLERK}", json=suspend).status_code == 403


def test_receipts_allocate_idempotently_and_clear_overdue(client):
    contract = make_contract(client, "PN-PAY-1")
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-001", "received_by": "cashier-li"}
    first = client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    assert first.status_code == 201
    detail = first.json()
    assert detail["paid_cents"] == 100000
    assert detail["installments"][0]["status"] == "paid"
    # 重复流水号幂等返回，不重复入账
    repeated = client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    assert repeated.json()["paid_cents"] == 100000
    assert len(repeated.json()["receipts"]) == 1
    # 逾期契约：前两期均已过期未缴 → 标记逾期；逐期补齐后自动恢复有效
    late = make_contract(client, "PN-PAY-LATE",
                         due_dates=("2026-07-01", "2026-08-01", "2026-09-01"))
    overdue = client.post(f"/api/preneed/contracts/{late['id']}/overdue?actor=batch&role=system_batch")
    assert overdue.status_code == 200 and overdue.json()["status"] == "overdue"
    pay1 = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-LATE-1", "received_by": "cashier-li"}
    after1 = client.post(f"/api/preneed/contracts/{late['id']}/receipts?role={CASHIER}", json=pay1)
    assert after1.json()["status"] == "overdue"  # 第 1 期结清，第 2、3 期仍逾期
    pay2 = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-LATE-2", "received_by": "cashier-li"}
    after2 = client.post(f"/api/preneed/contracts/{late['id']}/receipts?role={CASHIER}", json=pay2)
    assert after2.json()["status"] == "overdue"
    pay3 = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-LATE-3", "received_by": "cashier-li"}
    after3 = client.post(f"/api/preneed/contracts/{late['id']}/receipts?role={CASHIER}", json=pay3)
    assert after3.json()["status"] == "active"  # 逾期期次全部补齐，自动恢复


def test_suspended_contract_cannot_receive_money(client):
    contract = make_contract(client, "PN-SUS-1")
    suspend = {"reason": "家庭纠纷待裁", "operated_by": "lead-qin", "customer_confirmed_name": "客户王福海"}
    assert client.post(f"/api/preneed/contracts/{contract['id']}/suspend?role={LEAD}", json=suspend).status_code == 200
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-SUS-1", "received_by": "cashier-li"}
    assert client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt).status_code == 409
    resume = {"reason": "纠纷解决", "operated_by": "lead-qin", "customer_confirmed_name": "客户王福海"}
    assert client.post(f"/api/preneed/contracts/{contract['id']}/resume?role={LEAD}", json=resume).status_code == 200


def test_amendment_creates_new_version_and_keeps_old_promise(client):
    contract = make_contract(client, "PN-AMD-1")
    propose = {
        "items": [
            {"service_code": "body-care", "service_name": "遗体护理", "quantity": 1, "unit_price_cents": 180000},
            {"service_code": "farewell-hall", "service_name": "送别厅", "quantity": 1, "unit_price_cents": 120000},
        ],
        "price_basis": {"price_list": "2026版", "deduct_note": "减免护理费用"},
        "reason": "客户申请减免护理项目",
        "proposed_by": "sales-zhao",
    }
    proposed = client.post(f"/api/preneed/contracts/{contract['id']}/amendments?role={CLERK}", json=propose)
    assert proposed.status_code == 201, proposed.text
    version_id = proposed.json()["id"]
    # 未确认前契约总价仍是旧承诺
    pending = client.get(f"/api/preneed/contracts/{contract['id']}").json()
    assert pending["total_cents"] == 300000
    # 未确认版本不能再提新变更
    again = client.post(f"/api/preneed/contracts/{contract['id']}/amendments?role={CLERK}", json=propose)
    assert again.status_code == 409
    confirm = {"customer_name": "客户王福海", "confirmed_by": "sales-zhao", "confirmation_ref": "SMS-CODE-001"}
    confirmed = client.post(f"/api/preneed/amendments/{version_id}/confirm?actor=sales-zhao&role={CLERK}", json=confirm)
    assert confirmed.status_code == 200, confirmed.text
    detail = confirmed.json()
    assert detail["current_version"] == 2
    assert detail["total_cents"] == 300000  # 180000+120000
    # 旧版本完整保留，仍是签约时冻结的清单与价格
    old = detail["versions"][0]
    assert old["change_type"] == "sign" and old["total_cents"] == 300000
    assert old["items"][0]["unit_price_cents"] == 180000
    # 减价 0（示例）；再做一次真实减价，差额并入末期
    propose2 = {
        "items": [
            {"service_code": "body-care", "service_name": "遗体护理", "quantity": 1, "unit_price_cents": 80000},
            {"service_code": "farewell-hall", "service_name": "送别厅", "quantity": 1, "unit_price_cents": 120000},
        ],
        "price_basis": {"price_list": "2026版"},
        "reason": "再次减免", "proposed_by": "sales-zhao",
    }
    v2 = client.post(f"/api/preneed/contracts/{contract['id']}/amendments?role={CLERK}", json=propose2).json()
    client.post(f"/api/preneed/amendments/{v2['id']}/confirm?actor=sales-zhao&role={CLERK}",
                json={"customer_name": "客户王福海", "confirmed_by": "sales-zhao", "confirmation_ref": "SMS-CODE-002"})
    final = client.get(f"/api/preneed/contracts/{contract['id']}").json()
    assert final["total_cents"] == 200000
    assert final["outstanding_cents"] == 200000 - final["paid_cents"]
    assert len(final["versions"]) == 3
    # 减价分录：借记合同负债、贷记应收
    events = [e["event_type"] for e in final["timeline"]]
    assert events.count("amendment.confirmed") == 2


def test_amendment_with_installment_replan(client):
    contract = make_contract(client, "PN-AMD-2")
    # 先缴清第 1 期
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-AMD2-1", "received_by": "cashier-li"}
    client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    propose = {
        "items": [{"service_code": "body-care", "service_name": "遗体护理", "quantity": 1, "unit_price_cents": 400000}],
        "price_basis": {"price_list": "2026版"},
        "reason": "升级套餐",
        "proposed_by": "sales-zhao",
        "installments": [
            {"period_no": 1, "due_date": "2026-09-01", "amount_cents": 100000},
            {"period_no": 2, "due_date": "2026-11-01", "amount_cents": 150000},
            {"period_no": 3, "due_date": "2027-02-01", "amount_cents": 150000},
        ],
    }
    v = client.post(f"/api/preneed/contracts/{contract['id']}/amendments?role={CLERK}", json=propose)
    assert v.status_code == 201, v.text
    confirmed = client.post(f"/api/preneed/amendments/{v.json()['id']}/confirm?actor=sales-zhao&role={CLERK}",
                            json={"customer_name": "客户王福海", "confirmed_by": "sales-zhao",
                                  "confirmation_ref": "SMS-AMD2"})
    assert confirmed.status_code == 200
    detail = confirmed.json()
    assert detail["total_cents"] == 400000
    assert [p["amount_cents"] for p in sorted(detail["installments"], key=lambda x: x["period_no"])] == [100000, 150000, 150000]
    # 不能改动已缴清期次
    bad = dict(propose, installments=[
        {"period_no": 1, "due_date": "2026-09-01", "amount_cents": 90000},
        {"period_no": 2, "due_date": "2026-11-01", "amount_cents": 155000},
        {"period_no": 3, "due_date": "2027-02-01", "amount_cents": 155000},
    ], reason="试图改已缴期")
    resp = client.post(f"/api/preneed/contracts/{contract['id']}/amendments?role={CLERK}", json=bad)
    assert resp.status_code == 422


def test_terminate_requires_separation_and_records_refund(client):
    # 冷静期外：退 70%
    contract = make_contract(client, "PN-TERM-1", signed_at="2026-09-01", refund_rule={
        "cooling_days": 0, "cooling_rate_permille": 1000,
        "after_cooling_rate_permille": 700, "after_overdue_rate_permille": 300})
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-TERM-1", "received_by": "cashier-li"}
    client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    payload = {"reason": "全家迁居国外", "applied_by": "sales-zhao", "approved_by": "manager-sun"}
    # 经办审批不能同一人
    same = dict(payload, approved_by="sales-zhao")
    assert client.post(f"/api/preneed/contracts/{contract['id']}/terminate?actor=sales-zhao&role={MANAGER}",
                       json=same).status_code == 403
    # 非财务主管无权解除
    assert client.post(f"/api/preneed/contracts/{contract['id']}/terminate?actor=manager-sun&role={LEAD}",
                       json=payload).status_code == 403
    terminated = client.post(f"/api/preneed/contracts/{contract['id']}/terminate?actor=manager-sun&role={MANAGER}",
                             json=payload)
    assert terminated.status_code == 200, terminated.text
    detail = terminated.json()
    assert detail["status"] == "terminated"
    refund = detail["refunds"][-1]
    assert refund["payable_cents"] == 70000 and refund["penalty_cents"] == 30000
    # 解除后旧版本与承诺仍可追溯
    assert detail["versions"][0]["change_type"] == "sign"
    assert detail["versions"][-1]["change_type"] == "terminate"
    # 出纳支付退款，复式分录结清应退科目
    paid = client.post(f"/api/preneed/refunds/{refund['id']}/pay?actor=cashier-li&role={CASHIER}",
                       json={"payment_reference": "REF-TERM-1", "paid_by": "cashier-li"})
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    final = client.get(f"/api/preneed/contracts/{contract['id']}").json()
    assert final["next_action"] == "契约已解除并结清"


def test_transfer_preserves_old_contract_and_moves_funds(client):
    contract = make_contract(client, "PN-TR-1")
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-TR-1", "received_by": "cashier-li"}
    client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    apply = {
        "new_beneficiary_name": "王小明", "new_beneficiary_identity": "ID-1101-0099",
        "new_beneficiary_phone": "13900000099", "relationship": "子女",
        "reason": "原受益人指定子女承接", "applied_by": "sales-zhao",
        "customer_confirmation_ref": "CONFIRM-TR-1", "customer_confirmed_by": "客户王福海",
    }
    applied = client.post(f"/api/preneed/contracts/{contract['id']}/transfers?role={CLERK}", json=apply)
    assert applied.status_code == 201, applied.text
    transfer_id = applied.json()["id"]
    # 经办员自己不能批准
    assert client.post(f"/api/preneed/transfers/{transfer_id}/approve?actor=sales-zhao&role={CLERK}").status_code == 403
    approved = client.post(f"/api/preneed/transfers/{transfer_id}/approve?actor=lead-qin&role={LEAD}")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    new_contract = body["new_contract"]
    assert new_contract["contract_no"] == "PN-TR-1-T1"
    assert new_contract["beneficiary_name"] == "王小明"
    # 清单与价格冻结不变，已缴资金继承，未缴余额保留
    assert new_contract["total_cents"] == 300000
    assert new_contract["paid_cents"] == 100000
    assert new_contract["outstanding_cents"] == 200000
    assert new_contract["versions"][0]["items"][0]["service_code"] == "body-care"
    # 原契约历史完整保留、状态为已转让
    old = client.get(f"/api/preneed/contracts/{contract['id']}").json()
    assert old["status"] == "transferred"
    assert old["versions"][-1]["change_type"] == "transfer"
    assert any(e["event_type"] == "contract.transferred" for e in old["timeline"])
    # 新契约可以继续收缴
    receipt2 = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-TR-2", "received_by": "cashier-li"}
    ok = client.post(f"/api/preneed/contracts/{new_contract['id']}/receipts?role={CASHIER}", json=receipt2)
    assert ok.status_code == 201


def test_conversion_only_after_death_is_idempotent_guard_and_keeps_balance(client):
    contract = make_contract(client, "PN-CV-1")
    receipt = {"amount_cents": 100000, "channel": "bank", "external_reference": "R-CV-1", "received_by": "cashier-li"}
    client.post(f"/api/preneed/contracts/{contract['id']}/receipts?role={CASHIER}", json=receipt)
    payload = {
        "external_ref": "CASE-CV-1", "death_cert_ref": "DC-2026-1001",
        "decedent_name": "王福海本人", "death_time": "2026-10-01T06:00:00Z",
        "received_from": "市第一医院", "family_contact": "王小明", "family_phone": "13900000099",
        "special_notes": "按生前契约履行", "handled_by": "sales-zhao", "approved_by": "lead-qin",
    }
    # 死亡时间在未来 → 拒绝
    future = dict(payload, death_time="2099-01-01T00:00:00Z", external_ref="CASE-CV-FUT")
    assert client.post(f"/api/preneed/contracts/{contract['id']}/convert?actor=lead-qin&role={LEAD}",
                       json=future).status_code == 422
    # 经办与复核不能同人
    self_approved = dict(payload, approved_by="sales-zhao")
    assert client.post(f"/api/preneed/contracts/{contract['id']}/convert?actor=sales-zhao&role={LEAD}",
                       json=self_approved).status_code == 403
    converted = client.post(f"/api/preneed/contracts/{contract['id']}/convert?actor=lead-qin&role={LEAD}",
                            json=payload)
    assert converted.status_code == 201, converted.text
    body = converted.json()
    case_id = body["case_id"]
    assert body["status"] == "converted"
    assert body["conversion"]["unfulfilled_cents"] == 200000
    # 订单按签约冻结版本价格生成且为已确认
    case = client.get(f"/api/mortuary/cases/{case_id}").json()
    assert len(case["service_orders"]) == 2
    assert sum(o["amount_cents"] for o in case["service_orders"]) == 300000
    assert all(o["status"] == "confirmed" for o in case["service_orders"])
    # 禁止重复转换
    again = client.post(f"/api/preneed/contracts/{contract['id']}/convert?actor=lead-qin&role={LEAD}",
                        json=dict(payload, external_ref="CASE-CV-2", death_cert_ref="DC-2"))
    assert again.status_code == 409
    # 同一业务档案编号不能被第二个契约占用
    other = make_contract(client, "PN-CV-2")
    clash = client.post(f"/api/preneed/contracts/{other['id']}/convert?actor=lead-qin&role={LEAD}", json=payload)
    assert clash.status_code == 409


def test_point_in_time_snapshot(client):
    contract = make_contract(client, "PN-SNAP-1")
    # 当前时点重放：全额义务、零收款、第一期待缴
    early = client.get(f"/api/preneed/contracts/{contract['id']}/snapshot?as_of=2099-01-01T00:00:00Z")
    assert early.status_code == 200, early.text
    snap = early.json()
    assert snap["version_no"] == 1 and snap["paid_cents"] == 0
    assert snap["outstanding_cents"] == 300000
    assert snap["liability_cents"] == 300000
    assert snap["next_action"] == "缴纳第 1 期款项"
    # 签约之前不可查
    before = client.get(f"/api/preneed/contracts/{contract['id']}/snapshot?as_of=2020-01-01T00:00:00Z")
    assert before.status_code == 404
