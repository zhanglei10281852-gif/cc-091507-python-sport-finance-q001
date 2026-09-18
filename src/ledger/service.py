"""资金管控领域服务。

设计要点
--------
* **事件溯源**：所有状态变更先追加为不可变事件（含业务时间 ``business_date``、
  接收时间 ``received_at``、接收序号 ``seq`` 与信封版本 ``version``），再折叠为
  内存状态；重启后按 ``seq`` 重放即可完整恢复。
* **期间归属**：每条带业务日期的事件在接收时确定归属期间 ``period``。若业务
  月份已发布快照（冻结），事件保留真实业务日期，但计入最近一个未冻结月份，
  因此迟到的验收回执不会改写已对外报出的月度快照。
* **合同变更**：以生效日为界，只替换 ``due_date >= effective_date`` 的分期，
  历史分期与已到账款项不受影响；每次变更产生新的计划版本。
* **幂等**：银行回单按 ``(contract_id, bank_ref)`` 去重，票据按
  ``(contract_id, receipt_ref)`` 去重；重复上传返回原流水而非新增。
* **关联流水**：退款、冲正、汇率差额通过 ``links`` 指向原始流水，读取时可
  双向追溯。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from ledger.money import Money, month_of, next_month, parse_date, valid_month
from ledger.store import EventStore, IdempotencyStore, SnapshotStore

ZERO = Decimal("0")

DEFAULT_CURRENCIES = ["CNY", "HKD", "USD"]
DEFAULT_FUND_USES = ["training", "travel", "equipment", "tax_reserve"]
DEFAULT_PRECISION = 6

FINANCIAL_TYPES = {
    "sponsor_payment",
    "expense",
    "refund",
    "tax_withholding",
    "reversal",
    "fx_difference",
}

# 流水桶：received=到账，spent=消耗（支出/代扣为负、退款为正），adjustment=汇率等调整
ENTRY_BUCKET = {
    "sponsor_payment": "received",
    "expense": "spent",
    "tax_withholding": "spent",
    "refund": "spent",
    "fx_difference": "adjustment",
}


class ApiError(Exception):
    """业务校验错误，映射为 HTTP 响应。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _require(payload: dict[str, Any], field: str) -> Any:
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(422, "missing_field", f"缺少必填字段: {field}")
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LedgerService:
    """赞助资金账本服务。"""

    def __init__(
        self,
        runtime_dir: str | Path = ".runtime",
        clock: Callable[[], datetime] | None = None,
        reference_path: str | Path | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = EventStore(runtime_dir)
        self.snapshot_store = SnapshotStore(runtime_dir)
        self.idempotency = IdempotencyStore(runtime_dir)
        self._load_reference(reference_path)
        self._reset_state()
        last_seq = 0
        for event in self.store.load():
            self._apply(event)
            last_seq = event["seq"]
        self._seq = last_seq

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _load_reference(self, reference_path: str | Path | None) -> None:
        path: Path | None = None
        if reference_path is not None:
            path = Path(reference_path)
        else:
            candidate = Path(__file__).resolve().parents[2] / "reference" / "domain.json"
            if candidate.exists():
                path = candidate
        data: dict[str, Any] = {}
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        self.currencies = list(data.get("currencies") or DEFAULT_CURRENCIES)
        self.fund_uses = list(data.get("fund_uses") or DEFAULT_FUND_USES)
        precision = int(data.get("quantity_precision") or DEFAULT_PRECISION)
        self.money = Money(precision)

    def _reset_state(self) -> None:
        self._seq = 0
        self._events: list[dict[str, Any]] = []
        self.athletes: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self.entries_by_id: dict[str, dict[str, Any]] = {}
        self.rates: dict[tuple[str, str, str], str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.frozen_months: set[str] = set()
        self.snapshot_index: dict[str, dict[str, Any]] = {}
        self.bank_refs: dict[tuple[str, str], str] = {}
        self.receipt_refs: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    # 事件发射与折叠
    # ------------------------------------------------------------------
    def _period_for(self, business_date: str) -> str:
        """接收时确定事件的归属期间：业务月未冻结则归业务月，
        否则归入最近一个未冻结月份（迟到事件不改写已发布快照）。"""
        month = month_of(parse_date(business_date, "business_date"))
        if month not in self.frozen_months:
            return month
        current = month_of(self.clock().date())
        if current not in self.frozen_months:
            return current
        month = current
        while month in self.frozen_months:
            month = next_month(month)
        return month

    def _emit(
        self,
        type_: str,
        payload: dict[str, Any],
        business_date: str | None = None,
    ) -> dict[str, Any]:
        seq = self._seq + 1
        event = {
            "seq": seq,
            "id": _new_id("evt"),
            "version": 1,
            "type": type_,
            "business_date": business_date,
            "received_at": self.clock().isoformat(),
            "period": self._period_for(business_date) if business_date else None,
            "payload": payload,
        }
        self.store.append(event)
        self._apply(event)
        self._seq = seq
        return event

    def _apply(self, event: dict[str, Any]) -> None:
        handler = getattr(self, f"_apply_{event['type']}", None)
        if handler is not None:
            handler(event)
        self._events.append(event)

    def _today(self) -> str:
        return self.clock().date().isoformat()

    def _business_date(self, payload: dict[str, Any]) -> str:
        raw = payload.get("business_date") or payload.get("value_date") or self._today()
        return parse_date(raw, "business_date").isoformat()

    # ------------------------------------------------------------------
    # 事件折叠（重放路径，纯函数式地重建状态）
    # ------------------------------------------------------------------
    def _apply_athlete_registered(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self.athletes[p["athlete_id"]] = {
            "athlete_id": p["athlete_id"],
            "name": p["name"],
            "registered_seq": event["seq"],
        }

    def _apply_contract_created(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        installments = {i["installment_id"]: dict(i) for i in p["installments"]}
        self.contracts[p["contract_id"]] = {
            "contract_id": p["contract_id"],
            "athlete_id": p["athlete_id"],
            "season": p["season"],
            "currency": p["currency"],
            "title": p.get("title"),
            "thresholds": p.get("thresholds") or {},
            "versions": [
                {
                    "version": 1,
                    "from_seq": event["seq"],
                    "effective_date": None,
                    "reason": "contract_created",
                    "installments": installments,
                }
            ],
            "created_seq": event["seq"],
        }

    def _apply_contract_amended(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        contract = self.contracts[p["contract_id"]]
        current = {
            iid: dict(inst)
            for iid, inst in contract["versions"][-1]["installments"].items()
        }
        for item in p["installments"]:
            iid = item["installment_id"]
            base = current.get(iid, {})
            current[iid] = {
                "installment_id": iid,
                "due_date": item.get("due_date") or base.get("due_date"),
                "amount": item["amount"],
                "milestone": item.get("milestone") or base.get("milestone") or iid,
            }
        contract["versions"].append(
            {
                "version": len(contract["versions"]) + 1,
                "from_seq": event["seq"],
                "effective_date": p["effective_date"],
                "reason": p.get("reason"),
                "installments": current,
            }
        )

    def _apply_milestone_submitted(self, event: dict[str, Any]) -> None:
        pass  # 验收状态在读取时按事件折叠，见 _installment_states

    def _apply_milestone_approved(self, event: dict[str, Any]) -> None:
        pass

    def _apply_sponsor_payment(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        entry = self._make_entry(
            event,
            type="sponsor_payment",
            bucket="received",
            contract_amount=p["amount"],
            amount=p["amount"],
            currency=p["currency"],
            bank_ref=p["bank_ref"],
            installment_id=p.get("installment_id"),
        )
        self.bank_refs[(p["contract_id"], p["bank_ref"])] = entry["entry_id"]

    def _apply_expense(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        entry = self._make_entry(
            event,
            type="expense",
            bucket="spent",
            contract_amount=self.money.s(-self.money.dec(p["converted_amount"])),
            amount=p["amount"],
            currency=p["currency"],
            use=p["use"],
            receipt_ref=p.get("receipt_ref"),
            rate_used=p.get("rate_used"),
            converted_amount=p["converted_amount"],
            settled_amount=p.get("settled_amount"),
            note=p.get("note"),
        )
        if p.get("receipt_ref"):
            self.receipt_refs[(p["contract_id"], p["receipt_ref"])] = entry["entry_id"]

    def _apply_tax_withholding(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self._make_entry(
            event,
            type="tax_withholding",
            bucket="spent",
            contract_amount=self.money.s(-self.money.dec(p["amount"])),
            amount=p["amount"],
            currency=p["currency"],
            use="tax_reserve",
            reason=p.get("reason"),
        )

    def _apply_refund(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self._make_entry(
            event,
            type="refund",
            bucket="spent",
            contract_amount=p["converted_amount"],
            amount=p["amount"],
            currency=p["currency"],
            use=p.get("use"),
            rate_used=p.get("rate_used"),
            converted_amount=p["converted_amount"],
            reason=p.get("reason"),
            links=[p["original_entry_id"]],
        )

    def _apply_reversal(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        target = self.entries_by_id[p["original_entry_id"]]
        entry = self._make_entry(
            event,
            type="reversal",
            bucket=target["bucket"],
            contract_amount=self.money.s(-self.money.dec(target["contract_amount"])),
            amount=target["amount"],
            currency=target["currency"],
            use=target.get("use"),
            reason=p.get("reason"),
            links=[target["entry_id"]],
        )
        target["reversed_by"] = entry["entry_id"]

    def _apply_fx_difference(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self._make_entry(
            event,
            type="fx_difference",
            bucket="adjustment",
            contract_amount=p["amount"],
            amount=p["amount"],
            currency=p["currency"],
            reason=p.get("reason") or "汇率差额",
            links=[p["expense_entry_id"]],
        )

    def _apply_fx_rate_set(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self.rates[(p["date"], p["base"], p["quote"])] = p["rate"]

    def _apply_task_created(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self.tasks[p["task_id"]] = {
            "task_id": p["task_id"],
            "kind": p["kind"],
            "contract_id": p["contract_id"],
            "athlete_id": p["athlete_id"],
            "season": p["season"],
            "message": p["message"],
            "details": p["details"],
            "status": "pending",
            "created_seq": event["seq"],
            "created_at": event["received_at"],
            "resolution": None,
        }

    def _apply_task_resolved(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        task = self.tasks[p["task_id"]]
        task["status"] = p["status"]
        task["resolution"] = {
            "action": p["action"],
            "note": p.get("note"),
            "seq": event["seq"],
            "at": event["received_at"],
        }

    def _apply_snapshot_published(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        self.frozen_months.add(p["month"])
        self.snapshot_index[p["snapshot_id"]] = {
            "snapshot_id": p["snapshot_id"],
            "month": p["month"],
            "as_of_seq": p["as_of_seq"],
            "published_seq": event["seq"],
            "published_at": event["received_at"],
        }

    def _make_entry(
        self,
        event: dict[str, Any],
        *,
        type: str,
        bucket: str,
        contract_amount: str,
        amount: str,
        currency: str,
        links: list[str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        p = event["payload"]
        contract = self.contracts[p["contract_id"]]
        entry = {
            "entry_id": event["id"],
            "seq": event["seq"],
            "type": type,
            "bucket": bucket,
            "contract_id": p["contract_id"],
            "athlete_id": contract["athlete_id"],
            "season": contract["season"],
            "contract_currency": contract["currency"],
            "contract_amount": contract_amount,
            "amount": amount,
            "currency": currency,
            "business_date": event["business_date"],
            "period": event["period"],
            "received_at": event["received_at"],
            "links": list(links or []),
            "reversed_by": None,
        }
        entry.update(extra)
        self.entries.append(entry)
        self.entries_by_id[entry["entry_id"]] = entry
        return entry

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def _contract(self, contract_id: str) -> dict[str, Any]:
        contract = self.contracts.get(contract_id)
        if contract is None:
            raise ApiError(404, "contract_not_found", f"合同不存在: {contract_id}")
        return contract

    def _athlete(self, athlete_id: str) -> dict[str, Any]:
        athlete = self.athletes.get(athlete_id)
        if athlete is None:
            raise ApiError(404, "athlete_not_found", f"运动员不存在: {athlete_id}")
        return athlete

    def _schedule_as_of(self, contract: dict[str, Any], seq: int) -> dict[str, Any]:
        version = contract["versions"][0]
        for candidate in contract["versions"]:
            if candidate["from_seq"] <= seq:
                version = candidate
            else:
                break
        return version

    def _installment_states(
        self, contract_id: str, seq_cutoff: int, period_cutoff: str | None
    ) -> dict[str, str]:
        states: dict[str, str] = {}
        for event in self._events:
            if event["seq"] > seq_cutoff:
                break
            if event["type"] not in ("milestone_submitted", "milestone_approved"):
                continue
            payload = event["payload"]
            if payload["contract_id"] != contract_id:
                continue
            if period_cutoff is not None and (
                event["period"] is None or event["period"] > period_cutoff
            ):
                continue
            states[payload["installment_id"]] = (
                "submitted" if event["type"] == "milestone_submitted" else "approved"
            )
        return states

    def _figures(
        self,
        contract_id: str,
        seq_cutoff: int | None = None,
        period_cutoff: str | None = None,
    ) -> dict[str, Any]:
        """计算单合同四本账及计算依据（可复算的核心）。"""
        contract = self._contract(contract_id)
        seq_cutoff = self._seq if seq_cutoff is None else seq_cutoff
        scoped = [
            e
            for e in self.entries
            if e["contract_id"] == contract_id
            and e["seq"] <= seq_cutoff
            and (period_cutoff is None or (e["period"] or "") <= period_cutoff)
        ]
        received_ids = [e["entry_id"] for e in scoped if e["bucket"] == "received"]
        spent_ids = [e["entry_id"] for e in scoped if e["bucket"] == "spent"]
        adjust_ids = [e["entry_id"] for e in scoped if e["bucket"] == "adjustment"]

        def total(ids: Iterable[str]) -> Decimal:
            return sum(
                (self.money.dec(self.entries_by_id[i]["contract_amount"]) for i in ids),
                ZERO,
            )

        received = total(received_ids)
        spent = total(spent_ids)
        adjustments = total(adjust_ids)
        available = received + spent + adjustments
        used = -spent

        schedule = self._schedule_as_of(contract, seq_cutoff)
        installments = schedule["installments"]
        committed = sum(
            (self.money.dec(i["amount"]) for i in installments.values()), ZERO
        )

        states = self._installment_states(contract_id, seq_cutoff, period_cutoff)
        pending_ids = sorted(
            iid for iid, st in states.items() if st == "submitted" and iid in installments
        )
        pending = sum(
            (self.money.dec(installments[iid]["amount"]) for iid in pending_ids), ZERO
        )

        paid_by_installment: dict[str, Decimal] = {}
        for e in scoped:
            if e["type"] == "sponsor_payment" and e.get("installment_id"):
                paid_by_installment[e["installment_id"]] = paid_by_installment.get(
                    e["installment_id"], ZERO
                ) + self.money.dec(e["contract_amount"])
        receivable_ids = sorted(
            iid for iid, st in states.items() if st == "approved" and iid in installments
        )
        receivable = ZERO
        for iid in receivable_ids:
            due = self.money.dec(installments[iid]["amount"])
            paid = paid_by_installment.get(iid, ZERO)
            if due - paid > 0:
                receivable += due - paid

        m = self.money.s
        return {
            "currency": contract["currency"],
            "committed": m(committed),
            "received": m(received),
            "used": m(used),
            "pending_acceptance": m(pending),
            "receivable": m(receivable),
            "fx_adjustments": m(adjustments),
            "available": m(available),
            "basis": {
                "committed": {
                    "schedule_version": schedule["version"],
                    "installments": [
                        {
                            "installment_id": i["installment_id"],
                            "due_date": i["due_date"],
                            "amount": i["amount"],
                        }
                        for i in sorted(
                            installments.values(), key=lambda x: (x["due_date"], x["installment_id"])
                        )
                    ],
                },
                "received": received_ids,
                "used": spent_ids,
                "fx_adjustments": adjust_ids,
                "available": [e["entry_id"] for e in scoped],
                "pending_acceptance": pending_ids,
                "receivable": receivable_ids,
            },
        }

    def _evaluate_thresholds(self, contract_id: str) -> None:
        """资金越过预警线时生成需经纪人确认的处置任务（仅命令路径调用）。"""
        contract = self._contract(contract_id)
        thresholds = contract.get("thresholds") or {}
        figures = self._figures(contract_id)
        available = self.money.dec(figures["available"])
        committed = self.money.dec(figures["committed"])
        used = self.money.dec(figures["used"])

        checks: list[tuple[str, bool, dict[str, Any], str]] = []
        low = thresholds.get("low_balance")
        if low is not None:
            breached = available < self.money.dec(low)
            checks.append(
                (
                    "low_balance",
                    breached,
                    {"available": figures["available"], "threshold": str(low)},
                    f"合同 {contract_id} 可支配余额 {figures['available']} "
                    f"{contract['currency']} 低于预警线 {low}",
                )
            )
        ratio = thresholds.get("burn_ratio")
        if ratio is not None and committed > 0:
            r = self.money.dec(ratio)
            breached = used > committed * r
            checks.append(
                (
                    "burn_ratio",
                    breached,
                    {
                        "used": figures["used"],
                        "committed": figures["committed"],
                        "threshold_ratio": str(ratio),
                    },
                    f"合同 {contract_id} 已用额 {figures['used']} 超过承诺额 "
                    f"{figures['committed']} 的 {ratio}",
                )
            )

        for kind, breached, details, message in checks:
            if not breached:
                continue
            open_task = any(
                t["contract_id"] == contract_id
                and t["kind"] == kind
                and t["status"] == "pending"
                for t in self.tasks.values()
            )
            if open_task:
                continue
            self._emit(
                "task_created",
                {
                    "task_id": _new_id("task"),
                    "kind": kind,
                    "contract_id": contract_id,
                    "athlete_id": contract["athlete_id"],
                    "season": contract["season"],
                    "message": message,
                    "details": details,
                },
            )

    # ------------------------------------------------------------------
    # 命令：运动员与合同
    # ------------------------------------------------------------------
    def register_athlete(self, payload: dict[str, Any]) -> dict[str, Any]:
        athlete_id = str(_require(payload, "athlete_id"))
        name = str(_require(payload, "name"))
        if athlete_id in self.athletes:
            raise ApiError(409, "athlete_exists", f"运动员已存在: {athlete_id}")
        self._emit("athlete_registered", {"athlete_id": athlete_id, "name": name})
        return dict(self.athletes[athlete_id])

    def list_athletes(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self.athletes.values()]

    def create_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        athlete_id = str(_require(payload, "athlete_id"))
        season = str(_require(payload, "season"))
        currency = str(_require(payload, "currency"))
        self._athlete(athlete_id)
        if contract_id in self.contracts:
            raise ApiError(409, "contract_exists", f"合同已存在: {contract_id}")
        if currency not in self.currencies:
            raise ApiError(422, "unknown_currency", f"不支持的币种: {currency}")
        raw_installments = payload.get("installments")
        if not isinstance(raw_installments, list) or not raw_installments:
            raise ApiError(422, "missing_field", "installments 必须是非空数组")
        installments: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_installments:
            iid = str(_require(item, "installment_id"))
            if iid in seen:
                raise ApiError(422, "duplicate_installment", f"分期编号重复: {iid}")
            seen.add(iid)
            due = parse_date(_require(item, "due_date"), "due_date").isoformat()
            amount = self._positive_amount(item.get("amount"), "amount")
            installments.append(
                {
                    "installment_id": iid,
                    "due_date": due,
                    "amount": amount,
                    "milestone": item.get("milestone") or iid,
                }
            )
        thresholds = self._parse_thresholds(payload.get("thresholds"))
        self._emit(
            "contract_created",
            {
                "contract_id": contract_id,
                "athlete_id": athlete_id,
                "season": season,
                "currency": currency,
                "title": payload.get("title"),
                "installments": installments,
                "thresholds": thresholds,
            },
        )
        return self.get_contract(contract_id)

    def _parse_thresholds(self, raw: Any) -> dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ApiError(422, "invalid_thresholds", "thresholds 必须是对象")
        parsed: dict[str, str] = {}
        if raw.get("low_balance") is not None:
            parsed["low_balance"] = self.money.s(raw["low_balance"])
        if raw.get("burn_ratio") is not None:
            ratio = self.money.dec(raw["burn_ratio"])
            if ratio <= 0:
                raise ApiError(422, "invalid_thresholds", "burn_ratio 必须为正数")
            parsed["burn_ratio"] = str(ratio)
        return parsed

    def amend_contract(self, contract_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        contract = self._contract(contract_id)
        effective = parse_date(
            _require(payload, "effective_date"), "effective_date"
        ).isoformat()
        raw_items = payload.get("installments")
        if not isinstance(raw_items, list) or not raw_items:
            raise ApiError(422, "missing_field", "installments 必须是非空数组")
        current = contract["versions"][-1]["installments"]
        changes: list[dict[str, Any]] = []
        for item in raw_items:
            iid = str(_require(item, "installment_id"))
            existing = current.get(iid)
            due_raw = item.get("due_date") or (existing or {}).get("due_date")
            if due_raw is None:
                raise ApiError(
                    422, "missing_field", f"新分期 {iid} 必须提供 due_date"
                )
            due = parse_date(due_raw, "due_date").isoformat()
            if due < effective:
                raise ApiError(
                    422,
                    "amendment_only_future",
                    f"分期 {iid} 到期日 {due} 早于生效日 {effective}，"
                    "合同变更只能影响生效日及之后的分期",
                )
            changes.append(
                {
                    "installment_id": iid,
                    "due_date": due,
                    "amount": self._positive_amount(item.get("amount"), "amount"),
                    "milestone": item.get("milestone")
                    or (existing or {}).get("milestone"),
                }
            )
        self._emit(
            "contract_amended",
            {
                "contract_id": contract_id,
                "effective_date": effective,
                "reason": payload.get("reason"),
                "installments": changes,
            },
            business_date=effective,
        )
        self._evaluate_thresholds(contract_id)
        return self.get_contract(contract_id)

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        contract = self._contract(contract_id)
        schedule = contract["versions"][-1]
        states = self._installment_states(contract_id, self._seq, None)
        installments = []
        for iid, inst in sorted(
            schedule["installments"].items(), key=lambda kv: (kv[1]["due_date"], kv[0])
        ):
            installments.append(
                {
                    **inst,
                    "milestone_state": states.get(iid, "scheduled"),
                }
            )
        return {
            "contract_id": contract["contract_id"],
            "athlete_id": contract["athlete_id"],
            "season": contract["season"],
            "currency": contract["currency"],
            "title": contract.get("title"),
            "thresholds": contract.get("thresholds") or {},
            "schedule_version": schedule["version"],
            "installments": installments,
            "versions": [
                {
                    "version": v["version"],
                    "from_seq": v["from_seq"],
                    "effective_date": v["effective_date"],
                    "reason": v["reason"],
                }
                for v in contract["versions"]
            ],
            "figures": self._figures(contract_id),
        }

    def list_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "contract_id": c["contract_id"],
                "athlete_id": c["athlete_id"],
                "season": c["season"],
                "currency": c["currency"],
                "title": c.get("title"),
                "schedule_version": c["versions"][-1]["version"],
            }
            for c in self.contracts.values()
        ]

    # ------------------------------------------------------------------
    # 命令：里程碑验收
    # ------------------------------------------------------------------
    def submit_milestone(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        iid = str(_require(payload, "installment_id"))
        business_date = self._business_date(payload)
        contract = self._contract(contract_id)
        schedule = contract["versions"][-1]["installments"]
        if iid not in schedule:
            raise ApiError(404, "installment_not_found", f"分期不存在: {iid}")
        state = self._installment_states(contract_id, self._seq, None).get(
            iid, "scheduled"
        )
        if state != "scheduled":
            raise ApiError(
                409, "invalid_state", f"分期 {iid} 当前状态为 {state}，不能重复报验"
            )
        event = self._emit(
            "milestone_submitted",
            {"contract_id": contract_id, "installment_id": iid},
            business_date=business_date,
        )
        return {
            "contract_id": contract_id,
            "installment_id": iid,
            "state": "submitted",
            "business_date": business_date,
            "period": event["period"],
        }

    def approve_milestone(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        iid = str(_require(payload, "installment_id"))
        business_date = self._business_date(payload)
        contract = self._contract(contract_id)
        schedule = contract["versions"][-1]["installments"]
        if iid not in schedule:
            raise ApiError(404, "installment_not_found", f"分期不存在: {iid}")
        state = self._installment_states(contract_id, self._seq, None).get(
            iid, "scheduled"
        )
        if state == "approved":
            raise ApiError(409, "invalid_state", f"分期 {iid} 已验收通过")
        event = self._emit(
            "milestone_approved",
            {
                "contract_id": contract_id,
                "installment_id": iid,
                "note": payload.get("note"),
            },
            business_date=business_date,
        )
        return {
            "contract_id": contract_id,
            "installment_id": iid,
            "state": "approved",
            "business_date": business_date,
            "period": event["period"],
        }

    # ------------------------------------------------------------------
    # 命令：资金流水
    # ------------------------------------------------------------------
    def _positive_amount(self, value: Any, field: str) -> str:
        if value is None:
            raise ApiError(422, "missing_field", f"缺少必填字段: {field}")
        try:
            amount = self.money.q(value)
        except Exception:
            raise ApiError(422, "invalid_amount", f"{field} 不是有效金额: {value!r}")
        if amount <= 0:
            raise ApiError(422, "invalid_amount", f"{field} 必须为正数: {value!r}")
        return self.money.s(amount)

    def record_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        contract = self._contract(contract_id)
        bank_ref = str(_require(payload, "bank_ref"))
        existing = self.bank_refs.get((contract_id, bank_ref))
        if existing is not None:
            # 同一银行回单重传：幂等返回原流水
            return {"entry": self._entry_view(existing), "duplicate": True}
        amount = self._positive_amount(payload.get("amount"), "amount")
        currency = str(payload.get("currency") or contract["currency"])
        if currency != contract["currency"]:
            raise ApiError(
                422,
                "currency_mismatch",
                f"到账币种 {currency} 与合同币种 {contract['currency']} 不一致",
            )
        business_date = self._business_date(payload)
        iid = payload.get("installment_id")
        if iid is not None and iid not in contract["versions"][-1]["installments"]:
            raise ApiError(404, "installment_not_found", f"分期不存在: {iid}")
        event = self._emit(
            "sponsor_payment",
            {
                "contract_id": contract_id,
                "amount": amount,
                "currency": currency,
                "bank_ref": bank_ref,
                "installment_id": iid,
                "reason": payload.get("reason"),
            },
            business_date=business_date,
        )
        self._evaluate_thresholds(contract_id)
        return {"entry": self._entry_view(event["id"]), "duplicate": False}

    def record_expense(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        contract = self._contract(contract_id)
        receipt_ref = payload.get("receipt_ref")
        if receipt_ref:
            existing = self.receipt_refs.get((contract_id, str(receipt_ref)))
            if existing is not None:
                return {
                    "entry": self._entry_view(existing),
                    "fx_difference": None,
                    "duplicate": True,
                }
        use = str(_require(payload, "use"))
        if use not in self.fund_uses:
            raise ApiError(422, "unknown_fund_use", f"未知资金用途: {use}")
        amount = self._positive_amount(payload.get("amount"), "amount")
        currency = str(_require(payload, "currency"))
        if currency not in self.currencies:
            raise ApiError(422, "unknown_currency", f"不支持的币种: {currency}")
        business_date = self._business_date(payload)

        rate_used: str | None = None
        if currency == contract["currency"]:
            converted = self.money.dec(amount)
        else:
            rate = self._find_rate(business_date, currency, contract["currency"])
            if rate is None:
                raise ApiError(
                    422,
                    "fx_rate_missing",
                    f"缺少 {business_date} 或之前 {currency}->{contract['currency']} 的汇率",
                )
            rate_used = str(rate)
            converted = self.money.q(self.money.dec(amount) * rate)
        settled_raw = payload.get("settled_amount")
        settled = (
            self.money.q(settled_raw) if settled_raw is not None else converted
        )

        event = self._emit(
            "expense",
            {
                "contract_id": contract_id,
                "use": use,
                "amount": amount,
                "currency": currency,
                "receipt_ref": receipt_ref,
                "rate_used": rate_used,
                "converted_amount": self.money.s(converted),
                "settled_amount": self.money.s(settled),
                "note": payload.get("note"),
            },
            business_date=business_date,
        )
        fx_entry = None
        diff = converted - settled
        if diff != 0:
            fx_event = self._emit(
                "fx_difference",
                {
                    "contract_id": contract_id,
                    "expense_entry_id": event["id"],
                    "amount": self.money.s(diff),
                    "currency": contract["currency"],
                    "reason": f"票据结算差额（入账 {self.money.s(converted)}，"
                    f"实结 {self.money.s(settled)}）",
                },
                business_date=business_date,
            )
            fx_entry = self._entry_view(fx_event["id"])
        self._evaluate_thresholds(contract_id)
        return {
            "entry": self._entry_view(event["id"]),
            "fx_difference": fx_entry,
            "duplicate": False,
        }

    def record_tax_withholding(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(_require(payload, "contract_id"))
        contract = self._contract(contract_id)
        amount = self._positive_amount(payload.get("amount"), "amount")
        currency = str(payload.get("currency") or contract["currency"])
        if currency != contract["currency"]:
            raise ApiError(
                422,
                "currency_mismatch",
                f"代扣币种 {currency} 与合同币种 {contract['currency']} 不一致",
            )
        business_date = self._business_date(payload)
        event = self._emit(
            "tax_withholding",
            {
                "contract_id": contract_id,
                "amount": amount,
                "currency": currency,
                "reason": payload.get("reason") or "代扣税款",
            },
            business_date=business_date,
        )
        self._evaluate_thresholds(contract_id)
        return {"entry": self._entry_view(event["id"])}

    def record_refund(self, payload: dict[str, Any]) -> dict[str, Any]:
        original_id = str(_require(payload, "original_entry_id"))
        original = self.entries_by_id.get(original_id)
        if original is None or original["type"] != "expense":
            raise ApiError(404, "entry_not_found", f"原始支出流水不存在: {original_id}")
        if original["reversed_by"]:
            raise ApiError(409, "invalid_state", "原始支出已被冲正，不能再退款")
        amount = self._positive_amount(payload.get("amount"), "amount")
        refunded = sum(
            (
                self.money.dec(e["amount"])
                for e in self.entries
                if e["type"] == "refund"
                and original_id in e["links"]
                and not e["reversed_by"]
            ),
            ZERO,
        )
        remaining = self.money.dec(original["amount"]) - refunded
        if self.money.dec(amount) > remaining:
            raise ApiError(
                422,
                "refund_exceeds",
                f"退款金额超过可退余额 {self.money.s(remaining)} {original['currency']}",
            )
        rate = original.get("rate_used")
        converted = (
            self.money.q(self.money.dec(amount) * self.money.dec(rate))
            if rate
            else self.money.dec(amount)
        )
        business_date = self._business_date(payload)
        event = self._emit(
            "refund",
            {
                "contract_id": original["contract_id"],
                "original_entry_id": original_id,
                "amount": amount,
                "currency": original["currency"],
                "use": original.get("use"),
                "rate_used": rate,
                "converted_amount": self.money.s(converted),
                "reason": payload.get("reason"),
            },
            business_date=business_date,
        )
        self._evaluate_thresholds(original["contract_id"])
        return {"entry": self._entry_view(event["id"])}

    def record_reversal(self, payload: dict[str, Any]) -> dict[str, Any]:
        original_id = str(_require(payload, "original_entry_id"))
        original = self.entries_by_id.get(original_id)
        if original is None:
            raise ApiError(404, "entry_not_found", f"流水不存在: {original_id}")
        if original["type"] == "reversal":
            raise ApiError(422, "invalid_state", "冲正流水不能再次被冲正")
        if original["reversed_by"]:
            raise ApiError(409, "invalid_state", "该流水已被冲正")
        if original["type"] == "expense":
            has_refund = any(
                e["type"] == "refund"
                and original_id in e["links"]
                and not e["reversed_by"]
                for e in self.entries
            )
            if has_refund:
                raise ApiError(
                    409, "invalid_state", "该支出存在退款流水，请先冲正退款"
                )
        business_date = self._business_date(payload)
        event = self._emit(
            "reversal",
            {
                "contract_id": original["contract_id"],
                "original_entry_id": original_id,
                "reason": payload.get("reason") or "冲正",
            },
            business_date=business_date,
        )
        self._evaluate_thresholds(original["contract_id"])
        return {"entry": self._entry_view(event["id"])}

    def set_fx_rate(self, payload: dict[str, Any]) -> dict[str, Any]:
        day = parse_date(_require(payload, "date"), "date").isoformat()
        base = str(_require(payload, "base"))
        quote = str(_require(payload, "quote"))
        if base not in self.currencies or quote not in self.currencies:
            raise ApiError(422, "unknown_currency", "币种不在支持列表中")
        if base == quote:
            raise ApiError(422, "invalid_pair", "base 与 quote 不能相同")
        try:
            rate = self.money.dec(_require(payload, "rate"))
        except Exception:
            raise ApiError(422, "invalid_amount", "rate 不是有效数值")
        if rate <= 0:
            raise ApiError(422, "invalid_amount", "rate 必须为正数")
        self._emit(
            "fx_rate_set",
            {"date": day, "base": base, "quote": quote, "rate": str(rate)},
        )
        return {"date": day, "base": base, "quote": quote, "rate": str(rate)}

    def _find_rate(self, day: str, base: str, quote: str) -> Decimal | None:
        best_date = ""
        best: Decimal | None = None
        for (rate_date, b, q), raw in self.rates.items():
            if rate_date > day or rate_date < best_date:
                continue
            if b == base and q == quote:
                best_date, best = rate_date, self.money.dec(raw)
            elif b == quote and q == base:
                best_date, best = rate_date, Decimal(1) / self.money.dec(raw)
        return best

    def list_fx_rates(self, base: str | None = None, quote: str | None = None) -> list[dict[str, Any]]:
        rows = [
            {"date": d, "base": b, "quote": q, "rate": r}
            for (d, b, q), r in self.rates.items()
            if (base is None or b == base) and (quote is None or q == quote)
        ]
        return sorted(rows, key=lambda x: (x["date"], x["base"], x["quote"]))

    # ------------------------------------------------------------------
    # 查询：余额视图与流水
    # ------------------------------------------------------------------
    def _entry_view(self, entry_id: str) -> dict[str, Any]:
        entry = self.entries_by_id[entry_id]
        linked_by = sorted(
            e["entry_id"] for e in self.entries if entry_id in e["links"]
        )
        view = dict(entry)
        view["linked_by"] = linked_by
        return view

    def get_ledger(
        self,
        athlete_id: str | None = None,
        contract_id: str | None = None,
        season: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self._entry_view(e["entry_id"])
            for e in self.entries
            if (athlete_id is None or e["athlete_id"] == athlete_id)
            and (contract_id is None or e["contract_id"] == contract_id)
            and (season is None or e["season"] == season)
        ]

    def get_balances(
        self,
        athlete_id: str | None = None,
        contract_id: str | None = None,
        season: str | None = None,
    ) -> dict[str, Any]:
        if athlete_id is not None:
            self._athlete(athlete_id)
        if contract_id is not None:
            self._contract(contract_id)
        selected = [
            c
            for c in self.contracts.values()
            if (athlete_id is None or c["athlete_id"] == athlete_id)
            and (contract_id is None or c["contract_id"] == contract_id)
            and (season is None or c["season"] == season)
        ]
        contracts_view = []
        totals: dict[str, dict[str, Decimal]] = {}
        for c in sorted(selected, key=lambda x: x["contract_id"]):
            figures = self._figures(c["contract_id"])
            contracts_view.append(
                {
                    "contract_id": c["contract_id"],
                    "athlete_id": c["athlete_id"],
                    "season": c["season"],
                    "figures": figures,
                }
            )
            bucket = totals.setdefault(
                c["currency"],
                {k: ZERO for k in ("committed", "received", "used", "pending_acceptance", "receivable", "available")},
            )
            for key in bucket:
                bucket[key] += self.money.dec(figures[key])
        totals_view = {
            ccy: {k: self.money.s(v) for k, v in values.items()}
            for ccy, values in sorted(totals.items())
        }
        return {
            "filters": {
                "athlete_id": athlete_id,
                "contract_id": contract_id,
                "season": season,
            },
            "as_of_seq": self._seq,
            "generated_at": self.clock().isoformat(),
            "contracts": contracts_view,
            "totals_by_currency": totals_view,
        }

    # ------------------------------------------------------------------
    # 月度快照：发布、查询与复算
    # ------------------------------------------------------------------
    def _snapshot_doc(self, month: str, as_of_seq: int) -> dict[str, Any]:
        athletes: dict[str, Any] = {}
        for contract in sorted(
            self.contracts.values(), key=lambda c: c["contract_id"]
        ):
            if contract["created_seq"] > as_of_seq:
                continue
            figures = self._figures(
                contract["contract_id"], seq_cutoff=as_of_seq, period_cutoff=month
            )
            slot = athletes.setdefault(
                contract["athlete_id"], {"contracts": {}, "totals_by_currency": {}}
            )
            slot["contracts"][contract["contract_id"]] = {
                "season": contract["season"],
                "figures": figures,
            }
            totals = slot["totals_by_currency"].setdefault(
                contract["currency"],
                {k: ZERO for k in ("committed", "received", "used", "pending_acceptance", "receivable", "available")},
            )
            for key in totals:
                totals[key] += self.money.dec(figures[key])
        for slot in athletes.values():
            slot["totals_by_currency"] = {
                ccy: {k: self.money.s(v) for k, v in values.items()}
                for ccy, values in sorted(slot["totals_by_currency"].items())
            }
        return {
            "snapshot_id": f"snap-{month}-{as_of_seq}",
            "month": month,
            "as_of_seq": as_of_seq,
            "athletes": athletes,
        }

    def publish_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        month = str(_require(payload, "month"))
        if not valid_month(month):
            raise ApiError(422, "invalid_month", f"month 必须是 YYYY-MM 格式: {month}")
        if month in self.frozen_months:
            raise ApiError(409, "snapshot_exists", f"月份 {month} 的快照已发布，禁止改写")
        doc = self._snapshot_doc(month, self._seq)
        event = self._emit(
            "snapshot_published",
            {
                "snapshot_id": doc["snapshot_id"],
                "month": month,
                "as_of_seq": doc["as_of_seq"],
            },
        )
        doc["published_seq"] = event["seq"]
        doc["published_at"] = event["received_at"]
        self.snapshot_store.save(doc)
        return doc

    def list_snapshots(self) -> list[dict[str, Any]]:
        return sorted(self.snapshot_index.values(), key=lambda s: s["month"])

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        doc = self.snapshot_store.get(snapshot_id)
        if doc is None:
            raise ApiError(404, "snapshot_not_found", f"快照不存在: {snapshot_id}")
        return doc

    def verify_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """从事件日志复算快照，验证历史快照与每笔余额的计算依据。"""
        doc = self.get_snapshot(snapshot_id)
        recomputed = self._snapshot_doc(doc["month"], doc["as_of_seq"])
        diffs: list[str] = []
        self._diff("athletes", doc["athletes"], recomputed["athletes"], diffs)
        return {
            "snapshot_id": snapshot_id,
            "month": doc["month"],
            "as_of_seq": doc["as_of_seq"],
            "match": not diffs,
            "diffs": diffs,
        }

    def _diff(self, path: str, a: Any, b: Any, out: list[str]) -> None:
        if type(a) is not type(b):
            out.append(f"{path}: {a!r} != {b!r}")
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a:
                    out.append(f"{path}.{key}: 复算多出键")
                elif key not in b:
                    out.append(f"{path}.{key}: 复算缺少键")
                else:
                    self._diff(f"{path}.{key}", a[key], b[key], out)
        elif isinstance(a, list):
            if len(a) != len(b):
                out.append(f"{path}: 长度 {len(a)} != {len(b)}")
            else:
                for i, (x, y) in enumerate(zip(a, b)):
                    self._diff(f"{path}[{i}]", x, y, out)
        elif a != b:
            out.append(f"{path}: {a!r} != {b!r}")

    # ------------------------------------------------------------------
    # 预警处置任务
    # ------------------------------------------------------------------
    def list_tasks(
        self, status: str | None = None, athlete_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            dict(t)
            for t in sorted(self.tasks.values(), key=lambda x: x["created_seq"])
            if (status is None or t["status"] == status)
            and (athlete_id is None or t["athlete_id"] == athlete_id)
        ]

    def resolve_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", f"处置任务不存在: {task_id}")
        if task["status"] != "pending":
            raise ApiError(409, "invalid_state", f"任务已处理，当前状态: {task['status']}")
        action = str(_require(payload, "action"))
        if action not in ("confirm", "dismiss"):
            raise ApiError(422, "invalid_action", "action 必须是 confirm 或 dismiss")
        status = "confirmed" if action == "confirm" else "dismissed"
        self._emit(
            "task_resolved",
            {
                "task_id": task_id,
                "action": action,
                "status": status,
                "note": payload.get("note"),
            },
        )
        return dict(self.tasks[task_id])
