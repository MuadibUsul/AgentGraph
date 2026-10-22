"""Reference verifier for lineage attestations (PRD AG-ID-08).

Depends on nothing from AgentGraph: only the issuing tenant's public keys
(``GET /.well-known/agentgraph/tenants/<alias>/jwks.json``) and the
``cryptography`` package. It checks the EdDSA signature, the audience, the
expiry and that every link of the chain grants a subset of the link above it.

    python scripts/verify_lineage.py <attestation> <jwks.json> <audience>

As a library: ``verify(token, jwks, audience)`` returns the payload or raises
``AttestationError``.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LEEWAY_SECONDS = 30


class AttestationError(Exception):
    """The attestation is not valid; the message says why."""


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _subset(child: list[str], parent: list[str]) -> bool:
    if "*" in parent:
        return True
    return set(child) <= set(parent)


def check_narrowing(chain: list[dict[str, Any]]) -> None:
    """Each grant must be within the one above it (scopes, namespaces, depth)."""
    for upper, lower in zip(chain, chain[1:], strict=False):
        above, below = upper.get("grant") or {}, lower.get("grant") or {}
        if not _subset(below.get("scopes") or [], above.get("scopes") or []):
            raise AttestationError(f"scopes widen at {lower.get('ref')}")
        if not _subset(below.get("namespaces") or [], above.get("namespaces") or []):
            raise AttestationError(f"namespaces widen at {lower.get('ref')}")
        if int(below.get("depth", 0)) <= int(above.get("depth", 0)):
            raise AttestationError(f"depth does not grow at {lower.get('ref')}")
        if above.get("expires_at") and lower.get("expires_at") and \
                lower["expires_at"] > above["expires_at"]:
            raise AttestationError(f"outlives its issuer at {lower.get('ref')}")


def verify(token: str, jwks: dict[str, Any], audience: str, *,
           now: float | None = None) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64decode(header_b64))
        payload = json.loads(_b64decode(payload_b64))
        signature = _b64decode(signature_b64)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AttestationError("not a compact JWS") from exc
    if header.get("alg") != "EdDSA":
        raise AttestationError("unsupported algorithm")
    keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")]
    if not keys or keys[0].get("kty") != "OKP" or keys[0].get("crv") != "Ed25519":
        raise AttestationError("unknown signing key")
    public = Ed25519PublicKey.from_public_bytes(_b64decode(keys[0]["x"]))
    try:
        public.verify(signature, f"{header_b64}.{payload_b64}".encode())
    except InvalidSignature as exc:
        raise AttestationError("bad signature") from exc
    moment = time.time() if now is None else now
    if payload.get("aud") != audience:
        raise AttestationError("issued for another audience")
    if int(payload.get("exp", 0)) + LEEWAY_SECONDS < moment:
        raise AttestationError("expired")
    if int(payload.get("iat", 0)) - LEEWAY_SECONDS > moment:
        raise AttestationError("issued in the future")
    chain = payload.get("chain") or []
    if not chain:
        raise AttestationError("empty chain")
    check_narrowing(chain)
    return payload


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    token = open(sys.argv[1]).read().strip() if sys.argv[1].endswith(".jws") else sys.argv[1]
    with open(sys.argv[2], encoding="utf-8") as handle:
        jwks = json.load(handle)
    try:
        payload = verify(token, jwks, sys.argv[3])
    except AttestationError as exc:
        print(f"invalid: {exc}")
        return 1
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
