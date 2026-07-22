#!/usr/bin/env bash
set -euo pipefail

DEST="${1:-$HOME/repos/reference}"
mkdir -p "$DEST"

REPOS=(
  # Agentic / Orchestration
  https://github.com/langchain-ai/langgraph.git
  https://github.com/microsoft/autogen.git
  https://github.com/anthropics/anthropic-cookbook.git
  https://github.com/openai/swarm.git
  https://github.com/prefecthq/prefect.git

  # Engineering Practices
  https://github.com/astral-sh/ruff.git
  https://github.com/astral-sh/uv.git
  https://github.com/pydantic/pydantic.git
  https://github.com/fastapi/fastapi.git

  # Data Engineering
  https://github.com/apache/arrow.git
  https://github.com/duckdb/duckdb.git
  https://github.com/dagster-io/dagster.git
  https://github.com/tobymao/sqlglot.git

  # Distributed Computing
  https://github.com/ray-project/ray.git
  https://github.com/temporalio/temporal.git
  https://github.com/hashicorp/consul.git
  https://github.com/vitessio/vitess.git

  # AI Chat
  https://github.com/vercel/chat.git
)

for url in "${REPOS[@]}"; do
  name=$(basename "$url" .git)
  if [ -d "$DEST/$name" ]; then
    echo "[SKIP] $name — already cloned"
  else
    echo "[CLONE] $name..."
    git clone --depth 1 "$url" "$DEST/$name"
    echo "  OK"
  fi
done

echo ""
echo "Done. $DEST ready."
