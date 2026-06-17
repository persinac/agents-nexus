#!/usr/bin/env bash
# Unified installer for agents-nexus.
# Detects OS, installs system deps, links configs, generates a named
# environment profile, and (optionally) brings up the Docker stack.
#
# Usage:
#   ./install.sh                       # full interactive flow (recommended)
#   ./install.sh --profile <name>      # use/create a specific profile
#   ./install.sh --switch <name>       # repoint .env at an existing profile
#   ./install.sh --finish-langfuse     # paste Langfuse keys after first run
#   ./install.sh --finish-slack        # paste Slack bridge tokens after first run
#   ./install.sh --non-interactive     # deps + skills + dashboard only (no prompts)
#   ./install.sh --no-ui               # skip dashboard npm setup
#
# Supported platforms: macOS, Linux. Windows path is left in place but no
# longer actively maintained against the interactive flow.

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

# ── Flags ──────────────────────────────────────────────────────
SKIP_UI=false
INTERACTIVE=true
PROFILE_ARG=""
MODE="install"   # install | switch | finish-langfuse | finish-slack

while [ $# -gt 0 ]; do
  case "$1" in
    --no-ui)            SKIP_UI=true ;;
    --non-interactive)  INTERACTIVE=false ;;
    --profile)          shift; PROFILE_ARG="${1:-}" ;;
    --switch)           shift; PROFILE_ARG="${1:-}"; MODE="switch" ;;
    --finish-langfuse)  MODE="finish-langfuse" ;;
    --finish-slack)     MODE="finish-slack" ;;
    -h|--help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag: $1"
      exit 1
      ;;
  esac
  shift
done

echo ""
echo "  Agent Orchestration Installer"
echo "  Platform: $OS"
echo "  Repo:     $REPO_DIR"
echo ""

if [ ! -d "$PLATFORM_DIR" ]; then
  echo "ERROR: No platform directory found at $PLATFORM_DIR"
  echo "Supported platforms: mac, linux"
  exit 1
fi

# ────────────────────────────────────────────────────────────────
# Helpers (shared by install / switch / finish-langfuse modes)
# ────────────────────────────────────────────────────────────────

check_cmd() { command -v "$1" >/dev/null 2>&1; }

# Expand a leading ~ to $HOME without invoking `eval` on the whole value.
expand_path() {
  case "$1" in
    "~")     echo "$HOME" ;;
    "~/"*)   echo "$HOME/${1#~/}" ;;
    *)       echo "$1" ;;
  esac
}

prompt_with_default() {
  # prompt_with_default <var-out-name> <prompt-text> <default>
  local __outvar="$1" __prompt="$2" __default="$3" __reply=""
  if [ -n "$__default" ]; then
    printf "  %s [%s]: " "$__prompt" "$__default"
  else
    printf "  %s: " "$__prompt"
  fi
  IFS= read -r __reply </dev/tty || __reply=""
  [ -z "$__reply" ] && __reply="$__default"
  eval "$__outvar=\$__reply"
}

prompt_secret() {
  # prompt_secret <var-out-name> <prompt-text>
  local __outvar="$1" __prompt="$2" __reply=""
  printf "  %s (input hidden): " "$__prompt"
  stty -echo 2>/dev/null || true
  IFS= read -r __reply </dev/tty || __reply=""
  stty echo 2>/dev/null || true
  printf "\n"
  eval "$__outvar=\$__reply"
}

