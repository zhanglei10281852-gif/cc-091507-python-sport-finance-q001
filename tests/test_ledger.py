from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger.service import ApiError, LedgerService

FIXED_NOW = datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED_NOW


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LedgerService(runtime_dir=self.tmp.name, clock=fixed_clock)

    def reopen(self) -> LedgerService:
        """模拟重启：从同一运行目录重建服务。"""
        return LedgerService(runtime_dir=self.tmp.name, clock=fixed_clock)

    def make_contract(
        self,
        cid: str = "C1",
        athlete: str = "A1",
        currency: str = "CNY",
        amounts: list[str] = ("100", "100", "100", "100"),
        thresholds: dict | None = None,
        season: str = "2026",
    ) -> dict:
        if athlete not in self.service.athletes:
            self.service.register_athlete({"athlete_id": athlete, "name": "运动员"})
        return self.service.create_contract(
            {
                "contract_id": cid,
                "athlete_id": athlete,
                "season": season,
                "currency": currency,
                "installments": [
                    {
                        "installment_id": f"I{i + 1}",
                        "due_date": f"2026-0{i + 1}-15",
                        "amount": amt,
                        "milestone": f"里程碑{i + 1}",
                    }
                    for i, amt in enumerate(amounts)
                ],
                "thresholds": thresholds,
            }
        )


class ContractAndScheduleTest(ServiceTestCase):
    def test_committed_and_views(self) -> None:
        self.make_contract()
        view = self.service.get_balances(contract_id="C1")
        figures = view["contracts"][0]["figures"]
        self.assertEqual("400.000000", figures["committed"])
        self.assertEqual("0.000000", figures["used"])
        self.assertEqual("0.000000", figures["available"])
        self.assertEqual(1, figures["basis"]["committed"]["schedule_version"])

    def test_amendment_only_affects_future_installments(self) -> None:
        self.make_contract()
        updated = self.service.amend_contract(
            "C1",
            {
                "effective_date": "2026-03-01",
                "reason": "追加赞助",
                "installments": [
                    {"installment_id": "I3", "amount": "150"},
                    {"installment_id": "I4", "amount": "180"},
                ],
            },
        )
        self.assertEqual(2, updated["schedule_version"])
        amounts = {i["installment_id"]: i["amount"] for i in updated["installments"]}
        self.assertEqual("100.000000", amounts["I1"])
        self.assertEqual("100.000000", amounts["I2"])
        self.assertEqual("150.000000", amounts["I3"])
        self.assertEqual("180.000000", amounts["I4"])
        figures = self.service.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("530.000000", figures["committed"])

    def test_amendment_cannot_touch_past_installments(self) -> None:
        self.make_contract()
        with self.assertRaises(ApiError) as ctx:
            self.service.amend_contract(
                "C1",
                {
                    "effective_date": "2026-03-01",
                    "installments": [{"installment_id": "I2", "amount": "999"}],
                },
            )
        self.assertEqual(422, ctx.exception.status)
        self.assertEqual("amendment_only_future", ctx.exception.code)

    def test_amendment_can_add_new_future_installment(self) -> None:
        self.make_contract()
        updated = self.service.amend_contract(
            "C1",
            {
                "effective_date": "2026-05-01",
                "installments": [
                    {"installment_id": "I5", "due_date": "2026-05-20", "amount": "60"}
                ],
            },
        )
        self.assertEqual("460.000000", updated["figures"]["committed"])


class MilestoneTest(ServiceTestCase):
    def test_submit_then_approve_moves_pending(self) -> None:
        self.make_contract()
        self.service.submit_milestone(
            {"contract_id": "C1", "installment_id": "I1", "business_date": "2026-01-10"}
        )
        figures = self.service.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("100.000000", figures["pending_acceptance"])
        self.service.approve_milestone(
            {"contract_id": "C1", "installment_id": "I1", "business_date": "2026-01-12"}
        )
        figures = self.service.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("0.000000", figures["pending_acceptance"])
        self.assertEqual("100.000000", figures["receivable"])

    def test_double_submit_rejected(self) -> None:
        self.make_contract()
        self.service.submit_milestone({"contract_id": "C1", "installment_id": "I1"})
        with self.assertRaises(ApiError) as ctx:
            self.service.submit_milestone({"contract_id": "C1", "installment_id": "I1"})
        self.assertEqual(409, ctx.exception.status)


