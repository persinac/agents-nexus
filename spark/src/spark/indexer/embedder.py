"""Embedder — vendor-agnostic embedding via LiteLLM.

'Protocol dictates action!' — 127 Guilty Spark

Uses concurrent workers to parallelize embedding calls to Ollama.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import litellm
from tqdm import tqdm

from spark.config import SparkConfig

logger = __import__("logging").getLogger("spark.embedder")

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

# LiteLLM picks up OLLAMA_API_BASE automatically for ollama/* models.
# In Docker, this points to http://ollama:11434.

# nomic-embed-text via Ollama: token limit is 8192 but the practical
# char limit depends on content density. Dense code/minified strings
# fail at ~1024 chars. Normal code with whitespace/keywords survives
# to ~2000. Use 1000 as a safe universal ceiling.
_MAX_CHARS_PER_CHUNK = 1000

# Number of parallel embedding workers.
# Ollama can handle concurrent requests — this is the main speedup.
_DEFAULT_WORKERS = int(os.environ.get("SPARK_WORKERS", "8"))


def _truncate(text: str, max_chars: int = _MAX_CHARS_PER_CHUNK) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n... [truncated for embedding]"
    return text


def _embed_batch(batch: list[str], model: str, dimensions: int) -> list[list[float]] | None:
    """Embed a single batch. Returns embeddings or None on failure."""
    try:
        response = litellm.embedding(model=model, input=batch)
        return [item["embedding"] for item in response.data]
    except Exception:
        return None


def _embed_single_with_retry(text: str, model: str, dimensions: int, max_chars_per_chunk: int = _MAX_CHARS_PER_CHUNK) -> list[float]:
    """Embed a single chunk with progressive truncation and retries."""
    for max_chars in [max_chars_per_chunk, 500, 200]:
        truncated = text[:max_chars]
        for attempt in range(2):
            try:
                response = litellm.embedding(model=model, input=[truncated])
                return response.data[0]["embedding"]
            except Exception:
                time.sleep(0.3)
    return [0.0] * dimensions


def embed_texts(
    texts: list[str],
    config: SparkConfig,
    batch_size: int = 4,
    workers: int | None = None,
) -> list[list[float]]:
    """Embed texts using parallel workers against Ollama.

    Splits texts into batches, sends them concurrently via a thread pool.
    Falls back to single-chunk retry on batch failure.
    """
    if workers is None:
        workers = _DEFAULT_WORKERS

    total = len(texts)
    zero_count = 0

    # Pre-truncate all texts
    truncated_texts = [_truncate(t, max_chars=config.max_chars_per_chunk) for t in texts]

    # Build batches: list of (batch_index, [texts])
    batches: list[tuple[int, list[str]]] = []
    for i in range(0, total, batch_size):
        batches.append((i, truncated_texts[i : i + batch_size]))

    # Results array — pre-allocate so we can fill by index
    results: list[list[float] | None] = [None] * total

    pbar = tqdm(
        total=total,
        desc="  Embedding",
        unit="chunks",
        bar_format="  {l_bar}{bar:40}{r_bar}",
    )

    def process_batch(batch_info: tuple[int, list[str]]) -> tuple[int, list[list[float]] | None]:
        idx, batch = batch_info
        embeddings = _embed_batch(batch, config.embedding_model, config.embedding_dimensions)
        return idx, embeddings

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_batch, b): b for b in batches}

        for future in as_completed(futures):
            idx, embeddings = future.result()
            batch_info = futures[future]
            batch_texts = batch_info[1]
            batch_len = len(batch_texts)

            if embeddings is not None:
                # Success — fill results by position
                for j, emb in enumerate(embeddings):
                    results[idx + j] = emb
            else:
                # Batch failed — retry individually (still in thread pool)
                for j, text in enumerate(batch_texts):
                    emb = _embed_single_with_retry(text, config.embedding_model, config.embedding_dimensions, config.max_chars_per_chunk)
                    if emb == [0.0] * config.embedding_dimensions:
                        zero_count += 1
                    results[idx + j] = emb

            pbar.update(batch_len)
            if zero_count:
                pbar.set_postfix(failed=zero_count)

    pbar.close()

    if zero_count:
        logger.warning("%d/%d chunks used zero vectors (embedding failed)", zero_count, total)

    # Fill any remaining Nones (shouldn't happen, but safety)
    final = []
    for r in results:
        if r is None:
            zero_count += 1
            final.append([0.0] * config.embedding_dimensions)
        else:
            final.append(r)

    return final


def embed_single(text: str, config: SparkConfig) -> list[float]:
    """Embed a single text string."""
    truncated = _truncate(text, max_chars=config.max_chars_per_chunk)
    return _embed_single_with_retry(truncated, config.embedding_model, config.embedding_dimensions, config.max_chars_per_chunk)
