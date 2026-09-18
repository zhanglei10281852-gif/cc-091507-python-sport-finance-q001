from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from service import Service, ServiceError, serialize_event

SERVICE_NAME = '运动员赞助资金管控台'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def _financial_result(result: dict):
    status = 201 if result["created"] else 200
    return status, {
        "duplicate": not result["created"],
        "event": serialize_event(result["event"]),
        "tasks_created": result["tasks_created"],
    }


def _post_athlete(service, params, query, body):
    result = service.register_athlete(
        body.get("athlete_id"), body.get("name"), body.get("base_currency"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return 201, result


def _get_athletes(service, params, query, body):
    return 200, service.list_athletes()


def _post_contract(service, params, query, body):
    result = service.register_contract(
        body.get("contract_id"), body.get("athlete_id"), body.get("season"),
        body.get("currency"), body.get("installments"),
        warning_threshold=body.get("warning_threshold"),
        start_date=body.get("start_date"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return 201, result


def _get_contract(service, params, query, body):
    return 200, {"contract": service.get_contract(params["contract_id"])}


def _post_amendment(service, params, query, body):
    result = service.amend_contract(
        params["contract_id"], body.get("effective_date"), body.get("installments"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return 201, result


def _post_milestone(service, params, query, body):
    result = service.approve_milestone(
        body.get("contract_id"), body.get("installment_id"), body.get("business_date"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return (201 if result["created"] else 200), {
        "duplicate": not result["created"], "event": result["event"]}


def _post_payment(service, params, query, body):
    result = service.record_payment(
        body.get("receipt_no"), body.get("contract_id"), body.get("installment_id"),
        body.get("amount"), body.get("currency"), body.get("value_date"),
        fx_rate=body.get("fx_rate"), memo=body.get("memo"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return _financial_result(result)


def _post_expense(service, params, query, body):
    result = service.record_expense(
        body.get("contract_id"), body.get("fund_use"), body.get("amount"),
        body.get("currency"), body.get("business_date"), memo=body.get("memo"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return _financial_result(result)


def _post_tax(service, params, query, body):
    result = service.record_tax_withholding(
        body.get("contract_id"), body.get("amount"), body.get("currency"),
        body.get("business_date"), authority=body.get("authority"),
        memo=body.get("memo"), event_id=body.get("event_id"),
        received_at=body.get("received_at"))
    return _financial_result(result)


def _post_refund(service, params, query, body):
    result = service.record_refund(
        body.get("links_to"), body.get("amount"), body.get("business_date"),
        reason=body.get("reason"), event_id=body.get("event_id"),
        received_at=body.get("received_at"))
    return _financial_result(result)


def _post_reversal(service, params, query, body):
    result = service.record_reversal(
        body.get("links_to"), body.get("business_date"), reason=body.get("reason"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return _financial_result(result)


def _post_fx(service, params, query, body):
    result = service.record_fx_adjustment(
        body.get("links_to"), body.get("amount"), body.get("currency"),
        body.get("business_date"), memo=body.get("memo"),
        event_id=body.get("event_id"), received_at=body.get("received_at"))
    return _financial_result(result)


def _get_ledger(service, params, query, body):
    return 200, service.ledger_view(
        contract_id=query.get("contract_id"), athlete_id=query.get("athlete_id"),
        season=query.get("season"))


def _get_balances(service, params, query, body):
    return 200, service.balances_view(
        athlete_id=query.get("athlete_id"), contract_id=query.get("contract_id"),
        season=query.get("season"))


def _get_events(service, params, query, body):
    return 200, service.list_events(
        event_type=query.get("event_type"), period=query.get("period"),
        contract_id=query.get("contract_id"))


def _post_publish(service, params, query, body):
    result = service.publish_snapshot(
        body.get("period"), event_id=body.get("event_id"),
        received_at=body.get("received_at"))
    return 201, result


def _get_snapshots(service, params, query, body):
    return 200, service.list_snapshots()


def _get_snapshot(service, params, query, body):
    return 200, service.get_snapshot(params["period"])


def _get_recompute(service, params, query, body):
    return 200, service.recompute_snapshot(params["period"])


def _get_tasks(service, params, query, body):
    return 200, service.list_tasks(status=query.get("status"),
                                   contract_id=query.get("contract_id"))


def _post_confirm(service, params, query, body):
    result = service.confirm_task(
        params["task_id"], body.get("decision"), note=body.get("note"),
        resolved_by=body.get("resolved_by"), event_id=body.get("event_id"),
        received_at=body.get("received_at"))
    return 200, result


ROUTES = [
    ("GET", re.compile(r"^/health$"), lambda s, p, q, b: (200, health_payload())),
    ("POST", re.compile(r"^/athletes$"), _post_athlete),
    ("GET", re.compile(r"^/athletes$"), _get_athletes),
    ("POST", re.compile(r"^/contracts$"), _post_contract),
    ("GET", re.compile(r"^/contracts/(?P<contract_id>[^/]+)$"), _get_contract),
    ("POST", re.compile(r"^/contracts/(?P<contract_id>[^/]+)/amendments$"), _post_amendment),
    ("POST", re.compile(r"^/events/milestone-approvals$"), _post_milestone),
    ("POST", re.compile(r"^/events/sponsor-payments$"), _post_payment),
    ("POST", re.compile(r"^/events/expenses$"), _post_expense),
    ("POST", re.compile(r"^/events/tax-withholdings$"), _post_tax),
    ("POST", re.compile(r"^/events/refunds$"), _post_refund),
    ("POST", re.compile(r"^/events/reversals$"), _post_reversal),
    ("POST", re.compile(r"^/events/fx-adjustments$"), _post_fx),
    ("GET", re.compile(r"^/ledger$"), _get_ledger),
    ("GET", re.compile(r"^/balances$"), _get_balances),
    ("GET", re.compile(r"^/events$"), _get_events),
    ("POST", re.compile(r"^/snapshots/publish$"), _post_publish),
    ("GET", re.compile(r"^/snapshots$"), _get_snapshots),
    ("GET", re.compile(r"^/snapshots/(?P<period>\d{4}-\d{2})$"), _get_snapshot),
    ("GET", re.compile(r"^/snapshots/(?P<period>\d{4}-\d{2})/recompute$"), _get_recompute),
    ("GET", re.compile(r"^/tasks$"), _get_tasks),
    ("POST", re.compile(r"^/tasks/(?P<task_id>[^/]+)/confirm$"), _post_confirm),
]


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        path = self.path.split("?", 1)[0]
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs
            query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
        try:
            body = self._read_body()
            status, payload = self._route(path, query, body)
        except ServiceError as exc:
            status, payload = exc.status, {"error": {"code": exc.code, "message": exc.message}}
        except ValueError as exc:
            status, payload = 400, {"error": {"code": "invalid_input", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - 兜底，保证始终返回 JSON
            status, payload = 500, {"error": {"code": "internal", "message": str(exc)}}
        self._send(status, payload)

    def _route(self, path: str, query: dict, body: dict):
        for method, pattern, handler in ROUTES:
            if method != self.command:
                continue
            match = pattern.match(path)
            if match:
                return handler(self.server.service, match.groupdict(), query, body)
        raise ServiceError(404, "not_found", f"接口不存在: {self.command} {path}")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体必须是合法的 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: Service | None = None) -> ThreadingHTTPServer:
    if service is None:
        service = Service(os.getenv("RUNTIME_DIR", ".runtime"))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.service = service
    return server
