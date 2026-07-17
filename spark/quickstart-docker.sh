#!/bin/bash
set -euo pipefail

# ============================================================
# 127 Guilty Spark — Docker Quickstart
# For when you want everything containerized.
# Note: Ollama in Docker won't use Apple Silicon GPU.
#       Use quickstart.sh for native macOS (faster embeddings).
# ============================================================

SPARK_DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[Guilty Spark]${NC} $*"; }
ok()    { echo -e "${GREEN}[Guilty Spark]${NC} $*"; }

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   127 Guilty Spark — Docker Quickstart       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

cd "$SPARK_DIR"

# ----------------------------------------------------------
# 1. Build and start services
# ----------------------------------------------------------
info "Building and starting containers..."
docker compose up -d --build
ok "Containers running"

# ----------------------------------------------------------
# 2. Pull embedding model inside Ollama container
# ----------------------------------------------------------
info "Pulling nomic-embed-text model in Ollama container..."
docker compose exec ollama ollama pull nomic-embed-text
ok "Model pulled"

# ----------------------------------------------------------
# 3. Run initial index build
# ----------------------------------------------------------
info "Building the Index..."
docker compose exec spark uv run spark reclaim
ok "Index built"

# ----------------------------------------------------------
# 4. Install global CLI (talks to Docker)
# ----------------------------------------------------------
info "Installing global 'spark' command (Docker mode)..."

SPARK_BIN="/usr/local/bin/spark"
SPARK_WRAPPER=$(cat <<WRAPPER
#!/bin/bash
# 127 Guilty Spark — Docker CLI wrapper
docker compose -f "$SPARK_DIR/docker-compose.yml" exec spark uv run spark "\$@"
WRAPPER
)

if [[ -w /usr/local/bin ]]; then
  echo "$SPARK_WRAPPER" > "$SPARK_BIN"
  chmod +x "$SPARK_BIN"
else
  echo "$SPARK_WRAPPER" | sudo tee "$SPARK_BIN" > /dev/null
  sudo chmod +x "$SPARK_BIN"
fi
ok "Installed: $SPARK_BIN"

echo ""
ok "Done! The MCP server is running on the spark container."
ok "Use 'docker compose logs -f spark' to watch it."
echo ""
