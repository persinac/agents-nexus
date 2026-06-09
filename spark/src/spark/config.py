from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class SparkConfig:
    installations_path: Path
    index_path: Path
    metadata_path: Path = field(init=False)
    embedding_model: str
    embedding_dimensions: int
    embedder: str  # "litellm" (Ollama, default) | "fastembed" (in-process ONNX) | "bedrock" (AWS Titan v2)
    teams: dict[str, str]
    include_patterns: list[str]
    exclude_dirs: list[str]
    max_file_size: int
    max_files_per_installation: int
    gitlab_url: str
    gitlab_token: str
    gitlab_enabled: bool
    github_token: str
    github_url: str
    github_enabled: bool
    webhook_secret: str
    chat_model: str
    decisions_enabled: bool
    reranker_enabled: bool
    reranker_model: str
    reranker_top_k_multiplier: int
    symbol_chunking_enabled: bool
    hybrid_search_enabled: bool
    spark_deep_stage1_k: int
    # Chunking constants
    chunk_size: int
    chunk_overlap: int
    summary_max_chars: int
    mr_chunk_max_chars: int
    max_chars_per_chunk: int

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> SparkConfig:
        # Load .env from project root (next to config.yaml)
        if config_path is None:
            config_path = os.environ.get(
                "SPARK_CONFIG",
                Path(__file__).parent.parent.parent / "config.yaml",
            )
        config_path = Path(config_path)
        load_dotenv(config_path.parent / ".env")
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        # Environment overrides for Docker
        installations_path = os.environ.get("SPARK_INSTALLATIONS_PATH", raw["installations_path"])
        index_path = os.environ.get("SPARK_INDEX_PATH", raw["index_path"])
        embedding_model = os.environ.get("SPARK_EMBEDDING_MODEL", raw["embedding_model"])
        embedder = os.environ.get("SPARK_EMBEDDER", raw.get("embedder", "litellm"))

        gitlab_url = os.environ.get("GITLAB_URL", raw.get("gitlab_url", ""))
        gitlab_token = os.environ.get("GITLAB_TOKEN", raw.get("gitlab_token", ""))
        github_token = os.environ.get("GITHUB_TOKEN", raw.get("github_token", ""))
        github_url = os.environ.get("GITHUB_URL", raw.get("github_url", "https://api.github.com"))
        webhook_secret = os.environ.get(
            "SPARK_WEBHOOK_SECRET",
            raw.get("webhook_secret", ""),
        )

        return cls(
            installations_path=Path(installations_path),
            index_path=Path(index_path),
            embedding_model=embedding_model,
            embedding_dimensions=raw["embedding_dimensions"],
            embedder=embedder,
            teams=raw.get("teams", {}),
            include_patterns=raw.get("include_patterns", []),
            exclude_dirs=raw.get("exclude_dirs", []),
            max_file_size=raw.get("max_file_size", 32768),
            max_files_per_installation=raw.get("max_files_per_installation", 100),
            gitlab_url=gitlab_url,
            gitlab_token=gitlab_token,
            gitlab_enabled=bool(gitlab_url and gitlab_token),
            github_token=github_token,
            github_url=github_url,
            github_enabled=bool(github_token),
            webhook_secret=webhook_secret,
            chat_model=raw.get("chat_model", "ollama/llama3.2"),
            decisions_enabled=raw.get("decisions_enabled", False),
            reranker_enabled=raw.get("reranker_enabled", False),
            reranker_model=raw.get("reranker_model", "BAAI/bge-reranker-base"),
            reranker_top_k_multiplier=raw.get("reranker_top_k_multiplier", 3),
            symbol_chunking_enabled=raw.get("symbol_chunking_enabled", True),
            hybrid_search_enabled=raw.get("hybrid_search_enabled", True),
            spark_deep_stage1_k=raw.get("spark_deep_stage1_k", 8),
            chunk_size=raw.get("chunk_size", 900),
            chunk_overlap=raw.get("chunk_overlap", 150),
            summary_max_chars=raw.get("summary_max_chars", 950),
            mr_chunk_max_chars=raw.get("mr_chunk_max_chars", 900),
            max_chars_per_chunk=raw.get("max_chars_per_chunk", 1000),
        )

    def __post_init__(self) -> None:
        self.metadata_path = self.index_path / "installations.json"

    def resolve_team(self, relative_path: str) -> str:
        """Resolve a relative repo path to its team name."""
        for prefix, team_name in sorted(self.teams.items(), key=lambda x: -len(x[0])):
            if relative_path.startswith(prefix):
                return team_name
        return "Unknown"
