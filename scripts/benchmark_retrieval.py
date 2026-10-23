"""End-to-end retrieval benchmark for Veynod's hybrid search.

Creates an isolated benchmark workspace, seeds a deterministic corpus, verifies
the documents, then measures recall@k, MRR and latency for semantic, keyword,
hybrid and auto modes. Results are written as Markdown + JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from datetime import date
from pathlib import Path

import httpx

TOPICS = [
    ("liquidity", "order book depth, bid-ask spread and slippage"),
    ("volatility", "realized variance, implied vol and volatility clustering"),
    ("momentum", "trend persistence and moving-average crossovers"),
    ("mean_reversion", "z-score, half-life and cointegration"),
    ("risk", "drawdown, tail loss and position limits"),
    ("execution", "VWAP, TWAP and implementation shortfall"),
    ("macro", "rates, inflation and central-bank policy"),
    ("sentiment", "news tone, positioning and crowding"),
    ("onchain", "wallet flows, exchange reserves and stablecoin supply"),
    ("grid", "grid spacing, inventory skew and fee capture"),
    ("breakout", "range expansion and false breakout filters"),
    ("stat_arb", "pairs spread and residual stationarity"),
    ("funding", "perpetual funding and basis carry"),
    ("liquidation", "leverage cascade and liquidation heatmaps"),
    ("latency", "feed latency and order routing"),
    ("portfolio", "risk parity and covariance shrinkage"),
    ("backtest", "walk-forward validation and deflated Sharpe"),
    ("regime", "hidden Markov regimes and state transitions"),
]


def seed_document(topic: str, term: str, extra: str) -> str:
    return (
        f"# {topic.title()} research\n\n"
        f"## Thesis\n\n"
        f"The {term} playbook combines {extra}. "
        f"Unique identifier: benchmark-{topic}-{term}."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--output", default="docs/benchmarks/latest.md"
    )
    args = parser.parse_args()
    if not args.admin_token:
        raise SystemExit("Provide --admin-token or ADMIN_TOKEN")

    base = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.admin_token}"}
    results: dict[str, dict] = {}
    with httpx.Client(base_url=base, timeout=60) as client:
        ready = client.get("/readyz")
        ready.raise_for_status()
        readiness = ready.json()
        created = client.post(
            "/v1/admin/workspaces",
            headers=headers,
            json={"name": f"retrieval-bench-{uuid.uuid4().hex[:6]}"},
        )
        created.raise_for_status()
        workspace = created.json()
        wid = workspace["workspace"]["id"]
        commander = {"Authorization": f"Bearer {workspace['api_key']}"}
        term_by_topic: dict[str, str] = {}
        for index, (topic, detail) in enumerate(TOPICS):
            term = f"{topic}{index:02d}"
            term_by_topic[topic] = term
            key = f"bench/{topic}"
            response = client.put(
                f"/v1/documents/{key}",
                headers=commander,
                json={
                    "title": f"{topic.title()} research",
                    "content": seed_document(topic, term, detail),
                    "frontmatter": {"status": "DONE"},
                    "expected_version": 0,
                },
            )
            response.raise_for_status()
            version = response.json()["document"]["version"]
            verify = client.post(
                f"/v1/admin/workspaces/{wid}/documents/{key}/verify",
                headers=headers,
                json={"expected_version": version},
            )
            verify.raise_for_status()
        reindex = client.post(
            f"/v1/admin/workspaces/{wid}/reindex", headers=headers
        )
        reindex.raise_for_status()

        queries = [
            (topic, f"{term_by_topic[topic]} {topic} {detail}")
            for topic, detail in TOPICS
        ]
        for mode in ("semantic", "keyword", "hybrid", "auto"):
            hit = 0
            reciprocal_ranks: list[float] = []
            latencies: list[float] = []
            for topic, query in queries:
                started = time.perf_counter()
                response = client.post(
                    "/v1/query",
                    headers=commander,
                    json={"query": query, "mode": mode, "limit": args.top_k},
                )
                latency = (time.perf_counter() - started) * 1000
                latencies.append(latency)
                response.raise_for_status()
                items = response.json()["items"]
                keys = [item["doc_key"] for item in items]
                expected = f"bench/{topic}"
                if expected in keys:
                    hit += 1
                    reciprocal_ranks.append(1.0 / (keys.index(expected) + 1))
                else:
                    reciprocal_ranks.append(0.0)
            results[mode] = {
                "recall_at_k": round(hit / len(queries), 4),
                "mrr": round(statistics.mean(reciprocal_ranks), 4),
                "latency_ms_mean": round(statistics.mean(latencies), 2),
                "latency_ms_p95": round(
                    sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2
                ),
                "queries": len(queries),
                "top_k": args.top_k,
            }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base,
        "corpus_documents": len(TOPICS),
        "embedding_provider": readiness.get("embedding_provider"),
        "embedding_model": readiness.get("embedding_model"),
        "results": results,
        "caveat": (
            "The default hash provider is lexical feature hashing, not a neural "
            "semantic model. Run with OpenAI/Ollama for production recall claims; "
            "these numbers validate retrieval plumbing and latency."
        ),
    }
    output.write_text(
        "# Retrieval benchmark\n\n"
        f"Generated: {payload['generated_at']}\n\n"
        f"Corpus: {len(TOPICS)} verified documents; top-k={args.top_k}; "
        "embedding provider is whatever the target deployment reports.\n\n"
        "| Mode | Recall@k | MRR | Mean latency (ms) | P95 latency (ms) |\n"
        "|---|---:|---:|---:|---:|\n"
        + "\n".join(
            f"| {mode} | {value['recall_at_k']:.3f} | {value['mrr']:.3f} | "
            f"{value['latency_ms_mean']:.1f} | {value['latency_ms_p95']:.1f} |"
            for mode, value in results.items()
        )
        + "\n",
        encoding="utf-8",
    )
    output.with_suffix(".json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    dated = output.parent / f"retrieval-{date.today().isoformat()}.md"
    dated.write_text(output.read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