prompt_yes_no() {
  # prompt_yes_no <prompt-text> <default y|n>  -> returns 0 for yes, 1 for no
  local __prompt="$1" __default="$2" __reply=""
  local __hint="[Y/n]"
  [ "$__default" = "n" ] && __hint="[y/N]"
  printf "  %s %s: " "$__prompt" "$__hint"
  IFS= read -r __reply </dev/tty || __reply=""
  [ -z "$__reply" ] && __reply="$__default"
  case "$__reply" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

gen_secret_base64() { openssl rand -base64 32 | tr -d '\n' | tr -d '='; }
gen_secret_hex()    { openssl rand -hex "${1:-32}"; }

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
    if [ "$major" -eq 3 ] && [ "$minor" -ge 14 ]; then
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

  if ! check_cmd claude; then
    echo "  Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code
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

install_deps_windows() {
  # Windows path retained but unmaintained — runs the historical flow.
  local deps=(tmux fzf)
  local to_install=()
  for dep in "${deps[@]}"; do
    if ! check_cmd "$dep"; then to_install+=("$dep"); else echo "  [ok] $dep"; fi
  done
  if [ ${#to_install[@]} -gt 0 ]; then
    pacman -S --noconfirm "${to_install[@]/#/mingw-w64-x86_64-}" tmux 2>/dev/null \
      || pacman -S --noconfirm tmux mingw-w64-x86_64-fzf
  fi
  check_cmd node   || echo "  WARNING: Node.js not in PATH"
  check_cmd claude || echo "  WARNING: claude not in PATH (npm install -g @anthropic-ai/claude-code)"
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
      ln -sfn "$src" "$target"
      echo "  [ok] skill: $name"
    elif [ -d "$target" ]; then
      rm -rf "$target"
      ln -sfn "$src" "$target"
      echo "  -> ~/.claude/skills/$name (adopted from real dir)"
    else
      ln -sfn "$src" "$target"
      echo "  -> ~/.claude/skills/$name"
    fi
  done
}

validate_setup() {
  local all_ok=true

  if check_cmd uv;      then echo "  [ok] uv $(uv --version 2>&1)";     else echo "  !! uv not found"; all_ok=false; fi
  if check_cmd python3; then echo "  [ok] $(python3 --version)";        else echo "  !! python3 not in PATH"; all_ok=false; fi

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

  if [ -L "$REPO_DIR/.env" ]; then
    local target
    target=$(readlink "$REPO_DIR/.env")
    echo "  [ok] .env -> $target"
  elif [ -f "$REPO_DIR/.env" ]; then
    echo "  ?? .env exists but is not a symlink (installer manages a symlink)"
  fi

  echo ""
  $all_ok && echo "  All checks passed." || echo "  Some checks failed — see above."
}

# ────────────────────────────────────────────────────────────────
# Profile + env helpers
# ────────────────────────────────────────────────────────────────

profile_path() { echo "$REPO_DIR/.env.$1"; }

active_profile() {
  if [ -f "$REPO_DIR/.nexus-profile" ]; then
    head -n1 "$REPO_DIR/.nexus-profile" | tr -d '[:space:]'
  elif [ -L "$REPO_DIR/.env" ]; then
    basename "$(readlink "$REPO_DIR/.env")" | sed 's/^\.env\.//'
  fi
}

link_profile() {
  local name="$1"
  local target=".env.$name"
  ( cd "$REPO_DIR" && ln -sfn "$target" .env )
  printf '%s\n' "$name" > "$REPO_DIR/.nexus-profile"
  echo "  -> .env symlinks to $target"
  echo "  -> .nexus-profile = $name"
}

# Migration: profiles written before per-service selection have no
# COMPOSE_PROFILES, so a bare `docker compose up` would now start nothing.
# Backfill the prior always-on set so existing boxes keep running the same
# stack. Idempotent — a no-op once COMPOSE_PROFILES is present.
backfill_compose_profiles() {
  local env_path="$1"
  [ -f "$env_path" ] || return 0
  grep -q '^COMPOSE_PROFILES=' "$env_path" && return 0

  local flavor="personal"
  grep -q '^NEXUS_COMPOSE_FILE=docker-compose.work.yml' "$env_path" && flavor="work"

  local profiles="proxy,ollama,spark,mnemon,dashboard"
  [ "$flavor" = "work" ] && profiles="proxy,ollama,postgres,spark,mnemon,dashboard"
  # langfuse only if this profile actually configured it (avoid surprise-starting 6 containers).
  grep -q '^LANGFUSE_DB_PASSWORD=.' "$env_path" && profiles="$profiles,langfuse"

  local tmp="$env_path.tmp.$$"
  if grep -q '^NEXUS_COMPOSE_FILE=' "$env_path"; then
    awk -v p="$profiles" '
      { print }
      /^NEXUS_COMPOSE_FILE=/ && !ins {
        print ""
        print "# ── Service selection (backfilled by install.sh) ────"
        print "COMPOSE_PROFILES=" p
        print "NEXUS_SERVICES=" p
        ins=1
      }
    ' "$env_path" > "$tmp"
  else
    { echo "COMPOSE_PROFILES=$profiles"; echo "NEXUS_SERVICES=$profiles"; cat "$env_path"; } > "$tmp"
  fi
  mv "$tmp" "$env_path"
  chmod 600 "$env_path"
  echo "  -> backfilled COMPOSE_PROFILES=$profiles into $(basename "$env_path")"
}

# ────────────────────────────────────────────────────────────────
# Interactive setup (the new bit)
# ────────────────────────────────────────────────────────────────

# Multi-select TUI using a numbered list + toggle loop. Works on bash 3.2.
# Inputs:  globals SELECT_LABELS[] (display names)
#          SELECT_DEFAULTS[] (optional, aligned to SELECT_LABELS; "1" = pre-checked)
#          SELECT_TITLE (optional header line; defaults to the peripherals prompt)
# Outputs: SELECT_STATE[] aligned to SELECT_LABELS, "1" = selected, "0" = not
multi_select() {
  local n=${#SELECT_LABELS[@]} i reply
  local title="${SELECT_TITLE:-Toggle peripherals}"
  SELECT_STATE=()
  for ((i=0; i<n; i++)); do
    SELECT_STATE+=("${SELECT_DEFAULTS[$i]:-0}")
  done

  while :; do
    echo ""
    echo "  $title (enter # to flip, ENTER when done, 'a' selects all):"
    for ((i=0; i<n; i++)); do
      local mark="[ ]"
      [ "${SELECT_STATE[$i]}" = "1" ] && mark="[x]"
      printf "    %s %d) %s\n" "$mark" "$((i+1))" "${SELECT_LABELS[$i]}"
    done
    printf "  > "
    IFS= read -r reply </dev/tty || reply=""
    [ -z "$reply" ] && break
    if [ "$reply" = "a" ] || [ "$reply" = "A" ]; then
      for ((i=0; i<n; i++)); do SELECT_STATE[$i]="1"; done
      continue
    fi
    case "$reply" in
      ''|*[!0-9]*) echo "  (enter a number 1-$n, 'a' for all, or ENTER to finish)"; continue ;;
    esac
    if [ "$reply" -ge 1 ] && [ "$reply" -le "$n" ]; then
      local idx=$((reply-1))
      if [ "${SELECT_STATE[$idx]}" = "1" ]; then SELECT_STATE[$idx]="0"; else SELECT_STATE[$idx]="1"; fi
    else
      echo "  (out of range)"
    fi
  done
}

interactive_setup() {
  echo "── Step 3: Profile + environment ────────────────────────"

  # ── Profile name ─────────────────────────────────────────────
  local default_profile profile
  default_profile="${PROFILE_ARG:-$(whoami)-personal}"
  if [ -n "$PROFILE_ARG" ]; then
    profile="$PROFILE_ARG"
    echo "  Profile: $profile"
  else
    prompt_with_default profile "Profile name" "$default_profile"
  fi
  # Sanitize: lowercase kebab-case-ish
  case "$profile" in
    *[!a-zA-Z0-9_-]*) echo "  ERROR: profile name must be alphanumeric/-/_"; exit 1 ;;
  esac

  local env_path
  env_path=$(profile_path "$profile")
  if [ -f "$env_path" ]; then
    echo "  Profile already exists at $env_path"
    if prompt_yes_no "Overwrite existing profile?" "n"; then
      :
    else
      echo "  Keeping existing $env_path. Re-pointing .env -> $env_path."
      backfill_compose_profiles "$env_path"
      link_profile "$profile"
      return 0
    fi
  fi

  # ── Compose file (personal vs work) ─────────────────────────
  local compose_file flavor
  if [ "$profile" = "work" ]; then
    flavor="work"
    compose_file="docker-compose.work.yml"
  else
    echo ""
    echo "  Which stack flavor?"
    echo "    1) personal — uses docker-compose.yml; you bring an external Postgres (or point at the bundled one)"
    echo "    2) work     — uses docker-compose.work.yml; bundles a local Postgres container"
    local choice
    prompt_with_default choice "Choice" "1"
    if [ "$choice" = "2" ] || [ "$choice" = "work" ]; then
      flavor="work"; compose_file="docker-compose.work.yml"
    else
      flavor="personal"; compose_file="docker-compose.yml"
    fi
  fi
  echo "  Compose file: $compose_file"

  # ── Service selection (which Docker containers this box runs) ─
  # Every service carries a compose profile; the chosen set is written to
  # COMPOSE_PROFILES in .env so every `docker compose up` honors it.
  echo ""
  echo "  Which services should this box run?"
  echo "  (all default-on except Langfuse — pick a subset for e.g. an observability-only box)"
  SELECT_TITLE="Toggle services"
  if [ "$flavor" = "work" ]; then
    SELECT_KEYS=(proxy ollama postgres spark mnemon dashboard langfuse)
    SELECT_LABELS=(
      "proxy      — Anthropic API gateway + Langfuse tap"
      "ollama     — local embedding model host"
      "postgres   — bundled local Postgres (agent memory store)"
      "spark      — semantic index over your repos"
      "mnemon     — agent memory: event flush + MCP server"
      "dashboard  — command-center web UI"
      "langfuse   — self-hosted trace/observability stack (6 containers)"
    )
    SELECT_DEFAULTS=(1 1 1 1 1 1 0)
  else
    SELECT_KEYS=(proxy ollama spark mnemon dashboard langfuse)
    SELECT_LABELS=(
      "proxy      — Anthropic API gateway + Langfuse tap"
      "ollama     — local embedding model host"
      "spark      — semantic index over your repos"
      "mnemon     — agent memory: event flush + MCP server (needs external Postgres)"
      "dashboard  — command-center web UI"
      "langfuse   — self-hosted trace/observability stack (6 containers)"
    )
    SELECT_DEFAULTS=(1 1 1 1 1 0)
  fi
  multi_select

  # Map the toggle state back to per-service booleans (index-shift safe).
  local sel_proxy=0 sel_ollama=0 sel_postgres=0 sel_spark=0 sel_mnemon=0 sel_dashboard=0 sel_langfuse=0
  local _i
  for ((_i=0; _i<${#SELECT_KEYS[@]}; _i++)); do
    case "${SELECT_KEYS[$_i]}" in
      proxy)     sel_proxy="${SELECT_STATE[$_i]}" ;;
      ollama)    sel_ollama="${SELECT_STATE[$_i]}" ;;
      postgres)  sel_postgres="${SELECT_STATE[$_i]}" ;;
      spark)     sel_spark="${SELECT_STATE[$_i]}" ;;
      mnemon)    sel_mnemon="${SELECT_STATE[$_i]}" ;;
      dashboard) sel_dashboard="${SELECT_STATE[$_i]}" ;;
      langfuse)  sel_langfuse="${SELECT_STATE[$_i]}" ;;
    esac
  done

  # Dependency closure (computed here so we never lean on cross-profile depends_on):
  #   spark  ⇒ ollama;   mnemon ⇒ ollama (+ postgres on the work flavor only).
  [ "$sel_spark" = "1" ] && sel_ollama=1
  if [ "$sel_mnemon" = "1" ]; then
    sel_ollama=1
    [ "$flavor" = "work" ] && sel_postgres=1
  fi

  # Build COMPOSE_PROFILES in a stable order.
  local compose_profiles="" _pair _key _on
  for _pair in "proxy:$sel_proxy" "ollama:$sel_ollama" "postgres:$sel_postgres" \
               "spark:$sel_spark" "mnemon:$sel_mnemon" "dashboard:$sel_dashboard" \
               "langfuse:$sel_langfuse"; do
    _key="${_pair%%:*}"; _on="${_pair##*:}"
    [ "$_on" = "1" ] && compose_profiles="${compose_profiles:+$compose_profiles,}$_key"
  done
  local nexus_services="$compose_profiles"
  echo ""
  echo "  Services: ${compose_profiles:-<none>}"

  # ── Per-service configuration (only what the selected services need) ──
  local repos_path="$HOME/repos" host_tmux_dir="$HOME/.tmux"
  local anthropic_api_base=""
  local postgres_db="agents" postgres_user="agents" postgres_password="" postgres_port="5432"
  local database_url=""
  local langfuse_db_password="" langfuse_redis_auth="" langfuse_clickhouse_password=""
  local langfuse_nextauth_secret="" langfuse_salt="" langfuse_encryption_key=""
  local langfuse_public_key="" langfuse_secret_key=""
  local sel_gitlab=0 sel_cloudflare=0 sel_github=0
  local gitlab_url="https://gitlab.com" gitlab_token="" spark_webhook_secret=""
  local cloudflare_tunnel_token=""
  local github_url="https://api.github.com" github_token=""
  local sel_slack=0 slack_bot_token="" slack_app_token="" slack_channel=""

  # proxy ⇒ upstream (proxy hard-requires ANTHROPIC_API_BASE)
  if [ "$sel_proxy" = "1" ]; then
    echo ""
    echo "  Proxy upstream — where the gateway forwards Anthropic API calls:"
    if [ "$OS" = "linux" ]; then
      echo "  NOTE: for a host-local gateway on Linux, 'host.docker.internal' does not"
      echo "        auto-resolve — use a routable IP or add extra_hosts to the proxy service."
    fi
    prompt_with_default anthropic_api_base "ANTHROPIC_API_BASE" "https://api.anthropic.com"
  fi

  # postgres / DATABASE_URL
  if [ "$flavor" = "work" ] && [ "$sel_postgres" = "1" ]; then
    echo ""
    echo "  Local Postgres (bundled with work compose):"
    local pw_choice
    prompt_with_default pw_choice "Generate a random Postgres password? [Y/n]" "Y"
    case "$pw_choice" in
      n|N|no|NO) prompt_secret postgres_password "POSTGRES_PASSWORD" ;;
      *)         postgres_password=$(gen_secret_hex 16); echo "  -> generated POSTGRES_PASSWORD" ;;
    esac
    database_url="postgresql://${postgres_user}:${postgres_password}@localhost:${postgres_port}/${postgres_db}?sslmode=disable"
  elif [ "$flavor" != "work" ] && [ "$sel_mnemon" = "1" ]; then
    echo ""
    echo "  DATABASE_URL (mnemon memory storage — bring your own Postgres):"
    echo "    Default points at a local Postgres on :5432."
    prompt_with_default database_url "DATABASE_URL" "postgresql://agents:changeme@localhost:5432/agents?sslmode=disable"
  fi

  # mnemon ⇒ host tmux event-log dir
  if [ "$sel_mnemon" = "1" ]; then
    echo ""
    prompt_with_default host_tmux_dir "HOST_TMUX_DIR (where mnemon reads tmux event logs)" "$HOME/.tmux"
  fi
  host_tmux_dir=$(expand_path "$host_tmux_dir")

  # spark ⇒ repo index path + (optional) indexing integrations
  if [ "$sel_spark" = "1" ]; then
    echo ""
    echo "  Spark indexing:"
    prompt_with_default repos_path "REPOS_PATH (directory spark will index)" "$HOME/repos"

    if prompt_yes_no "Enable GitLab webhook re-indexing?" "n"; then
      sel_gitlab=1
      prompt_with_default gitlab_url "GITLAB_URL" "https://gitlab.com"
      prompt_secret       gitlab_token "GITLAB_TOKEN (personal access token, api scope)"
      spark_webhook_secret=$(gen_secret_hex 32)
      echo "  -> SPARK_WEBHOOK_SECRET generated"
    fi
    if [ "$flavor" = "work" ] && prompt_yes_no "Enable GitHub integration?" "n"; then
      sel_github=1
      prompt_with_default github_url   "GITHUB_URL" "https://api.github.com"
      prompt_secret       github_token "GITHUB_TOKEN (PAT, repo + workflow scopes)"
    fi
    if prompt_yes_no "Expose spark publicly via a Cloudflare tunnel?" "n"; then
      sel_cloudflare=1
      prompt_secret cloudflare_tunnel_token "CLOUDFLARE_TUNNEL_TOKEN"
    fi
  fi
  repos_path=$(expand_path "$repos_path")

  # langfuse ⇒ stack secrets
  if [ "$sel_langfuse" = "1" ]; then
    echo ""
    echo "  Generating Langfuse stack secrets..."
    langfuse_db_password=$(gen_secret_hex 16)
    langfuse_redis_auth=$(gen_secret_hex 16)
    langfuse_clickhouse_password=$(gen_secret_hex 16)
    langfuse_nextauth_secret=$(gen_secret_base64)
    langfuse_salt=$(gen_secret_base64)
    langfuse_encryption_key=$(gen_secret_hex 32)
    echo "  -> 6 secrets generated. Public/secret API keys are set later via --finish-langfuse."
  fi

  # ── Integrations (host services, outside the Docker stack) ───
  echo ""
  if prompt_yes_no "Enable the Slack bridge (two-way #nexus <-> agent control)?" "n"; then
    sel_slack=1
    echo "  Slack bridge — needs a Slack app with Socket Mode enabled."
    echo "  Full setup (app manifest, scopes, where to find each value): docs/slack-bridge.md"
    if prompt_yes_no "Do you have the Slack tokens now?" "n"; then
      prompt_secret       slack_bot_token "SLACK_BOT_TOKEN (xoxb-...)"
      prompt_secret       slack_app_token "SLACK_APP_TOKEN (xapp-...)"
      prompt_with_default slack_channel   "SLACK_NEXUS_CHANNEL (channel id, C...)" ""
    else
      echo "  -> writing empty SLACK_* keys; finish later with: ./install.sh --finish-slack"
    fi
  fi

  # ── Write .env.<profile> ─────────────────────────────────────
  echo ""
  echo "  Writing $env_path ..."
  write_profile_env \
    "$profile" "$flavor" "$compose_file" "$compose_profiles" "$nexus_services" \
    "$repos_path" "$host_tmux_dir" \
    "$sel_postgres" "$postgres_db" "$postgres_user" "$postgres_password" "$postgres_port" "$database_url" \
    "$sel_langfuse" "$langfuse_db_password" "$langfuse_redis_auth" "$langfuse_clickhouse_password" \
    "$langfuse_nextauth_secret" "$langfuse_salt" "$langfuse_encryption_key" \
    "$langfuse_public_key" "$langfuse_secret_key" \
    "$sel_gitlab" "$gitlab_url" "$gitlab_token" "$spark_webhook_secret" \
    "$sel_cloudflare" "$cloudflare_tunnel_token" \
    "$sel_github" "$github_url" "$github_token" \
    "$sel_proxy" "$anthropic_api_base" \
    "$sel_slack" "$slack_bot_token" "$slack_app_token" "$slack_channel"

  chmod 600 "$env_path"
  link_profile "$profile"

  # ── Summary ──────────────────────────────────────────────────
  echo ""
  echo "  Summary:"
  echo "    profile         $profile"
  echo "    compose file    $compose_file"
  echo "    services        ${compose_profiles:-<none>}"
  [ "$sel_proxy"  = "1" ] && echo "    proxy upstream  $anthropic_api_base"
  [ "$sel_spark"  = "1" ] && echo "    repos path      $repos_path"
  [ "$sel_mnemon" = "1" ] && echo "    host tmux dir   $host_tmux_dir"
  echo "    integrations:"
  [ "$sel_langfuse"   = "1" ] && echo "      - Langfuse (6 secrets generated; finish with: ./install.sh --finish-langfuse)"
  [ "$sel_gitlab"     = "1" ] && echo "      - GitLab webhook re-indexing (spark)"
  [ "$sel_cloudflare" = "1" ] && echo "      - Cloudflare tunnel (spark)"
  [ "$sel_github"     = "1" ] && echo "      - GitHub integration (spark)"
  [ "$sel_slack"      = "1" ] && echo "      - Slack bridge (finish with: ./install.sh --finish-slack)"

  # ── Optionally start the stack ───────────────────────────────
  echo ""
  if prompt_yes_no "Start the Docker stack now?" "y"; then
    start_stack "$compose_file" "$compose_profiles" "$sel_ollama" "$sel_postgres" "$flavor"
  else
    local init_flags="--profile init --profile ollama"
    [ "$flavor" = "work" ] && init_flags="$init_flags --profile postgres"
    echo "  Skipping. COMPOSE_PROFILES is saved in .env, so start later with:"
    echo "    docker compose -f $compose_file up -d"
    [ "$flavor" = "work" ] && [ "$sel_postgres" = "1" ] && \
      echo "    docker compose -f $compose_file $init_flags run --rm db-migrate"
    [ "$sel_ollama" = "1" ] && \
      echo "    docker compose -f $compose_file $init_flags run --rm ollama-init"
  fi
}

