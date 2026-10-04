"""纹样授权计费服务的 HTTP/JSON 边界，仅依赖 Python 标准库。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from creative_program_foundation.errors import DomainError, ValidationError

from .service import LicensingService
from .storage import LicensingDatabase


def _dataclass_items(items) -> list[dict[str, Any]]:
    converted = []
    for item in items:
        data = item.__dict__
        if "allocations" in data:
            data["allocations"] = [{"holder_id": holder_id, "amount_cents": amount}
                                   for holder_id, amount in data["allocations"]]
        converted.append(data)
    return converted


def route(service: LicensingService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到授权计费领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    p = parsed.path
    try:
        if method == "GET" and p == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        post_routes = {
            "/principals": service.register_principal,
            "/holders": service.register_holder,
            "/works": service.register_work,
            "/work-versions": service.register_work_version,
            "/entitlement-versions": service.register_entitlement_version,
            "/control-groups": service.register_control_group,
            "/licensees": service.register_licensee,
            "/licenses": service.register_license,
            "/license-terminations": service.terminate_license,
            "/rate-versions": service.register_rate_version,
            "/deduction-rules": service.register_deduction_rule,
            "/guarantees": service.register_guarantee,
            "/reporting-cycles": service.register_reporting_cycle,
            "/usage-batches": service.import_usage_batch,
            "/periods/close": service.close_period,
            "/disputes": service.open_dispute,
            "/disputes/resolve": service.resolve_dispute,
            "/orders/mark-paid": service.mark_order_paid,
            "/claims/resolve": service.resolve_claim,
        }
        if method == "POST" and p in post_routes:
            body.pop("actor_id", None)  # actor_id 只取自 X-Actor-Id 头
            receipt = post_routes[p](actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        if method == "GET" and p == "/entries":
            period_key = query.get("period_key", [None])[0]
            licensee_id = query.get("licensee_id", [None])[0]
            items = service.list_entries(actor_id, period_key, licensee_id)
            return 200, {"items": _dataclass_items(items)}
        if method == "GET" and p.startswith("/entries/"):
            entry_id = p.split("/", 2)[2]
            return 200, service.explain_entry(actor_id, entry_id)
        if method == "GET" and p == "/facts":
            period_key = query.get("period_key", [None])[0]
            return 200, {"items": _dataclass_items(service.list_facts(actor_id, period_key))}
        if method == "GET" and p == "/claims":
            status = query.get("status", [None])[0]
            return 200, {"items": service.list_claims(actor_id, status)}
        if method == "GET" and p == "/disputes":
            status = query.get("status", [None])[0]
            return 200, {"items": service.list_disputes(actor_id, status)}
        if method == "GET" and p == "/orders":
            period_key = query.get("period_key", [None])[0]
            return 200, {"items": service.list_orders(actor_id, period_key)}
        if method == "GET" and p == "/queue":
            limit = int(query.get("limit", ["50"])[0])
            return 200, {"items": service.order_queue(actor_id, limit)}
        if method == "GET" and p == "/totals":
            period_key = query.get("period_key", [None])[0]
            return 200, service.totals(actor_id, period_key)
        if method == "GET" and p.startswith("/periods/") and p.endswith("/recalculate"):
            period_key = p.rsplit("/", 2)[1]
            return 200, service.recalculate_period(actor_id, period_key)
        if method == "GET" and p == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            rows = service.database.connection.execute(
                "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after,)).fetchall()
            items = [{"sequence": row["sequence"], "event_id": row["event_id"],
                      "actor_id": row["actor_id"], "action": row["action"],
                      "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                      "detail": json.loads(row["detail_json"]),
                      "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                      "occurred_at": row["occurred_at"]} for row in rows]
            return 200, {"items": items}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: LicensingService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
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
    """启动纹样授权计费 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动纹样授权计费与结算服务")
    parser.add_argument("--database", default="licensing.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = LicensingDatabase(args.database)
    Handler.service = LicensingService(database)
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
