"""业务服务层：校验、幂等、事件追加、预警任务与月度快照。"""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal
from pathlib import Path

from domain import (
    ZERO,
    apply_event,
    compute_snapshot,
    contract_money,
    contract_view,
    load_reference,
    money_str,
    month_of,
    next_month,
    parse_date,
    parse_money,
    parse_month,
    parse_timestamp,
    replay,
    utc_now_iso,
)
from store import EventStore


class ServiceError(ValueError):
    """业务校验错误，携带 HTTP 状态码与机器可读的错误码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _need(fields: dict) -> None:
    missing = [name for name, value in fields.items() if value is None or value == ""]
    if missing:
        raise ServiceError(400, "missing_field", "缺少必填字段: " + ", ".join(missing))


def serialize_event(event: dict) -> dict:
    return {
        "seq": event["seq"],
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "business_date": event["business_date"],
        "received_at": event["received_at"],
        "period": event["period"],
        "links_to": event.get("links_to"),
        "idempotency_key": event.get("idempotency_key"),
        "payload": event["payload"],
    }


def serialize_entry(entry: dict, disposable_after=None) -> dict:
    data = {
        "seq": entry["seq"],
        "entry_id": entry["entry_id"],
        "kind": entry["kind"],
        "bucket": entry["bucket"],
        "amount": money_str(entry["amount"]),
        "currency": entry["currency"],
        "contract_id": entry["contract_id"],
        "athlete_id": entry["athlete_id"],
        "season": entry["season"],
        "fund_use": entry["fund_use"],
        "memo": entry["memo"],
        "receipt_no": entry.get("receipt_no"),
        "installment_id": entry.get("installment_id"),
        "links_to": entry["links_to"],
        "business_date": entry["business_date"],
        "period": entry["period"],
        "received_at": entry["received_at"],
        "reversed": entry["reversed"],
        "refunded_total": money_str(entry["refunded"]),
    }
    if disposable_after is not None:
        data["disposable_after"] = money_str(disposable_after)
    return data


class Service:
    """资金管控服务：所有写操作先落事件日志，再更新内存投影。"""

    def __init__(self, runtime_dir: str | Path = ".runtime", reference: dict | None = None) -> None:
        self.reference = reference or load_reference()
        self.store = EventStore(Path(runtime_dir) / "events.jsonl")
        self.events = self.store.load_all()
        self.state = replay(self.events)
        self._by_id = {e["event_id"]: e for e in self.events}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _period_for(self, business_date: str) -> str:
        """确认期间：业务月份若已对外报出，则顺延到第一个未报出的月份。"""
        period = month_of(business_date)
        while period in self.state.snapshots:
            period = next_month(period)
        return period

    def _append(self, event_type: str, payload: dict, *, business_date: str,
                received_at: str | None = None, links_to: str | None = None,
                idem_key: str | None = None, event_id: str | None = None,
                min_period: str | None = None):
        if event_id is not None and f"id:{event_id}" in self.state.idempotency:
            return self._by_id[self.state.idempotency[f"id:{event_id}"]], False
        if idem_key is not None and f"key:{idem_key}" in self.state.idempotency:
            return self._by_id[self.state.idempotency[f"key:{idem_key}"]], False
        period = self._period_for(business_date)
        # 事件的确认期间不得早于其依赖对象（合同登记、被关联流水）的期间，
        # 保证按期间截取的任意事件子集在因果上闭合、可安全重放。
        if min_period is not None and period < min_period:
            period = min_period
        event = {
            "event_id": event_id or f"evt-{uuid.uuid4().hex[:16]}",
            "event_type": event_type,
            "business_date": business_date,
            "received_at": received_at or utc_now_iso(),
            "period": period,
            "links_to": links_to,
            "idempotency_key": idem_key,
            "payload": payload,
            "version": 1,
        }
        self.store.append(event)
        self.events.append(event)
        self._by_id[event["event_id"]] = event
        apply_event(self.state, event)
        return event, True

    def _currency(self, value) -> str:
        if value not in self.reference.get("currencies", []):
            raise ServiceError(400, "invalid_currency", f"不支持的币种: {value}")
        return value

    def _fund_use(self, value) -> str:
        if value not in self.reference.get("fund_uses", []):
            raise ServiceError(400, "invalid_fund_use", f"不支持的资金用途: {value}")
        return value

    def _contract(self, contract_id) -> dict:
        contract = self.state.contracts.get(contract_id)
        if contract is None:
            raise ServiceError(404, "contract_not_found", f"合同不存在: {contract_id}")
        return contract

    def _entry(self, entry_id) -> dict:
        entry = self.state.entries_by_id.get(entry_id)
        if entry is None:
            raise ServiceError(404, "entry_not_found", f"流水不存在: {entry_id}")
        return entry

    def _disposable_now(self, contract_id: str, currency: str) -> Decimal:
        money = contract_money(self.state, contract_id)
        bucket = money["by_currency"].get(currency)
        return bucket["disposable"] if bucket else ZERO

    def _evaluate_alerts(self, contract_id: str, before: Decimal) -> list[str]:
        """资金越过预警线时生成需要经纪人确认的处置任务。"""
        contract = self.state.contracts[contract_id]
        currency = contract["currency"]
        after = self._disposable_now(contract_id, currency)
        threshold = contract["warning_threshold"]
        kinds: list[tuple[str, str]] = []
        if threshold is not None and before >= threshold > after:
            kinds.append((
                "low_balance",
                f"合同 {contract_id} 可支配余额 {money_str(after)} {currency} "
                f"已跌破预警线 {money_str(threshold)}",
            ))
        if before >= ZERO > after:
            kinds.append((
                "negative_balance",
                f"合同 {contract_id} 可支配余额转为负数 {money_str(after)} {currency}",
            ))
        created = []
        for kind, message in kinds:
            duplicate = any(
                t["contract_id"] == contract_id and t["kind"] == kind and t["status"] == "pending"
                for t in self.state.tasks.values()
            )
            if duplicate:
                continue
            task_id = f"task-{contract_id}-{kind}-{self.state.max_seq}"
            payload = {
                "task_id": task_id,
                "kind": kind,
                "contract_id": contract_id,
                "athlete_id": contract["athlete_id"],
                "season": contract["season"],
                "currency": currency,
                "threshold": money_str(threshold) if threshold is not None else None,
                "disposable": money_str(after),
                "message": message,
            }
            self._append("task_created", payload, business_date=utc_now_iso()[:10])
            created.append(task_id)
        return created

    def _record_financial(self, event_type: str, payload: dict, *, business_date: str,
                          contract_id: str, links_to=None, idem_key=None,
                          event_id=None, received_at=None,
                          min_period: str | None = None) -> dict:
        contract = self._contract(contract_id)
        before = self._disposable_now(contract_id, contract["currency"])
        floor = contract["registered_period"]
        if min_period is not None and min_period > floor:
            floor = min_period
        event, created = self._append(
            event_type, payload, business_date=business_date, received_at=received_at,
            links_to=links_to, idem_key=idem_key, event_id=event_id, min_period=floor,
        )
        tasks = self._evaluate_alerts(contract_id, before) if created else []
        return {"event": event, "created": created, "tasks_created": tasks}

    # ------------------------------------------------------------------
    # 运动员与合同
    # ------------------------------------------------------------------

    def register_athlete(self, athlete_id, name, base_currency=None, *,
                         event_id=None, received_at=None) -> dict:
        with self._lock:
            _need({"athlete_id": athlete_id, "name": name})
            if athlete_id in self.state.athletes:
                raise ServiceError(409, "athlete_exists", f"运动员已存在: {athlete_id}")
            if base_currency is not None:
                self._currency(base_currency)
            if received_at is not None:
                parse_timestamp(received_at)
            payload = {"athlete_id": athlete_id, "name": name, "base_currency": base_currency}
            event, _ = self._append(
                "athlete_registered", payload,
                business_date=(received_at or utc_now_iso())[:10],
                received_at=received_at, event_id=event_id,
            )
            return {"athlete": payload, "event": serialize_event(event)}

    def list_athletes(self) -> dict:
        with self._lock:
            return {"athletes": [self.state.athletes[k] for k in sorted(self.state.athletes)]}

    def register_contract(self, contract_id, athlete_id, season, currency, installments, *,
                          warning_threshold=None, start_date=None, event_id=None,
                          received_at=None) -> dict:
        with self._lock:
            _need({"contract_id": contract_id, "athlete_id": athlete_id,
                   "season": season, "currency": currency})
            if contract_id in self.state.contracts:
                raise ServiceError(409, "contract_exists", f"合同已存在: {contract_id}")
            if athlete_id not in self.state.athletes:
                raise ServiceError(404, "athlete_not_found", f"运动员不存在: {athlete_id}")
            self._currency(currency)
            items = self._validate_installments(installments)
            threshold = None
            if warning_threshold is not None:
                threshold = money_str(parse_money(warning_threshold, "warning_threshold",
                                                  allow_zero=True))
            start = parse_date(start_date, "start_date") if start_date else utc_now_iso()[:10]
            payload = {
                "contract_id": contract_id,
                "athlete_id": athlete_id,
                "season": str(season),
                "currency": currency,
                "warning_threshold": threshold,
                "start_date": start,
                "installments": items,
            }
            self._append("contract_registered", payload, business_date=start,
                         received_at=received_at, event_id=event_id)
            return {"contract": self.get_contract(contract_id)}

    def _validate_installments(self, installments) -> list[dict]:
        if not isinstance(installments, list) or not installments:
            raise ServiceError(400, "invalid_installments", "installments 必须是非空数组")
        seen = set()
        items = []
        for raw in installments:
            if not isinstance(raw, dict):
                raise ServiceError(400, "invalid_installments", "分期必须是对象")
            _need({"installment_id": raw.get("installment_id"),
                   "milestone": raw.get("milestone"),
                   "amount": raw.get("amount"),
                   "due_date": raw.get("due_date")})
            iid = raw["installment_id"]
            if iid in seen:
                raise ServiceError(400, "duplicate_installment", f"分期编号重复: {iid}")
            seen.add(iid)
            items.append({
                "installment_id": iid,
                "milestone": raw["milestone"],
                "amount": money_str(parse_money(raw["amount"], "amount")),
                "due_date": parse_date(raw["due_date"], "due_date"),
            })
        return items

    def amend_contract(self, contract_id, effective_date, installments, *,
                       event_id=None, received_at=None) -> dict:
        """合同变更：只允许影响生效日及之后的分期，历史分期保持不变。"""
        with self._lock:
            contract = self._contract(contract_id)
            effective = parse_date(effective_date, "effective_date")
            items = self._validate_installments(installments)
            for item in items:
                if item["due_date"] < effective:
                    raise ServiceError(
                        400, "retroactive_change",
                        f"分期 {item['installment_id']} 的到期日早于生效日，合同变更只能影响未来分期")
            payload = {"contract_id": contract_id, "effective_date": effective,
                       "installments": items}
            self._append("contract_amended", payload, business_date=effective,
                         received_at=received_at, event_id=event_id,
                         min_period=contract["registered_period"])
            return {"contract": self.get_contract(contract_id)}

    def get_contract(self, contract_id) -> dict:
        with self._lock:
            contract = self._contract(contract_id)
            installments = sorted(contract["schedule"].values(),
                                  key=lambda i: (i["due_date"], i["installment_id"]))
            return {
                "contract_id": contract_id,
                "athlete_id": contract["athlete_id"],
                "season": contract["season"],
                "currency": contract["currency"],
                "warning_threshold": (money_str(contract["warning_threshold"])
                                      if contract["warning_threshold"] is not None else None),
                "installments": [{
                    "installment_id": i["installment_id"],
                    "milestone": i["milestone"],
                    "amount": money_str(i["amount"]),
                    "due_date": i["due_date"],
                    "status": i["status"],
                    "paid_amount": money_str(i["paid_amount"]),
                } for i in installments],
                "versions": contract["versions"],
                "view": contract_view(self.state, contract_id),
            }

    # ------------------------------------------------------------------
    # 里程碑验收与资金流水
    # ------------------------------------------------------------------

    def approve_milestone(self, contract_id, installment_id, business_date, *,
                          event_id=None, received_at=None) -> dict:
        with self._lock:
            contract = self._contract(contract_id)
            _need({"installment_id": installment_id, "business_date": business_date})
            inst = contract["schedule"].get(installment_id)
            if inst is None:
                raise ServiceError(404, "installment_not_found", f"分期不存在: {installment_id}")
            if inst["status"] != "scheduled":
                raise ServiceError(409, "installment_not_scheduled",
                                   f"分期 {installment_id} 当前状态为 {inst['status']}，不能重复验收")
            business_date = parse_date(business_date)
            if received_at is not None:
                parse_timestamp(received_at)
            payload = {"contract_id": contract_id, "installment_id": installment_id,
                       "milestone": inst["milestone"]}
            event, created = self._append(
                "milestone_approved", payload, business_date=business_date,
                received_at=received_at, event_id=event_id,
                min_period=contract["registered_period"])
            return {"event": serialize_event(event), "created": created}

    def record_payment(self, receipt_no, contract_id, installment_id, amount, currency,
                       value_date, *, fx_rate=None, memo=None, event_id=None,
                       received_at=None) -> dict:
        """赞助分期到账。同一银行回单号（receipt_no）重传幂等。"""
        with self._lock:
            _need({"receipt_no": receipt_no, "contract_id": contract_id,
                   "installment_id": installment_id, "amount": amount,
                   "currency": currency, "value_date": value_date})
            contract = self._contract(contract_id)
            self._currency(currency)
            amount_dec = parse_money(amount)
            value_date = parse_date(value_date, "value_date")
            inst = contract["schedule"].get(installment_id)
            if inst is None:
                raise ServiceError(404, "installment_not_found", f"分期不存在: {installment_id}")
            if inst["status"] == "cancelled":
                raise ServiceError(409, "installment_cancelled", f"分期已取消: {installment_id}")
            if currency != contract["currency"]:
                _need({"fx_rate": fx_rate})
                rate = parse_money(fx_rate, "fx_rate")
                contract_amount = amount_dec * rate
            else:
                rate = None
                contract_amount = amount_dec
            payload = {
                "receipt_no": receipt_no,
                "contract_id": contract_id,
                "installment_id": installment_id,
                "amount": money_str(amount_dec),
                "currency": currency,
                "fx_rate": money_str(rate) if rate is not None else None,
                "contract_amount": money_str(contract_amount),
                "memo": memo,
            }
            return self._record_financial(
                "sponsor_payment", payload, business_date=value_date,
                contract_id=contract_id, idem_key=f"receipt:{receipt_no}",
                event_id=event_id, received_at=received_at)

    def record_expense(self, contract_id, fund_use, amount, currency, business_date, *,
                       memo=None, event_id=None, received_at=None) -> dict:
        with self._lock:
            _need({"contract_id": contract_id, "fund_use": fund_use, "amount": amount,
                   "currency": currency, "business_date": business_date})
            self._contract(contract_id)
            self._fund_use(fund_use)
            self._currency(currency)
            business_date = parse_date(business_date)
            payload = {
                "contract_id": contract_id,
                "fund_use": fund_use,
                "amount": money_str(parse_money(amount)),
                "currency": currency,
                "memo": memo,
            }
            return self._record_financial(
                "expense", payload, business_date=business_date,
                contract_id=contract_id, event_id=event_id, received_at=received_at)

    def record_tax_withholding(self, contract_id, amount, currency, business_date, *,
                               authority=None, memo=None, event_id=None, received_at=None) -> dict:
        with self._lock:
            _need({"contract_id": contract_id, "amount": amount, "currency": currency,
                   "business_date": business_date})
            self._contract(contract_id)
            self._currency(currency)
            business_date = parse_date(business_date)
            payload = {
                "contract_id": contract_id,
                "amount": money_str(parse_money(amount)),
                "currency": currency,
                "authority": authority,
                "memo": memo,
            }
            return self._record_financial(
                "tax_withholding", payload, business_date=business_date,
                contract_id=contract_id, event_id=event_id, received_at=received_at)

    def record_refund(self, links_to, amount, business_date, *, reason=None,
                      event_id=None, received_at=None) -> dict:
        """退款：冲减目标流水的金额，与目标流水相互关联。"""
        with self._lock:
            _need({"links_to": links_to, "amount": amount, "business_date": business_date})
            target = self._entry(links_to)
            if target["kind"] not in ("payment", "expense", "tax_withholding"):
                raise ServiceError(400, "invalid_refund_target",
                                   f"流水类型 {target['kind']} 不支持退款")
            if target["reversed"]:
                raise ServiceError(409, "entry_reversed", "目标流水已被冲正，不能再退款")
            amount_dec = parse_money(amount)
            if target["refunded"] + amount_dec > target["amount"]:
                raise ServiceError(409, "refund_exceeds", "退款金额超过目标流水可退余额")
            business_date = parse_date(business_date)
            if business_date < target["business_date"]:
                raise ServiceError(400, "invalid_business_date", "退款业务日期不能早于目标流水")
            payload = {"amount": money_str(amount_dec), "reason": reason,
                       "target_kind": target["kind"], "contract_id": target["contract_id"]}
            return self._record_financial(
                "refund", payload, business_date=business_date,
                contract_id=target["contract_id"], links_to=links_to,
                event_id=event_id, received_at=received_at,
                min_period=target["period"])

    def record_reversal(self, links_to, business_date, *, reason=None,
                        event_id=None, received_at=None) -> dict:
        """冲正：全额抵消目标流水，留下相互关联的流水。"""
        with self._lock:
            _need({"links_to": links_to, "business_date": business_date})
            target = self._entry(links_to)
            if target["kind"] == "reversal":
                raise ServiceError(400, "invalid_reversal_target", "冲正流水不能再被冲正")
            if target["reversed"]:
                raise ServiceError(409, "entry_reversed", "目标流水已被冲正")
            if target["refunded"] > ZERO:
                raise ServiceError(409, "entry_refunded", "目标流水存在退款，请先冲正退款")
            business_date = parse_date(business_date)
            if business_date < target["business_date"]:
                raise ServiceError(400, "invalid_business_date", "冲正业务日期不能早于目标流水")
            payload = {"reason": reason, "target_kind": target["kind"],
                       "contract_id": target["contract_id"]}
            return self._record_financial(
                "reversal", payload, business_date=business_date,
                contract_id=target["contract_id"], links_to=links_to,
                event_id=event_id, received_at=received_at,
                min_period=target["period"])

    def record_fx_adjustment(self, links_to, amount, currency, business_date, *,
                             memo=None, event_id=None, received_at=None) -> dict:
        """汇率差额：与目标流水关联的汇兑损益（amount 可正可负）。"""
        with self._lock:
            _need({"links_to": links_to, "amount": amount, "currency": currency,
                   "business_date": business_date})
            target = self._entry(links_to)
            self._currency(currency)
            amount_dec = parse_money(amount, allow_negative=True)
            business_date = parse_date(business_date)
            if business_date < target["business_date"]:
                raise ServiceError(400, "invalid_business_date", "汇兑调整日期不能早于目标流水")
            payload = {"amount": money_str(amount_dec), "currency": currency,
                       "memo": memo, "contract_id": target["contract_id"]}
            return self._record_financial(
                "fx_adjustment", payload, business_date=business_date,
                contract_id=target["contract_id"], links_to=links_to,
                event_id=event_id, received_at=received_at,
                min_period=target["period"])

    # ------------------------------------------------------------------
    # 月度快照
    # ------------------------------------------------------------------

    def publish_snapshot(self, period, *, event_id=None, received_at=None) -> dict:
        """对外报出月度快照；发布后该月不再接受迟到事件改写。"""
        with self._lock:
            period = parse_month(period)
            if period in self.state.snapshots:
                raise ServiceError(409, "snapshot_exists", f"快照已发布: {period}")
            later = [p for p in self.state.snapshots if p > period]
            if later:
                raise ServiceError(409, "snapshot_order",
                                   f"已存在更晚期间的快照 {min(later)}，不能回补发布 {period}")
            snapshot = compute_snapshot(self.events, period, self.state.max_seq)
            snapshot["published_at"] = received_at or utc_now_iso()
            self._append("snapshot_published", snapshot,
                         business_date=utc_now_iso()[:10],
                         received_at=received_at, event_id=event_id)
            return {"snapshot": snapshot}

    def list_snapshots(self) -> dict:
        with self._lock:
            return {"snapshots": [
                {"period": p, "published_at": s["published_at"], "basis": s["basis"]}
                for p, s in sorted(self.state.snapshots.items())
            ]}

    def get_snapshot(self, period) -> dict:
        with self._lock:
            snapshot = self.state.snapshots.get(period)
            if snapshot is None:
                raise ServiceError(404, "snapshot_not_found", f"快照不存在: {period}")
            return {"snapshot": snapshot}

    def recompute_snapshot(self, period) -> dict:
        """按快照记录的水位线重放事件，验证历史快照仍可复算。"""
        with self._lock:
            snapshot = self.state.snapshots.get(period)
            if snapshot is None:
                raise ServiceError(404, "snapshot_not_found", f"快照不存在: {period}")
            recomputed = compute_snapshot(self.events, period, snapshot["basis"]["watermark"])
            matches = (recomputed["contracts"] == snapshot["contracts"]
                       and recomputed["basis"]["digest"] == snapshot["basis"]["digest"])
            return {
                "period": period,
                "matches": matches,
                "basis": snapshot["basis"],
                "snapshot_contracts": snapshot["contracts"],
                "recomputed_contracts": recomputed["contracts"],
            }

    # ------------------------------------------------------------------
    # 处置任务
    # ------------------------------------------------------------------

    def list_tasks(self, status=None, contract_id=None) -> dict:
        with self._lock:
            tasks = sorted(self.state.tasks.values(), key=lambda t: t["created_seq"])
            if status:
                tasks = [t for t in tasks if t["status"] == status]
            if contract_id:
                tasks = [t for t in tasks if t["contract_id"] == contract_id]
            return {"tasks": tasks}

    def confirm_task(self, task_id, decision, *, note=None, resolved_by=None,
                     event_id=None, received_at=None) -> dict:
        with self._lock:
            task = self.state.tasks.get(task_id)
            if task is None:
                raise ServiceError(404, "task_not_found", f"处置任务不存在: {task_id}")
            if task["status"] != "pending":
                raise ServiceError(409, "task_resolved", f"任务已处理，当前状态: {task['status']}")
            if decision not in ("confirmed", "rejected"):
                raise ServiceError(400, "invalid_decision", "decision 必须是 confirmed 或 rejected")
            payload = {"task_id": task_id, "decision": decision,
                       "note": note, "resolved_by": resolved_by}
            self._append("task_resolved", payload, business_date=utc_now_iso()[:10],
                         received_at=received_at, event_id=event_id)
            return {"task": self.state.tasks[task_id]}

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------

    def balances_view(self, athlete_id=None, contract_id=None, season=None) -> dict:
        """按运动员/合同/赛季查看承诺额、已用额、待验收款与可支配余额。"""
        with self._lock:
            selected = []
            for cid in sorted(self.state.contracts):
                c = self.state.contracts[cid]
                if athlete_id and c["athlete_id"] != athlete_id:
                    continue
                if contract_id and cid != contract_id:
                    continue
                if season and c["season"] != season:
                    continue
                selected.append(cid)
            views = [contract_view(self.state, cid) for cid in selected]
            totals: dict[str, dict] = {}

            def bucket(currency):
                return totals.setdefault(currency, {
                    "committed": ZERO, "received": ZERO, "used": ZERO,
                    "fx_difference": ZERO, "disposable": ZERO,
                    "pending_acceptance": ZERO, "receivable": ZERO,
                })

            for cid, view in zip(selected, views):
                home = bucket(view["currency"])
                home["committed"] += Decimal(view["committed"])
                home["pending_acceptance"] += Decimal(view["pending_acceptance"])
                home["receivable"] += Decimal(view["receivable"])
                for currency, amounts in view["by_currency"].items():
                    b = bucket(currency)
                    for key in ("received", "used", "fx_difference", "disposable"):
                        b[key] += Decimal(amounts[key])
            return {
                "filters": {"athlete_id": athlete_id, "contract_id": contract_id,
                            "season": season},
                "contracts": views,
                "totals_by_currency": {
                    cur: {k: money_str(v) for k, v in amounts.items()}
                    for cur, amounts in sorted(totals.items())
                },
            }

    def ledger_view(self, contract_id=None, athlete_id=None, season=None) -> dict:
        """流水台账：每笔流水附带过账后的可支配余额，构成余额的计算依据。"""
        with self._lock:
            running: dict[tuple[str, str], dict] = {}
            out = []
            for entry in sorted(self.state.entries, key=lambda e: e["seq"]):
                key = (entry["contract_id"], entry["currency"])
                bucket = running.setdefault(
                    key, {"received": ZERO, "used": ZERO, "fx_difference": ZERO})
                bucket[entry["bucket"]] += entry["amount"]
                if contract_id and entry["contract_id"] != contract_id:
                    continue
                if athlete_id and entry["athlete_id"] != athlete_id:
                    continue
                if season and entry["season"] != season:
                    continue
                disposable = bucket["received"] - bucket["used"] + bucket["fx_difference"]
                out.append(serialize_entry(entry, disposable))
            return {
                "filters": {"athlete_id": athlete_id, "contract_id": contract_id,
                            "season": season},
                "entries": out,
            }

    def list_events(self, event_type=None, period=None, contract_id=None) -> dict:
        with self._lock:
            events = self.events
            if event_type:
                events = [e for e in events if e["event_type"] == event_type]
            if period:
                events = [e for e in events if e["period"] == period]
            if contract_id:
                events = [e for e in events
                          if e["payload"].get("contract_id") == contract_id]
            return {"events": [serialize_event(e) for e in events]}
