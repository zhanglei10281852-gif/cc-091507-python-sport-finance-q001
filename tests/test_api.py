from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server
from ledger.service import LedgerService


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        clock = lambda: datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)
        cls.service = LedgerService(runtime_dir=cls.tmp.name, clock=clock)
        cls.server = create_server("127.0.0.1", 0, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data, dict(resp.getheaders())


class HappyPathTest(ApiTestCase):
    def test_full_flow_over_http(self) -> None:
        status, health, _ = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", health["status"])

        status, athlete, _ = self.request(
            "POST", "/api/athletes", {"athlete_id": "ATH-1", "name": "林一"}
        )
        self.assertEqual(201, status)

        status, contract, _ = self.request(
            "POST",
            "/api/contracts",
            {
                "contract_id": "CT-1",
                "athlete_id": "ATH-1",
                "season": "2026",
                "currency": "CNY",
                "title": "运动饮料代言",
                "installments": [
                    {"installment_id": "P1", "due_date": "2026-01-15", "amount": "50000"},
                    {"installment_id": "P2", "due_date": "2026-04-15", "amount": "50000"},
                ],
                "thresholds": {"low_balance": "1000"},
            },
        )
        self.assertEqual(201, status)
        self.assertEqual("100000.000000", contract["figures"]["committed"])

        status, sub, _ = self.request(
            "POST",
            "/api/milestones/submit",
            {"contract_id": "CT-1", "installment_id": "P1", "business_date": "2026-01-05"},
        )
        self.assertEqual(200, status)
        status, appr, _ = self.request(
            "POST",
            "/api/milestones/approve",
            {"contract_id": "CT-1", "installment_id": "P1", "business_date": "2026-01-08"},
        )
        self.assertEqual(200, status)

        # 幂等键重放：第二次返回缓存响应并标记
        payment_body = {
            "contract_id": "CT-1",
            "amount": "50000",
            "bank_ref": "BK-2026-0001",
            "installment_id": "P1",
            "value_date": "2026-01-16",
        }
        status, pay1, _ = self.request(
            "POST", "/api/payments", payment_body, {"Idempotency-Key": "req-1"}
        )
        self.assertEqual(200, status)
        status, pay2, headers = self.request(
            "POST", "/api/payments", payment_body, {"Idempotency-Key": "req-1"}
        )
        self.assertEqual(200, status)
        self.assertEqual("true", headers.get("Idempotency-Replayed"))
        self.assertEqual(pay1, pay2)

        # 同一银行回单重传（无幂等键）：领域级幂等
        status, pay3, _ = self.request("POST", "/api/payments", payment_body)
        self.assertEqual(200, status)
        self.assertTrue(pay3["duplicate"])

        status, expense, _ = self.request(
            "POST",
            "/api/expenses",
            {
                "contract_id": "CT-1",
                "use": "training",
                "amount": "20000",
                "currency": "CNY",
                "business_date": "2026-02-10",
                "receipt_ref": "RC-100",
            },
        )
        self.assertEqual(200, status)

        status, tax, _ = self.request(
            "POST",
            "/api/tax-withholdings",
            {"contract_id": "CT-1", "amount": "5000", "business_date": "2026-02-11"},
        )
        self.assertEqual(200, status)

        status, balances, _ = self.request("GET", "/api/balances?athlete_id=ATH-1")
        self.assertEqual(200, status)
        totals = balances["totals_by_currency"]["CNY"]
        self.assertEqual("100000.000000", totals["committed"])
        self.assertEqual("25000.000000", totals["used"])
        self.assertEqual("25000.000000", totals["available"])

        status, snapshot, _ = self.request("POST", "/api/snapshots", {"month": "2026-02"})
        self.assertEqual(201, status)
        status, verify, _ = self.request(
            "GET", f"/api/snapshots/{snapshot['snapshot_id']}/verify"
        )
        self.assertEqual(200, status)
        self.assertTrue(verify["match"])

        status, tasks, _ = self.request("GET", "/api/tasks?status=pending")
        self.assertEqual(200, status)
        self.assertEqual(0, len(tasks["tasks"]))

        status, ledger, _ = self.request("GET", "/api/ledger?contract_id=CT-1")
        self.assertEqual(200, status)
        self.assertEqual(3, len(ledger["entries"]))


class ErrorShapeTest(ApiTestCase):
    def test_404_and_422_and_405(self) -> None:
        status, body, _ = self.request("GET", "/api/nope")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

        status, body, _ = self.request("POST", "/api/athletes", {"name": "缺编号"})
        self.assertEqual(422, status)
        self.assertEqual("missing_field", body["error"]["code"])

        status, body, _ = self.request("GET", "/api/contracts/NOPE")
        self.assertEqual(404, status)

        status, body, _ = self.request("GET", "/api/payments")
        self.assertEqual(405, status)

    def test_invalid_json(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/athletes", body="{bad json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(400, resp.status)
        self.assertEqual("invalid_json", body["error"]["code"])


class ThresholdApiTest(ApiTestCase):
    def test_task_lifecycle_over_http(self) -> None:
        self.request("POST", "/api/athletes", {"athlete_id": "ATH-2", "name": "王二"})
        self.request(
            "POST",
            "/api/contracts",
            {
                "contract_id": "CT-2",
                "athlete_id": "ATH-2",
                "season": "2026",
                "currency": "CNY",
                "installments": [
                    {"installment_id": "P1", "due_date": "2026-01-15", "amount": "1000"}
                ],
                "thresholds": {"low_balance": "100"},
            },
        )
        self.request(
            "POST", "/api/payments", {"contract_id": "CT-2", "amount": "500", "bank_ref": "BK-T2"}
        )
        self.request(
            "POST",
            "/api/expenses",
            {"contract_id": "CT-2", "use": "travel", "amount": "450", "currency": "CNY"},
        )
        status, tasks, _ = self.request("GET", "/api/tasks?athlete_id=ATH-2")
        self.assertEqual(200, status)
        self.assertEqual(1, len(tasks["tasks"]))
        task_id = tasks["tasks"][0]["task_id"]

        status, resolved, _ = self.request(
            "POST", f"/api/tasks/{task_id}/resolve", {"action": "confirm", "note": "已确认"}
        )
        self.assertEqual(200, status)
        self.assertEqual("confirmed", resolved["status"])

        status, body, _ = self.request(
            "POST", f"/api/tasks/{task_id}/resolve", {"action": "confirm"}
        )
        self.assertEqual(409, status)


if __name__ == "__main__":
    unittest.main()
