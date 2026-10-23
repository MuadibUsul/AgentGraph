"""Public, unauthenticated keys and revocation checks (PRD AG-ID-08)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.services import attestation

router = APIRouter(prefix="/.well-known/agentgraph", tags=["Lineage attestations"])


@router.get("/tenants/{alias}/jwks.json",
            summary="A tenant's public signing keys (rotated keys stay for 7 days)")
async def tenant_jwks(alias: str) -> dict:
    return await attestation.jwks(alias)


@router.get("/tenants/{alias}/revoked",
            summary="Whether a pseudonymous link of an attestation is revoked now")
async def tenant_revoked(
    alias: str,
    ref: str = Query(min_length=4, max_length=64),
    aud: str = Query(min_length=1, max_length=256),
) -> dict:
    return await attestation.revoked(alias, ref, aud)
