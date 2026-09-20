#!/usr/bin/env bash
# Tests link_claude_tree against a sandbox HOME, never the caller's ~/.claude.
set -uo pipefail

INSTALL_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/install.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n     %s\n' "$1" "${2:-}"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want [$3] got [$2]"; fi; }

new_sandbox() {
  SANDBOX="$(mktemp -d)"
  export HOME="$SANDBOX/home"
  REPO_DIR="$SANDBOX/repo"
  mkdir -p "$HOME/.claude" "$REPO_DIR"
}

# set -e comes from install.sh; the harness needs a failed check to keep going.
# shellcheck disable=SC1090
load() { INSTALL_SH_LIB=1 . "$INSTALL_SH"; set +e; }

echo "link_claude_tree"

new_sandbox; load
mkdir -p "$REPO_DIR/agents"
touch "$REPO_DIR/agents/alpha.md" "$REPO_DIR/agents/beta.md"
link_claude_tree agents agents "agents" files >/dev/null
check "files mode links each entry" \
  "$([ -L "$HOME/.claude/agents/alpha.md" ] && [ -L "$HOME/.claude/agents/beta.md" ] && echo yes)" yes
check "parent stays a real dir" "$([ -L "$HOME/.claude/agents" ] && echo link || echo dir)" dir
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/skills/one"
touch "$REPO_DIR/skills/one/SKILL.md" "$REPO_DIR/skills/README.md"
link_claude_tree skills skills "skills" dirs >/dev/null
check "dirs mode links the dir"    "$([ -L "$HOME/.claude/skills/one" ] && echo yes)" yes
check "dirs mode skips loose file" "$([ -e "$HOME/.claude/skills/README.md" ] && echo yes || echo no)" no
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/commands/sub"
touch "$REPO_DIR/commands/top.md" "$REPO_DIR/commands/sub/nested.md"
link_claude_tree commands commands "commands" both >/dev/null
check "both mode links file" "$([ -L "$HOME/.claude/commands/top.md" ] && echo yes)" yes
check "both mode links dir"  "$([ -L "$HOME/.claude/commands/sub" ] && echo yes)" yes
rm -rf "$SANDBOX"

# Without the containing-symlink guard this writes $REPO_DIR/agents/alpha.md -> itself.
new_sandbox; load
mkdir -p "$REPO_DIR/agents"; touch "$REPO_DIR/agents/alpha.md"
ln -sfn "$REPO_DIR/agents" "$HOME/.claude/agents"
out="$(link_claude_tree agents agents "agents" files 2>&1)"
check "overlay-linked dest is recognized" "$(printf '%s' "$out" | grep -c 'overlay-managed')" 1
check "no self-referential link created" \
  "$([ -L "$REPO_DIR/agents/alpha.md" ] && echo broken || echo clean)" clean
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/agents" "$SANDBOX/elsewhere"; touch "$REPO_DIR/agents/alpha.md"
ln -sfn "$SANDBOX/elsewhere" "$HOME/.claude/agents"
out="$(link_claude_tree agents agents "agents" files 2>&1)"
check "foreign symlink dest is skipped" "$(printf '%s' "$out" | grep -c 'skipping')" 1
check "foreign dest not written into" \
  "$([ -e "$SANDBOX/elsewhere/alpha.md" ] && echo written || echo untouched)" untouched
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/agents" "$HOME/.claude/agents"
touch "$REPO_DIR/agents/alpha.md"
printf 'mine\n' > "$HOME/.claude/agents/alpha.md"
link_claude_tree agents agents "agents" files >/dev/null
check "real file moved aside" "$(cat "$HOME/.claude/agents/alpha.md.pre-nexus" 2>/dev/null)" mine
check "link replaces it"      "$([ -L "$HOME/.claude/agents/alpha.md" ] && echo yes)" yes
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/agents"; touch "$REPO_DIR/agents/alpha.md"
link_claude_tree agents agents "agents" files >/dev/null
link_claude_tree agents agents "agents" files >/dev/null
check "second run leaves one link" \
  "$(find "$HOME/.claude/agents" -maxdepth 1 -mindepth 1 | wc -l | tr -d ' ')" 1
check "no .pre-nexus on re-run" \
  "$([ -e "$HOME/.claude/agents/alpha.md.pre-nexus" ] && echo yes || echo no)" no
rm -rf "$SANDBOX"

new_sandbox; load
link_claude_tree nope nope "nope" files >/dev/null
check "absent src creates nothing" "$([ -e "$HOME/.claude/nope" ] && echo yes || echo no)" no
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/agents"
link_claude_tree agents agents "agents" files >/dev/null
check "empty src links nothing" \
  "$(find "$HOME/.claude/agents" -maxdepth 1 -mindepth 1 | wc -l | tr -d ' ')" 0
rm -rf "$SANDBOX"

new_sandbox; load
mkdir -p "$REPO_DIR/agents"; touch "$REPO_DIR/agents/two words.md"
link_claude_tree agents agents "agents" files >/dev/null
check "spaced name links" "$([ -L "$HOME/.claude/agents/two words.md" ] && echo yes)" yes
rm -rf "$SANDBOX"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