write_profile_env() {
  local profile="$1" flavor="$2" compose_file="$3" compose_profiles="$4" nexus_services="$5"
  local repos_path="$6" host_tmux_dir="$7"
  local sel_postgres="$8"
  local postgres_db="$9" postgres_user="${10}" postgres_password="${11}" postgres_port="${12}" database_url="${13}"
  local sel_langfuse="${14}" lf_db_pw="${15}" lf_redis="${16}" lf_ch_pw="${17}"
  local lf_nextauth="${18}" lf_salt="${19}" lf_enc="${20}"
  local lf_pub_key="${21}" lf_sec_key="${22}"
  local sel_gitlab="${23}" gitlab_url="${24}" gitlab_token="${25}" spark_webhook_secret="${26}"
  local sel_cloudflare="${27}" cf_token="${28}"
  local sel_github="${29}" github_url="${30}" github_token="${31}"
  local sel_proxy="${32}" anthropic_api_base="${33}"
  local sel_slack="${34}" slack_bot_token="${35}" slack_app_token="${36}" slack_channel="${37}"

  local env_path
  env_path=$(profile_path "$profile")

  {
    echo "# agents-nexus profile: $profile"
    echo "# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# Compose file: $compose_file"
    echo ""
    echo "NEXUS_PROFILE=$profile"
    echo "NEXUS_COMPOSE_FILE=$compose_file"
    echo ""
    echo "# ── Service selection ────────────────────────────────"
    echo "# docker compose reads COMPOSE_PROFILES from .env, so every"
    echo "# 'docker compose up' (and 'task up') honors this set."
    echo "# Valid profiles: proxy, ollama, postgres (work), spark, mnemon, dashboard, langfuse"
    echo "COMPOSE_PROFILES=$compose_profiles"
    echo "NEXUS_SERVICES=$nexus_services"
    echo ""
    echo "# ── Core ─────────────────────────────────────────────"
    echo "REPOS_PATH=$repos_path"
    echo "HOST_TMUX_DIR=$host_tmux_dir"

    if { [ "$flavor" = "work" ] && [ "$sel_postgres" = "1" ]; } || [ -n "$database_url" ]; then
      echo ""
      echo "# ── Postgres / memory store ──────────────────────────"
      if [ "$flavor" = "work" ] && [ "$sel_postgres" = "1" ]; then
        echo "POSTGRES_DB=$postgres_db"
        echo "POSTGRES_USER=$postgres_user"
        echo "POSTGRES_PASSWORD=$postgres_password"
        echo "POSTGRES_PORT=$postgres_port"
      fi
      [ -n "$database_url" ] && echo "DATABASE_URL=$database_url"
    fi

    echo ""
    echo "# ── Ports (compose defaults — edit only if you have collisions) ──"
    echo "OLLAMA_PORT=11434"
    echo "OLLAMA_BASE_URL=http://localhost:11434"
    echo "SPARK_PORT=8343"
    echo "MNEMON_MCP_PORT=8330"
    echo "DASHBOARD_PORT=8421"

    if [ "$sel_proxy" = "1" ]; then
      echo ""
      echo "# ── Proxy upstream (required by the proxy service) ───"
      echo "ANTHROPIC_API_BASE=$anthropic_api_base"
    fi

    if [ "$sel_langfuse" = "1" ]; then
      echo ""
      echo "# ── Langfuse stack ───────────────────────────────────"
      echo "LANGFUSE_PORT=3000"
      echo "LANGFUSE_MINIO_PORT=9094"
      echo "LANGFUSE_HOST=http://localhost:3000"
      echo "LANGFUSE_DB_PASSWORD=$lf_db_pw"
      echo "LANGFUSE_REDIS_AUTH=$lf_redis"
      echo "LANGFUSE_CLICKHOUSE_PASSWORD=$lf_ch_pw"
      echo "LANGFUSE_NEXTAUTH_SECRET=$lf_nextauth"
      echo "LANGFUSE_SALT=$lf_salt"
      echo "LANGFUSE_ENCRYPTION_KEY=$lf_enc"
      echo "LANGFUSE_PUBLIC_KEY=$lf_pub_key"
      echo "LANGFUSE_SECRET_KEY=$lf_sec_key"
    fi

    if [ "$sel_gitlab" = "1" ]; then
      echo ""
      echo "# ── GitLab webhook re-indexing (spark) ───────────────"
      echo "GITLAB_URL=$gitlab_url"
      echo "GITLAB_TOKEN=$gitlab_token"
      echo "SPARK_WEBHOOK_SECRET=$spark_webhook_secret"
    fi

    if [ "$sel_cloudflare" = "1" ]; then
      echo ""
      echo "# ── Cloudflare tunnel (spark) ────────────────────────"
      echo "CLOUDFLARE_TUNNEL_TOKEN=$cf_token"
    fi

    if [ "$sel_github" = "1" ]; then
      echo ""
      echo "# ── GitHub integration (spark, work flavor) ──────────"
      echo "GITHUB_URL=$github_url"
      echo "GITHUB_TOKEN=$github_token"
    fi

    if [ "$sel_slack" = "1" ]; then
      echo ""
      echo "# ── Slack bridge (host integration) ──────────────────"
      echo "SLACK_BOT_TOKEN=$slack_bot_token"
      echo "SLACK_APP_TOKEN=$slack_app_token"
      echo "SLACK_NEXUS_CHANNEL=$slack_channel"
      echo "SLACK_BRIDGE_PORT=8788"
    fi
  } > "$env_path"
}

