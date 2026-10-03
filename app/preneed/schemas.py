from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ContractStatus(str, Enum):
    active = "active"
    overdue = "overdue"
    suspended = "suspended"
    terminated = "terminated"
    transferred = "transferred"
    converted = "converted"
    void = "void"


class ServiceItemInput(BaseModel):
    service_code: str = Field(min_length=2, max_length=60)
    service_name: str = Field(min_length=2, max_length=120)
    quantity: int = Field(default=1, ge=1, le=100)
    unit_price_cents: int = Field(ge=0, le=100_000_000)
    frozen: bool = True


class InstallmentPlanInput(BaseModel):
    number: int = Field(ge=1, le=120)
    first_due_on: date
    interval_months: int = Field(default=1, ge=1, le=60)


class RefundRuleInput(BaseModel):
    admin_fee_bps: int = Field(default=0, ge=0, le=10_000)
    performance_penalty_bps: int = Field(default=0, ge=0, le=10_000)
    note: str = Field(default="", max_length=500)


class ContractCreate(BaseModel):
    contract_no: str = Field(min_length=3, max_length=80)
    plan_code: str = Field(min_length=2, max_length=40)
    plan_name: str = Field(min_length=2, max_length=120)
    customer_name: str = Field(min_length=2, max_length=120)
    customer_identity: str = Field(min_length=4, max_length=80)
    customer_phone: str = Field(min_length=5, max_length=40)
    beneficiary_name: str = Field(min_length=2, max_length=120)
    beneficiary_identity: str = Field(min_length=4, max_length=80)
    beneficiary_relation: str = Field(default="", max_length=40)
    price_list_code: str = Field(min_length=2, max_length=60)
    price_list_effective_on: date
    service_items: list[ServiceItemInput] = Field(min_length=1, max_length=100)
    sales_discount_cents: int = Field(default=0, ge=0, le=100_000_000)
    refund_rule: RefundRuleInput = Field(default_factory=RefundRuleInput)
    installment_plan: InstallmentPlanInput | None = None
    grace_days: int = Field(default=0, ge=0, le=365)
    created_by: str = Field(min_length=2, max_length=80)
    customer_confirmer: str = Field(min_length=2, max_length=120)

    @model_validator(mode="after")
    def validate_discount(self):
        total = sum(item.quantity * item.unit_price_cents for item in self.service_items)
        if self.sales_discount_cents > total:
            raise ValueError("销售折让不能超过服务清单合计金额")
        return self


class ContractAmend(BaseModel):
    change_reason: str = Field(min_length=4, max_length=500)
    price_list_code: str | None = Field(default=None, min_length=2, max_length=60)
    price_list_effective_on: date | None = None
    service_items: list[ServiceItemInput] | None = Field(default=None, min_length=1, max_length=100)
    sales_discount_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    refund_rule: RefundRuleInput | None = None
    installment_plan: InstallmentPlanInput | None = None
    grace_days: int | None = Field(default=None, ge=0, le=365)
    proposed_by: str = Field(min_length=2, max_length=80)

    @model_validator(mode="after")
    def validate_change(self):
        if self.service_items is not None and self.sales_discount_cents is not None:
            total = sum(item.quantity * item.unit_price_cents for item in self.service_items)
            if self.sales_discount_cents > total:
                raise ValueError("销售折让不能超过服务清单合计金额")
        if not any(value is not None for key, value in self.model_dump().items() if key not in {"change_reason", "proposed_by"}):
            raise ValueError("变更必须至少修改一项内容")
        return self


class VersionConfirm(BaseModel):
    customer_confirmer: str = Field(min_length=2, max_length=120)
    confirmed_by: str = Field(min_length=2, max_length=80)


class VersionReject(BaseModel):
    rejection_reason: str = Field(min_length=4, max_length=500)
    rejected_by: str = Field(min_length=2, max_length=80)


class ReceiptCreate(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    channel: str = Field(min_length=2, max_length=40)
    external_reference: str = Field(min_length=4, max_length=120)
    received_by: str = Field(min_length=2, max_length=80)
    installment_seq: int | None = Field(default=None, ge=1, le=120)


class RefundCreate(BaseModel):
    amount_cents: int | None = Field(default=None, gt=0, le=100_000_000)
    reason: str = Field(min_length=4, max_length=500)
    external_reference: str = Field(min_length=4, max_length=120)
    handled_by: str = Field(min_length=2, max_length=80)


class SuspendAction(BaseModel):
    reason: str = Field(min_length=4, max_length=500)
    actor: str = Field(min_length=2, max_length=80)


class ResumeAction(BaseModel):
    actor: str = Field(min_length=2, max_length=80)


class TerminateAction(BaseModel):
    reason: str = Field(min_length=4, max_length=500)
    actor: str = Field(min_length=2, max_length=80)
    refund: RefundCreate | None = None


class TransferAction(BaseModel):
    to_beneficiary_name: str = Field(min_length=2, max_length=120)
    to_beneficiary_identity: str = Field(min_length=4, max_length=80)
    to_relation: str = Field(default="", max_length=40)
    reason: str = Field(min_length=4, max_length=500)
    transferred_by: str = Field(min_length=2, max_length=80)
    customer_confirmer: str = Field(min_length=2, max_length=120)


class ConvertAction(BaseModel):
    external_ref: str = Field(min_length=3, max_length=80)
    death_time: datetime
    received_from: str = Field(min_length=2, max_length=160)
    family_contact: str = Field(min_length=2, max_length=120)
    family_phone: str = Field(min_length=5, max_length=40)
    special_notes: str = Field(default="", max_length=2000)
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
