from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from favhub.embedding import (
    EmbeddingProfile,
    EmbeddingProvider,
    QueryDocumentEmbeddingProvider,
    decode_float32,
    encode_float32,
    validate_embeddings,
)


def test_query_document_protocol_supports_simple_fake_provider() -> None:
    @dataclass(frozen=True)
    class FakeProvider:
        name: str = "fake"
        version: str = "1"
        dimensions: int = 2

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return self.embed_documents(texts)

        def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

        def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0, 1.0] for _ in texts]

    provider: QueryDocumentEmbeddingProvider = FakeProvider()
    assert provider.embed_documents(["doc"]) == [[1.0, 0.0]]
    assert provider.embed_queries(["query"]) == [[0.0, 1.0]]


def test_normalized_float32_blob_round_trip_is_little_endian() -> None:
    blob = encode_float32((0.6, 0.8), dimensions=2)

    assert blob == bytes.fromhex("9a99193fcdcc4c3f")
    assert decode_float32(blob, dimensions=2) == pytest.approx((0.6, 0.8), abs=1e-6)


@pytest.mark.parametrize(
    ("blob", "dimensions", "message"),
    [
        (b"\x00", 2, "length"),
        (b"\x00" * 8, 3, "dimension"),
        (bytes.fromhex("0000c07f00000000"), 2, "finite"),
        (encode_float32((1.0, 0.0), dimensions=2), 3, "dimension"),
    ],
)
def test_decode_float32_rejects_invalid_persisted_vectors(
    blob: bytes, dimensions: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decode_float32(blob, dimensions=dimensions)


@pytest.mark.parametrize("values", [(0.0, 0.0), (1.0, 1.0), (float("nan"), 0.0)])
def test_encode_float32_requires_finite_l2_normalized_vectors(values: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="finite|normalized"):
        encode_float32(values, dimensions=2)


def test_fastembed_provider_prefixes_query_and_documents_once() -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    class FakeBackend:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.calls: list[list[str]] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return [[1.0, 0.0] for _ in texts]

    backend = FakeBackend()
    provider = FastEmbedProvider(backend=backend, dimensions=2, max_input_tokens=512)

    assert provider.embed_documents(["passage: already"]) == ((1.0, 0.0),)
    assert provider.embed_queries(["query: already", "hello"]) == ((1.0, 0.0), (1.0, 0.0))
    assert backend.calls == [["passage: already"], ["query: already", "query: hello"]]


def test_fastembed_provider_exposes_model_token_boundaries_and_decode() -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    encoding = SimpleNamespace(
        ids=[101, 11, 12, 102],
        special_tokens_mask=[1, 0, 0, 1],
    )

    class FakeTokenizer:
        def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
            assert skip_special_tokens is True
            return "|".join(str(token_id) for token_id in token_ids)

    class FakeModel:
        tokenizer = FakeTokenizer()

        def tokenize(self, documents: list[str]):
            assert documents == ["中文subword"]
            return [encoding]

    class FakeBackend:
        model = FakeModel()

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    provider = FastEmbedProvider(backend=FakeBackend(), dimensions=2)

    assert provider.tokenize("中文subword") == (11, 12)
    assert provider.decode_tokens((11, 12)) == "11|12"


def test_fastembed_imports_are_lazy_and_constructor_forwards_cache_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import favhub.fastembed_provider as provider_module

    calls: list[tuple[str, object]] = []

    class FakeTextEmbedding:
        @classmethod
        def list_supported_models(cls) -> list[dict[str, str]]:
            return [{"model": "intfloat/multilingual-e5-small"}]

        def __init__(self, **kwargs: object) -> None:
            calls.append(("construct", kwargs))

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    fake_fastembed = SimpleNamespace(__version__="0.8.0", TextEmbedding=FakeTextEmbedding)

    def fake_import(name: str) -> object:
        calls.append(("import", name))
        if name == "fastembed":
            return fake_fastembed
        if name == "numpy":
            return SimpleNamespace()
        raise AssertionError(name)

    monkeypatch.setattr(provider_module.importlib, "import_module", fake_import)
    assert calls == []

    cache_dir = Path("model-cache")
    provider = provider_module.FastEmbedProvider(
        cache_dir=cache_dir, local_files_only=True, dimensions=2
    )

    assert provider.version == "0.8.0"
    assert calls[:2] == [("import", "fastembed"), ("import", "numpy")]
    assert calls[2] == (
        "construct",
        {
            "model_name": "intfloat/multilingual-e5-small",
            "cache_dir": str(cache_dir),
            "local_files_only": True,
        },
    )


def test_fastembed_provider_registers_default_model_with_public_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import favhub.fastembed_provider as provider_module

    registrations: list[dict[str, object]] = []

    class FakeTextEmbedding:
        @classmethod
        def list_supported_models(cls) -> list[dict[str, str]]:
            return []

        @classmethod
        def add_custom_model(cls, **kwargs: object) -> None:
            registrations.append(kwargs)

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * 383 for _ in texts]

    fake_fastembed = SimpleNamespace(__version__="0.8.0", TextEmbedding=FakeTextEmbedding)
    fake_description = SimpleNamespace(
        PoolingType=SimpleNamespace(MEAN="mean"),
        ModelSource=lambda *, hf: {"hf": hf},
    )

    def fake_import(name: str) -> object:
        return {
            "fastembed": fake_fastembed,
            "numpy": SimpleNamespace(),
            "fastembed.common.model_description": fake_description,
        }[name]

    monkeypatch.setattr(provider_module.importlib, "import_module", fake_import)

    provider_module.FastEmbedProvider(cache_dir=Path("model-cache"), local_files_only=True)

    assert len(registrations) == 1
    assert registrations[0]["model"] == "intfloat/multilingual-e5-small"
    assert registrations[0]["pooling"] == "mean"
    assert registrations[0]["normalization"] is True
    assert registrations[0]["sources"] == {"hf": "intfloat/multilingual-e5-small"}
    assert registrations[0]["dim"] == 384
    assert registrations[0]["model_file"] == "onnx/model.onnx"


