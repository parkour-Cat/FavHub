"""Explicit, local-only lifecycle for the optional embedding provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from favhub.config import FavHubPaths
from favhub.embedding import EmbeddingProfile, encode_float32, validate_embeddings
from favhub.embedding_profiles import EmbeddingProfileStore
from favhub.fastembed_provider import (
    DEFAULT_MODEL,
    FAVHUB_PROVIDER_IMPLEMENTATION_VERSION,
    FastEmbedProvider,
    model_artifact_digest,
)


class EmbeddingRuntimeError(RuntimeError):
    """Stable base error for embedding runtime lifecycle failures."""


class EmbeddingDependencyUnavailableError(EmbeddingRuntimeError):
    pass


class EmbeddingModelCacheMissingError(EmbeddingRuntimeError):
    pass


ProviderFactory = Callable[..., FastEmbedProvider]


class EmbeddingRuntime:
    def __init__(
        self,
        paths: FavHubPaths,
        profiles: EmbeddingProfileStore,
        *,
        provider_factory: ProviderFactory | None = None,
        model: str = DEFAULT_MODEL,
        segment_tokens: int = 480,
        overlap_tokens: int = 32,
    ) -> None:
        self.paths = paths
        self.profiles = profiles
        self.provider_factory = provider_factory or FastEmbedProvider
        self.model = model
        self.segment_tokens = segment_tokens
        self.overlap_tokens = overlap_tokens
        self._cached_provider: FastEmbedProvider | None = None
        self._cached_profile_id: str | None = None

    def initialize(self) -> EmbeddingProfile:
        """Download/load the model, run probes, and return an unactivated profile."""
        self.paths.models.mkdir(parents=True, exist_ok=True)
        provider = self._construct(
            model=self.model,
            cache_dir=self.paths.models,
            local_files_only=False,
        )
        probe_texts = ("FavHub embedding probe", "收藏内容语义检索探针")
        try:
            for method_name in ("embed_queries", "embed_documents"):
                method = getattr(provider, method_name)
                vectors = method(probe_texts)
                validated = validate_embeddings(provider, probe_texts, vectors)
                for vector in validated:
                    encode_float32(vector, dimensions=provider.dimensions)
            artifact_digest = self._artifact_digest(provider)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EmbeddingRuntimeError(f"embedding initialization probe failed: {exc}") from exc

        max_input_tokens = int(getattr(provider, "max_input_tokens", 512))
        payload = {
            "provider": provider.name,
            "provider_version": provider.version,
            "provider_implementation_version": FAVHUB_PROVIDER_IMPLEMENTATION_VERSION,
            "model": self.model,
            "dimensions": provider.dimensions,
            "prefix_strategy": "query:/passage:",
            "normalization": "l2",
            "max_input_tokens": max_input_tokens,
            "segment_tokens": self.segment_tokens,
            "overlap_tokens": self.overlap_tokens,
            "artifact_digest": artifact_digest,
        }
        profile_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return EmbeddingProfile(
            id=profile_id,
            provider=str(provider.name),
            provider_version=str(provider.version),
            model=self.model,
            dimensions=int(provider.dimensions),
            normalization="l2",
            max_input_tokens=max_input_tokens,
            segment_tokens=self.segment_tokens,
            overlap_tokens=self.overlap_tokens,
            artifact_digest=artifact_digest,
        )

    def load_active(self, local_only: bool = True) -> FastEmbedProvider:
        """Load the active provider without downloading when ``local_only`` is true."""
        profile = self.profiles.active()
        if profile is None:
            raise EmbeddingRuntimeError("embedding profile is not initialized")
        if local_only:
            try:
                has_cache = any(path.is_file() for path in self.paths.models.rglob("*"))
            except OSError as exc:
                raise EmbeddingModelCacheMissingError(
                    "embedding model cache is invalid or inaccessible; run embeddings init"
                ) from exc
            if not has_cache:
                raise EmbeddingModelCacheMissingError(
                    "embedding model cache is missing; run embeddings init"
                )
        if self._cached_provider is not None and self._cached_profile_id == profile.id:
            # Deliberately no re-hash here. The digest is verified once, below,
            # against the files the provider was built from; after that the
            # provider is an ONNX session already resident in memory, and
            # re-reading model.onnx tells you nothing about it — swapping the
            # file on disk would not change the loaded session either way. The
            # check only looked like a safety net: it re-read and SHA-256'd
            # 464 MB on every call, which is once per batch, and allocated the
            # 448 MB model file each time. That is where the bad_alloc failures
            # in long runs came from.
            return self._cached_provider
        try:
            provider = self._construct(
                model=profile.model,
                cache_dir=self.paths.models,
                local_files_only=local_only,
                dimensions=profile.dimensions,
                max_input_tokens=profile.max_input_tokens,
            )
            artifact_digest = self._artifact_digest(provider)
            if artifact_digest != profile.artifact_digest.lower():
                raise EmbeddingModelCacheMissingError(
                    "embedding model cache identity does not match the active profile; "
                    "run embeddings init"
                )
            self._cached_provider = provider
            self._cached_profile_id = profile.id
            return provider
        except ImportError as exc:
            raise EmbeddingDependencyUnavailableError(
                "embedding dependency unavailable; install FavHub's embedding extra"
            ) from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            if local_only:
                raise EmbeddingModelCacheMissingError(
                    "embedding model cache is missing or invalid; run embeddings init"
                ) from exc
            raise EmbeddingRuntimeError(f"embedding provider failed to load: {exc}") from exc

    def cache_available(self) -> bool:
        """Check the active model manifest without loading a provider or downloading."""
        profile = self.profiles.active()
        if profile is None:
            return False
        try:
            return model_artifact_digest(self.paths.models) == profile.artifact_digest.lower()
        except (OSError, ValueError):
            return False

    def _construct(self, **kwargs: Any) -> FastEmbedProvider:
        try:
            return self.provider_factory(**kwargs)
        except ImportError as exc:
            raise EmbeddingDependencyUnavailableError(
                "embedding dependency unavailable; install FavHub's embedding extra"
            ) from exc

    @staticmethod
    def _artifact_digest(provider: FastEmbedProvider) -> str:
        digest_method = getattr(provider, "artifact_digest", None)
        if callable(digest_method):
            digest = digest_method()
            if (
                isinstance(digest, str)
                and len(digest) == 64
                and all(character in "0123456789abcdefABCDEF" for character in digest)
            ):
                return digest.lower()
        raise ValueError("embedding provider does not expose a valid model artifact digest")


__all__ = [
    "EmbeddingDependencyUnavailableError",
    "EmbeddingModelCacheMissingError",
    "EmbeddingRuntime",
    "EmbeddingRuntimeError",
]
