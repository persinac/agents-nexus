#!/usr/bin/env bash
# Unified installer for agents-nexus.
# Detects OS, installs system deps, links configs, and sets up the pixel dashboard.
#
# Usage:
#   ./install.sh          # full install (deps + configs + dashboard)
#   ./install.sh --no-ui  # skip pixel dashboard setup

set -euo pipefail

# ── Detect OS ──────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Darwin)  echo "mac" ;;
    Linux)
      if [ -d "/c/msys64" ] || [ -n "${MSYSTEM:-}" ]; then
        echo "windows"
      else
        echo "linux"
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *)
      echo "unknown"
      ;;
  esac
}

OS=$(detect_os)
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$REPO_DIR/tmux/$OS"
SKIP_UI=false
[[ "${1:-}" == "--no-ui" ]] && SKIP_UI=true

echo ""
echo "  Agent Orchestration Installer"
echo "  Platform: $OS"
echo "  Repo:     $REPO_DIR"
echo ""

if [ ! -d "$PLATFORM_DIR" ]; then
  echo "ERROR: No platform directory found at $PLATFORM_DIR"
  echo "Supported platforms: mac, windows, linux"
  exit 1
fi

# ── Step 1: System dependencies ────────────────────────────────
echo "── Step 1: System dependencies ──────────────────────────"

check_cmd() {
  command -v "$1" &>/dev/null
}

# ── Python / uv ─────────────────────────────────────────────────
PYTHON_VERSION="3.14"

install_uv() {
  if check_cmd uv; then echo "  [ok] uv"; return; fi
  echo "  Installing uv..."
  case "$OS" in
    mac)     brew install uv ;;
    linux)   curl -LsSf https://astral.sh/uv/install.sh | sh
             export PATH="$HOME/.local/bin:$PATH" ;;
    windows)
      if check_cmd scoop; then scoop install uv
      else
        echo "  WARNING: scoop not found — install uv from https://docs.astral.sh/uv/"
      fi ;;
  esac
}

