from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server  # noqa: E402
from service import Service  # noqa: E402


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server("127.0.0.1", 0, Service(cls.tmp.name))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, _ = self.call("GET", "/health")
        self.assertEqual(200, status)

        status, body = self.call("POST", "/athletes", {
            "athlete_id": "ath-1", "name": "运动员甲", "base_currency": "USD"})
        self.assertEqual(201, status)

        status, body = self.call("POST", "/contracts", {
            "contract_id": "c-1", "athlete_id": "ath-1", "season": "2026",
            "currency": "USD", "warning_threshold": "1000", "start_date": "2026-06-01",
            "installments": [
                {"installment_id": "i-1", "milestone": "签约", "amount": "10000",
                 "due_date": "2026-07-01"},
                {"installment_id": "i-2", "milestone": "赛季末", "amount": "10000",
                 "due_date": "2026-09-01"},
            ]})
        self.assertEqual(201, status)
        self.assertEqual("20000.000000", body["contract"]["view"]["committed"])

        status, body = self.call("POST", "/events/milestone-approvals", {
            "contract_id": "c-1", "installment_id": "i-1", "business_date": "2026-07-02"})
        self.assertEqual(201, status)

        status, body = self.call("POST", "/events/sponsor-payments", {
            "receipt_no": "R-1", "contract_id": "c-1", "installment_id": "i-1",
            "amount": "10000", "currency": "USD", "value_date": "2026-07-05"})
        self.assertEqual(201, status)
        self.assertFalse(body["duplicate"])

        # 同一银行回单重传 -> 幂等
        status, body = self.call("POST", "/events/sponsor-payments", {
            "receipt_no": "R-1", "contract_id": "c-1", "installment_id": "i-1",
            "amount": "10000", "currency": "USD", "value_date": "2026-07-05"})
        self.assertEqual(200, status)
        self.assertTrue(body["duplicate"])

        status, body = self.call("POST", "/events/expenses", {
            "contract_id": "c-1", "fund_use": "training", "amount": "9500",
            "currency": "USD", "business_date": "2026-08-10"})
        self.assertEqual(201, status)
        self.assertEqual(1, len(body["tasks_created"]))  # 跌破预警线

        status, body = self.call("GET", "/balances?contract_id=c-1")
        self.assertEqual(200, status)
        view = body["contracts"][0]
        self.assertEqual("500.000000", view["disposable"])
        self.assertEqual("warning", view["status"])

        status, body = self.call("GET", "/tasks?status=pending")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["tasks"]))
        task_id = body["tasks"][0]["task_id"]

        status, body = self.call("POST", f"/tasks/{task_id}/confirm", {
            "decision": "confirmed", "note": "已知悉，暂停非必要支出"})
        self.assertEqual(200, status)
        self.assertEqual("confirmed", body["task"]["status"])

        status, body = self.call("POST", "/snapshots/publish", {"period": "2026-08"})
        self.assertEqual(201, status)
        self.assertEqual("500.000000", body["snapshot"]["contracts"][0]["disposable"])

        status, body = self.call("GET", "/snapshots/2026-08/recompute")
        self.assertEqual(200, status)
        self.assertTrue(body["matches"])

        status, body = self.call("GET", "/ledger?contract_id=c-1")
        self.assertEqual(200, status)
        self.assertEqual(2, len(body["entries"]))

    def test_error_responses_are_json(self):
        status, body = self.call("GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

        status, body = self.call("POST", "/events/expenses", {"contract_id": "ghost"})
        self.assertEqual(400, status)
        self.assertEqual("missing_field", body["error"]["code"])

        status, body = self.call("GET", "/contracts/ghost")
        self.assertEqual(404, status)
        self.assertEqual("contract_not_found", body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
