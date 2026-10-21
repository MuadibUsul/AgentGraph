"""Embedding providers: OpenAI, local Ollama, and a deterministic offline hash.

The offline provider exists so the whole stack can be booted and tested without
any external AI service. It is lexical feature hashing, not a neural semantic
model; use OpenAI or Ollama for production-quality recall.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

import httpx
from openai import AsyncOpenAI, RateLimitError

from app.config import get_settings
from app.errors import EmbeddingFailure

logger = logging.getLogger(__name__)
settings = get_settings()

_LATIN_WORD = re.compile(r"[a-z0-9]+")
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class EmbeddingProvider(Protocol):
    provider_name: str
    model: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    async def embed_one(self, text: str) -> list[float]:
        ...

    async def aclose(self) -> None:
        ...


def _validate_vectors(
    vectors: list[list[float]],
    expected_count: int,
    *,
    pad_smaller: bool = False,
) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise EmbeddingFailure(
            f"embedding provider returned {len(vectors)} vectors for "
            f"{expected_count} inputs"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != settings.embedding_dimensions:
            if pad_smaller and len(vector) < settings.embedding_dimensions:
                # Cosine distance is invariant to appending zeros, so a local
                # model that returns fewer dimensions can still fit the
                # configured vector column.
                vectors[index] = vector + [0.0] * (
                    settings.embedding_dimensions - len(vector)
                )
                vector = vectors[index]
            else:
                raise EmbeddingFailure(
                    "embedding dimension mismatch: provider returned "
                    f"{len(vector)}, schema requires {settings.embedding_dimensions}. "
                    "Use a model with matching dimensions or migrate the vector column."
                )
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingFailure("embedding provider returned non-finite values")
    return vectors


class HashEmbeddingProvider:
    """Deterministic feature-hashing embeddings for offline use and tests."""

    provider_name = "hash"
    model = f"feature-hash-{settings.embedding_dimensions}"
    dimensions = settings.embedding_dimensions

    def _features(self, text: str) -> Counter[str]:
        lowered = " ".join(text.lower().split())
        features: Counter[str] = Counter()
        words = _LATIN_WORD.findall(lowered)
        for word in words:
            features[f"w:{word}"] += 1.0
            if len(word) > 3:
                for index in range(len(word) - 2):
                    features[f"g:{word[index:index + 3]}"] += 0.35
        cjk = _CJK_CHAR.findall(lowered)
        for char in cjk:
            features[f"c:{char}"] += 1.0
        for left, right in zip(cjk, cjk[1:], strict=False):
            features[f"b:{left}{right}"] += 0.75
        if not features and lowered:
            features[f"raw:{lowered}"] = 1.0
        return features

    def _embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in self._features(text).items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log1p(weight))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # Valid, finite zero vector for empty/degenerate text.
            return vector
        return [value / norm for value in vector]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_sync(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed_sync(text)

    async def aclose(self) -> None:
        return None


class OpenAIEmbeddingProvider:
    provider_name = "openai"

    def __init__(self) -> None:
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=settings.embedding_timeout_seconds,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batches: list[list[float]] = []
        try:
            for start in range(0, len(texts), settings.embedding_batch_size):
                batch = list(texts[start : start + settings.embedding_batch_size])
                if not batch:
                    continue
                kwargs: dict[str, object] = {
                    "model": self.model,
                    "input": batch,
                    "encoding_format": "float",
                }
                if self.model.startswith("text-embedding-3"):
                    kwargs["dimensions"] = self.dimensions
                for attempt in range(3):
                    try:
                        response = await self._client.embeddings.create(**kwargs)
                        break
                    except RateLimitError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(20 * (attempt + 1))
                ordered = sorted(response.data, key=lambda item: item.index)
                batches.extend([list(item.embedding) for item in ordered])
                if (
                    settings.embedding_batch_interval_seconds > 0
                    and start + settings.embedding_batch_size < len(texts)
                ):
                    await asyncio.sleep(settings.embedding_batch_interval_seconds)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.exception("OpenAI embedding request failed")
            raise EmbeddingFailure(f"OpenAI embedding request failed: {exc}") from exc
        return _validate_vectors(batches, len(texts))

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def aclose(self) -> None:
        await self._client.close()


class OllamaEmbeddingProvider:
    provider_name = "ollama"

    def __init__(self) -> None:
        self.model = settings.ollama_embedding_model
        self.dimensions = settings.embedding_dimensions
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.embedding_timeout_seconds,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), settings.embedding_batch_size):
                batch = list(texts[start : start + settings.embedding_batch_size])
                if not batch:
                    continue
                response = await self._client.post(
                    "/api/embed", json={"model": self.model, "input": batch}
                )
                if response.status_code == 404:
                    # Older Ollama builds only expose /api/embeddings.
                    for text in batch:
                        legacy = await self._client.post(
                            "/api/embeddings",
                            json={"model": self.model, "prompt": text},
                        )
                        legacy.raise_for_status()
                        vectors.append(legacy.json()["embedding"])
                    continue
                response.raise_for_status()
                payload = response.json()
                vectors.extend(payload.get("embeddings", []))
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.exception("Ollama embedding request failed")
            raise EmbeddingFailure(f"Ollama embedding request failed: {exc}") from exc
        return _validate_vectors(vectors, len(texts), pad_smaller=True)

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


def configured_embedding_model() -> str:
    if settings.embedding_provider == "ollama":
        return settings.ollama_embedding_model
    if settings.embedding_provider == "openai":
        return settings.embedding_model
    return f"feature-hash-{settings.embedding_dimensions}"


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    if settings.embedding_provider == "openai":
        return OpenAIEmbeddingProvider()
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingProvider()
    return HashEmbeddingProvider()


async def close_embedding_provider() -> None:
    if get_embedding_provider.cache_info().currsize:
        await get_embedding_provider().aclose()
