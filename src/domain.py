"""领域核心：事件模型、状态投影与余额/快照计算。

账本采用事件溯源：所有业务事实以追加事件的方式写入日志，
当前视图与历史月度快照都由同一套事件流重放（fold）得到，
因此进程重启后，历史快照与每笔余额的计算依据仍可复算。

关键约定：
- 每个事件同时携带业务时间（business_date）与接收时间（received_at），
  并被分配一个单调递增的 seq（接收顺序）。
- 每个事件归属一个“确认期间” period（YYYY-MM）：取业务月份，若该月
  快照已对外报出，则顺延到第一个未报出的月份。迟到事件因此永远不会
  改写已发布的月度快照。
- 金额使用 Decimal，精度取自 reference/domain.json 的 quantity_precision。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# 参考数据（公开枚举）与金额精度
# ---------------------------------------------------------------------------

DEFAULT_REFERENCE = {
    "event_types": ["milestone_approved", "sponsor_payment", "expense", "refund", "tax_withholding"],
    "fund_uses": ["training", "travel", "equipment", "tax_reserve"],
    "currencies": ["CNY", "HKD", "USD"],
    "quantity_precision": 6,
}


def reference_path() -> Path:
    return Path(__file__).resolve().parents[1] / "reference" / "domain.json"


def load_reference(path: str | Path | None = None) -> dict:
    candidate = Path(path) if path else reference_path()
    data = None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        return dict(DEFAULT_REFERENCE)
    merged = dict(DEFAULT_REFERENCE)
    merged.update(data)
    return merged


PRECISION = int(load_reference().get("quantity_precision", 6))
QUANT = Decimal(1).scaleb(-PRECISION)
ZERO = Decimal("0").quantize(QUANT)

# 会产生账本流水的财务事件类型
FINANCIAL_TYPES = (
    "sponsor_payment",
    "expense",
    "tax_withholding",
    "refund",
    "reversal",
    "fx_adjustment",
)

# 余额桶：到账 / 已用 / 汇率差额
BUCKETS = ("received", "used", "fx_difference")


# ---------------------------------------------------------------------------
# 基础解析工具
# ---------------------------------------------------------------------------

def parse_money(value, field: str = "amount", *, allow_negative: bool = False,
                allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数值")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} 必须是数值") from None
    if amount.is_nan() or amount.is_infinite():
        raise ValueError(f"{field} 必须是有限数值")
    amount = amount.quantize(QUANT)
    if amount < ZERO and not allow_negative:
        raise ValueError(f"{field} 不能为负数")
    if amount == ZERO and not allow_zero:
        raise ValueError(f"{field} 不能为零")
    return amount


def money_str(value: Decimal) -> str:
    return str(value.quantize(QUANT))


def parse_date(value, field: str = "business_date") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} 必须是合法日期 (YYYY-MM-DD)") from None
    return value


def parse_month(value, field: str = "period") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM 字符串")
    try:
        year, month = value.split("-")
        _dt.date(int(year), int(month), 1)
    except (ValueError, TypeError):
        raise ValueError(f"{field} 必须是合法月份 (YYYY-MM)") from None
    return value


def parse_timestamp(value, field: str = "received_at") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO 8601 时间戳字符串")
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} 必须是 ISO 8601 时间戳") from None
    return value


def month_of(date_str: str) -> str:
    return date_str[:7]


def next_month(ym: str) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 状态投影：事件 -> 内存状态
# ---------------------------------------------------------------------------

class State:
    """由事件流重放得到的内存投影。"""

    def __init__(self) -> None:
        self.athletes: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}
        self.entries: list[dict] = []            # 财务流水，按 seq 顺序
        self.entries_by_id: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}     # period -> 已发布快照
        self.idempotency: dict[str, str] = {}    # 幂等键 -> event_id
        self.max_seq: int = 0


def apply_event(state: State, event: dict) -> None:
    state.max_seq = max(state.max_seq, int(event["seq"]))
    state.idempotency[f"id:{event['event_id']}"] = event["event_id"]
    key = event.get("idempotency_key")
    if key:
        state.idempotency[f"key:{key}"] = event["event_id"]
    applier = _APPLIERS.get(event["event_type"])
    if applier is not None:
        applier(state, event)


def replay(events) -> State:
    state = State()
    for event in sorted(events, key=lambda e: e["seq"]):
        apply_event(state, event)
    return state


def _add_entry(state: State, event: dict, contract: dict, *, kind: str, bucket: str,
               amount: Decimal, currency: str, fund_use=None, memo=None,
               receipt_no=None, installment_id=None, contract_amount=None) -> dict:
    entry = {
        "entry_id": event["event_id"],
        "seq": event["seq"],
        "kind": kind,
        "bucket": bucket,
        "amount": amount,                 # Decimal，带符号
        "currency": currency,
        "contract_id": contract["contract_id"],
        "athlete_id": contract["athlete_id"],
        "season": contract["season"],
        "fund_use": fund_use,
        "memo": memo,
        "receipt_no": receipt_no,
        "installment_id": installment_id,
        "contract_amount": contract_amount,  # 合同币种金额（仅赞助到账）
        "links_to": event.get("links_to"),
        "business_date": event["business_date"],
        "period": event["period"],
        "received_at": event["received_at"],
        "refunded": ZERO,
        "reversed": False,
    }
    state.entries.append(entry)
    state.entries_by_id[entry["entry_id"]] = entry
    return entry


def _apply_athlete_registered(state: State, event: dict) -> None:
    p = event["payload"]
    state.athletes[p["athlete_id"]] = {
        "athlete_id": p["athlete_id"],
        "name": p["name"],
        "base_currency": p.get("base_currency"),
    }


def _apply_contract_registered(state: State, event: dict) -> None:
    p = event["payload"]
    schedule = {}
    for item in p["installments"]:
        schedule[item["installment_id"]] = {
            "installment_id": item["installment_id"],
            "milestone": item["milestone"],
            "amount": parse_money(item["amount"]),
            "due_date": item["due_date"],
            "status": "scheduled",        # scheduled -> approved；cancelled 为终态
            "paid_amount": ZERO,
        }
    threshold = p.get("warning_threshold")
    state.contracts[p["contract_id"]] = {
        "contract_id": p["contract_id"],
        "athlete_id": p["athlete_id"],
        "season": p["season"],
        "currency": p["currency"],
        "warning_threshold": parse_money(threshold, allow_zero=True) if threshold is not None else None,
        "schedule": schedule,
        "versions": [{"effective_date": p["start_date"], "installments": p["installments"]}],
        "registered_period": event["period"],
    }


def _apply_contract_amended(state: State, event: dict) -> None:
    """合同变更：只重写生效日及之后、且仍未验收的分期。"""
    p = event["payload"]
    contract = state.contracts[p["contract_id"]]
    effective = p["effective_date"]
    incoming_ids = set()
    for item in p["installments"]:
        iid = item["installment_id"]
        incoming_ids.add(iid)
        existing = contract["schedule"].get(iid)
        if existing is not None and existing["status"] != "scheduled":
            continue  # 已验收/已取消的分期不随变更改动
        if existing is not None and existing["due_date"] < effective:
            continue  # 生效日之前的分期保持原样
        paid = existing["paid_amount"] if existing is not None else ZERO
        contract["schedule"][iid] = {
            "installment_id": iid,
            "milestone": item["milestone"],
            "amount": parse_money(item["amount"]),
            "due_date": item["due_date"],
            "status": "scheduled",
            "paid_amount": paid,
        }
    # 变更清单未覆盖的未来分期视为取消
    for iid, inst in contract["schedule"].items():
        if inst["status"] == "scheduled" and inst["due_date"] >= effective and iid not in incoming_ids:
            inst["status"] = "cancelled"
    contract["versions"].append({"effective_date": effective, "installments": p["installments"]})


def _apply_milestone_approved(state: State, event: dict) -> None:
    p = event["payload"]
    contract = state.contracts.get(p["contract_id"])
    if contract is None:
        return
    inst = contract["schedule"].get(p["installment_id"])
    if inst is not None and inst["status"] == "scheduled":
        inst["status"] = "approved"


def _apply_sponsor_payment(state: State, event: dict) -> None:
    p = event["payload"]
    contract = state.contracts[p["contract_id"]]
    amount = parse_money(p["amount"])
    contract_amount = parse_money(p["contract_amount"])
    _add_entry(
        state, event, contract,
        kind="payment", bucket="received", amount=amount, currency=p["currency"],
        memo=p.get("memo"), receipt_no=p.get("receipt_no"),
        installment_id=p.get("installment_id"), contract_amount=contract_amount,
    )
    inst = contract["schedule"].get(p["installment_id"])
    if inst is not None:
        inst["paid_amount"] = inst["paid_amount"] + contract_amount


def _apply_expense(state: State, event: dict) -> None:
    p = event["payload"]
    contract = state.contracts[p["contract_id"]]
    _add_entry(
        state, event, contract,
        kind="expense", bucket="used", amount=parse_money(p["amount"]),
        currency=p["currency"], fund_use=p.get("fund_use"), memo=p.get("memo"),
    )


def _apply_tax_withholding(state: State, event: dict) -> None:
    p = event["payload"]
    contract = state.contracts[p["contract_id"]]
    _add_entry(
        state, event, contract,
        kind="tax_withholding", bucket="used", amount=parse_money(p["amount"]),
        currency=p["currency"], fund_use="tax_reserve", memo=p.get("memo"),
    )


def _apply_refund(state: State, event: dict) -> None:
    p = event["payload"]
    target = state.entries_by_id[event["links_to"]]
    contract = state.contracts[target["contract_id"]]
    amount = parse_money(p["amount"])
    target["refunded"] = target["refunded"] + amount
    _add_entry(
        state, event, contract,
        kind="refund", bucket=target["bucket"], amount=-amount,
        currency=target["currency"], memo=p.get("reason"),
    )


def _apply_reversal(state: State, event: dict) -> None:
    target = state.entries_by_id[event["links_to"]]
    contract = state.contracts[target["contract_id"]]
    target["reversed"] = True
    _add_entry(
        state, event, contract,
        kind="reversal", bucket=target["bucket"], amount=-target["amount"],
        currency=target["currency"], memo=event["payload"].get("reason"),
    )
    # 冲正到账流水时，同步回滚对应分期的已付金额
    if target["kind"] == "payment" and target.get("installment_id"):
        inst = contract["schedule"].get(target["installment_id"])
        if inst is not None and target.get("contract_amount") is not None:
            inst["paid_amount"] = inst["paid_amount"] - target["contract_amount"]


def _apply_fx_adjustment(state: State, event: dict) -> None:
    p = event["payload"]
    target = state.entries_by_id[event["links_to"]]
    contract = state.contracts[target["contract_id"]]
    _add_entry(
        state, event, contract,
        kind="fx_adjustment", bucket="fx_difference",
        amount=parse_money(p["amount"], allow_negative=True),
        currency=p["currency"], memo=p.get("memo"),
    )


def _apply_snapshot_published(state: State, event: dict) -> None:
    snapshot = event["payload"]
    state.snapshots[snapshot["period"]] = snapshot


def _apply_task_created(state: State, event: dict) -> None:
    p = event["payload"]
    state.tasks[p["task_id"]] = {
        "task_id": p["task_id"],
        "kind": p["kind"],
        "status": "pending",
        "contract_id": p["contract_id"],
        "athlete_id": p["athlete_id"],
        "season": p["season"],
        "currency": p["currency"],
        "threshold": p.get("threshold"),
        "disposable": p["disposable"],
        "message": p["message"],
        "created_at": event["received_at"],
        "created_seq": event["seq"],
        "resolution": None,
    }


def _apply_task_resolved(state: State, event: dict) -> None:
    p = event["payload"]
    task = state.tasks.get(p["task_id"])
    if task is None:
        return
    task["status"] = p["decision"]
    task["resolution"] = {
        "note": p.get("note"),
        "resolved_by": p.get("resolved_by"),
        "resolved_at": event["received_at"],
    }


_APPLIERS = {
    "athlete_registered": _apply_athlete_registered,
    "contract_registered": _apply_contract_registered,
    "contract_amended": _apply_contract_amended,
    "milestone_approved": _apply_milestone_approved,
    "sponsor_payment": _apply_sponsor_payment,
    "expense": _apply_expense,
    "tax_withholding": _apply_tax_withholding,
    "refund": _apply_refund,
    "reversal": _apply_reversal,
    "fx_adjustment": _apply_fx_adjustment,
    "snapshot_published": _apply_snapshot_published,
    "task_created": _apply_task_created,
    "task_resolved": _apply_task_resolved,
}


# ---------------------------------------------------------------------------
# 余额与视图计算
# ---------------------------------------------------------------------------

def contract_money(state: State, contract_id: str) -> dict:
    """按合同计算各金额口径（Decimal 形式）。"""
    contract = state.contracts[contract_id]
    committed = ZERO
    pending_acceptance = ZERO
    receivable = ZERO
    for inst in contract["schedule"].values():
        if inst["status"] == "cancelled":
            continue
        committed += inst["amount"]
        remaining = inst["amount"] - inst["paid_amount"]
        if remaining < ZERO:
            remaining = ZERO
        if inst["status"] == "scheduled":
            pending_acceptance += remaining
        else:
            receivable += remaining
    by_currency: dict[str, dict] = {}
    for entry in state.entries:
        if entry["contract_id"] != contract_id:
            continue
        bucket = by_currency.setdefault(
            entry["currency"], {"received": ZERO, "used": ZERO, "fx_difference": ZERO})
        bucket[entry["bucket"]] += entry["amount"]
    for bucket in by_currency.values():
        bucket["disposable"] = bucket["received"] - bucket["used"] + bucket["fx_difference"]
    return {
        "committed": committed,
        "pending_acceptance": pending_acceptance,
        "receivable": receivable,
        "by_currency": by_currency,
    }


def contract_view(state: State, contract_id: str) -> dict:
    """合同视图（JSON 可序列化）：承诺额/已用额/待验收款/可支配余额。"""
    contract = state.contracts[contract_id]
    money = contract_money(state, contract_id)
    currency = contract["currency"]
    home = money["by_currency"].get(
        currency, {"received": ZERO, "used": ZERO, "fx_difference": ZERO, "disposable": ZERO})
    threshold = contract["warning_threshold"]
    disposable = home["disposable"]
    if disposable < ZERO:
        status = "negative"
    elif threshold is not None and disposable < threshold:
        status = "warning"
    else:
        status = "ok"
    return {
        "contract_id": contract_id,
        "athlete_id": contract["athlete_id"],
        "season": contract["season"],
        "currency": currency,
        "warning_threshold": money_str(threshold) if threshold is not None else None,
        "committed": money_str(money["committed"]),
        "received": money_str(home["received"]),
        "used": money_str(home["used"]),
        "fx_difference": money_str(home["fx_difference"]),
        "disposable": money_str(disposable),
        "pending_acceptance": money_str(money["pending_acceptance"]),
        "receivable": money_str(money["receivable"]),
        "status": status,
        "by_currency": {
            cur: {key: money_str(val) for key, val in sorted(bucket.items())}
            for cur, bucket in sorted(money["by_currency"].items())
        },
    }


def compute_snapshot(events, period: str, watermark: int) -> dict:
    """计算 period 月度快照：重放 seq <= watermark 且确认期间 <= period 的事件。

    watermark 与事件摘要共同构成快照的“计算依据”，重启后按同一口径
    重放即可逐字复算。
    """
    included = [e for e in events if e["seq"] <= watermark and e["period"] <= period]
    included.sort(key=lambda e: e["seq"])
    state = replay(included)
    digest = hashlib.sha256(
        "\n".join(e["event_id"] for e in included).encode("utf-8")).hexdigest()
    return {
        "period": period,
        "basis": {
            "watermark": watermark,
            "event_count": len(included),
            "digest": digest,
        },
        "contracts": [contract_view(state, cid) for cid in sorted(state.contracts)],
    }
