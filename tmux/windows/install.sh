#!/usr/bin/env bash
# Copies config files from this repo into their expected locations.
# Run from the repo root: ./tmux/windows/install.sh
#
# Works in both MSYS2 (where $HOME=/home/<user>) and Git Bash / MINGW64
# (where $HOME=/c/Users/<user>). Installs into whatever $HOME resolves to.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Ensure $USER is set (empty in Git Bash / MINGW64)
: "${USER:=${USERNAME:=$(whoami)}}"

echo "Installing to HOME=$HOME (user: $USER)"

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

# Claude Code settings (template __HOME__ → actual $HOME)
CLAUDE_DIR="$HOME/.claude"
WIN_CLAUDE_DIR="/c/Users/$USER/.claude"
for dir in "$CLAUDE_DIR" "$WIN_CLAUDE_DIR"; do
  [ "$CLAUDE_DIR" = "$WIN_CLAUDE_DIR" ] && [ "$dir" = "$WIN_CLAUDE_DIR" ] && continue
  if [ -d "$dir" ]; then
    if [ -f "$dir/settings.json" ]; then
      echo "  !! $dir/settings.json exists — compare with claude-settings.json manually"
    else
      MSYS_HOME="/c/msys64${HOME}"
      sed "s|__MSYS_HOME__|$MSYS_HOME|g" "$SCRIPT_DIR/claude-settings.json" > "$dir/settings.json"
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
  "tmux:/c/msys64/usr/bin/tmux.exe"
  "fzf:/c/msys64/mingw64/bin/fzf.exe"
  "aws:/c/Program Files/Amazon/AWSCLIV2/aws.exe"
  "docker:/c/Program Files/Docker/Docker/resources/bin/docker.exe"
  "docker-compose:/c/Program Files/Docker/Docker/resources/bin/docker-compose.exe"
  "docker-credential-wincred:/c/Program Files/Docker/Docker/resources/bin/docker-credential-wincred.exe"
  "git:/c/Program Files/Git/cmd/git.exe"
  "kubectl:/c/ProgramData/chocolatey/bin/kubectl.exe"
  "uv:/c/Users/$USER/scoop/shims/uv.exe"
  "uvx:/c/Users/$USER/scoop/shims/uvx.exe"
  "task:/c/Users/$USER/scoop/shims/task.exe"
  "node:/c/Program Files/nodejs/node.exe"
  "npm:/c/Program Files/nodejs/npm.cmd"
  "claude:/c/Users/$USER/.local/bin/claude.exe"
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

# Install Python 3.14 via uv and wire ~/.local/bin/python + python3
if command -v uv &>/dev/null; then
  echo "  Installing Python 3.14 via uv..."
  uv python install 3.14
  UV_PYTHON=$(uv python find 3.14 2>/dev/null) || true
  if [ -n "$UV_PYTHON" ]; then
    for name in python python3; do
      printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$UV_PYTHON" > "$HOME/.local/bin/$name"
      chmod +x "$HOME/.local/bin/$name"
    done
    echo "  -> ~/.local/bin/{python,python3} -> Python 3.14 (uv-managed)"
  fi
else
  echo "  WARNING: uv not found — Python 3.14 not installed"
  echo "    Install via scoop: scoop install uv"
  echo "    Then run: uv python install 3.14"
fi

echo ""
echo "Done. Open a new terminal or run: source ~/.bashrc"
echo "Then type 'work' to start tmux."
