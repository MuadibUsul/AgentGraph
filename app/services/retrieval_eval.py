"""Retrieval recall baseline and probes (PRD AG-ST-12).

``run_eval`` loads ``eval/retrieval/<dataset>.corpus.jsonl`` into a dedicated
evaluation workspace (once per corpus hash), asks every query of
``<dataset>.jsonl`` through the production ranking (``hybrid_document_ids``)
and records recall@k and MRR in ``retrieval_eval_runs``. ``compare`` is the
regression gate: recall@50 may not drop more than ``MAX_DROP`` below the
baseline. ``probe_due`` samples documents of each workspace once a day and
records whether a document's own title finds it in the top 50
(``self_recall@50``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.errors import ApiError, NotFound
from app.models import SharedDocument, Workspace

logger = logging.getLogger(__name__)
EVAL_DIR = Path(__file__).resolve().parents[2] / "eval" / "retrieval"
MAX_DROP = 0.03
PROBE_SAMPLE = 20
PROBE_K = 50
PROBE_EVERY_SECONDS = 24 * 3600
_DATASET = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _load(dataset: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if not _DATASET.match(dataset or ""):
        raise ApiError(422, "validation_error", "dataset is a file name under eval/retrieval",
                       details={"field": "dataset"})
    corpus_path = EVAL_DIR / f"{dataset}.corpus.jsonl"
    queries_path = EVAL_DIR / f"{dataset}.jsonl"
    if not corpus_path.exists() or not queries_path.exists():
        raise NotFound("eval_dataset", f"dataset '{dataset}' not found")
    corpus_raw = corpus_path.read_bytes()
    corpus = [json.loads(line) for line in corpus_raw.decode().splitlines() if line.strip()]
    queries = [json.loads(line) for line in queries_path.read_text().splitlines()
               if line.strip()]
    return corpus, queries, hashlib.sha256(corpus_raw).hexdigest()[:16]


async def _eval_workspace(dataset: str, corpus: list[dict[str, Any]],
                          corpus_hash: str) -> Workspace:
    from app.services.onboarding import write_platform_document
    from app.services.workspace import create_workspace

    name = f"eval:retrieval:{dataset}:{corpus_hash}"
    async with async_session_factory() as platform:
        workspace = await platform.scalar(select(Workspace).where(Workspace.name == name))
        if workspace is None:
            created = await create_workspace(platform, name=name, tier="pro",
                                             operator_managed=True)
            workspace = await platform.get(Workspace, created.workspace.id)
        seeded = int(await platform.scalar(text(
            "SELECT count(*) FROM shared_documents WHERE workspace_id = :ws "
            "AND frontmatter->>'eval_corpus' = :h"), {"ws": workspace.id, "h": corpus_hash}))
        if seeded < len(corpus):
            await platform.execute(text("SELECT set_config('app.workspace_id', :ws, true)"),
                                   {"ws": str(workspace.id)})
            for item in corpus:
                await write_platform_document(
                    platform, workspace.id, namespace=item.get("namespace") or "default",
                    doc_key=item["doc_key"], title=item["title"], content=item["content"],
                    frontmatter={"eval_corpus": corpus_hash}, author="eval",
                    change_summary="retrieval evaluation corpus")
                await platform.commit()
        return workspace


async def _ranked_keys(session: AsyncSession, workspace_id: Any, query: str,
                       limit: int) -> list[str]:
    from app.services.retrieval import hybrid_document_ids

    ranked = await hybrid_document_ids(
        session, workspace_id=workspace_id,
        doc_filter=SharedDocument.workspace_id == workspace_id, query=query, limit=limit)
    if not ranked:
        return []
    ids = [doc_id for doc_id, _ in ranked]
    keys = dict((await session.execute(
        select(SharedDocument.id, SharedDocument.doc_key).where(SharedDocument.id.in_(ids))
    )).all())
    return [keys[doc_id] for doc_id in ids if doc_id in keys]


async def run_eval(dataset: str = "v1", ks: list[int] | None = None) -> dict[str, Any]:
    from app.config import get_settings

    ks = sorted(set(ks or [10, 50]))
    if not ks or ks[0] < 1 or ks[-1] > 200:
        raise ApiError(422, "validation_error", "k values are 1-200", details={"field": "k"})
    corpus, queries, corpus_hash = _load(dataset)
    started = time.monotonic()
    workspace = await _eval_workspace(dataset, corpus, corpus_hash)
    hits = {k: 0.0 for k in ks}
    reciprocal = 0.0
    misses: list[dict[str, Any]] = []
    async with async_session_factory() as session:
        for item in queries:
            relevant = set(item["relevant"])
            ranked = await _ranked_keys(session, workspace.id, item["query"], ks[-1])
            for k in ks:
                hits[k] += len(relevant & set(ranked[:k])) / len(relevant)
            first = next((rank for rank, key in enumerate(ranked, start=1)
                          if key in relevant), None)
            reciprocal += 1.0 / first if first else 0.0
            if first is None or first > ks[0]:
                misses.append({"query": item["query"], "first_relevant_rank": first})
        index_size = int(await session.scalar(text(
            "SELECT count(*) FROM shared_documents WHERE workspace_id = :ws"),
            {"ws": workspace.id}))
    n = len(queries)
    metrics = {f"recall@{k}": round(hits[k] / n, 4) for k in ks}
    metrics["mrr"] = round(reciprocal / n, 4)
    settings = get_settings()
    async with async_session_factory() as platform:
        run_id = await platform.scalar(text(
            "INSERT INTO retrieval_eval_runs (dataset, corpus_hash, ks, metrics, n_queries, "
            "index_size, embedding_provider, embedding_dimensions, duration_ms, misses) "
            "VALUES (:d, :h, :ks, CAST(:m AS jsonb), :n, :size, :p, :dim, :ms, "
            "CAST(:miss AS jsonb)) RETURNING run_id"),
            {"d": dataset, "h": corpus_hash, "ks": ks, "m": json.dumps(metrics), "n": n,
             "size": index_size, "p": settings.embedding_provider,
             "dim": settings.embedding_dimensions,
             "ms": int((time.monotonic() - started) * 1000),
             "miss": json.dumps(misses[:50])})
        await platform.commit()
    return {**metrics, "n_queries": n, "index_size": index_size, "run_id": run_id,
            "dataset": dataset, "corpus_hash": corpus_hash, "misses": misses[:20]}


def compare(baseline: dict[str, Any], result: dict[str, Any], *, metric: str = "recall@50",
            max_drop: float = MAX_DROP) -> dict[str, Any]:
    """The regression gate: fail when ``metric`` drops more than ``max_drop``."""
    before, after = float(baseline[metric]), float(result[metric])
    return {"metric": metric, "baseline": before, "current": after,
            "drop": round(before - after, 4), "max_drop": max_drop,
            "passed": before - after <= max_drop}


async def list_runs(dataset: str | None, limit: int = 50) -> dict[str, Any]:
    async with async_session_factory() as platform:
        rows = (await platform.execute(text(
            "SELECT run_id, dataset, corpus_hash, ks, metrics, n_queries, index_size, "
            "embedding_provider, embedding_dimensions, duration_ms, created_at FROM "
            "retrieval_eval_runs WHERE (CAST(:d AS text) IS NULL OR dataset = :d) "
            "ORDER BY created_at DESC LIMIT :l"), {"d": dataset, "l": limit})).mappings().all()
    return {"items": [{**dict(row), "created_at": row["created_at"].isoformat()}
                      for row in rows]}


async def probe_workspace(workspace_id: Any) -> dict[str, Any] | None:
    """Sample documents; a document's own title should find it in the top 50."""
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT d.id, d.title, left(d.content, 300) AS head FROM shared_documents d "
            "WHERE d.workspace_id = :ws AND d.archived_at IS NULL AND EXISTS (SELECT 1 FROM "
            "document_chunks c WHERE c.document_id = d.id) ORDER BY random() LIMIT :n"),
            {"ws": workspace_id, "n": PROBE_SAMPLE})).mappings().all()
        if not rows:
            return None
        from app.services.retrieval import hybrid_document_ids

        found = 0
        for row in rows:
            query = (row["title"] or "").strip() or (row["head"] or "").strip()
            ranked = await hybrid_document_ids(
                session, workspace_id=workspace_id,
                doc_filter=SharedDocument.workspace_id == workspace_id, query=query,
                limit=PROBE_K)
            found += int(any(doc_id == row["id"] for doc_id, _ in ranked))
        value = round(found / len(rows), 4)
        await session.execute(text(
            "INSERT INTO retrieval_probes (workspace_id, sampled, hits, self_recall) "
            "VALUES (:ws, :n, :h, :v)"),
            {"ws": workspace_id, "n": len(rows), "h": found, "v": value})
        await session.commit()
    return {"workspace_id": str(workspace_id), "sampled": len(rows), "hits": found,
            "self_recall@50": value}


