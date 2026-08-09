"""Interfaces and validation shared by future embedding implementations."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Immutable identity and sizing contract for persisted embeddings."""

    id: str
    provider: str
    provider_version: str
    model: str
    dimensions: int
    normalization: str
    max_input_tokens: int
    segment_tokens: int
    overlap_tokens: int
    artifact_digest: str

    def __post_init__(self) -> None:
        for field_name in ("id", "provider", "provider_version", "model"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"embedding profile {field_name} must be non-empty")
        if self.normalization != "l2":
            raise ValueError("embedding profile normalization must be 'l2'")
        for field_name in ("dimensions", "max_input_tokens", "segment_tokens"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"embedding profile {field_name} must be positive")
        if self.segment_tokens > self.max_input_tokens:
            raise ValueError("embedding profile segment_tokens cannot exceed max_input_tokens")
        if (
            isinstance(self.overlap_tokens, bool)
            or not isinstance(self.overlap_tokens, int)
            or self.overlap_tokens < 0
            or self.overlap_tokens >= self.segment_tokens
        ):
            raise ValueError("embedding profile overlap_tokens must be in [0, segment_tokens)")
        if (
            not isinstance(self.artifact_digest, str)
            or len(self.artifact_digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.artifact_digest)
        ):
            raise ValueError("embedding profile artifact_digest must be a SHA-256 hex digest")


def encode_float32(values: Sequence[float], *, dimensions: int) -> bytes:
    """Encode one normalized vector as a little-endian float32 BLOB."""
    vector = _validated_persisted_vector(values, dimensions=dimensions)
    try:
        return struct.pack(f"<{dimensions}f", *vector)
    except (OverflowError, struct.error) as error:
        raise ValueError("embedding values must be representable as float32") from error


def decode_float32(blob: bytes, *, dimensions: int) -> tuple[float, ...]:
    """Decode and validate one little-endian float32 BLOB."""
    _validate_dimensions(dimensions)
    if len(blob) % 4 != 0:
        raise ValueError("embedding BLOB length must be a multiple of four bytes")
    if len(blob) != dimensions * 4:
        raise ValueError("embedding BLOB dimension does not match expected dimensions")
    values = struct.unpack(f"<{dimensions}f", blob)
    return _validated_persisted_vector(values, dimensions=dimensions)


def _validated_persisted_vector(values: Sequence[float], *, dimensions: int) -> tuple[float, ...]:
    _validate_dimensions(dimensions)
    if len(values) != dimensions:
        raise ValueError("embedding vector dimension does not match expected dimensions")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError("embedding values must be real numbers")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("embedding values must be finite")
        vector.append(converted)
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-6):
        raise ValueError("persisted embedding vector must be L2 normalized")
    return tuple(vector)


def _validate_dimensions(dimensions: object) -> None:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
        raise ValueError("embedding dimensions must be a positive integer")


class EmbeddingProvider(Protocol):
    """A named, versioned provider of fixed-size embeddings."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class QueryDocumentEmbeddingProvider(EmbeddingProvider, Protocol):
    """Provider exposing distinct query and document embedding operations."""

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


def validate_embeddings(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> tuple[tuple[float, ...], ...]:
    """Validate provider output and return a finite, immutable representation."""
    name = provider.name
    version = provider.version
    dimensions = provider.dimensions
    _validate_provider_metadata(name, version, dimensions)

    if len(vectors) != len(texts):
        raise ValueError("embedding vector count must equal text count")

    normalized: list[tuple[float, ...]] = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError("embedding vector dimension does not match provider dimensions")

        normalized_vector: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError("embedding values must be real numbers")
            normalized_value = float(value)
            if not math.isfinite(normalized_value):
                raise ValueError("embedding values must be finite")
            normalized_vector.append(normalized_value)
        normalized.append(tuple(normalized_vector))

    return tuple(normalized)


def _validate_provider_metadata(name: object, version: object, dimensions: object) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("embedding provider name must be non-empty")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("embedding provider version must be non-empty")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError("embedding provider dimensions must be a positive integer")
    if dimensions <= 0:
        raise ValueError("embedding provider dimensions must be positive")
