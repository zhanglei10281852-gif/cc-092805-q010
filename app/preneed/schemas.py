from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ContractItem(BaseModel):
    service_code: str = Field(min_length=2, max_length=60)
    service_name: str = Field(min_length=1, max_length=160)
    quantity: int = Field(default=1, ge=1, le=100)
    unit_price_cents: int = Field(ge=0, le=100_000_000)


class InstallmentPlanItem(BaseModel):
    period_no: int = Field(ge=1, le=240)
    due_date: date
    amount_cents: int = Field(ge=0, le=100_000_000)


class RefundRule(BaseModel):
    # 签约后多少天内退订按冷静期处理
    cooling_days: int = Field(default=7, ge=0, le=365)
    # 各阶段退款比例（按已缴金额），不填区间默认 0
    cooling_rate_permille: int = Field(default=1000, ge=0, le=1000)
    after_cooling_rate_permille: int = Field(default=700, ge=0, le=1000)
    after_overdue_rate_permille: int = Field(default=300, ge=0, le=1000)
    note: str = Field(default="", max_length=500)


class ContractCreate(BaseModel):
    contract_no: str = Field(min_length=3, max_length=80)
    plan_code: str = Field(min_length=2, max_length=60)
    plan_name: str = Field(min_length=2, max_length=160)
    customer_name: str = Field(min_length=2, max_length=120)
    customer_phone: str = Field(min_length=5, max_length=40)
    customer_identity: str = Field(default="", max_length=80)
    customer_address: str = Field(default="", max_length=300)
    beneficiary_name: str = Field(min_length=2, max_length=120)
    beneficiary_identity: str = Field(default="", max_length=80)
    beneficiary_phone: str = Field(default="", max_length=40)
    relationship: str = Field(default="", max_length=40)
    items: list[ContractItem] = Field(min_length=1, max_length=100)
    price_basis: dict[str, Any] = Field(default_factory=dict, max_length=40)
    refund_rule: RefundRule = Field(default_factory=RefundRule)
    installments: list[InstallmentPlanItem] = Field(min_length=1, max_length=240)
    signed_at: date | None = None
    signed_by: str = Field(min_length=2, max_length=80)

    @model_validator(mode="after")
    def validate_plan(self):
        codes = [item.service_code for item in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("服务清单中不能出现重复的服务项目")
        numbers = [plan.period_no for plan in self.installments]
        if len(numbers) != len(set(numbers)):
            raise ValueError("分期期号不能重复")
        ordered = sorted(self.installments, key=lambda item: item.period_no)
        if [item.period_no for item in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("分期期号必须从 1 开始连续编号")
        for previous, current in zip(ordered, ordered[1:]):
            if current.due_date <= previous.due_date:
                raise ValueError("分期到期日必须严格递增")
        return self


class ReceiptCreate(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    channel: str = Field(min_length=2, max_length=40)
    external_reference: str = Field(min_length=4, max_length=120)
    received_by: str = Field(min_length=2, max_length=80)
    installment_id: int | None = Field(default=None, gt=0)
    received_at: date | None = None


class AmendmentPropose(BaseModel):
    items: list[ContractItem] = Field(min_length=1, max_length=100)
    price_basis: dict[str, Any] = Field(default_factory=dict, max_length=40)
    reason: str = Field(min_length=2, max_length=500)
    proposed_by: str = Field(min_length=2, max_length=80)
    # 可同步调整后续未缴分期；金额之和必须等于新总价
    installments: list[InstallmentPlanItem] | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_items(self):
        codes = [item.service_code for item in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("服务清单中不能出现重复的服务项目")
        if self.installments is not None:
            numbers = [plan.period_no for plan in self.installments]
            if len(numbers) != len(set(numbers)) or sorted(numbers) != list(range(1, len(numbers) + 1)):
                raise ValueError("分期期号必须从 1 开始连续编号")
        return self


class AmendmentConfirm(BaseModel):
    customer_name: str = Field(min_length=2, max_length=120)
    confirmed_by: str = Field(min_length=2, max_length=80)
    confirmation_ref: str = Field(min_length=4, max_length=120)


class TransferApply(BaseModel):
    new_beneficiary_name: str = Field(min_length=2, max_length=120)
    new_beneficiary_identity: str = Field(default="", max_length=80)
    new_beneficiary_phone: str = Field(default="", max_length=40)
    relationship: str = Field(default="", max_length=40)
    reason: str = Field(min_length=2, max_length=500)
    applied_by: str = Field(min_length=2, max_length=80)
    customer_confirmation_ref: str = Field(min_length=4, max_length=120)
    customer_confirmed_by: str = Field(min_length=2, max_length=120)


class TransferReject(BaseModel):
    rejected_by: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=2, max_length=500)


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    operated_by: str = Field(min_length=2, max_length=80)
    reason_detail: str = Field(default="", max_length=1000)
    customer_confirmed_name: str = Field(min_length=2, max_length=120)


class ResumeRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=200)
    operated_by: str = Field(min_length=2, max_length=80)
    customer_confirmed_name: str = Field(min_length=2, max_length=120)


class TerminateRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    applied_by: str = Field(min_length=2, max_length=80)
    approved_by: str = Field(min_length=2, max_length=80)


class RefundPay(BaseModel):
    payment_reference: str = Field(min_length=4, max_length=120)
    paid_by: str = Field(min_length=2, max_length=80)


class ConversionCreate(BaseModel):
    external_ref: str = Field(min_length=3, max_length=80)
    death_cert_ref: str = Field(min_length=3, max_length=120)
    decedent_name: str = Field(min_length=1, max_length=120)
    death_time: str = Field(min_length=8, max_length=40)
    received_from: str = Field(min_length=2, max_length=160)
    family_contact: str = Field(min_length=2, max_length=120)
    family_phone: str = Field(min_length=5, max_length=40)
    special_notes: str = Field(default="", max_length=2000)
    handled_by: str = Field(min_length=2, max_length=80)
    approved_by: str = Field(min_length=2, max_length=80)
