#!/usr/bin/env bash
# Copies config files from this repo into their expected locations.
# Run from the repo root inside MSYS2: ./install.sh
#
# IMPORTANT: On MSYS2, $HOME is /home/<user> (C:\msys64\home\<user>),
# NOT /c/Users/<user>. This script installs into MSYS2's $HOME.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing to HOME=$HOME"

# tmux config
cp "$SCRIPT_DIR/tmux.conf" "$HOME/.tmux.conf"
echo "  -> ~/.tmux.conf"

# tmux scripts (.sh and .py)
mkdir -p "$HOME/.tmux"
for script in "$SCRIPT_DIR"/tmux-scripts/*.sh; do
  cp "$script" "$HOME/.tmux/$(basename "$script")"
  chmod +x "$HOME/.tmux/$(basename "$script")"
done
# Copy shared Python scripts from mac/tmux-scripts (platform-agnostic)
MAC_SCRIPTS="$(dirname "$SCRIPT_DIR")/mac/tmux-scripts"
for script in "$SCRIPT_DIR"/tmux-scripts/*.py "$MAC_SCRIPTS"/memory-status.py "$MAC_SCRIPTS"/memory-recall.py; do
  [ -f "$script" ] || continue
  cp "$script" "$HOME/.tmux/$(basename "$script")"
  chmod +x "$HOME/.tmux/$(basename "$script")"
done
echo "  -> ~/.tmux/*.sh + *.py"

# Claude Code settings
CLAUDE_DIR="$HOME/.claude"
# Also try Windows-side claude dir
WIN_CLAUDE_DIR="/c/Users/$USER/.claude"
for dir in "$CLAUDE_DIR" "$WIN_CLAUDE_DIR"; do
  if [ -d "$dir" ]; then
    if [ -f "$dir/settings.json" ]; then
      echo "  !! $dir/settings.json exists — compare with claude-settings.json manually"
    else
      cp "$SCRIPT_DIR/claude-settings.json" "$dir/settings.json"
      echo "  -> $dir/settings.json"
    fi
  fi
done

# Shell functions
cp "$SCRIPT_DIR/bashrc" "$HOME/.bashrc"
echo "  -> ~/.bashrc"

# Ensure .bash_profile sources .bashrc (MSYS2 uses login shells)
if ! grep -qF '.bashrc' "$HOME/.bash_profile" 2>/dev/null; then
  echo 'if [ -f "$HOME/.bashrc" ]; then source "$HOME/.bashrc"; fi' >> "$HOME/.bash_profile"
  echo "  -> added .bashrc sourcing to ~/.bash_profile"
fi

# Create wrapper scripts for Windows tools in ~/.local/bin
# NOTE: MSYS2 symlinks don't work with Windows .exe files — use wrappers instead.
# NOTE: Do NOT add "Program Files" paths to PATH directly — breaks fzf in tmux.
mkdir -p "$HOME/.local/bin"
WRAPPERS=(
  "aws:/c/Program Files/Amazon/AWSCLIV2/aws.exe"
  "docker:/c/Program Files/Docker/Docker/resources/bin/docker.exe"
  "docker-compose:/c/Program Files/Docker/Docker/resources/bin/docker-compose.exe"
  "docker-credential-wincred:/c/Program Files/Docker/Docker/resources/bin/docker-credential-wincred.exe"
  "git:/c/Program Files/Git/cmd/git.exe"
  "kubectl:/c/ProgramData/chocolatey/bin/kubectl.exe"
  "uv:/c/Users/$USER/.local/bin/uv.exe"
  "uvx:/c/Users/$USER/.local/bin/uvx.exe"
  "task:/c/Users/$USER/scoop/shims/task.exe"
)
for entry in "${WRAPPERS[@]}"; do
  name="${entry%%:*}"
  exe="${entry#*:}"
  if [ -f "$exe" ]; then
    printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$exe" > "$HOME/.local/bin/$name"
    chmod +x "$HOME/.local/bin/$name"
    echo "  -> ~/.local/bin/$name (wrapper for $exe)"
  else
    echo "  !! $exe not found, skipping $name"
  fi
done

echo ""
echo "Done. Open a new MSYS2 terminal or run: source ~/.bashrc"
echo "Then type 'work' to start tmux."
