"""Vendor-agnostic embedding helper — default: Ollama nomic-embed-text (local, no API key).

Default model: ollama/nomic-embed-text (768 dims, runs locally via Ollama).
Override via EMBEDDING_MODEL env var, e.g.:
  EMBEDDING_MODEL=voyage/voyage-3          VOYAGE_API_KEY=...   (1024 dims)
  EMBEDDING_MODEL=text-embedding-3-small   OPENAI_API_KEY=...   (1536 dims)
"""

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ollama/nomic-embed-text"
DEFAULT_DIMS = 768


class LiteLLMEmbeddingProvider:
    """EmbeddingProvider implementation using LiteLLM's aembedding()."""

    def __init__(
        self,
        model: str | None = None,
        dims: int | None = None,
    ):
        self._model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        self._dims = dims or int(os.getenv("EMBEDDING_DIMS", str(DEFAULT_DIMS)))

    @property
    def dimensions(self) -> int:
        return self._dims

    async def embed(self, text: str) -> list[float]:
        import litellm

        response = await litellm.aembedding(model=self._model, input=[text])
        return response.data[0]["embedding"]
