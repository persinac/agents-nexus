"""Shared search primitives — hybrid (vector + FTS) retrieval + cross-encoder rerank.

Used by BOTH the CLI (`spark query`) and the MCP server so terminal and agent
search behave identically. Previously the CLI did vector-only, summary-only,
un-reranked search while the MCP tools did hybrid + rerank — a real quality gap.
"""
from __future__ import annotations

import logging

from spark.config import SparkConfig
from spark.indexer import reranker as _reranker_mod

logger = logging.getLogger("spark.search")


def hybrid_search(table, query: str, query_vector: list, where: str, limit: int, config: SparkConfig) -> list[dict]:
    """Hybrid (vector + full-text/BM25, RRF-fused) search; vector-only fallback."""
    if config.hybrid_search_enabled:
        try:
            return (
                table.search(query_type="hybrid")
                .vector(query_vector)
                .text(query)
                .where(where, prefilter=False)
                .rerank()
                .limit(limit)
                .to_list()
            )
        except Exception as exc:
            logger.warning("hybrid search failed, falling back to vector-only: %s", exc)
    return table.search(query_vector).where(where).limit(limit).to_list()


def maybe_rerank(query: str, rows: list[dict], config: SparkConfig) -> list[dict]:
    """Cross-encoder rerank when enabled; pass-through on disabled/failure."""
    if config.reranker_enabled and rows:
        try:
            return _reranker_mod.rerank(query, rows, config.reranker_model)
        except Exception as exc:
            logger.warning("rerank failed, using retrieval order: %s", exc)
    return rows


def with_archived_filter(where: str, include_archived: bool) -> str:
    """Append an archived-exclusion clause unless archived repos are explicitly wanted.

    `archived` is a non-null bool on every chunk, so this is safe across all
    chunk types (summary / file / symbol / merge_request)."""
    if include_archived:
        return where
    return f"({where}) AND archived = false" if where else "archived = false"


def fetch_k(top_k: int, config: SparkConfig) -> int:
    """Over-fetch before reranking so the cross-encoder has candidates to reorder."""
    return top_k * config.reranker_top_k_multiplier if config.reranker_enabled else top_k