start_stack() {
  local compose_file="$1" compose_profiles="$2" sel_ollama="$3" sel_postgres="$4" flavor="$5"
  if ! check_cmd docker; then
    echo "  ERROR: docker not on PATH — skipping stack startup."
    return 0
  fi
  if [ -z "$compose_profiles" ]; then
    echo "  No services selected — nothing to start."
    return 0
  fi
  # COMPOSE_PROFILES is read from .env; this brings up exactly the chosen set.
  echo "  docker compose -f $compose_file up -d  (profiles: $compose_profiles) ..."
  ( cd "$REPO_DIR" && docker compose -f "$compose_file" up -d )

  # One-shot init jobs live in the 'init' profile (never in COMPOSE_PROFILES, so a
  # bare 'up' never runs them). Two gotchas drive the flags below:
  #   1. A CLI --profile OVERRIDES (does not merge with) the .env COMPOSE_PROFILES.
  #   2. The 'init' profile enables BOTH one-shots, and each depends on a profiled
  #      service (ollama-init→ollama, db-migrate→postgres), so the project only
  #      validates when every needed profile is named. `run <svc>` still starts
  #      just the target + its direct deps.
  local init_flags="--profile init --profile ollama"
  [ "$flavor" = "work" ] && init_flags="$init_flags --profile postgres"

  if [ "$flavor" = "work" ] && [ "$sel_postgres" = "1" ]; then
    echo "  docker compose -f $compose_file $init_flags run --rm db-migrate ..."
    ( cd "$REPO_DIR" && docker compose -f "$compose_file" $init_flags run --rm db-migrate ) || \
      echo "  (db-migrate failed — re-run manually after postgres is healthy)"
  fi
  if [ "$sel_ollama" = "1" ]; then
    echo "  docker compose -f $compose_file $init_flags run --rm ollama-init ..."
    ( cd "$REPO_DIR" && docker compose -f "$compose_file" $init_flags run --rm ollama-init ) || \
      echo "  (ollama-init failed — re-run manually after containers stabilize)"
  fi
}

