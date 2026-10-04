"""纹样授权计费结算服务的 HTTP/JSON 边界（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .models import BillingEntry, Claim, Dispute, Distribution, Payment, UsageRecord
from .service import SettlementService
from .storage import Database


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return {key: _jsonable(item) for key, item in value.__dict__.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def route(service: SettlementService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到结算领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    if headers.get("X-Actor-Id"):
        body["actor_id"] = headers["X-Actor-Id"]
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    p = parsed.path

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and p == "/health":
            valid, count = service.verify_audit()
            balanced, balances = service.accounting_balances()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count,
                         "ledger_balanced": balanced, "balances": balances}

        # ---- 主数据 ----
        if method == "POST" and p == "/actors":
            receipt = service.register_actor(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/works":
            return _receipt(service.register_work(**body))
        if method == "POST" and p == "/rate-cards":
            return _receipt(service.register_rate_card(**body))
        if method == "POST" and p == "/work-versions":
            return _receipt(service.register_work_version(**body))
        if method == "POST" and p == "/licensees":
            return _receipt(service.register_licensee(**body))
        if method == "POST" and p == "/licenses":
            return _receipt(service.register_license(**body))
        if method == "POST" and p == "/license-status":
            return _receipt(service.update_license_status(**body))

        # ---- 用量与计费 ----
        if method == "POST" and p == "/usage-imports":
            return _receipt(service.import_usage(**body))
        if method == "POST" and p == "/billing":
            return 201, service.bill_period(**body)

        # ---- 追偿 ----
        if method == "POST" and p == "/claims/assess":
            return _receipt(service.assess_claim(**body))
        if method == "POST" and p == "/claims/recover":
            return _receipt(service.recover_claim(**body))
        if method == "POST" and p == "/claims/write-off":
            return _receipt(service.write_off_claim(**body))

        # ---- 争议 ----
        if method == "POST" and p == "/disputes":
            return _receipt(service.open_dispute(**body))
        if method == "POST" and p == "/disputes/resolve":
            return _receipt(service.resolve_dispute(**body))

        # ---- 关账与队列 ----
        if method == "POST" and p == "/periods/close":
            return _receipt(service.close_period(**body))
        if method == "POST" and p == "/queues/close":
            return 200, service.process_close_queue(**body)
        if method == "POST" and p == "/queues/disputes":
            return 200, service.process_dispute_queue(**body)
        if method == "POST" and p == "/queues/payments":
            return 200, service.process_payment_queue(**body)

        # ---- 回款、核销、付款 ----
        if method == "POST" and p == "/cash-receipts":
            return _receipt(service.receive_cash(**body))
        if method == "POST" and p == "/receivables/write-off":
            return _receipt(service.write_off_receivable(**body))
        if method == "POST" and p == "/payouts/enqueue":
            return 200, service.enqueue_payouts(**body)

        # ---- 查询 ----
        if method == "GET" and p == "/usage":
            items = service.list_usage(body["actor_id"], q("period_key"))
            return 200, {"items": _jsonable(items)}
        if method == "GET" and p == "/entries":
            items = service.list_entries(body["actor_id"], q("period_key"))
            return 200, {"items": _jsonable(items)}
        if method == "GET" and p.startswith("/entries/") and p.endswith("/explain"):
            entry_id = p.split("/")[2]
            return 200, _jsonable(service.explain_entry(body["actor_id"], entry_id))
        if method == "GET" and p == "/distributions":
            items = service.list_distributions(body["actor_id"], q("period_key"))
            return 200, {"items": _jsonable(items)}
        if method == "GET" and p == "/claims":
            return 200, {"items": _jsonable(service.list_claims(body["actor_id"]))}
        if method == "GET" and p == "/payments":
            return 200, {"items": _jsonable(service.list_payments(body["actor_id"]))}
        if method == "GET" and p.startswith("/settlements/"):
            period_key = p.rsplit("/", 1)[-1]
            return 200, _jsonable(service.settlement_summary(body["actor_id"], period_key))
        if method == "GET" and p == "/recompute":
            period_key = q("period_key")
            if not period_key:
                return 400, {"error": "validation_error", "message": "period_key 不能为空"}
            return 200, _jsonable(service.recompute_period(body["actor_id"], period_key))
        if method == "GET" and p == "/audit-events":
            return 200, {"items": service.audit_events(int(q("after_sequence", "0") or "0"))}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.replayed else 201, receipt.__dict__


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: SettlementService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed_body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, parsed_body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="启动纹样授权计费结算服务")
    parser.add_argument("--database", default="settlement.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = SettlementService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
