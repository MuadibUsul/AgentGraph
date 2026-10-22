"""Chunk indexing and hybrid (vector + lexical) document retrieval."""

from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.engine.chunking import split_markdown
from app.engine.embeddings import get_embedding_provider
from app.models import DocumentChunk, SharedDocument

settings = get_settings()


async def reindex_document_chunks(
    session: AsyncSession, document: SharedDocument
) -> int:
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
    )
    chunks = split_markdown(
        document.content,
        chunk_size=settings.chunk_size_chars,
        overlap=settings.chunk_overlap_chars,
    )
    provider = get_embedding_provider()
    texts = [
        chr(10).join(
            part for part in (document.title, chunk.heading or "", chunk.content) if part
        ).strip()
        for chunk in chunks
    ]
    vectors = await provider.embed(texts)
    from app.services.budget import meter_embedding
    from app.services.token_budget import stamp_chain

    await meter_embedding(
        document.workspace_id, sum(len(item) for item in texts), kind="chunks",
        model=provider.model, chain=stamp_chain(document.updated_stamp),
    )
    for chunk, vector in zip(chunks, vectors, strict=True):
        session.add(
            DocumentChunk(
                workspace_id=document.workspace_id,
                document_id=document.id,
                doc_key=document.doc_key,
                chunk_index=chunk.index,
                heading=chunk.heading,
                content=chunk.content,
                content_hash=chunk.content_hash,
                embedding=vector,
                embedding_model=provider.model,
            )
        )
    if vectors:
        document.embedding = [
            sum(column) / len(vectors) for column in zip(*vectors, strict=False)
        ]
    else:
        document.embedding = None
    document.embedding_model = provider.model
    await session.flush()
    return len(chunks)



async def _vector_doc_ids(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    doc_filter: Any,
    query: str,
    limit: int,
    threshold: float = 0.0,
) -> list[uuid.UUID]:
    vector = await query_vector(query, workspace_id)
    await session.execute(
        text(f"SET LOCAL hnsw.ef_search = {max(settings.hnsw_ef_search, 10)}")
    )
    distance = DocumentChunk.embedding.cosine_distance(vector)
    conditions = [
        DocumentChunk.workspace_id == workspace_id,
        DocumentChunk.embedding.is_not(None),
        doc_filter,
    ]
    if threshold > 0:
        conditions.append(distance <= 1 - threshold)
    rows = (
        await session.execute(
            select(DocumentChunk.document_id)
            .join(SharedDocument, SharedDocument.id == DocumentChunk.document_id)
            .where(*conditions)
            .order_by(distance)
            .limit(limit)
        )
    ).scalars().all()
    return list(dict.fromkeys(rows))


async def _keyword_doc_ids(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    doc_filter: Any,
    query: str,
    limit: int,
) -> list[uuid.UUID]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    statement = (
        select(DocumentChunk.document_id)
        .join(SharedDocument, SharedDocument.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.workspace_id == workspace_id,
            doc_filter,
            text(
                "(document_chunks.search_vector @@ "
                "websearch_to_tsquery('simple', :q) "
                "OR document_chunks.content ILIKE :like ESCAPE '\\' "
                "OR similarity(document_chunks.content, :q) > 0.05)"
            ),
        )
        .order_by(
            text(
                "ts_rank_cd(document_chunks.search_vector, "
                "websearch_to_tsquery('simple', :q)) DESC, "
                "similarity(document_chunks.content, :q) DESC"
            )
        )
        .limit(limit)
        .params(q=query, like=f"%{escaped}%")
    )
    rows = (await session.execute(statement)).scalars().all()
    return list(dict.fromkeys(rows))


_QUERY_VECTORS: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_QUERY_VECTOR_CACHE_SIZE = 256


async def query_vector(query: str, workspace_id: uuid.UUID | None = None) -> list[float]:
    """Embed a search query once; ranking and chunk matching reuse the vector."""
    provider = get_embedding_provider()
    key = (provider.model, query)
    cached = _QUERY_VECTORS.get(key)
    if cached is not None:
        _QUERY_VECTORS.move_to_end(key)
        return cached
    vector = await provider.embed_one(query)
    if workspace_id is not None:
        from app.services.budget import meter_embedding

        await meter_embedding(workspace_id, len(query), kind="query", model=provider.model)
    _QUERY_VECTORS[key] = vector
    if len(_QUERY_VECTORS) > _QUERY_VECTOR_CACHE_SIZE:
        _QUERY_VECTORS.popitem(last=False)
    return vector


async def hybrid_document_ids(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    doc_filter: Any,
    query: str,
    limit: int,
    mode: str = "hybrid",
    threshold: float | None = None,
) -> list[tuple[uuid.UUID, float]]:
    candidate_limit = max(limit * settings.hybrid_candidate_multiplier, limit)
    threshold = settings.semantic_threshold if threshold is None else threshold
    vector_ids: list[uuid.UUID] = []
    keyword_ids: list[uuid.UUID] = []
    if mode in ("auto", "semantic", "hybrid"):
        vector_ids = await _vector_doc_ids(
            session,
            workspace_id=workspace_id,
            doc_filter=doc_filter,
            query=query,
            limit=candidate_limit,
            threshold=threshold,
        )
    if mode in ("auto", "keyword", "hybrid"):
        keyword_ids = await _keyword_doc_ids(
            session,
            workspace_id=workspace_id,
            doc_filter=doc_filter,
            query=query,
            limit=candidate_limit,
        )
    if mode == "semantic":
        return [
            (doc_id, 1.0 / (settings.hybrid_rrf_k + rank))
            for rank, doc_id in enumerate(vector_ids[:limit], start=1)
        ]
    if mode == "keyword":
        return [
            (doc_id, 1.0 / (settings.hybrid_rrf_k + rank))
            for rank, doc_id in enumerate(keyword_ids[:limit], start=1)
        ]

    scores: dict[uuid.UUID, float] = {}
    k = settings.hybrid_rrf_k
    for rank, doc_id in enumerate(vector_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    for rank, doc_id in enumerate(keyword_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]