class PaymentIdempotencyTest(ServiceTestCase):
    def test_same_bank_ref_reupload_is_idempotent(self) -> None:
        self.make_contract()
        first = self.service.record_payment(
            {
                "contract_id": "C1",
                "amount": "100",
                "bank_ref": "BK-2026-0001",
                "installment_id": "I1",
                "value_date": "2026-01-16",
            }
        )
        second = self.service.record_payment(
            {
                "contract_id": "C1",
                "amount": "100",
                "bank_ref": "BK-2026-0001",
                "installment_id": "I1",
                "value_date": "2026-01-16",
            }
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["entry"]["entry_id"], second["entry"]["entry_id"])
        entries = self.service.get_ledger(contract_id="C1")
        self.assertEqual(1, len(entries))
        figures = self.service.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("100.000000", figures["available"])

    def test_payment_currency_must_match_contract(self) -> None:
        self.make_contract()
        with self.assertRaises(ApiError) as ctx:
            self.service.record_payment(
                {"contract_id": "C1", "amount": "10", "currency": "USD", "bank_ref": "B1"}
            )
        self.assertEqual("currency_mismatch", ctx.exception.code)


class ExpenseRefundReversalTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_contract()
        self.service.record_payment(
            {"contract_id": "C1", "amount": "400", "bank_ref": "BK-1", "value_date": "2026-01-16"}
        )

    def figures(self) -> dict:
        return self.service.get_balances(contract_id="C1")["contracts"][0]["figures"]

    def test_expense_and_tax_reduce_available(self) -> None:
        self.service.record_expense(
            {
                "contract_id": "C1",
                "use": "training",
                "amount": "120",
                "currency": "CNY",
                "business_date": "2026-02-01",
                "receipt_ref": "RC-1",
            }
        )
        self.service.record_tax_withholding(
            {"contract_id": "C1", "amount": "30", "business_date": "2026-02-02"}
        )
        figures = self.figures()
        self.assertEqual("150.000000", figures["used"])
        self.assertEqual("250.000000", figures["available"])

    def test_receipt_ref_reupload_is_idempotent(self) -> None:
        payload = {
            "contract_id": "C1",
            "use": "travel",
            "amount": "80",
            "currency": "CNY",
            "receipt_ref": "RC-9",
        }
        first = self.service.record_expense(payload)
        second = self.service.record_expense(payload)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["entry"]["entry_id"], second["entry"]["entry_id"])
        self.assertEqual("80.000000", self.figures()["used"])

    def test_refund_links_to_original_and_restores_balance(self) -> None:
        expense = self.service.record_expense(
            {"contract_id": "C1", "use": "equipment", "amount": "50", "currency": "CNY"}
        )["entry"]
        refund = self.service.record_refund(
            {"original_entry_id": expense["entry_id"], "amount": "20", "reason": "商家退货"}
        )["entry"]
        self.assertEqual([expense["entry_id"]], refund["links"])
        reloaded = self.service.entries_by_id[expense["entry_id"]]
        view = self.service._entry_view(reloaded["entry_id"])
        self.assertIn(refund["entry_id"], view["linked_by"])
        figures = self.figures()
        self.assertEqual("30.000000", figures["used"])
        self.assertEqual("370.000000", figures["available"])

    def test_refund_cannot_exceed_remaining(self) -> None:
        expense = self.service.record_expense(
            {"contract_id": "C1", "use": "equipment", "amount": "50", "currency": "CNY"}
        )["entry"]
        self.service.record_refund({"original_entry_id": expense["entry_id"], "amount": "40"})
        with self.assertRaises(ApiError) as ctx:
            self.service.record_refund({"original_entry_id": expense["entry_id"], "amount": "20"})
        self.assertEqual("refund_exceeds", ctx.exception.code)

    def test_reversal_negates_original_and_links(self) -> None:
        expense = self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "60", "currency": "CNY"}
        )["entry"]
        reversal = self.service.record_reversal(
            {"original_entry_id": expense["entry_id"], "reason": "重复记账"}
        )["entry"]
        self.assertEqual([expense["entry_id"]], reversal["links"])
        self.assertEqual("60.000000", reversal["contract_amount"])
        original = self.service.entries_by_id[expense["entry_id"]]
        self.assertEqual(reversal["entry_id"], original["reversed_by"])
        figures = self.figures()
        self.assertEqual("0.000000", figures["used"])
        self.assertEqual("400.000000", figures["available"])
        with self.assertRaises(ApiError):
            self.service.record_reversal({"original_entry_id": expense["entry_id"]})

    def test_expense_with_refund_cannot_be_reversed(self) -> None:
        expense = self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "60", "currency": "CNY"}
        )["entry"]
        self.service.record_refund({"original_entry_id": expense["entry_id"], "amount": "10"})
        with self.assertRaises(ApiError) as ctx:
            self.service.record_reversal({"original_entry_id": expense["entry_id"]})
        self.assertEqual(409, ctx.exception.status)


class FxTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_contract(cid="CU", currency="USD")
        self.service.record_payment(
            {"contract_id": "CU", "amount": "1000", "bank_ref": "BK-USD-1"}
        )
        self.service.set_fx_rate(
            {"date": "2026-02-01", "base": "HKD", "quote": "USD", "rate": "0.13"}
        )

    def figures(self) -> dict:
        return self.service.get_balances(contract_id="CU")["contracts"][0]["figures"]

    def test_foreign_expense_converts_and_books_fx_difference(self) -> None:
        result = self.service.record_expense(
            {
                "contract_id": "CU",
                "use": "travel",
                "amount": "1000",
                "currency": "HKD",
                "business_date": "2026-02-10",
                "settled_amount": "132",
            }
        )
        entry = result["entry"]
        fx = result["fx_difference"]
        self.assertIsNotNone(fx)
        self.assertEqual("130.000000", entry["converted_amount"])
        self.assertEqual("-130.000000", entry["contract_amount"])
        self.assertEqual("-2.000000", fx["contract_amount"])
        self.assertEqual([entry["entry_id"]], fx["links"])
        figures = self.figures()
        self.assertEqual("130.000000", figures["used"])
        self.assertEqual("868.000000", figures["available"])
        self.assertEqual("-2.000000", figures["fx_adjustments"])

    def test_missing_rate_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.service.record_expense(
                {
                    "contract_id": "CU",
                    "use": "travel",
                    "amount": "10",
                    "currency": "CNY",
                    "business_date": "2026-02-10",
                }
            )
        self.assertEqual("fx_rate_missing", ctx.exception.code)

    def test_inverse_rate_used(self) -> None:
        self.service.set_fx_rate(
            {"date": "2026-02-01", "base": "USD", "quote": "CNY", "rate": "7.2"}
        )
        result = self.service.record_expense(
            {
                "contract_id": "CU",
                "use": "training",
                "amount": "720",
                "currency": "CNY",
                "business_date": "2026-02-11",
            }
        )
        self.assertEqual("100.000000", result["entry"]["converted_amount"])


