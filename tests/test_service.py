from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import Service, ServiceError  # noqa: E402


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = Service(self.tmp.name)

    def make_contract(self, contract_id="c-1", athlete_id="ath-1", season="2026",
                      currency="USD", threshold=None, installments=None):
        if athlete_id not in self.svc.state.athletes:
            self.svc.register_athlete(athlete_id, f"运动员-{athlete_id}",
                                      base_currency=currency)
        installments = installments or [
            {"installment_id": "i-1", "milestone": "签约", "amount": "10000",
             "due_date": "2026-07-01"},
            {"installment_id": "i-2", "milestone": "夏训达标", "amount": "10000",
             "due_date": "2026-08-01"},
            {"installment_id": "i-3", "milestone": "赛季末", "amount": "10000",
             "due_date": "2026-09-01"},
        ]
        self.svc.register_contract(contract_id, athlete_id, season, currency,
                                   installments, warning_threshold=threshold,
                                   start_date="2026-06-01")
        return contract_id

    def view(self, contract_id="c-1"):
        data = self.svc.balances_view(contract_id=contract_id)
        self.assertEqual(1, len(data["contracts"]))
        return data["contracts"][0]


class AmendmentTest(ServiceTestCase):
    def test_amendment_only_affects_future_installments(self):
        self.make_contract()
        # 第一期已验收并到账，成为历史事实
        self.svc.approve_milestone("c-1", "i-1", "2026-07-02")
        self.svc.record_payment("R-1", "c-1", "i-1", "10000", "USD", "2026-07-05")

        # 变更里试图改写生效日之前的分期 -> 拒绝
        with self.assertRaises(ServiceError) as ctx:
            self.svc.amend_contract("c-1", "2026-08-01", [
                {"installment_id": "i-1", "milestone": "签约", "amount": "1",
                 "due_date": "2026-07-01"},
            ])
        self.assertEqual("retroactive_change", ctx.exception.code)

        # 合法变更：调整 i-3 金额、新增 i-4；未覆盖的 i-2 若不在清单中会被取消，
        # 因此把 i-2 原样保留在清单里
        self.svc.amend_contract("c-1", "2026-08-01", [
            {"installment_id": "i-2", "milestone": "夏训达标", "amount": "10000",
             "due_date": "2026-08-01"},
            {"installment_id": "i-3", "milestone": "赛季末", "amount": "12000",
             "due_date": "2026-09-01"},
            {"installment_id": "i-4", "milestone": "附加条款", "amount": "5000",
             "due_date": "2026-10-01"},
        ])
        contract = self.svc.get_contract("c-1")
        by_id = {i["installment_id"]: i for i in contract["installments"]}
        # 历史分期保持原样
        self.assertEqual("10000.000000", by_id["i-1"]["amount"])
        self.assertEqual("10000.000000", by_id["i-1"]["paid_amount"])
        self.assertEqual("approved", by_id["i-1"]["status"])
        # 未来分期按变更执行
        self.assertEqual("12000.000000", by_id["i-3"]["amount"])
        self.assertEqual("5000.000000", by_id["i-4"]["amount"])
        self.assertEqual("37000.000000", contract["view"]["committed"])

        # 再次变更：清单未覆盖的 i-4 被取消，已验收的 i-1 不受影响
        self.svc.amend_contract("c-1", "2026-09-01", [
            {"installment_id": "i-3", "milestone": "赛季末", "amount": "12000",
             "due_date": "2026-09-01"},
        ])
        contract = self.svc.get_contract("c-1")
        by_id = {i["installment_id"]: i for i in contract["installments"]}
        self.assertEqual("cancelled", by_id["i-4"]["status"])
        self.assertEqual("approved", by_id["i-1"]["status"])
        self.assertEqual("32000.000000", contract["view"]["committed"])


class LateAcceptanceTest(ServiceTestCase):
    def test_late_acceptance_does_not_rewrite_published_snapshot(self):
        self.make_contract()
        self.svc.record_expense("c-1", "training", "3000", "USD", "2026-08-10")
        published = self.svc.publish_snapshot("2026-08")["snapshot"]
        self.assertEqual("30000.000000",  # i-1/i-2/i-3 均未验收
                         published["contracts"][0]["pending_acceptance"])

        # 验收回执迟到：业务日期在 8 月，但 8 月快照已报出
        result = self.svc.approve_milestone(
            "c-1", "i-1", "2026-08-20", received_at="2026-09-03T09:00:00Z")
        self.assertEqual("2026-09", result["event"]["period"])

        # 已发布快照保持原样，且可按水位线复算
        snapshot = self.svc.get_snapshot("2026-08")["snapshot"]
        self.assertEqual(published, snapshot)
        check = self.svc.recompute_snapshot("2026-08")
        self.assertTrue(check["matches"])

        # 迟到验收体现在当前视图与下一个期间
        view = self.view()
        self.assertEqual("20000.000000", view["pending_acceptance"])
        self.assertEqual("10000.000000", view["receivable"])

    def test_snapshot_publish_rules(self):
        self.make_contract()
        self.svc.publish_snapshot("2026-08")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish_snapshot("2026-08")
        self.assertEqual("snapshot_exists", ctx.exception.code)
        self.svc.publish_snapshot("2026-10")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish_snapshot("2026-09")
        self.assertEqual("snapshot_order", ctx.exception.code)


