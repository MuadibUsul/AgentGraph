"""Async SQLAlchemy engine, session factory, and schema bootstrap helpers."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_pool_kwargs: dict[str, object] = {
    "echo": settings.db_echo,
    "pool_pre_ping": True,
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_timeout": settings.db_pool_timeout,
}
if settings.environment == "test":
    # Tests use short-lived event loops; pooled asyncpg connections would be
    # bound to a loop that no longer exists.
    from sqlalchemy.pool import NullPool

    _pool_kwargs = {"echo": settings.db_echo, "poolclass": NullPool}

engine: AsyncEngine = create_async_engine(settings.database_url, **_pool_kwargs)
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transactional session per request."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def asyncpg_dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _backfill_document_versions(conn: asyncpg.Connection) -> int:
    """Backfill hashes and the first immutable version row for legacy docs."""
    rows = await conn.fetch(
        """
        SELECT
            d.id, d.workspace_id, d.version, d.title, d.content,
            d.frontmatter::text AS frontmatter, d.namespace,
            d.verification_status, d.trust_level, d.updated_by, d.updated_at,
            d.content_hash,
            EXISTS (
                SELECT 1 FROM document_versions v
                WHERE v.document_id = d.id AND v.version = d.version
            ) AS has_version
        FROM shared_documents d
        WHERE d.content_hash IS NULL
           OR NOT EXISTS (
                SELECT 1 FROM document_versions v
                WHERE v.document_id = d.id AND v.version = d.version
           )
        """
    )
    created = 0
    for row in rows:
        content_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        await conn.execute(
            "UPDATE shared_documents SET content_hash = $1 WHERE id = $2",
            content_hash,
            row["id"],
        )
        if row["has_version"]:
            await conn.execute(
                """
                UPDATE document_versions SET content_hash = $1
                WHERE document_id = $2 AND version = $3 AND content_hash IS NULL
                """,
                content_hash,
                row["id"],
                row["version"],
            )
            continue
        await conn.execute(
            """
            INSERT INTO document_versions (
                workspace_id, document_id, version, parent_version, content_hash,
                title, content, frontmatter, namespace, verification_status,
                trust_level, author, session_batch_id, change_summary, created_at
            ) VALUES (
                $1, $2, $3, NULL, $4, $5, $6, $7::jsonb, $8, $9, $10, $11,
                NULL, 'backfilled from existing document', $12
            )
            ON CONFLICT (document_id, version) DO NOTHING
            """,
            row["workspace_id"],
            row["id"],
            row["version"],
            content_hash,
            row["title"],
            row["content"],
            row["frontmatter"] or "{}",
            row["namespace"],
            row["verification_status"],
            row["trust_level"],
            row["updated_by"],
            row["updated_at"],
        )
        created += 1
    return created


async def _ensure_vector_dimension(conn: asyncpg.Connection) -> None:
    """Reconcile pgvector columns with EMBEDDING_DIMENSIONS.

    PostgreSQL cannot cast between vector dimensions, so a dimension change
    drops the HNSW indexes, nulls existing embeddings and recreates the indexes.
    A full reindex is required afterwards; this function only makes the schema
    consistent with the configured provider.
    """
    dimension = int(settings.embedding_dimensions)
    if not 1 <= dimension <= 2000:
        raise RuntimeError("embedding_dimensions must be between 1 and 2000")
    current = await conn.fetchval(
        """
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = 'shared_documents'::regclass
          AND attname = 'embedding'
          AND NOT attisdropped
        """
    )
    expected = f"vector({dimension})"
    if current == expected:
        return
    logger.warning(
        "embedding dimension change %s -> %s; existing vectors will be cleared "
        "and a full reindex is required",
        current,
        expected,
    )
    await conn.execute("DROP INDEX IF EXISTS idx_docs_embedding")
    await conn.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
    await conn.execute(
        f"ALTER TABLE shared_documents ALTER COLUMN embedding "
        f"TYPE vector({dimension}) USING NULL"
    )
    if await conn.fetchval("SELECT to_regclass('document_chunks') IS NOT NULL"):
        await conn.execute(
            f"ALTER TABLE document_chunks ALTER COLUMN embedding "
            f"TYPE vector({dimension}) USING NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding "
            "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_docs_embedding "
        "ON shared_documents USING hnsw (embedding vector_cosine_ops)"
    )
    await conn.execute(
        "UPDATE shared_documents SET embedding_model = NULL WHERE embedding IS NULL"
    )
    if await conn.fetchval("SELECT to_regclass('document_chunks') IS NOT NULL"):
        await conn.execute(
            "UPDATE document_chunks SET embedding_model = NULL WHERE embedding IS NULL"
        )


async def apply_schema() -> None:
    """Apply the canonical schema, run pending migrations, and backfill data.

    A session advisory lock prevents multiple API replicas from racing DDL.
    Fresh databases get sql/init.sql; a v2 database gets the in-place migration;
    v3 databases missing the hardening tables get v3_hardening.sql; every v3
    database then gets the v3.2 production expansion and v3.2.1 improvements.
    """
    root = Path(__file__).resolve().parent.parent
    schema_path = root / "sql" / "init.sql"
    migration_path = root / "sql" / "migrations" / "v2_to_v3_collab.sql"
    hardening_path = root / "sql" / "migrations" / "v3_hardening.sql"
    production_path = root / "sql" / "migrations" / "v3_2_production.sql"
    improvements_path = root / "sql" / "migrations" / "v3_2_1_mcp_improvements.sql"
    if not schema_path.exists():
        raise RuntimeError(f"schema file not found: {schema_path}")
    if not hardening_path.exists():
        raise RuntimeError(f"migration file not found: {hardening_path}")
    if not production_path.exists():
        raise RuntimeError(f"migration file not found: {production_path}")
    if not improvements_path.exists():
        raise RuntimeError(f"migration file not found: {improvements_path}")

    conn = await asyncpg.connect(asyncpg_dsn(), timeout=settings.db_pool_timeout)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock(hashtext('agentgraph_schema_v3')::bigint)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(32) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        has_v2 = await conn.fetchval(
            """
            SELECT to_regclass('memory_crystals') IS NOT NULL
               AND to_regclass('shared_documents') IS NULL
            """
        )
        if has_v2:
            if not migration_path.exists():
                raise RuntimeError(f"migration file not found: {migration_path}")
            logger.info("v2 database detected; applying v2 -> v3 migration")
            await conn.execute(migration_path.read_text(encoding="utf-8"))
            logger.info("v2 -> v3 migration complete")

        hardened = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.1.0'"
        )
        production = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.2.0'"
        )
        improved = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.2.1'"
        )
        has_documents = await conn.fetchval(
            "SELECT to_regclass('shared_documents') IS NOT NULL"
        )
        if has_documents:
            # Existing v3/v2-upgraded tables were created before the hardening
            # columns existed. Add them BEFORE init.sql creates indexes on them.
            if not hardened:
                logger.info("applying v3.1 hardening migration")
                await conn.execute(hardening_path.read_text(encoding="utf-8"))
            await conn.execute(schema_path.read_text(encoding="utf-8"))
        else:
            await conn.execute(schema_path.read_text(encoding="utf-8"))
            if not hardened:
                logger.info("applying v3.1 hardening migration")
                await conn.execute(hardening_path.read_text(encoding="utf-8"))
        if not production:
            logger.info("applying v3.2 production expansion")
            await conn.execute(production_path.read_text(encoding="utf-8"))
        if not hardened:
            backfilled = await _backfill_document_versions(conn)
            if backfilled:
                logger.info("backfilled %s document version(s)", backfilled)
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.1.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        else:
            await _backfill_document_versions(conn)
        if not production:
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.2.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        if not improved:
            logger.info("applying v3.2.1 MCP improvements")
            await conn.execute(improvements_path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.2.1') "
                "ON CONFLICT (version) DO NOTHING"
            )
        await _ensure_vector_dimension(conn)
        logger.info("database schema is up to date (v3.2.1)")
    finally:
        await conn.close()


async def check_database() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:  # pragma: no cover - readiness path
        logger.exception("database readiness check failed")
        return False

# Register the tenant RLS session listener.
from app import tenant as _tenant  # noqa: E402,F401