def test_artifact_digest_hashes_only_the_model_snapshot_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    class FakeBackend:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    root = Path("cache")
    snapshot = root / "models--intfloat--multilingual-e5-small" / "snapshots" / "rev"
    contents = {
        snapshot / "config.json": b"config",
        snapshot / "onnx" / "model.onnx": b"model-v1",
        snapshot / "tokenizer.json": b"tokenizer",
        snapshot / "tokenizer_config.json": b"tokenizer-config",
        snapshot / "special_tokens_map.json": b"special-tokens",
        root / "models--intfloat--multilingual-e5-small" / "refs" / "main": b"rev",
        root / "models--intfloat--multilingual-e5-small" / "blobs" / "sha": b"blob",
        root / "models--intfloat--multilingual-e5-small" / ".locks" / "lock": b"lock",
        root / "models--intfloat--multilingual-e5-small" / "metadata.json": b"metadata",
        snapshot / "download.tmp": b"temp",
        root / "models--other--model" / "snapshots" / "rev" / "model.onnx": b"other",
    }
    repo_root = root / "models--intfloat--multilingual-e5-small"
    rglob_roots: list[Path] = []

    def fake_rglob(path: Path, pattern: str) -> object:
        rglob_roots.append(path)
        return iter(contents)

    monkeypatch.setattr(Path, "rglob", fake_rglob)
    monkeypatch.setattr(Path, "is_dir", lambda self: self == repo_root)
    monkeypatch.setattr(Path, "is_file", lambda self: self in contents)
    monkeypatch.setattr(Path, "read_bytes", lambda self: contents[self])
    provider = FastEmbedProvider(backend=FakeBackend(), cache_dir=root, dimensions=2)

    expected = hashlib.sha256()
    for relative, content in (
        ("config.json", b"config"),
        ("onnx/model.onnx", b"model-v1"),
        ("special_tokens_map.json", b"special-tokens"),
        ("tokenizer.json", b"tokenizer"),
        ("tokenizer_config.json", b"tokenizer-config"),
    ):
        expected.update(relative.encode("utf-8"))
        expected.update(b"\0")
        expected.update(content)
        expected.update(b"\0")

    assert provider.artifact_digest() == expected.hexdigest()
    assert rglob_roots == [repo_root]
    contents[root / "models--intfloat--multilingual-e5-small" / "metadata.json"] = b"changed"
    assert provider.artifact_digest() == expected.hexdigest()

    contents[snapshot / "onnx" / "model.onnx"] = b"model-v2"
    assert provider.artifact_digest() != expected.hexdigest()


def test_artifact_digest_rejects_missing_model_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    class FakeBackend:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(()))
    provider = FastEmbedProvider(backend=FakeBackend(), cache_dir=Path("empty"), dimensions=2)

    with pytest.raises(ValueError, match="artifact"):
        provider.artifact_digest()


def test_artifact_digest_rejects_incomplete_required_manifest(tmp_path: Path) -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    class FakeBackend:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    snapshot = tmp_path / "models--intfloat--multilingual-e5-small" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "onnx").mkdir()
    (snapshot / "onnx" / "model.onnx").write_bytes(b"model")

    provider = FastEmbedProvider(backend=FakeBackend(), cache_dir=tmp_path, dimensions=2)

    with pytest.raises(ValueError, match="config.json|tokenizer"):
        provider.artifact_digest()