class IdempotencyTest(ServiceTestCase):
    def test_same_bank_receipt_is_idempotent(self):
        self.make_contract()
        first = self.svc.record_payment("R-100", "c-1", "i-1", "5000", "USD",
                                        "2026-08-05")
        self.assertTrue(first["created"])
        again = self.svc.record_payment("R-100", "c-1", "i-1", "5000", "USD",
                                        "2026-08-05")
        self.assertFalse(again["created"])
        self.assertEqual(first["event"]["event_id"], again["event"]["event_id"])
        view = self.view()
        self.assertEqual("5000.000000", view["received"])
        payments = [e for e in self.svc.ledger_view(contract_id="c-1")["entries"]
                    if e["kind"] == "payment"]
        self.assertEqual(1, len(payments))

    def test_same_event_id_is_idempotent(self):
        self.make_contract()
        first = self.svc.record_expense("c-1", "travel", "800", "USD", "2026-08-11",
                                        event_id="evt-expense-1")
        again = self.svc.record_expense("c-1", "travel", "800", "USD", "2026-08-11",
                                        event_id="evt-expense-1")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual("800.000000", self.view()["used"])


class LinkedEntriesTest(ServiceTestCase):
    def test_refund_reversal_and_fx_difference_are_linked(self):
        self.make_contract()
        payment = self.svc.record_payment("R-1", "c-1", "i-1", "10000", "USD",
                                          "2026-08-05")["event"]
        expense = self.svc.record_expense("c-1", "travel", "4000", "USD",
                                          "2026-08-10")["event"]

        refund = self.svc.record_refund(expense["event_id"], "1500", "2026-08-12",
                                        reason="机票退差")["event"]
        self.assertEqual(expense["event_id"], refund["links_to"])
        self.assertEqual("2500.000000", self.view()["used"])

        # 超额退款被拒绝
        with self.assertRaises(ServiceError) as ctx:
            self.svc.record_refund(expense["event_id"], "9999", "2026-08-13")
        self.assertEqual("refund_exceeds", ctx.exception.code)

        # 冲正到账：全额抵消，且回滚分期已付金额
        reversal = self.svc.record_reversal(payment["event_id"], "2026-08-15",
                                            reason="银行退票")["event"]
        self.assertEqual(payment["event_id"], reversal["links_to"])
        with self.assertRaises(ServiceError):
            self.svc.record_reversal(payment["event_id"], "2026-08-16")
        view = self.view()
        self.assertEqual("0.000000", view["received"])
        self.assertEqual("-2500.000000", view["disposable"])
        contract = self.svc.get_contract("c-1")
        self.assertEqual("0.000000", contract["installments"][0]["paid_amount"])

        # 汇率差额与目标流水关联，计入可支配余额
        fx = self.svc.record_fx_adjustment(expense["event_id"], "-12.5", "USD",
                                           "2026-08-20", memo="结汇差额")["event"]
        self.assertEqual(expense["event_id"], fx["links_to"])
        view = self.view()
        self.assertEqual("-12.500000", view["fx_difference"])
        self.assertEqual("-2512.500000", view["disposable"])

        # 台账中三笔调整均指向原始流水
        ledger = self.svc.ledger_view(contract_id="c-1")["entries"]
        linked = {e["kind"]: e for e in ledger if e["links_to"]}
        self.assertEqual({expense["event_id"], payment["event_id"]},
                         {e["links_to"] for e in linked.values()})


