"""Generate the synthetic retrieval evaluation set (PRD AG-ST-12).

Writes ``eval/retrieval/<dataset>.corpus.jsonl`` (one document per line:
doc_key, namespace, title, content) and ``eval/retrieval/<dataset>.jsonl``
(one query per line: query, namespace, relevant doc_keys). The data is
synthetic and deterministic (fixed seed), so a run is comparable with the
last one. ``--size`` makes larger corpora for the scale runs; the queries stay
the same and the extra documents are distractors.

    python scripts/gen_retrieval_eval.py            # v1: 120 documents, 150 queries
    python scripts/gen_retrieval_eval.py --size 10000 --dataset scale-10k
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "retrieval"

SYSTEMS = [
    "payments-gateway", "search-indexer", "billing-ledger", "auth-service", "mail-relay",
    "image-resizer", "report-builder", "feature-flags", "audit-trail", "chat-bridge",
    "geo-router", "price-engine", "inventory-sync", "push-notifier", "export-worker",
    "rate-limiter", "session-store", "tax-calculator", "fraud-scorer", "ledger-archive",
    "media-cdn", "invoice-mailer", "partner-api", "recommendation-ranker", "data-lake-loader",
    "sso-bridge", "webhook-dispatcher", "metrics-collector", "backup-runner", "consent-manager",
]
KINDS = {
    "runbook": ("Runbook", "How to operate {s}: restart with `{cmd}`, roll back by redeploying "
                "the previous image tag, and page the {team} on-call when the {metric} alarm "
                "stays red for {minutes} minutes."),
    "design": ("Design", "Design of {s}: it stores {store} in {db}, talks to {peer} over "
               "{proto}, and scales horizontally behind {lb}. The main trade-off was "
               "{tradeoff}."),
    "incident": ("Incident review", "Incident on {s} ({date}): {symptom} for {minutes} "
                 "minutes because {cause}. Fixed by {fix}; follow-up owner is the {team} team."),
    "decision": ("Decision record", "We decided that {s} will {decision} starting {date}, "
                 "because {reason}. Rejected alternative: {alt}."),
}
TEAMS = ["platform", "payments", "growth", "data", "security", "infra", "mobile", "billing"]
METRICS = ["p99 latency", "error rate", "queue depth", "disk usage", "CPU saturation"]
STORES = ["session tokens", "price snapshots", "audit events", "image variants", "job states"]
DBS = ["PostgreSQL", "Redis", "S3", "ClickHouse", "DynamoDB"]
PROTOS = ["gRPC", "HTTP/JSON", "AMQP", "Kafka topics", "WebSockets"]
LBS = ["an internal load balancer", "the service mesh", "a DNS round robin", "Envoy"]
TRADEOFFS = ["consistency over availability", "cost over latency", "simplicity over "
             "throughput", "durability over write speed"]
SYMPTOMS = ["checkout requests timed out", "emails were delivered twice", "search returned "
            "stale results", "logins failed for EU users", "reports were empty"]
CAUSES = ["a certificate expired", "a migration locked a hot table", "a retry storm "
          "exhausted the pool", "a feature flag defaulted to on", "the cache key changed"]
FIXES = ["rotating the certificate", "splitting the migration", "adding jittered backoff",
         "pinning the flag", "versioning the cache key"]
DECISIONS = ["move to a managed queue", "drop the legacy XML endpoint", "shard by tenant",
             "require idempotency keys", "run in two regions"]
REASONS = ["the on-call load doubled", "the vendor ends support", "big tenants starve "
           "small ones", "duplicate charges reached support", "a region outage cost a day"]
ALTS = ["keeping the self-hosted cluster", "a compatibility shim", "a bigger instance",
        "client-side dedup", "a warm standby only"]


def _doc(rng: random.Random, system: str, kind: str) -> dict:
    label, template = KINDS[kind]
    facts = {
        "s": system, "cmd": f"systemctl restart {system}", "team": rng.choice(TEAMS),
        "metric": rng.choice(METRICS), "minutes": rng.choice([5, 10, 15, 30, 45]),
        "store": rng.choice(STORES), "db": rng.choice(DBS), "peer": rng.choice(SYSTEMS),
        "proto": rng.choice(PROTOS), "lb": rng.choice(LBS), "tradeoff": rng.choice(TRADEOFFS),
        "date": f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}",
        "symptom": rng.choice(SYMPTOMS), "cause": rng.choice(CAUSES), "fix": rng.choice(FIXES),
        "decision": rng.choice(DECISIONS), "reason": rng.choice(REASONS),
        "alt": rng.choice(ALTS),
    }
    return {"doc_key": f"{kind}/{system}", "namespace": "default",
            "title": f"{label}: {system}", "content": template.format(**facts) + "\n",
            "facts": facts, "kind": kind}


def _queries(rng: random.Random, docs: list[dict]) -> list[dict]:
    queries = []
    for doc in docs:
        facts, system, kind = doc["facts"], doc["facts"]["s"], doc["kind"]
        if kind == "runbook":
            text = f"how do I restart and roll back {system}"
        elif kind == "design":
            text = f"where does {system} store {facts['store']} and how does it scale"
        elif kind == "incident":
            text = f"why {facts['symptom']} on {system}"
        else:
            text = f"why did we decide that {system} should {facts['decision']}"
        queries.append({"query": text, "namespace": "default", "relevant": [doc["doc_key"]]})
    # Cross-document questions: every document about one system.
    for system in rng.sample(SYSTEMS, 30):
        queries.append({"query": f"everything we know about {system}", "namespace": "default",
                        "relevant": [f"{kind}/{system}" for kind in KINDS]})
    return queries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="v1")
    parser.add_argument("--size", type=int, default=0,
                        help="pad the corpus with distractor documents up to this size")
    args = parser.parse_args()
    rng = random.Random(20261022)
    docs = [_doc(rng, system, kind) for system in SYSTEMS for kind in KINDS]
    queries = _queries(rng, docs)
    filler = random.Random(7)
    while len(docs) < args.size:
        index = len(docs)
        system = f"{filler.choice(SYSTEMS)}-shadow-{index}"
        doc = _doc(filler, system, filler.choice(list(KINDS)))
        doc["doc_key"] = f"filler/{index:06d}"
        docs.append(doc)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"{args.dataset}.corpus.jsonl").open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps({key: doc[key] for key in
                                     ("doc_key", "namespace", "title", "content")}) + "\n")
    with (OUT / f"{args.dataset}.jsonl").open("w", encoding="utf-8") as handle:
        for query in queries:
            handle.write(json.dumps(query) + "\n")
    print(f"wrote {len(docs)} documents and {len(queries)} queries to {OUT}")


if __name__ == "__main__":
    main()