ensure_python() {
  if ! check_cmd uv; then
    echo "  WARNING: uv unavailable, skipping Python $PYTHON_VERSION install"
    return
  fi
  if check_cmd python3; then
    local major minor
    major=$(python3 -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
    minor=$(python3 -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
    if [[ "$major" -eq 3 && "$minor" -ge 14 ]]; then
      echo "  [ok] $(python3 --version)"; return
    fi
    echo "  python3 $major.$minor found, need $PYTHON_VERSION — installing via uv..."
  else
    echo "  python3 not found — installing $PYTHON_VERSION via uv..."
  fi
  uv python install "$PYTHON_VERSION"
  local uv_py
  uv_py=$(uv python find "$PYTHON_VERSION" 2>/dev/null) || return 0
  mkdir -p "$HOME/.local/bin"
  for name in python python3; do
    printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$uv_py" > "$HOME/.local/bin/$name"
    chmod +x "$HOME/.local/bin/$name"
  done
  echo "  -> ~/.local/bin/{python,python3} -> Python $PYTHON_VERSION (uv-managed)"
  echo "     Ensure ~/.local/bin is early in your PATH"
}

install_deps_mac() {
  if ! check_cmd brew; then
    echo "Homebrew not found. Install it from https://brew.sh"
    exit 1
  fi

  local deps=(tmux fzf node)
  local to_install=()
  for dep in "${deps[@]}"; do
    if ! check_cmd "$dep"; then
      to_install+=("$dep")
    else
      echo "  [ok] $dep"
    fi
  done

  if [ ${#to_install[@]} -gt 0 ]; then
    echo "  Installing: ${to_install[*]}"
    brew install "${to_install[@]}"
  fi

  # Claude Code
  if ! check_cmd claude; then
    echo "  Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code
  else
    echo "  [ok] claude"
  fi

  install_uv
  ensure_python
}

install_deps_windows() {
  # Running inside MSYS2
  local deps=(tmux fzf)
  local to_install=()
  for dep in "${deps[@]}"; do
    if ! check_cmd "$dep"; then
      to_install+=("$dep")
    else
      echo "  [ok] $dep"
    fi
  done

  if [ ${#to_install[@]} -gt 0 ]; then
    echo "  Installing via pacman: ${to_install[*]}"
    pacman -S --noconfirm "${to_install[@]/#/mingw-w64-x86_64-}" tmux 2>/dev/null \
      || pacman -S --noconfirm tmux mingw-w64-x86_64-fzf
  fi

  if ! check_cmd node; then
    echo "  WARNING: Node.js not found in MSYS2 PATH."
    echo "  Install Node.js on Windows and ensure it's accessible."
    echo "  You may need a wrapper in ~/.local/bin/node"
  else
    echo "  [ok] node"
  fi

  if ! check_cmd claude; then
    echo "  WARNING: Claude Code not found in PATH."
    echo "  Install via: npm install -g @anthropic-ai/claude-code"
  else
    echo "  [ok] claude"
  fi

  install_uv
  ensure_python
}

install_deps_linux() {
  local deps=(tmux fzf node)
  local missing=()
  for dep in "${deps[@]}"; do
    if ! check_cmd "$dep"; then
      missing+=("$dep")
    else
      echo "  [ok] $dep"
    fi
  done

  if [ ${#missing[@]} -gt 0 ]; then
    echo "  Missing: ${missing[*]}"
    if check_cmd apt; then
      echo "  Installing via apt..."
      sudo apt update -qq && sudo apt install -y -qq tmux fzf nodejs npm
    elif check_cmd dnf; then
      echo "  Installing via dnf..."
      sudo dnf install -y tmux fzf nodejs npm
    elif check_cmd pacman; then
      echo "  Installing via pacman..."
      sudo pacman -S --noconfirm tmux fzf nodejs npm
    else
      echo "  Could not detect package manager. Install manually: ${missing[*]}"
      exit 1
    fi
  fi

  if ! check_cmd claude; then
    echo "  Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code
  else
    echo "  [ok] claude"
  fi

  install_uv
  ensure_python
}

setup_skills() {
  local skills_src="$REPO_DIR/skills"
  [ -d "$skills_src" ] || return 0
  mkdir -p "$HOME/.claude/skills"
  for skill_dir in "$skills_src"/*/; do
    [ -d "$skill_dir" ] || continue
    local name
    name=$(basename "$skill_dir")
    local target="$HOME/.claude/skills/$name"
    local src
    src="$(cd "$skill_dir" && pwd)"
    if [ -L "$target" ]; then
      ln -sf "$src" "$target"
      echo "  [ok] skill: $name"
    elif [ -d "$target" ]; then
      rm -rf "$target"
      ln -sf "$src" "$target"
      echo "  -> ~/.claude/skills/$name (adopted from real dir)"
    else
      ln -sf "$src" "$target"
      echo "  -> ~/.claude/skills/$name"
    fi
  done
}

validate_setup() {
  local all_ok=true

  if check_cmd uv; then
    echo "  [ok] uv $(uv --version 2>&1)"
  else
    echo "  !! uv not found"
    all_ok=false
  fi

  if check_cmd python3; then
    echo "  [ok] $(python3 --version)"
  else
    echo "  !! python3 not in PATH (ensure ~/.local/bin is early in PATH)"
    all_ok=false
  fi

  local mcp_config="$HOME/.claude/claude_code_config.json"
  if [ ! -f "$mcp_config" ]; then
    echo "  !! ~/.claude/claude_code_config.json not found"
    all_ok=false
  elif ! node -e "JSON.parse(require('fs').readFileSync('$mcp_config','utf8'))" 2>/dev/null; then
    echo "  !! ~/.claude/claude_code_config.json is invalid JSON"
    all_ok=false
  else
    echo "  [ok] ~/.claude/claude_code_config.json"
    local spark_cmd agent_python
    spark_cmd=$(node -e "const c=require('$mcp_config');process.stdout.write(c.mcpServers?.['guilty-spark']?.command??'')" 2>/dev/null)
    agent_python=$(node -e "const c=require('$mcp_config');process.stdout.write(c.mcpServers?.['agent-memory']?.command??'')" 2>/dev/null)
    [ -n "$spark_cmd"    ] && { [ -f "$spark_cmd"    ] && echo "  [ok] spark: $spark_cmd"          || echo "  !! spark binary not found: $spark_cmd"; }
    [ -n "$agent_python" ] && { [ -f "$agent_python" ] && echo "  [ok] agent-memory: $agent_python" || echo "  !! agent-memory python not found: $agent_python"; }
  fi

  echo ""
  $all_ok && echo "  All checks passed." || echo "  Some checks failed — see above."
}

case "$OS" in
  mac)     install_deps_mac ;;
  windows) install_deps_windows ;;
  linux)   install_deps_linux ;;
esac

echo ""

# ── Step 2: Platform configs ───────────────────────────────────
echo "── Step 2: Platform configs ─────────────────────────────"

# Run the platform-specific install script
if [ -f "$PLATFORM_DIR/install.sh" ]; then
  echo "  Running $OS/install.sh..."
  bash "$PLATFORM_DIR/install.sh"
else
  echo "  WARNING: $PLATFORM_DIR/install.sh not found, installing manually..."

  # Fallback: do the common steps
  mkdir -p "$HOME/.tmux"
  cp "$PLATFORM_DIR/tmux.conf" "$HOME/.tmux.conf" 2>/dev/null \
    && echo "  -> ~/.tmux.conf" || true

  for script in "$PLATFORM_DIR"/tmux-scripts/*.sh; do
    [ -f "$script" ] || continue
    cp "$script" "$HOME/.tmux/$(basename "$script")"
    chmod +x "$HOME/.tmux/$(basename "$script")"
    echo "  -> ~/.tmux/$(basename "$script")"
  done
fi

echo ""

# ── Step 3: Global Claude skills ──────────────────────────────
echo "── Step 3: Global Claude skills ─────────────────────────"
setup_skills
echo ""

# ── Step 4: Pixel Dashboard ───────────────────────────────────
if $SKIP_UI; then
  echo "── Step 4: Pixel Dashboard (skipped) ──────────────────"
else
  echo "── Step 4: Pixel Dashboard ────────────────────────────"
  DASHBOARD_DIR="$REPO_DIR/pixel-dashboard"

  if [ ! -d "$DASHBOARD_DIR" ]; then
    echo "  WARNING: pixel-dashboard/ not found, skipping"
  elif ! check_cmd node; then
    echo "  WARNING: Node.js not found, skipping dashboard setup"
  else
    echo "  Installing dashboard dependencies..."
    (cd "$DASHBOARD_DIR" && npm run setup)
    echo "  Dashboard ready. Start with: cd pixel-dashboard && npm run dev"
  fi
fi

echo ""

# ── Step 4: Validate ──────────────────────────────────────────
echo "── Step 5: Validate ─────────────────────────────────────"
validate_setup
echo ""

# ── Summary ────────────────────────────────────────────────────
echo "── Done ─────────────────────────────────────────────────"
echo ""
echo "  Quick start:"
echo "    1. Open a new terminal (or source your shell config)"
echo "    2. Type 'work' to start the tmux agent session"
echo "    3. ctrl+a N to spawn an agent in a repo"
if ! $SKIP_UI; then
  echo "    4. cd pixel-dashboard && npm run dev  (for the dashboard)"
fi
echo ""
echo "  Status bar colors:"
echo "    Green  = agent working"
echo "    Yellow = possibly stuck (>10min no tool use)"
echo "    Red    = waiting for your input"
echo "    Grey   = idle"
echo ""
