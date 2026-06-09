"""Embedder — vendor-agnostic embedding via LiteLLM.

'Protocol dictates action!' — 127 Guilty Spark

Uses concurrent workers to parallelize embedding calls to Ollama.
"""
from __future__ import annotations

import json
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

# FastEmbed model — bge-small-en-v1.5: 384-dim, ~10x faster on CPU than the
# heavy nomic model. NOTE: 384 dims != the old 768 Ollama index, so switching
# requires a full `spark reclaim` (the table is dropped + recreated at 384-dim).
_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_fastembed = None


def _get_fastembed():
    """Lazily construct the in-process FastEmbed model (ONNX, no daemon)."""
    global _fastembed
    if _fastembed is None:
        from fastembed import TextEmbedding

        _fastembed = TextEmbedding(model_name=_FASTEMBED_MODEL)
    return _fastembed


def _fastembed_embed(texts: list[str], max_chars: int) -> list[list[float]]:
    """Embed texts with FastEmbed. Document mode for all vectors (index + query),
    mirroring the single-mode Ollama behavior so the corpus stays self-consistent.
    """
    model = _get_fastembed()
    truncated = [_truncate(t, max_chars=max_chars) for t in texts]
    # model.embed() returns a generator of np.ndarray, order-preserving.
    return [vec.tolist() for vec in model.embed(truncated)]


# --- AWS Bedrock (Titan Text Embeddings V2) — POC ---------------------------
# Offloads embedding to Bedrock instead of in-process ONNX (fastembed) or Ollama.
# Credentials come from boto3's default chain (env vars, shared profile, SSO);
# region from AWS_REGION / BEDROCK_REGION (default us-east-1). Titan v2 embeds
# ONE inputText per InvokeModel call, so bulk indexing fans out over a thread
# pool (boto3 low-level clients are thread-safe for invoke_model).
#
# Vector space MUST match between index- and query-time: the same model + dims
# embed both. Switching to/from bedrock therefore requires a full `spark reclaim`.
_BEDROCK_DEFAULT_MODEL = "amazon.titan-embed-text-v2:0"
_bedrock_client = None


def _get_bedrock_client():
    """Lazily construct a shared bedrock-runtime client (thread-safe for invoke)."""
    global _bedrock_client
    if _bedrock_client is None:
        import boto3

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("BEDROCK_REGION")
            or "us-east-1"
        )
        _bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_client


def _bedrock_model(config: SparkConfig) -> str:
    """Resolve the Bedrock model id, falling back to Titan v2 if the configured
    model is clearly not a Bedrock id (e.g. left at the Ollama/FastEmbed default)."""
    m = config.embedding_model
    if not m or m.startswith("ollama/") or m.startswith("BAAI/"):
        return _BEDROCK_DEFAULT_MODEL
    return m


def _bedrock_dims(config: SparkConfig) -> int:
    """Titan v2 supports 256 / 512 / 1024 output dimensions; default to 1024."""
    return config.embedding_dimensions if config.embedding_dimensions in (256, 512, 1024) else 1024


# Non-retryable Bedrock errors — re-raised immediately so a reclaim aborts loudly
# instead of silently writing zero vectors. Covers authz, bad model id/region,
# malformed requests, and (critically) SSO/credential expiry — the failure mode
# that silently corrupted the first POC run.
_BEDROCK_FATAL_CODES = frozenset({
    "AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException",
    "ExpiredTokenException", "ValidationException", "ResourceNotFoundException",
    "ForbiddenException", "UnauthorizedException",
})
_BEDROCK_FATAL_EXC = frozenset({
    "NoCredentialsError", "TokenRetrievalError", "UnauthorizedSSOTokenError",
    "SSOTokenLoadError", "CredentialRetrievalError", "ExpiredTokenError",
})


class BedrockAuthError(RuntimeError):
    """Non-retryable Bedrock failure (authz, expired creds, bad model/region)."""


def _is_fatal_bedrock_error(e: Exception) -> bool:
    code = ""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code", "")
    return code in _BEDROCK_FATAL_CODES or type(e).__name__ in _BEDROCK_FATAL_EXC


