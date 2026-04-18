"""Cross-encoder reranker — improves precision after ANN retrieval.

Uses sentence-transformers CrossEncoder (BAAI/bge-reranker-* family).
Model is lazy-loaded on first call and cached for the process lifetime.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("spark.reranker")

_model_name: str | None = None
_reranker = None  # sentence_transformers.CrossEncoder, loaded on first use


def rerank(query: str, rows: list[dict], model: str) -> list[dict]:
    """Rerank rows by cross-encoder score, descending. Returns rows unchanged on error."""
    if not rows:
        return rows

    global _reranker, _model_name
    try:
        if _reranker is None or _model_name != model:
            from sentence_transformers import CrossEncoder
            logger.info(f"[reranker] Loading model: {model}")
            _reranker = CrossEncoder(model)
            _model_name = model

        pairs = [(query, row["content"][:2000]) for row in rows]
        scores = _reranker.predict(pairs)
        ranked = sorted(zip(scores, rows), key=lambda x: x[0], reverse=True)
        return [row for _, row in ranked]

    except Exception:
        logger.exception("[reranker] Error during reranking — falling back to ANN order")
        return rows
