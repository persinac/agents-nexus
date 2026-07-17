"""Cross-encoder reranker — improves precision after ANN retrieval.

Uses sentence-transformers CrossEncoder (BAAI/bge-reranker-* family).
Model is lazy-loaded on first call and cached for the process lifetime.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger("spark.reranker")


def _sigmoid(x: float) -> float:
    """Map a cross-encoder logit to a 0-1 relevance score for display sanity."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)

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
        # Stamp the cross-encoder score onto each row so result formatting shows
        # actual relevance, not the stale RRF fusion score from hybrid retrieval.
        for score, row in zip(scores, rows):
            row["_relevance_score"] = _sigmoid(float(score))
        ranked = sorted(rows, key=lambda r: r["_relevance_score"], reverse=True)
        return ranked

    except Exception:
        logger.exception("[reranker] Error during reranking — falling back to ANN order")
        return rows
