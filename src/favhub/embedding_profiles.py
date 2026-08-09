"""Persistence and diagnostics for the active local embedding profile."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from favhub.database import Database
from favhub.embedding import EmbeddingProfile, decode_float32
from favhub.enrichment_queue import now


def embedding_task_input_hash(profile_id: str, index_input_hash: str) -> str:
    """Return the durable identity for one profile/item embedding task."""
    payload = f"embed-v1\0{profile_id}\0{index_input_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingSummary:
    state: str
    active_profile: EmbeddingProfile | None = None
    current_chunks: int = 0
    embedded_chunks: int = 0
    pending_tasks: int = 0
    failed_tasks: int = 0
    corrupt_vectors: int = 0
    last_build_report: dict[str, Any] | None = None

    @property
    def active_profile_metadata(self) -> dict[str, Any] | None:
        profile = self.active_profile
        if profile is None:
            return None
        return {
            "id": profile.id,
            "provider": profile.provider,
            "provider_version": profile.provider_version,
            "model": profile.model,
            "dimensions": profile.dimensions,
            "normalization": profile.normalization,
            "max_input_tokens": profile.max_input_tokens,
            "segment_tokens": profile.segment_tokens,
            "overlap_tokens": profile.overlap_tokens,
            "artifact_digest": profile.artifact_digest,
        }

    @property
    def current_chunk_count(self) -> int:
        return self.current_chunks

    @property
    def embedded_chunk_count(self) -> int:
        return self.embedded_chunks

    @property
    def pending_task_count(self) -> int:
        return self.pending_tasks

    @property
    def failed_task_count(self) -> int:
        return self.failed_tasks


def _profile_config(profile: EmbeddingProfile) -> str:
    return json.dumps(
        {
            "provider": profile.provider,
            "provider_version": profile.provider_version,
            "model": profile.model,
            "dimensions": profile.dimensions,
            "normalization": profile.normalization,
            "max_input_tokens": profile.max_input_tokens,
            "segment_tokens": profile.segment_tokens,
            "overlap_tokens": profile.overlap_tokens,
            "artifact_digest": profile.artifact_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class EmbeddingProfileStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def active(self) -> EmbeddingProfile | None:
        row = self.database.connection.execute(
            """SELECT id, provider, provider_version, model, dimensions,
                      normalization, max_input_tokens, segment_tokens,
                      overlap_tokens, artifact_digest
               FROM embedding_profiles WHERE is_active = 1"""
        ).fetchone()
        return None if row is None else self._from_row(row)

    def get(self, profile_id: str) -> EmbeddingProfile | None:
        row = self.database.connection.execute(
            """SELECT id, provider, provider_version, model, dimensions,
                      normalization, max_input_tokens, segment_tokens,
                      overlap_tokens, artifact_digest
               FROM embedding_profiles WHERE id = ?""",
            (profile_id,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def activate(self, profile: EmbeddingProfile) -> bool:
        timestamp = now()
        with self.database.transaction():
            row = self.database.connection.execute(
                """SELECT id, provider, provider_version, model, dimensions,
                          normalization, max_input_tokens, segment_tokens,
                          overlap_tokens, artifact_digest, is_active
                   FROM embedding_profiles WHERE id = ?""",
                (profile.id,),
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                if existing != profile:
                    raise ValueError(
                        "embedding profile identity conflict: profile id already exists"
                    )
                was_active = int(row["is_active"]) == 1
            else:
                was_active = False
            # The partial unique index permits this transition only when the
            # old active row is cleared before the new row is activated.
            self.database.connection.execute(
                "UPDATE embedding_profiles SET is_active = 0 WHERE is_active = 1"
            )
            if row is None:
                self.database.connection.execute(
                    """INSERT INTO embedding_profiles(
                           id, provider, provider_version, model, dimensions,
                           normalization, max_input_tokens, segment_tokens,
                           overlap_tokens, artifact_digest, config_json,
                           is_active, initialized_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        profile.id,
                        profile.provider,
                        profile.provider_version,
                        profile.model,
                        profile.dimensions,
                        profile.normalization,
                        profile.max_input_tokens,
                        profile.segment_tokens,
                        profile.overlap_tokens,
                        profile.artifact_digest,
                        _profile_config(profile),
                        timestamp,
                    ),
                )
            else:
                self.database.connection.execute(
                    "UPDATE embedding_profiles SET is_active = 1 WHERE id = ?",
                    (profile.id,),
                )
        return not was_active

    def summary(self) -> EmbeddingSummary:
        profile = self.active()
        if profile is None:
            return EmbeddingSummary(state="disabled")

        current_row = self.database.connection.execute(
            """SELECT COUNT(*) AS count
               FROM content_chunks c JOIN items i
                 ON i.platform = c.platform AND i.source_id = c.source_id
               WHERE i.access_status = 'available'
                 AND c.input_hash = i.index_input_hash"""
        ).fetchone()
        current_chunks = int(current_row["count"] or 0)
        vector_cursor = self.database.connection.execute(
            """SELECT e.chunk_id, e.vector
               FROM chunk_embeddings e
               JOIN content_chunks c ON c.id = e.chunk_id
               JOIN items i
                 ON i.platform = c.platform AND i.source_id = c.source_id
               WHERE e.profile_id = ?
                 AND i.access_status = 'available'
                 AND c.input_hash = i.index_input_hash""",
            (profile.id,),
        )
        valid_chunks: set[int] = set()
        corrupt = 0
        while True:
            vector_rows = vector_cursor.fetchmany(256)
            if not vector_rows:
                break
            for row in vector_rows:
                try:
                    decode_float32(bytes(row["vector"]), dimensions=profile.dimensions)
                except (TypeError, ValueError):
                    corrupt += 1
                else:
                    valid_chunks.add(int(row["chunk_id"]))

        task_rows = self.database.connection.execute(
            """SELECT t.status, t.input_hash, t.error, i.index_input_hash
               FROM enrichment_tasks t JOIN items i
                 ON i.platform = t.platform AND i.source_id = t.source_id
               WHERE t.kind = 'embed_content'
                 AND i.access_status = 'available'"""
        ).fetchall()
        counts: dict[str, int] = {}
        failed = 0
        for row in task_rows:
            expected = embedding_task_input_hash(profile.id, str(row["index_input_hash"]))
            if str(row["input_hash"]) != expected:
                continue
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            if row["error"] is not None:
                failed += 1
        build = self.database.connection.execute(
            """SELECT status, counts_json, error_json, finished_at
               FROM embedding_build_runs
               WHERE profile_id = ?
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (profile.id,),
        ).fetchone()
        report: dict[str, Any] | None = None
        if build is not None:
            try:
                parsed = json.loads(str(build["counts_json"]))
                report = parsed if isinstance(parsed, dict) else {"counts": parsed}
            except (TypeError, ValueError, json.JSONDecodeError):
                report = {"status": str(build["status"])}
            report.setdefault("status", str(build["status"]))
            if build["error_json"] is not None:
                report.setdefault("error", build["error_json"])
            if build["finished_at"] is not None:
                report.setdefault("finished_at", str(build["finished_at"]))

        if corrupt:
            state = "degraded"
        elif len(valid_chunks) < current_chunks or counts.get("pending", 0):
            state = "partial"
        else:
            state = "ready"
        return EmbeddingSummary(
            state=state,
            active_profile=profile,
            current_chunks=current_chunks,
            embedded_chunks=len(valid_chunks),
            pending_tasks=counts.get("pending", 0),
            failed_tasks=failed,
            corrupt_vectors=corrupt,
            last_build_report=report,
        )

    @staticmethod
    def _from_row(row: Any) -> EmbeddingProfile:
        return EmbeddingProfile(
            id=str(row["id"]),
            provider=str(row["provider"]),
            provider_version=str(row["provider_version"]),
            model=str(row["model"]),
            dimensions=int(row["dimensions"]),
            normalization=str(row["normalization"]),
            max_input_tokens=int(row["max_input_tokens"]),
            segment_tokens=int(row["segment_tokens"]),
            overlap_tokens=int(row["overlap_tokens"]),
            artifact_digest=str(row["artifact_digest"]),
        )


__all__ = ["EmbeddingProfileStore", "EmbeddingSummary", "embedding_task_input_hash"]