def _bedrock_embed_one(text: str, model: str, dims: int, max_chars: int) -> list[float] | None:
    """Embed a single text via Bedrock Titan v2.

    Returns the vector on success, or None after exhausting retries on *transient*
    errors (throttling, timeouts). Raises BedrockAuthError on *fatal* errors so the
    caller aborts rather than writing zero vectors.
    """
    body = json.dumps({
        "inputText": _truncate(text, max_chars=max_chars),
        "dimensions": dims,
        "normalize": True,
    })
    client = _get_bedrock_client()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.invoke_model(modelId=model, body=body)
            return json.loads(resp["body"].read())["embedding"]
        except Exception as e:
            if _is_fatal_bedrock_error(e):
                raise BedrockAuthError(
                    f"Bedrock InvokeModel failed for '{model}' (not retryable): {e}. "
                    f"Check credentials (bedrock:InvokeModel), AWS_REGION, and model access."
                ) from e
            last_exc = e  # transient — back off and retry
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    logger.warning("bedrock embed failed after retries (transient): %s", last_exc)
    return None


def _bedrock_embed(texts: list[str], config: SparkConfig, workers: int | None = None) -> list[list[float]]:
    """Embed texts via Bedrock, fanning single-input calls across a thread pool."""
    model = _bedrock_model(config)
    dims = _bedrock_dims(config)
    if not texts:
        return []
    if workers is None:
        workers = _DEFAULT_WORKERS

    # Preflight: one probe call so authz/region/model-access errors abort in ~1s
    # instead of after a full run. Fatal errors raise out of here.
    _bedrock_embed_one(texts[0], model, dims, config.max_chars_per_chunk)

    total = len(texts)
    results: list[list[float] | None] = [None] * total
    zero_count = 0

    pbar = tqdm(
        total=total,
        desc="  Embedding",
        unit="chunks",
        bar_format="  {l_bar}{bar:40}{r_bar}",
    )

    def _one(i: int) -> tuple[int, list[float] | None]:
        return i, _bedrock_embed_one(texts[i], model, dims, config.max_chars_per_chunk)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_one, i) for i in range(total)]
        try:
            for future in as_completed(futures):
                i, emb = future.result()
                if emb is None:
                    emb = [0.0] * dims
                    zero_count += 1
                results[i] = emb
                pbar.update(1)
                if zero_count:
                    pbar.set_postfix(failed=zero_count)
        except BedrockAuthError:
            for f in futures:
                f.cancel()
            pbar.close()
            raise

    pbar.close()
    if zero_count:
        logger.warning("%d/%d chunks used zero vectors (transient bedrock failures)", zero_count, total)
    return [r if r is not None else [0.0] * dims for r in results]


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
    if config.embedder == "bedrock":
        # AWS Bedrock (Titan v2) — offloaded compute, single-input calls fanned
        # out across a thread pool. No local model, no Ollama HTTP.
        return _bedrock_embed(texts, config, workers=workers)

    if config.embedder == "fastembed":
        # In-process ONNX — batches + multithreads internally, no Ollama HTTP.
        model = _get_fastembed()
        truncated_texts = [_truncate(t, max_chars=config.max_chars_per_chunk) for t in texts]
        results = [
            vec.tolist()
            for vec in tqdm(
                model.embed(truncated_texts),
                total=len(texts),
                desc="  Embedding",
                unit="chunks",
                bar_format="  {l_bar}{bar:40}{r_bar}",
            )
        ]
        return results

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
    if config.embedder == "bedrock":
        dims = _bedrock_dims(config)
        emb = _bedrock_embed_one(text, _bedrock_model(config), dims, config.max_chars_per_chunk)
        return emb if emb is not None else [0.0] * dims
    if config.embedder == "fastembed":
        return _fastembed_embed([text], config.max_chars_per_chunk)[0]
    truncated = _truncate(text, max_chars=config.max_chars_per_chunk)
    return _embed_single_with_retry(truncated, config.embedding_model, config.embedding_dimensions, config.max_chars_per_chunk)