def test_artifact_digest_rejects_two_complete_snapshots(tmp_path: Path) -> None:
    from favhub.fastembed_provider import FastEmbedProvider

    class FakeBackend:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    repo_root = tmp_path / "models--intfloat--multilingual-e5-small" / "snapshots"
    required = (
        "config.json",
        "onnx/model.onnx",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    )
    for revision in ("revision-a", "revision-b"):
        snapshot = repo_root / revision
        snapshot.mkdir(parents=True)
        for filename in required:
            path = snapshot / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{revision}:{filename}".encode())

    provider = FastEmbedProvider(backend=FakeBackend(), cache_dir=tmp_path, dimensions=2)

    with pytest.raises(ValueError, match="ambiguous"):
        provider.artifact_digest()


def test_embedding_profile_is_immutable_and_validates_identity() -> None:
    profile = EmbeddingProfile(
        id="profile-1",
        provider="fastembed",
        provider_version="0.8.0",
        model="intfloat/multilingual-e5-small",
        dimensions=384,
        normalization="l2",
        max_input_tokens=512,
        segment_tokens=480,
        overlap_tokens=32,
        artifact_digest="a" * 64,
    )

    assert profile.dimensions == 384
    with pytest.raises((AttributeError, TypeError)):
        profile.model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": "", "artifact_digest": "a" * 64},
        {"id": "p", "normalization": "none", "artifact_digest": "a" * 64},
        {"id": "p", "segment_tokens": 0, "artifact_digest": "a" * 64},
        {"id": "p", "segment_tokens": 513, "artifact_digest": "a" * 64},
        {"id": "p", "overlap_tokens": 480, "artifact_digest": "a" * 64},
        {"id": "p", "artifact_digest": "bad"},
    ],
)
def test_embedding_profile_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    defaults: dict[str, object] = {
        "id": "p",
        "provider": "fastembed",
        "provider_version": "0.8.0",
        "model": "model",
        "dimensions": 384,
        "normalization": "l2",
        "max_input_tokens": 512,
        "segment_tokens": 480,
        "overlap_tokens": 32,
        "artifact_digest": "a" * 64,
    }
    defaults.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        EmbeddingProfile(**defaults)  # type: ignore[arg-type]


@dataclass(frozen=True)
class StubProvider:
    name: str = "test-provider"
    version: str = "1.0"
    dimensions: int = 2

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dimensions for _ in texts]


class StatefulMetadataProvider:
    def __init__(self) -> None:
        self.name_reads = 0
        self.version_reads = 0
        self.dimensions_reads = 0

    @property
    def name(self) -> str:
        self.name_reads += 1
        return "stateful"

    @property
    def version(self) -> str:
        self.version_reads += 1
        return "1.0"

    @property
    def dimensions(self) -> int:
        self.dimensions_reads += 1
        return 2 if self.dimensions_reads == 1 else 3

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0] for _ in texts]


def test_provider_satisfies_protocol_and_embeddings_are_normalized() -> None:
    provider: EmbeddingProvider = StubProvider()
    vectors = [[1, 2.5], (-3.0, 4)]

    result = validate_embeddings(provider, ["one", "two"], vectors)

    assert result == ((1.0, 2.5), (-3.0, 4.0))
    assert isinstance(result, tuple)
    assert all(isinstance(vector, tuple) for vector in result)
    assert vectors == [[1, 2.5], (-3.0, 4)]


def test_empty_texts_and_vectors_are_valid() -> None:
    assert validate_embeddings(StubProvider(), [], []) == ()


def test_reads_each_provider_metadata_property_once() -> None:
    provider = StatefulMetadataProvider()

    result = validate_embeddings(provider, ["one", "two"], [[1.0, 2.0], [3.0, 4.0]])

    assert result == ((1.0, 2.0), (3.0, 4.0))
    assert (provider.name_reads, provider.version_reads, provider.dimensions_reads) == (1, 1, 1)


def test_rejects_count_mismatch() -> None:
    with pytest.raises(ValueError, match="count"):
        validate_embeddings(StubProvider(), ["one"], [])


def test_rejects_wrong_vector_dimension() -> None:
    with pytest.raises(ValueError, match="dimension"):
        validate_embeddings(StubProvider(), ["one"], [[1.0]])


@pytest.mark.parametrize("value", [True, "1.0", object()])
def test_rejects_non_real_vector_values(value: object) -> None:
    with pytest.raises(TypeError, match="real"):
        validate_embeddings(StubProvider(), ["one"], [[value, 1.0]])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_nonfinite_vector_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_embeddings(StubProvider(), ["one"], [[value, 1.0]])


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (StubProvider(name=""), "name"),
        (StubProvider(version=""), "version"),
        (StubProvider(dimensions=0), "dimensions"),
        (StubProvider(dimensions=-1), "dimensions"),
    ],
)
def test_rejects_invalid_provider_metadata(provider: StubProvider, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_embeddings(provider, [], [])
