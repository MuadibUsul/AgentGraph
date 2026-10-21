from __future__ import annotations

import math

import pytest

from app.config import get_settings
from app.engine.embeddings import HashEmbeddingProvider

DIMENSIONS = get_settings().embedding_dimensions


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


@pytest.fixture
def provider() -> HashEmbeddingProvider:
    return HashEmbeddingProvider()


async def test_dimensions_and_normalization(provider):
    vectors = await provider.embed(["Bitcoin liquidation", "联邦储备会议波动率"])
    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == DIMENSIONS
        assert all(math.isfinite(value) for value in vector)
        assert sum(value * value for value in vector) == pytest.approx(1.0)


async def test_deterministic(provider):
    first = await provider.embed_one("Bitcoin liquidation risk")
    second = await provider.embed_one("Bitcoin liquidation risk")
    assert first == second


async def test_related_text_is_more_similar_than_unrelated(provider):
    query = await provider.embed_one("Bitcoin liquidation cascade risk")
    related = await provider.embed_one("Bitcoin liquidation risk rises near 65000")
    unrelated = await provider.embed_one("Federal reserve meeting volatility expands")
    assert cosine(query, related) > cosine(query, unrelated)
    assert cosine(query, related) > 0.5


async def test_empty_text_is_finite_zero_vector(provider):
    vector = await provider.embed_one("")
    assert len(vector) == DIMENSIONS
    assert all(value == 0.0 for value in vector)


def test_smaller_local_vectors_are_zero_padded_for_cosine_schema():
    from app.engine.embeddings import _validate_vectors

    padded = _validate_vectors([[0.6, 0.8]], 1, pad_smaller=True)[0]
    assert len(padded) == DIMENSIONS
    assert padded[:2] == [0.6, 0.8]
    assert all(value == 0.0 for value in padded[2:])


def test_oversized_vectors_are_rejected():
    from app.engine.embeddings import EmbeddingFailure, _validate_vectors

    with pytest.raises(EmbeddingFailure):
        _validate_vectors([[0.0] * (DIMENSIONS + 1)], 1, pad_smaller=True)
