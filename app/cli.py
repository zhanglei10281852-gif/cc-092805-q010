from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_mortuary_demo() -> int:
    with TestClient(app) as client:
        case = client.post(
            "/api/mortuary/cases?actor=cli-intake",
            json={"external_ref": "CLI-DEMO-001", "decedent_name": "演示档案", "identity_number": None,
                  "death_time": "2026-09-28T08:00:00Z", "received_from": "合作医院", "family_contact": "演示联系人",
                  "family_phone": "13800000000", "special_notes": "CLI 冒烟数据"},
        )
        resource = client.post(
            "/api/mortuary/resources?actor=cli-scheduler",
            json={"code": "CLI-HALL", "name": "演示送别厅", "kind": "farewell_hall", "site_code": "CLI-SITE", "capacity": 1, "attributes": {}},
        )
        cases = client.get("/api/mortuary/cases")
        resources = client.get("/api/mortuary/resources")
    result = {"case_status": case.status_code, "resource_status": resource.status_code, "cases": len(cases.json()), "resources": len(resources.json())}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if case.status_code in {201, 409} and resource.status_code in {201, 409} and cases.status_code == 200 and resources.status_code == 200 else 1


def command_preneed_demo() -> int:
    payload = {
        "contract_no": "PN-CLI-001",
        "plan_code": "PEACE-A",
        "plan_name": "安宁套餐甲",
        "customer_name": "演示客户",
        "customer_phone": "13900000000",
        "beneficiary_name": "演示受益人",
        "items": [
            {"service_code": "body-care", "service_name": "遗体护理", "quantity": 1, "unit_price_cents": 180000},
            {"service_code": "farewell-hall", "service_name": "送别厅", "quantity": 1, "unit_price_cents": 120000},
        ],
        "price_basis": {"price_list": "2026版"},
        "installments": [
            {"period_no": 1, "due_date": "2026-11-01", "amount_cents": 150000},
            {"period_no": 2, "due_date": "2027-01-01", "amount_cents": 150000},
        ],
        "signed_by": "cli-sales",
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/preneed/contracts?actor=cli-sales&role=preneed_clerk", json=payload)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        contracts = client.get("/api/preneed/contracts")
    result = {"contract_status": created.status_code, "contracts": len(contracts.json())}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if created.status_code in {201, 409} and contracts.status_code == 200 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="peaceful-care-operations", description="安宁礼仪与公墓运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("mortuary-demo", help="执行殡葬业务 API 冒烟检查")
    subparsers.add_parser("preneed-demo", help="执行生前契约 API 冒烟检查")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "mortuary-demo": command_mortuary_demo,
        "preneed-demo": command_preneed_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
