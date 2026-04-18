#!/bin/bash
set -euo pipefail

# ============================================================
# 127 Guilty Spark — Quickstart
# "Greetings! I am 127 Guilty Spark, the Monitor of
#  Installation 04. Someone has released the Index!"
# ============================================================

SPARK_DIR="$(cd "$(dirname "$0")" && pwd)"
REPOS_DIR="$(dirname "$SPARK_DIR")"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[Guilty Spark]${NC} $*"; }
ok()    { echo -e "${GREEN}[Guilty Spark]${NC} $*"; }
warn()  { echo -e "${YELLOW}[Guilty Spark]${NC} $*"; }

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   127 Guilty Spark — Installation Quickstart ║"
echo "  ║   'I can show you where the Index is.'       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# ----------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------
info "Checking prerequisites..."

if ! command -v ollama &>/dev/null; then
  warn "Ollama not found. Install it: brew install ollama"
  exit 1
fi

if ! command -v uv &>/dev/null; then
  warn "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

ok "Prerequisites found (ollama, uv)"

# ----------------------------------------------------------
# 2. Start Ollama (if not running)
# ----------------------------------------------------------
if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
  info "Starting Ollama..."
  ollama serve &>/dev/null &
  OLLAMA_PID=$!
  # Wait for it to be ready
  for i in $(seq 1 30); do
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
    warn "Failed to start Ollama. Start it manually: ollama serve"
    exit 1
  fi
  ok "Ollama started (PID $OLLAMA_PID)"
else
  ok "Ollama already running"
fi

# ----------------------------------------------------------
# 3. Pull embedding model
# ----------------------------------------------------------
if ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
  ok "nomic-embed-text model already pulled"
else
  info "Pulling nomic-embed-text model (~274MB)..."
  ollama pull nomic-embed-text
  ok "Model pulled"
fi

# ----------------------------------------------------------
# 4. Install guilty-spark
# ----------------------------------------------------------
info "Installing guilty-spark..."
cd "$SPARK_DIR"
uv sync --quiet 2>&1
ok "Dependencies installed"

# ----------------------------------------------------------
# 5. Build the Index
# ----------------------------------------------------------
PATH_FILTER="${SPARK_PATH_FILTER:-all}"
info "Building the Index for '${PATH_FILTER}/*' (set SPARK_PATH_FILTER to change, or 'all' for everything)..."
echo ""
if [[ "$PATH_FILTER" == "all" ]]; then
  uv run spark reclaim
else
  uv run spark reclaim -p "$PATH_FILTER"
fi
echo ""
info "To index more teams later, run: spark reclaim -p <team>"
info "To index everything:            spark reclaim"

# ----------------------------------------------------------
# 6. Install global CLI
# ----------------------------------------------------------
info "Installing global 'spark' command..."

SPARK_BIN="/usr/local/bin/spark"
SPARK_WRAPPER=$(cat <<WRAPPER
#!/bin/bash
# 127 Guilty Spark — Global CLI wrapper
cd "$SPARK_DIR" && uv run spark "\$@"
WRAPPER
)

if [[ -w /usr/local/bin ]]; then
  echo "$SPARK_WRAPPER" > "$SPARK_BIN"
  chmod +x "$SPARK_BIN"
  ok "Installed: $SPARK_BIN"
else
  warn "Need sudo to install to /usr/local/bin"
  echo "$SPARK_WRAPPER" | sudo tee "$SPARK_BIN" > /dev/null
  sudo chmod +x "$SPARK_BIN"
  ok "Installed: $SPARK_BIN (via sudo)"
fi

# ----------------------------------------------------------
# 7. Configure Claude Code MCP server
# ----------------------------------------------------------
info "Configuring Claude Code MCP integration..."

CLAUDE_CONFIG_DIR="$HOME/.claude"
CLAUDE_CONFIG="$CLAUDE_CONFIG_DIR/claude_code_config.json"
mkdir -p "$CLAUDE_CONFIG_DIR"

# Create or merge MCP config
if [[ -f "$CLAUDE_CONFIG" ]]; then
  # Check if guilty-spark is already configured
  if grep -q "guilty-spark" "$CLAUDE_CONFIG" 2>/dev/null; then
    ok "Claude Code MCP already configured"
  else
    warn "Claude Code config exists. Add this manually to mcpServers in $CLAUDE_CONFIG:"
    echo ""
    cat <<MCPJSON
    "guilty-spark": {
      "command": "$SPARK_BIN",
      "args": ["serve"]
    }
MCPJSON
    echo ""
  fi
else
  cat > "$CLAUDE_CONFIG" <<MCPJSON
{
  "mcpServers": {
    "guilty-spark": {
      "command": "$SPARK_BIN",
      "args": ["serve"]
    }
  }
}
MCPJSON
  ok "Created $CLAUDE_CONFIG with guilty-spark MCP server"
fi

# ----------------------------------------------------------
# 8. Install Claude Code slash command
# ----------------------------------------------------------
info "Installing Claude Code slash command..."

COMMANDS_DIR="$CLAUDE_CONFIG_DIR/commands"
mkdir -p "$COMMANDS_DIR"

cat > "$COMMANDS_DIR/spark.md" <<'SLASHCMD'
---
description: "Query the installation index (127 Guilty Spark)"
---

Use the guilty-spark MCP tools to answer the user's question about repositories.

The user's query: $ARGUMENTS

Follow this protocol:
1. Use the `spark` MCP tool with the user's query to search the installation index semantically
2. If the query asks about a specific repo, also call `installation_summary` for detailed info
3. If the query asks "which repos" or "list", use `list_installations` with an appropriate team filter
4. Present results concisely — repo name, team, path, and why it's relevant
5. If the user needs to look at actual code, tell them the file paths from the search results

For terraform questions, filter by team "Platform - Infrastructure".
For service questions, search broadly and note which team owns each result.
SLASHCMD

ok "Installed /spark slash command"

# ----------------------------------------------------------
# Done!
# ----------------------------------------------------------
echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   Installation complete!                     ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
ok "Global CLI:       spark query 'EKS cluster provisioning'"
ok "                  spark status"
ok "                  spark activate svc-chatbot"
ok "                  spark reclaim  (full rebuild)"
ok ""
ok "Claude Code:      /spark which terraform repo manages EKS?"
ok "                  /spark how does the chatbot handle handoffs?"
ok ""
ok "MCP server:       spark serve  (auto-started by Claude Code)"
echo ""
info "'I am a genius. Hee hee hee hee!'"
echo ""
