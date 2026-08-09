"""Optional FastEmbed implementation loaded only on explicit construction."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from favhub.embedding import validate_embeddings

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
FAVHUB_PROVIDER_IMPLEMENTATION_VERSION = "1"
DEFAULT_HF_SOURCE = "intfloat/multilingual-e5-small"
_REQUIRED_MODEL_ARTIFACT_PATHS = frozenset(
    {
        "config.json",
        "onnx/model.onnx",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    }
)
_MODEL_ARTIFACT_PATHS = frozenset(
    {
        "config.json",
        "onnx/model.onnx",
        "onnx/model.onnx_data",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    }
)


class _EmbeddingBackend(Protocol):
    def embed(self, texts: list[str]) -> Iterable[Sequence[float]]: ...


class FastEmbedProvider:
    """FastEmbed adapter with explicit E5 document/query contracts."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        cache_dir: str | Path | None = None,
        local_files_only: bool = True,
        backend: _EmbeddingBackend | None = None,
        dimensions: int = 384,
        max_input_tokens: int = 512,
    ) -> None:
        if backend is None:
            if cache_dir is None:
                raise ValueError("FastEmbed cache_dir must be explicit")
            fastembed_module: Any = importlib.import_module("fastembed")
            importlib.import_module("numpy")
            _register_default_model(fastembed_module.TextEmbedding, model)
            backend = cast(
                _EmbeddingBackend,
                fastembed_module.TextEmbedding(
                    model_name=model,
                    cache_dir=str(cache_dir),
                    local_files_only=local_files_only,
                ),
            )
            self._version = str(fastembed_module.__version__)
        else:
            self._version = "0.8"
        self.model = model
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.local_files_only = local_files_only
        self._backend = backend
        self._dimensions = dimensions
        self.max_input_tokens = max_input_tokens

    @property
    def name(self) -> str:
        return "fastembed"

    @property
    def version(self) -> str:
        return self._version

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self.embed_documents(texts)

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed_with_prefix(texts, "passage: ")

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed_with_prefix(texts, "query: ")

    def tokenize(self, text: str) -> tuple[int, ...]:
        """Return non-special model token IDs using FastEmbed's loaded tokenizer."""
        return self.tokenize_many((text,))[0]

    def tokenize_many(self, texts: Sequence[str]) -> tuple[tuple[int, ...], ...]:
        model = getattr(self._backend, "model", None)
        tokenize = getattr(model, "tokenize", None)
        if not callable(tokenize):
            raise ValueError("FastEmbed backend does not expose model tokenization")
        encodings = tokenize(list(texts))
        if len(encodings) != len(texts):
            raise ValueError("FastEmbed tokenizer must return one encoding per document")
        token_groups: list[tuple[int, ...]] = []
        for encoding in encodings:
            ids = tuple(int(token_id) for token_id in encoding.ids)
            special_mask = tuple(int(value) for value in encoding.special_tokens_mask)
            if len(ids) != len(special_mask):
                raise ValueError("FastEmbed tokenizer returned inconsistent token metadata")
            token_groups.append(
                tuple(
                    token_id
                    for token_id, special in zip(ids, special_mask, strict=True)
                    if not special
                )
            )
        return tuple(token_groups)

    def decode_many(self, token_windows: Sequence[Sequence[int]]) -> tuple[str, ...]:
        model = getattr(self._backend, "model", None)
        tokenizer = getattr(model, "tokenizer", None)
        decode_batch = getattr(tokenizer, "decode_batch", None)
        if not callable(decode_batch):
            return tuple(self.decode_tokens(tokens) for tokens in token_windows)
        decoded = decode_batch(
            [[int(token) for token in tokens] for tokens in token_windows],
            skip_special_tokens=True,
        )
        if len(decoded) != len(token_windows) or not all(isinstance(text, str) for text in decoded):
            raise ValueError("FastEmbed tokenizer returned non-text decoded content")
        return tuple(decoded)

    def decode_tokens(self, tokens: Sequence[int]) -> str:
        """Decode a model-token window into document text for embedding."""
        model = getattr(self._backend, "model", None)
        tokenizer = getattr(model, "tokenizer", None)
        decode = getattr(tokenizer, "decode", None)
        if not callable(decode):
            raise ValueError("FastEmbed backend does not expose model token decoding")
        decoded = decode([int(token) for token in tokens], skip_special_tokens=True)
        if not isinstance(decoded, str):
            raise ValueError("FastEmbed tokenizer returned non-text decoded content")
        return decoded

    def artifact_digest(self) -> str:
        """Hash only files in the unambiguous model snapshot used for inference."""
        if self.cache_dir is None:
            raise ValueError("FastEmbed cache_dir is required to hash artifacts")
        return model_artifact_digest(self.cache_dir)

    def _embed_with_prefix(
        self, texts: Sequence[str], prefix: str
    ) -> tuple[tuple[float, ...], ...]:
        prefixed = [text if text.startswith(prefix) else f"{prefix}{text}" for text in texts]
        vectors = tuple(tuple(vector) for vector in self._backend.embed(prefixed))
        return validate_embeddings(self, texts, vectors)