class ViewsTest(ServiceTestCase):
    def test_views_by_athlete_contract_and_season(self):
        self.make_contract("c-1", "ath-1", "2026", "USD")
        self.make_contract("c-2", "ath-1", "2026", "HKD")
        self.make_contract("c-3", "ath-2", "2027", "CNY")
        self.svc.record_payment("R-1", "c-1", "i-1", "10000", "USD", "2026-08-05")
        self.svc.record_expense("c-1", "training", "3000", "USD", "2026-08-10")
        self.svc.record_tax_withholding("c-1", "500", "USD", "2026-08-15")

        by_contract = self.svc.balances_view(contract_id="c-1")
        self.assertEqual(1, len(by_contract["contracts"]))
        view = by_contract["contracts"][0]
        self.assertEqual("30000.000000", view["committed"])
        self.assertEqual("10000.000000", view["received"])
        self.assertEqual("3500.000000", view["used"])
        self.assertEqual("6500.000000", view["disposable"])
        self.assertEqual("20000.000000", view["pending_acceptance"])

        by_athlete = self.svc.balances_view(athlete_id="ath-1", season="2026")
        self.assertEqual(2, len(by_athlete["contracts"]))
        self.assertEqual("30000.000000",
                         by_athlete["totals_by_currency"]["USD"]["committed"])
        self.assertEqual("30000.000000",
                         by_athlete["totals_by_currency"]["HKD"]["committed"])

        by_season = self.svc.balances_view(season="2027")
        self.assertEqual(["c-3"], [c["contract_id"] for c in by_season["contracts"]])

    def test_ledger_shows_running_disposable(self):
        self.make_contract()
        self.svc.record_payment("R-1", "c-1", "i-1", "10000", "USD", "2026-08-05")
        self.svc.record_expense("c-1", "equipment", "2500", "USD", "2026-08-09")
        entries = self.svc.ledger_view(contract_id="c-1")["entries"]
        self.assertEqual("10000.000000", entries[0]["disposable_after"])
        self.assertEqual("7500.000000", entries[1]["disposable_after"])


class AlertTaskTest(ServiceTestCase):
    def test_threshold_crossing_creates_task_requiring_confirmation(self):
        self.make_contract(threshold="1000")
        self.svc.record_payment("R-1", "c-1", "i-1", "5000", "USD", "2026-08-05")
        self.assertEqual([], self.svc.list_tasks()["tasks"])

        result = self.svc.record_expense("c-1", "training", "4500", "USD", "2026-08-10")
        self.assertEqual(1, len(result["tasks_created"]))
        tasks = self.svc.list_tasks(status="pending")["tasks"]
        self.assertEqual(1, len(tasks))
        task = tasks[0]
        self.assertEqual("low_balance", task["kind"])
        self.assertEqual("500.000000", task["disposable"])

        # 已有待处理任务时不重复生成
        self.svc.record_expense("c-1", "travel", "100", "USD", "2026-08-11")
        self.assertEqual(1, len(self.svc.list_tasks(status="pending")["tasks"]))

        # 余额转负再生成一条负余额任务
        self.svc.record_expense("c-1", "equipment", "600", "USD", "2026-08-12")
        kinds = {t["kind"] for t in self.svc.list_tasks(status="pending")["tasks"]}
        self.assertEqual({"low_balance", "negative_balance"}, kinds)

        resolved = self.svc.confirm_task(task["task_id"], "confirmed",
                                         note="已联系赞助商催款", resolved_by="agent-1")
        self.assertEqual("confirmed", resolved["task"]["status"])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.confirm_task(task["task_id"], "confirmed")
        self.assertEqual("task_resolved", ctx.exception.code)


class MultiCurrencyTest(ServiceTestCase):
    def test_cross_currency_payment_requires_fx_rate(self):
        self.make_contract(currency="USD")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.record_payment("R-9", "c-1", "i-1", "78000", "HKD", "2026-08-05")
        self.assertEqual("missing_field", ctx.exception.code)

        self.svc.record_payment("R-9", "c-1", "i-1", "78000", "HKD", "2026-08-05",
                                fx_rate="0.1")
        view = self.view()
        # 现金按到账币种入账，分期已付按合同币种折算
        self.assertEqual("78000.000000", view["by_currency"]["HKD"]["received"])
        self.assertEqual("0.000000", view["received"])  # 合同币种 USD 桶
        contract = self.svc.get_contract("c-1")
        self.assertEqual("7800.000000", contract["installments"][0]["paid_amount"])


class PersistenceTest(ServiceTestCase):
    def test_restart_recovers_state_and_snapshot_recomputable(self):
        self.make_contract()
        self.svc.approve_milestone("c-1", "i-1", "2026-07-02")
        self.svc.record_payment("R-1", "c-1", "i-1", "10000", "USD", "2026-07-05")
        self.svc.record_expense("c-1", "training", "3000", "USD", "2026-08-10")
        self.svc.publish_snapshot("2026-08")
        before = self.svc.balances_view(contract_id="c-1")

        restarted = Service(self.tmp.name)
        after = restarted.balances_view(contract_id="c-1")
        self.assertEqual(before, after)
        self.assertEqual(self.svc.get_snapshot("2026-08"),
                         restarted.get_snapshot("2026-08"))
        check = restarted.recompute_snapshot("2026-08")
        self.assertTrue(check["matches"])

        # 重启后银行回单重传仍然幂等
        dup = restarted.record_payment("R-1", "c-1", "i-1", "10000", "USD",
                                       "2026-07-05")
        self.assertFalse(dup["created"])
        self.assertEqual(after, restarted.balances_view(contract_id="c-1"))


if __name__ == "__main__":
    unittest.main()