class SnapshotTest(ServiceTestCase):
    def test_publish_freezes_month_and_verify_recomputes(self) -> None:
        self.make_contract()
        self.service.record_payment(
            {"contract_id": "C1", "amount": "100", "bank_ref": "BK-1", "value_date": "2026-01-16"}
        )
        doc = self.service.publish_snapshot({"month": "2026-01"})
        self.assertEqual("2026-01", doc["month"])
        with self.assertRaises(ApiError) as ctx:
            self.service.publish_snapshot({"month": "2026-01"})
        self.assertEqual("snapshot_exists", ctx.exception.code)
        result = self.service.verify_snapshot(doc["snapshot_id"])
        self.assertTrue(result["match"], result["diffs"])

    def test_late_acceptance_does_not_rewrite_published_snapshot(self) -> None:
        self.make_contract()
        self.service.submit_milestone(
            {"contract_id": "C1", "installment_id": "I1", "business_date": "2026-01-05"}
        )
        self.service.approve_milestone(
            {"contract_id": "C1", "installment_id": "I1", "business_date": "2026-01-08"}
        )
        self.service.record_payment(
            {
                "contract_id": "C1",
                "amount": "100",
                "bank_ref": "BK-1",
                "installment_id": "I1",
                "value_date": "2026-01-16",
            }
        )
        jan = self.service.publish_snapshot({"month": "2026-01"})
        jan_figures = jan["athletes"]["A1"]["contracts"]["C1"]["figures"]
        self.assertEqual("100.000000", jan_figures["received"])

        # 二月的快照也已报出后，一张业务日期为一月的迟到验收回执到达
        self.service.publish_snapshot({"month": "2026-02"})
        late = self.service.approve_milestone(
            {"contract_id": "C1", "installment_id": "I2", "business_date": "2026-01-20"}
        )
        self.assertEqual("2026-06", late["period"])  # 计入当前开放月，不改写历史

        jan_after = self.service.get_snapshot(jan["snapshot_id"])
        self.assertEqual(jan, jan_after)  # 已报出快照逐字节不变
        verify = self.service.verify_snapshot(jan["snapshot_id"])
        self.assertTrue(verify["match"], verify["diffs"])

        # 迟到事件体现在后续发布的快照中
        jun = self.service.publish_snapshot({"month": "2026-06"})
        jun_figures = jun["athletes"]["A1"]["contracts"]["C1"]["figures"]
        self.assertEqual("100.000000", jun_figures["receivable"])

    def test_snapshot_survives_restart_and_recomputes(self) -> None:
        self.make_contract()
        self.service.record_payment(
            {"contract_id": "C1", "amount": "100", "bank_ref": "BK-1", "value_date": "2026-01-16"}
        )
        self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "40", "currency": "CNY"}
        )
        doc = self.service.publish_snapshot({"month": "2026-06"})

        reopened = self.reopen()
        verify = reopened.verify_snapshot(doc["snapshot_id"])
        self.assertTrue(verify["match"], verify["diffs"])
        figures = reopened.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("60.000000", figures["available"])
        self.assertEqual("40.000000", figures["used"])
        # 计算依据仍可追溯
        self.assertEqual(2, len(figures["basis"]["available"]))


class ThresholdTaskTest(ServiceTestCase):
    def test_low_balance_breach_creates_task_requiring_confirmation(self) -> None:
        self.make_contract(thresholds={"low_balance": "50"})
        self.service.record_payment({"contract_id": "C1", "amount": "100", "bank_ref": "BK-1"})
        self.service.record_expense(
            {"contract_id": "C1", "use": "travel", "amount": "60", "currency": "CNY"}
        )
        tasks = self.service.list_tasks(status="pending")
        self.assertEqual(1, len(tasks))
        task = tasks[0]
        self.assertEqual("low_balance", task["kind"])
        self.assertEqual("C1", task["contract_id"])

        # 持续越线不重复生成
        self.service.record_expense(
            {"contract_id": "C1", "use": "travel", "amount": "1", "currency": "CNY"}
        )
        self.assertEqual(1, len(self.service.list_tasks(status="pending")))

        resolved = self.service.resolve_task(
            task["task_id"], {"action": "confirm", "note": "已通知运动员控制支出"}
        )
        self.assertEqual("confirmed", resolved["status"])
        with self.assertRaises(ApiError):
            self.service.resolve_task(task["task_id"], {"action": "confirm"})

    def test_burn_ratio_breach_and_recovery_cycle(self) -> None:
        self.make_contract(thresholds={"burn_ratio": "0.5"})
        self.service.record_payment({"contract_id": "C1", "amount": "400", "bank_ref": "BK-1"})
        self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "250", "currency": "CNY"}
        )
        tasks = self.service.list_tasks(status="pending")
        self.assertEqual(1, len(tasks))
        self.assertEqual("burn_ratio", tasks[0]["kind"])
        self.service.resolve_task(tasks[0]["task_id"], {"action": "dismiss"})

        # 退款后回落，再次越线时生成新任务
        expense = self.service.get_ledger(contract_id="C1")[1]
        self.service.record_refund({"original_entry_id": expense["entry_id"], "amount": "200"})
        self.assertEqual(0, len(self.service.list_tasks(status="pending")))
        self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "300", "currency": "CNY"}
        )
        self.assertEqual(1, len(self.service.list_tasks(status="pending")))

    def test_tasks_survive_restart(self) -> None:
        self.make_contract(thresholds={"low_balance": "0"})
        self.service.record_payment({"contract_id": "C1", "amount": "10", "bank_ref": "BK-1"})
        self.service.record_expense(
            {"contract_id": "C1", "use": "travel", "amount": "20", "currency": "CNY"}
        )
        reopened = self.reopen()
        tasks = reopened.list_tasks(status="pending")
        self.assertEqual(1, len(tasks))
        resolved = reopened.resolve_task(tasks[0]["task_id"], {"action": "confirm"})
        self.assertEqual("confirmed", resolved["status"])