# Mode: --switch <name>
switch_profile() {
  local name="$1"
  [ -n "$name" ] || { echo "ERROR: --switch requires a profile name"; exit 1; }
  local target
  target=$(profile_path "$name")
  [ -f "$target" ] || { echo "ERROR: $target does not exist"; exit 1; }
  backfill_compose_profiles "$target"
  link_profile "$name"
  echo ""
  echo "  Active profile is now: $name"
}

# Mode: --finish-langfuse
finish_langfuse() {
  local current
  current=$(active_profile)
  [ -n "$current" ] || { echo "ERROR: no active profile. Run ./install.sh first."; exit 1; }
  local env_path
  env_path=$(profile_path "$current")
  [ -f "$env_path" ] || { echo "ERROR: $env_path not found."; exit 1; }
  if ! grep -q '^LANGFUSE_PUBLIC_KEY=' "$env_path"; then
    echo "ERROR: $env_path has no LANGFUSE_* block — was Langfuse selected during install?"
    exit 1
  fi

  echo "  Active profile: $current"
  echo "  Open http://localhost:3000 → create a project → API Keys → Create new key."
  echo ""
  local pub sec
  prompt_with_default pub "LANGFUSE_PUBLIC_KEY (pk-lf-...)" ""
  prompt_secret       sec "LANGFUSE_SECRET_KEY (sk-lf-...)"

  # Portable in-place edit (works on mac BSD sed + GNU sed).
  local tmp="$env_path.tmp.$$"
  awk -v pub="$pub" -v sec="$sec" '
    /^LANGFUSE_PUBLIC_KEY=/  { print "LANGFUSE_PUBLIC_KEY=" pub; next }
    /^LANGFUSE_SECRET_KEY=/  { print "LANGFUSE_SECRET_KEY=" sec; next }
                             { print }
  ' "$env_path" > "$tmp"
  mv "$tmp" "$env_path"
  chmod 600 "$env_path"
  echo "  -> updated $env_path"

  local compose_file="docker-compose.yml"
  [ "$current" = "work" ] && compose_file="docker-compose.work.yml"
  if check_cmd docker; then
    echo "  Recreating proxy so it picks up the new keys..."
    ( cd "$REPO_DIR" && docker compose -f "$compose_file" up -d --force-recreate proxy ) || true
  fi
}

