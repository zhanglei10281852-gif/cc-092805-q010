from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from app.preneed.schemas import (
    ContractAmend,
    ContractCreate,
    ConvertAction,
    ReceiptCreate,
    RefundCreate,
    ResumeAction,
    SuspendAction,
    TerminateAction,
    TransferAction,
    VersionConfirm,
    VersionReject,
)
from app.preneed.service import PreneedService

router = APIRouter(prefix="/api/preneed", tags=["preneed"])

ROLE = Query(min_length=2, max_length=40)


@router.post("/contracts", status_code=201)
def create_contract(payload: ContractCreate, role: str = ROLE) -> dict:
    return PreneedService().create_contract(payload.model_dump(), role)


@router.get("/contracts")
def list_contracts(role: str = ROLE, status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return PreneedService().list_contracts(status, limit, role)


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int, role: str = ROLE) -> dict:
    return PreneedService().get_contract(contract_id, role=role)


@router.get("/contracts/{contract_id}/as-of")
def contract_as_of(contract_id: int, at: datetime, role: str = ROLE) -> dict:
    return PreneedService().contract_as_of(contract_id, at, role)


@router.post("/contracts/{contract_id}/amendments", status_code=201)
def propose_amendment(contract_id: int, payload: ContractAmend, role: str = ROLE) -> dict:
    return PreneedService().propose_amendment(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/versions/{version_id}/confirm")
def confirm_version(contract_id: int, version_id: int, payload: VersionConfirm, role: str = ROLE) -> dict:
    return PreneedService().confirm_version(contract_id, version_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/versions/{version_id}/reject")
def reject_version(contract_id: int, version_id: int, payload: VersionReject, role: str = ROLE) -> dict:
    return PreneedService().reject_version(contract_id, version_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/receipts", status_code=201)
def receive_payment(contract_id: int, payload: ReceiptCreate, role: str = ROLE) -> dict:
    return PreneedService().receive_payment(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/suspend")
def suspend_contract(contract_id: int, payload: SuspendAction, role: str = ROLE) -> dict:
    return PreneedService().suspend(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/resume")
def resume_contract(contract_id: int, payload: ResumeAction, role: str = ROLE) -> dict:
    return PreneedService().resume(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/terminate")
def terminate_contract(contract_id: int, payload: TerminateAction, role: str = ROLE) -> dict:
    return PreneedService().terminate(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/transfer")
def transfer_contract(contract_id: int, payload: TransferAction, role: str = ROLE) -> dict:
    return PreneedService().transfer_beneficiary(contract_id, payload.model_dump(), role)


@router.post("/contracts/{contract_id}/convert")
def convert_contract(contract_id: int, payload: ConvertAction, role: str = ROLE) -> dict:
    return PreneedService().convert(contract_id, payload.model_dump(), role)


@router.post("/overdue/sweep")
def sweep_overdue(actor: str = Query(min_length=2, max_length=80), role: str = ROLE) -> dict:
    return PreneedService().sweep_overdue(actor, role)