class MultiDimensionViewTest(ServiceTestCase):
    def test_views_by_athlete_contract_and_season(self) -> None:
        self.make_contract(cid="C1", season="2026", currency="CNY")
        self.make_contract(cid="C2", season="2026", currency="USD")
        self.make_contract(cid="C3", season="2027", currency="CNY")
        self.service.record_payment({"contract_id": "C1", "amount": "100", "bank_ref": "B1"})
        self.service.record_payment({"contract_id": "C2", "amount": "50", "bank_ref": "B2"})
        self.service.record_payment({"contract_id": "C3", "amount": "70", "bank_ref": "B3"})

        by_athlete = self.service.get_balances(athlete_id="A1")
        self.assertEqual(3, len(by_athlete["contracts"]))
        self.assertEqual("170.000000", by_athlete["totals_by_currency"]["CNY"]["available"])
        self.assertEqual("50.000000", by_athlete["totals_by_currency"]["USD"]["available"])

        by_season = self.service.get_balances(athlete_id="A1", season="2027")
        self.assertEqual(1, len(by_season["contracts"]))
        self.assertEqual("70.000000", by_season["totals_by_currency"]["CNY"]["available"])

        by_contract = self.service.get_balances(contract_id="C2")
        self.assertEqual("50.000000", by_contract["contracts"][0]["figures"]["available"])

        ledger = self.service.get_ledger(season="2026")
        self.assertEqual(2, len(ledger))


class RestartPersistenceTest(ServiceTestCase):
    def test_full_state_rebuilt_from_event_log(self) -> None:
        self.make_contract(thresholds={"low_balance": "10"})
        self.service.submit_milestone({"contract_id": "C1", "installment_id": "I1"})
        self.service.record_payment({"contract_id": "C1", "amount": "100", "bank_ref": "BK-1"})
        self.service.record_expense(
            {"contract_id": "C1", "use": "training", "amount": "95", "currency": "CNY"}
        )
        self.service.publish_snapshot({"month": "2026-06"})

        reopened = self.reopen()
        figures = reopened.get_balances(contract_id="C1")["contracts"][0]["figures"]
        self.assertEqual("5.000000", figures["available"])
        self.assertEqual("100.000000", figures["pending_acceptance"])
        self.assertEqual(1, len(reopened.list_tasks(status="pending")))
        self.assertEqual(1, len(reopened.list_snapshots()))
        # 幂等索引同样恢复：同一回单重传仍返回原流水
        dup = reopened.record_payment({"contract_id": "C1", "amount": "100", "bank_ref": "BK-1"})
        self.assertTrue(dup["duplicate"])


if __name__ == "__main__":
    unittest.main()
