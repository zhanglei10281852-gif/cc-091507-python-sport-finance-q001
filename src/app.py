from __future__ import annotations

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ledger.service import ApiError, LedgerService

SERVICE_NAME = '运动员赞助资金管控台'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


Handler = Callable[[dict[str, Any], dict[str, str]], tuple[int, Any]]


class Api:
    """HTTP 路由与请求级幂等。"""

    def __init__(self, service: LedgerService) -> None:
        self.service = service
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = [
            ("GET", re.compile(r"^/health$"), lambda b, q: (200, health_payload())),
            ("POST", re.compile(r"^/api/athletes$"), lambda b, q: (201, service.register_athlete(b))),
            ("GET", re.compile(r"^/api/athletes$"), lambda b, q: (200, {"athletes": service.list_athletes()})),
            ("POST", re.compile(r"^/api/contracts$"), lambda b, q: (201, service.create_contract(b))),
            ("GET", re.compile(r"^/api/contracts$"), lambda b, q: (200, {"contracts": service.list_contracts()})),
            ("GET", re.compile(r"^/api/contracts/(?P<cid>[^/]+)$"), lambda b, q: (200, service.get_contract(q["cid"]))),
            ("POST", re.compile(r"^/api/contracts/(?P<cid>[^/]+)/amendments$"), lambda b, q: (200, service.amend_contract(q["cid"], b))),
            ("GET", re.compile(r"^/api/contracts/(?P<cid>[^/]+)/ledger$"), lambda b, q: (200, {"entries": service.get_ledger(contract_id=q["cid"])})),
            ("POST", re.compile(r"^/api/milestones/submit$"), lambda b, q: (200, service.submit_milestone(b))),
            ("POST", re.compile(r"^/api/milestones/approve$"), lambda b, q: (200, service.approve_milestone(b))),
            ("POST", re.compile(r"^/api/payments$"), lambda b, q: (200, service.record_payment(b))),
            ("POST", re.compile(r"^/api/expenses$"), lambda b, q: (200, service.record_expense(b))),
            ("POST", re.compile(r"^/api/tax-withholdings$"), lambda b, q: (200, service.record_tax_withholding(b))),
            ("POST", re.compile(r"^/api/refunds$"), lambda b, q: (200, service.record_refund(b))),
            ("POST", re.compile(r"^/api/reversals$"), lambda b, q: (200, service.record_reversal(b))),
            ("POST", re.compile(r"^/api/fx-rates$"), lambda b, q: (201, service.set_fx_rate(b))),
            ("GET", re.compile(r"^/api/fx-rates$"), lambda b, q: (200, {"rates": service.list_fx_rates(q.get("base"), q.get("quote"))})),
            ("GET", re.compile(r"^/api/balances$"), lambda b, q: (200, service.get_balances(q.get("athlete_id"), q.get("contract_id"), q.get("season")))),
            ("GET", re.compile(r"^/api/ledger$"), lambda b, q: (200, {"entries": service.get_ledger(q.get("athlete_id"), q.get("contract_id"), q.get("season"))})),
            ("POST", re.compile(r"^/api/snapshots$"), lambda b, q: (201, service.publish_snapshot(b))),
            ("GET", re.compile(r"^/api/snapshots$"), lambda b, q: (200, {"snapshots": service.list_snapshots()})),
            ("GET", re.compile(r"^/api/snapshots/(?P<sid>[^/]+)$"), lambda b, q: (200, service.get_snapshot(q["sid"]))),
            ("GET", re.compile(r"^/api/snapshots/(?P<sid>[^/]+)/verify$"), lambda b, q: (200, service.verify_snapshot(q["sid"]))),
            ("GET", re.compile(r"^/api/tasks$"), lambda b, q: (200, {"tasks": service.list_tasks(q.get("status"), q.get("athlete_id"))})),
            ("POST", re.compile(r"^/api/tasks/(?P<tid>[^/]+)/resolve$"), lambda b, q: (200, service.resolve_task(q["tid"], b))),
        ]

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[int, Any, dict[str, str]]:
        matched_path = False
        for route_method, pattern, handler in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            matched_path = True
            if route_method != method:
                continue
            scope = f"{method} {path}"
            if method == "POST" and idempotency_key:
                cached = self.service.idempotency.get(scope, idempotency_key)
                if cached is not None:
                    return cached["status"], cached["body"], {"Idempotency-Replayed": "true"}
            params = {**query, **match.groupdict()}
            status, payload = handler(body, params)
            response_body = payload if isinstance(payload, dict) else {"data": payload}
            if method == "POST" and idempotency_key and status < 500:
                self.service.idempotency.put(scope, idempotency_key, status, response_body)
            return status, response_body, {}
        if matched_path:
            raise ApiError(405, "method_not_allowed", f"方法不允许: {method} {path}")
        raise ApiError(404, "not_found", f"路径不存在: {method} {path}")


class RequestHandler(BaseHTTPRequestHandler):
    api: Api  # 由 create_server 注入

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            body = self._read_body()
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            idempotency_key = self.headers.get("Idempotency-Key")
            if not idempotency_key and isinstance(body, dict):
                raw = body.get("request_id")
                idempotency_key = str(raw) if raw else None
            status, payload, extra = self.api.handle(
                self.command, parsed.path, query, body if isinstance(body, dict) else {}, idempotency_key
            )
            self._send(status, payload, extra)
        except ApiError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, {})
        except Exception:  # noqa: BLE001 - 兜底，保证服务不中断
            traceback.print_exc()
            self._send(500, {"error": {"code": "internal_error", "message": "服务内部错误"}}, {})

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON")

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str,
    port: int,
    service: LedgerService | None = None,
) -> ThreadingHTTPServer:
    if service is None:
        service = LedgerService(runtime_dir=os.getenv("RUNTIME_DIR", ".runtime"))
    handler = type("BoundRequestHandler", (RequestHandler,), {"api": Api(service)})
    return ThreadingHTTPServer((host, port), handler)