def model_artifact_digest(cache_dir: Path) -> str:
    artifacts = _model_artifact_files(cache_dir)
    digest = hashlib.sha256()
    for relative_path, path in artifacts:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _register_default_model(text_embedding: Any, model: str) -> None:
    if model != DEFAULT_MODEL:
        return
    registered_names = {entry["model"].lower() for entry in text_embedding.list_supported_models()}
    if model.lower() in registered_names:
        return
    model_description: Any = importlib.import_module("fastembed.common.model_description")
    text_embedding.add_custom_model(
        model=model,
        pooling=model_description.PoolingType.MEAN,
        normalization=True,
        sources=model_description.ModelSource(hf=DEFAULT_HF_SOURCE),
        dim=384,
        model_file="onnx/model.onnx",
        description="Multilingual E5 small retrieval embeddings",
        license="mit",
    )


def _model_artifact_files(cache_dir: Path) -> tuple[tuple[str, Path], ...]:
    repo_directory = f"models--{DEFAULT_HF_SOURCE.replace('/', '--')}"
    repo_root = cache_dir / repo_directory
    search_root = repo_root if repo_root.is_dir() else cache_dir
    snapshots: dict[tuple[str, ...], list[tuple[str, Path]]] = {}
    for path in search_root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(cache_dir).parts
        snapshot_key: tuple[str, ...]
        artifact_parts: tuple[str, ...]
        if len(relative_parts) >= 4 and relative_parts[:2] == (repo_directory, "snapshots"):
            snapshot_key = relative_parts[:3]
            artifact_parts = relative_parts[3:]
        elif len(relative_parts) >= 3 and relative_parts[0] == "snapshots":
            snapshot_key = relative_parts[:2]
            artifact_parts = relative_parts[2:]
        else:
            snapshot_key = ()
            artifact_parts = relative_parts
        artifact_path = "/".join(artifact_parts)
        if artifact_path in _MODEL_ARTIFACT_PATHS:
            snapshots.setdefault(snapshot_key, []).append((artifact_path, path))

    complete_snapshots = {
        key: files
        for key, files in snapshots.items()
        if {relative_path for relative_path, _ in files} >= _REQUIRED_MODEL_ARTIFACT_PATHS
    }
    if not complete_snapshots:
        discovered = {relative_path for files in snapshots.values() for relative_path, _ in files}
        missing = sorted(_REQUIRED_MODEL_ARTIFACT_PATHS - discovered)
        if missing:
            raise ValueError(
                "FastEmbed model artifact manifest is incomplete; missing: " + ", ".join(missing)
            )
        raise ValueError("FastEmbed model artifact files were not found")
    if len(complete_snapshots) != 1:
        raise ValueError("FastEmbed model artifact snapshot is ambiguous")
    files = next(iter(complete_snapshots.values()))
    return tuple(sorted(files, key=lambda item: item[0]))
