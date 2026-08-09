"""Deterministic local exact-scan benchmark for M2B; never downloads or writes data."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from favhub.hybrid_search import LexicalCandidate, SemanticCandidate, reciprocal_rank_fusion


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    chunks: int
    dimensions: int
    query_count: int
    elapsed_seconds: float
    fusion_seconds: float
    process_peak_rss_bytes: int | None
    peak_python_allocated_bytes: int


def _vectors(count: int, dimensions: int) -> np.ndarray:
    rng = np.random.default_rng(20260722 + count)
    vectors = rng.standard_normal((count, dimensions), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors


def _peak_rss_bytes() -> int | None:
    try:
        resource: Any = importlib.import_module("resource")
    except ImportError:
        return None
    usage = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return usage if sys.platform == "darwin" else usage * 1024


def run_case(count: int, *, query_count: int = 5, dimensions: int = 384) -> BenchmarkResult:
    for name, value in (("count", count), ("query_count", query_count), ("dimensions", dimensions)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    tracemalloc.start()
    try:
        vectors = _vectors(count, dimensions)
        queries = _vectors(query_count, dimensions)
        started = time.perf_counter()
        scores = vectors @ queries.T
        scan_elapsed = time.perf_counter() - started
        fusion_started = time.perf_counter()
        candidate_count = min(50, count)
        for query_index in range(query_count):
            best = np.argpartition(scores[:, query_index], -candidate_count)[-candidate_count:]
            semantic = tuple(
                SemanticCandidate(
                    f"favhub:x/item-{int(row)}#chunk-0",
                    float(scores[row, query_index]),
                    platform="x",
                    source_id=f"item-{int(row)}",
                )
                for row in best
            )
            lexical = tuple(
                LexicalCandidate(f"favhub:x/item-{rank}#chunk-0", rank + 1)
                for rank in range(min(10, count))
            )
            reciprocal_rank_fusion(lexical, semantic, limit=min(10, count))
        fusion_elapsed = time.perf_counter() - fusion_started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return BenchmarkResult(
        chunks=count,
        dimensions=dimensions,
        query_count=query_count,
        elapsed_seconds=scan_elapsed,
        fusion_seconds=fusion_elapsed,
        process_peak_rss_bytes=_peak_rss_bytes(),
        peak_python_allocated_bytes=peak,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", nargs="+", type=_positive_count, default=[1000, 10000, 50000])
    args = parser.parse_args()
    payload = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "results": [asdict(run_case(count)) for count in args.counts],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _positive_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("count must be a positive integer") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be a positive integer")
    return count


if __name__ == "__main__":
    raise SystemExit(main())