# Mode: --finish-slack — paste Slack bridge tokens into the active profile,
# install deps, and (mac) wire up the launchd supervisor.
finish_slack() {
  local current
  current=$(active_profile)
  [ -n "$current" ] || { echo "ERROR: no active profile. Run ./install.sh first."; exit 1; }
  local env_path
  env_path=$(profile_path "$current")
  [ -f "$env_path" ] || { echo "ERROR: $env_path not found."; exit 1; }
  if ! grep -q '^SLACK_BOT_TOKEN=' "$env_path"; then
    echo "ERROR: $env_path has no SLACK_* block — was the Slack bridge selected during install?"
    echo "       Re-run ./install.sh and tick the Slack bridge peripheral first."
    exit 1
  fi

  echo "  Active profile: $current"
  echo "  Create a Slack app with Socket Mode on (manifest in docs/slack-bridge.md),"
  echo "  invite its bot to a private #nexus channel, then paste the values below."
  echo ""
  local bot app chan
  prompt_secret       bot  "SLACK_BOT_TOKEN (xoxb-...)"
  prompt_secret       app  "SLACK_APP_TOKEN (xapp-...)"
  prompt_with_default chan "SLACK_NEXUS_CHANNEL (channel id, C...)" ""

  local tmp="$env_path.tmp.$$"
  awk -v bot="$bot" -v app="$app" -v chan="$chan" '
    /^SLACK_BOT_TOKEN=/     { print "SLACK_BOT_TOKEN=" bot; next }
    /^SLACK_APP_TOKEN=/     { print "SLACK_APP_TOKEN=" app; next }
    /^SLACK_NEXUS_CHANNEL=/ { print "SLACK_NEXUS_CHANNEL=" chan; next }
                            { print }
  ' "$env_path" > "$tmp"
  mv "$tmp" "$env_path"
  chmod 600 "$env_path"
  echo "  -> updated $env_path"

  if check_cmd npm; then
    echo "  Installing slack-bridge dependencies..."
    ( cd "$REPO_DIR/slack-bridge" && npm install --silent ) || true
  fi

  if [ "$OS" = "mac" ] && check_cmd task; then
    if prompt_yes_no "Install + start the launchd supervisor now?" "y"; then
      ( cd "$REPO_DIR" && task launchd:install:slack-bridge ) || true
    fi
  else
    echo "  Start it with: task slack:bridge   (supervise with: task launchd:install:slack-bridge)"
  fi
}

# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────

# Mode shortcuts: --switch / --finish-langfuse don't install deps
if [ "$MODE" = "switch" ]; then
  switch_profile "$PROFILE_ARG"
  exit 0
fi
if [ "$MODE" = "finish-langfuse" ]; then
  finish_langfuse
  exit 0
fi
if [ "$MODE" = "finish-slack" ]; then
  finish_slack
  exit 0
fi

# ── Step 1: System dependencies ────────────────────────────────
echo "── Step 1: System dependencies ──────────────────────────"
case "$OS" in
  mac)     install_deps_mac ;;
  windows) install_deps_windows ;;
  linux)   install_deps_linux ;;
esac
echo ""

# ── Step 2: Platform configs ───────────────────────────────────
echo "── Step 2: Platform configs ─────────────────────────────"
if [ -f "$PLATFORM_DIR/install.sh" ]; then
  echo "  Running $OS/install.sh..."
  bash "$PLATFORM_DIR/install.sh"
else
  echo "  WARNING: $PLATFORM_DIR/install.sh not found, installing manually..."
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

# ── Step 3: Interactive profile setup ──────────────────────────
if $INTERACTIVE; then
  interactive_setup
else
  echo "── Step 3: Profile + environment (skipped, --non-interactive) ──"
fi
echo ""