async def probe_due(limit: int = 50) -> int:
    """Run the daily probe for workspaces without one in the last 24 hours."""
    async with async_session_factory() as session:
        due = (await session.execute(text(
            "SELECT w.id FROM workspaces w WHERE w.name NOT LIKE 'eval:retrieval:%' AND "
            "NOT EXISTS (SELECT 1 FROM retrieval_probes p WHERE p.workspace_id = w.id AND "
            "p.created_at > NOW() - make_interval(secs => :every)) LIMIT :l"),
            {"every": PROBE_EVERY_SECONDS, "l": limit})).scalars().all()
    done = 0
    for workspace_id in due:
        try:
            if await probe_workspace(workspace_id) is not None:
                done += 1
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            logger.exception("self-recall probe failed for a workspace")
    return done


async def probes(workspace_id: Any | None, limit: int = 100) -> dict[str, Any]:
    async with async_session_factory() as platform:
        rows = (await platform.execute(text(
            "SELECT workspace_id, sampled, hits, self_recall, created_at FROM retrieval_probes "
            "WHERE (CAST(:ws AS uuid) IS NULL OR workspace_id = :ws) "
            "ORDER BY created_at DESC LIMIT :l"),
            {"ws": str(workspace_id) if workspace_id else None, "l": limit})).mappings().all()
    return {"items": [{"workspace_id": str(row["workspace_id"]), "sampled": row["sampled"],
                       "hits": row["hits"], "self_recall@50": float(row["self_recall"]),
                       "at": row["created_at"].isoformat()} for row in rows]}
