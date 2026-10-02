from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from app.preneed.schemas import (
    AmendmentConfirm,
    AmendmentPropose,
    ContractCreate,
    ConversionCreate,
    ReceiptCreate,
    RefundPay,
    ResumeRequest,
    SuspendRequest,
    TerminateRequest,
    TransferApply,
    TransferReject,
)
from app.preneed.service import PreneedService

router = APIRouter(prefix="/api/preneed", tags=["preneed"])

_ACTOR = Query(min_length=2, max_length=80, description="经办人")
_ROLE = Query(min_length=2, max_length=80, description="岗位角色")


@router.post("/contracts", status_code=201)
def create_contract(payload: ContractCreate, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().create_contract(payload.model_dump(), actor, role)


@router.get("/contracts")
def list_contracts(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return PreneedService().list_contracts(status, limit)


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int) -> dict:
    return PreneedService().get_contract(contract_id)


@router.get("/contracts/{contract_id}/versions")
def get_versions(contract_id: int) -> list[dict]:
    return PreneedService().get_versions(contract_id)


@router.get("/contracts/{contract_id}/snapshot")
def snapshot(contract_id: int, as_of: datetime) -> dict:
    return PreneedService().snapshot_at(contract_id, as_of)


@router.post("/contracts/{contract_id}/receipts", status_code=201)
def record_receipt(contract_id: int, payload: ReceiptCreate, role: str = _ROLE) -> dict:
    return PreneedService().record_receipt(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/overdue")
def mark_overdue(contract_id: int, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().mark_overdue(contract_id, actor, role)


@router.post("/contracts/{contract_id}/amendments", status_code=201)
def propose_amendment(contract_id: int, payload: AmendmentPropose, role: str = _ROLE) -> dict:
    return PreneedService().propose_amendment(contract_id, payload.model_dump(), role)


@router.post("/amendments/{version_id}/confirm")
def confirm_amendment(version_id: int, payload: AmendmentConfirm, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().confirm_amendment(version_id, payload.model_dump(), actor, role)


@router.post("/amendments/{version_id}/discard")
def discard_amendment(version_id: int, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().discard_amendment(version_id, actor, role)


@router.post("/contracts/{contract_id}/suspend")
def suspend(contract_id: int, payload: SuspendRequest, role: str = _ROLE) -> dict:
    return PreneedService().suspend(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/resume")
def resume(contract_id: int, payload: ResumeRequest, role: str = _ROLE) -> dict:
    return PreneedService().resume(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/terminate")
def terminate(contract_id: int, payload: TerminateRequest, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().terminate(contract_id, payload.model_dump(), actor, role)


@router.post("/refunds/{refund_id}/pay")
def pay_refund(refund_id: int, payload: RefundPay, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().pay_refund(refund_id, payload.model_dump(), actor, role)


@router.post("/contracts/{contract_id}/transfers", status_code=201)
def apply_transfer(contract_id: int, payload: TransferApply, role: str = _ROLE) -> dict:
    return PreneedService().apply_transfer(contract_id, payload.model_dump(), role)


@router.post("/transfers/{transfer_id}/approve")
def approve_transfer(transfer_id: int, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().approve_transfer(transfer_id, actor, role)


@router.post("/transfers/{transfer_id}/reject")
def reject_transfer(transfer_id: int, payload: TransferReject, role: str = _ROLE) -> dict:
    return PreneedService().reject_transfer(transfer_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/convert", status_code=201)
def convert_to_case(contract_id: int, payload: ConversionCreate, actor: str = _ACTOR, role: str = _ROLE) -> dict:
    return PreneedService().convert_to_case(contract_id, payload.model_dump(), actor, role)