# ── Step 4: Global Claude skills ───────────────────────────────
echo "── Step 4: Global Claude skills ─────────────────────────"
setup_skills
echo ""

# ── Step 5: Dashboard UI ───────────────────────────────────────
if $SKIP_UI; then
  echo "── Step 5: Dashboard UI (skipped) ──────────────────────"
else
  echo "── Step 5: Dashboard UI ────────────────────────────────"
  DASHBOARD_DIR="$REPO_DIR/dashboard/ui"
  if [ ! -f "$DASHBOARD_DIR/package.json" ]; then
    echo "  WARNING: $DASHBOARD_DIR/package.json not found, skipping"
  elif ! check_cmd node; then
    echo "  WARNING: Node.js not found, skipping dashboard setup"
  else
    echo "  Installing dashboard dependencies..."
    ( cd "$DASHBOARD_DIR" && npm install --silent )
    echo "  Dashboard ready. Start with: cd dashboard/ui && npm run dev"
  fi
fi
echo ""

# ── Step 6: Validate ───────────────────────────────────────────
echo "── Step 6: Validate ─────────────────────────────────────"
validate_setup
echo ""

# ── Summary ────────────────────────────────────────────────────
echo "── Done ─────────────────────────────────────────────────"
echo ""
active=$(active_profile || true)
if [ -n "${active:-}" ]; then
  echo "  Active profile: $active"
  echo "  Switch profiles with: ./install.sh --switch <name>"
  echo ""
fi
echo "  Quick start:"
echo "    1. Open a new terminal (or source your shell config)"
echo "    2. Type 'work' to start the tmux agent session"
echo "    3. ctrl+a N to spawn an agent in a repo"
if ! $SKIP_UI; then
  echo "    4. cd dashboard/ui && npm run dev  (for the dashboard)"
fi
echo ""
echo "  Status bar colors:"
echo "    Green  = agent working"
echo "    Yellow = possibly stuck (>10min no tool use)"
echo "    Red    = waiting for your input"
echo "    Grey   = idle"
echo ""
