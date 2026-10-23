"""Minimal signed webhook receiver for a bot-side notification worker.

Run next to the bot runtime, point an Veynod webhook rule at
``http://<host>:<port>/agentgraph`` and set ``AGENTGRAPH_WEBHOOK_SECRET`` to the
rule secret. Events are verified, then appended as JSON Lines to
``AGENTGRAPH_WEBHOOK_OUTBOX`` so the bot loop can react without giving the
console any inbound network access.

This is intentionally stdlib-only; it is a reference receiver, not a daemon
with durability, authentication or multi-tenant routing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SECRET = os.environ.get("AGENTGRAPH_WEBHOOK_SECRET", "")
OUTBOX = Path(os.environ.get("AGENTGRAPH_WEBHOOK_OUTBOX", "agentgraph-webhooks.jsonl"))
MAX_SKEW_SECONDS = int(os.environ.get("AGENTGRAPH_WEBHOOK_MAX_SKEW", "300"))
HOST = os.environ.get("AGENTGRAPH_WEBHOOK_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENTGRAPH_WEBHOOK_PORT", "8099"))


def verify(timestamp: str | None, signature: str | None, body: bytes) -> bool:
    if not SECRET or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - sent_at) > MAX_SKEW_SECONDS:
        return False
    expected = hmac.new(
        SECRET.encode("utf-8"),
        f"{sent_at}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class Handler(BaseHTTPRequestHandler):
    server_version = "VeynodWebhookReceiver/1.0"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") != "/agentgraph":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if not verify(
            self.headers.get("X-AgentGraph-Timestamp"),
            self.headers.get("X-AgentGraph-Signature"),
            body,
        ):
            self.send_error(401, "invalid or stale signature")
            return
        try:
            event = json.loads(body)
        except ValueError:
            self.send_error(400, "body must be JSON")
            return
        record = {
            "received_at": time.time(),
            "event": self.headers.get("X-AgentGraph-Event"),
            "delivery": self.headers.get("X-AgentGraph-Delivery"),
            "payload": event,
        }
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print(f"{self.address_string()} {format % args}")


def main() -> None:
    if not SECRET:
        raise SystemExit("AGENTGRAPH_WEBHOOK_SECRET is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"listening on http://{HOST}:{PORT}/agentgraph -> {OUTBOX}")
    server.serve_forever()


if __name__ == "__main__":
    main()
