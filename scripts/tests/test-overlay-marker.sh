#!/usr/bin/env bash
# Sandbox test for the overlay marker: mktemp only, never a live checkout.
set -uo pipefail

APPLY="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/overlay-apply.sh}"
[ -f "$APPLY" ] || { echo "overlay-apply.sh not found at $APPLY" >&2; exit 1; }

SB="$(mktemp -d)"
CORE="$SB/core"; PLUGS="$SB/plugs"
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad(){ FAIL=$((FAIL+1)); printf '  FAIL  %s\n     %s\n' "$1" "${2:-}"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want [$3] got [$2]"; fi; }

mkdir -p "$CORE" && git -C "$CORE" init -q
# The real core ignores these; without them the sandbox reports them as dirty.
printf '/overlay/\n/.overlay-applied\n/.overlay-applied.*\n' > "$CORE/.gitignore"
git -C "$CORE" add .gitignore && git -C "$CORE" commit -q -m init

mkdir -p "$PLUGS/files/agents" "$PLUGS/files/skills/foo/ref" "$PLUGS/files/deep/a/b"
printf 'name = "test"\n' > "$PLUGS/overlay.toml"
for f in agents/one.md skills/foo/SKILL.md skills/foo/ref/r.md deep/a/b/c.txt rootfile.md; do
  printf 'x\n' > "$PLUGS/files/$f"
done
git -C "$PLUGS" init -q && git -C "$PLUGS" add -A && git -C "$PLUGS" commit -q -m init

echo "overlay directory marker"

AGENTS_NEXUS_DIR="$CORE" bash "$APPLY" "$PLUGS" --dry-run >/dev/null 2>&1
check "dry-run leaves no marker" "$(find "$CORE" -name '.overlay-managed.*' | wc -l | tr -d ' ')" 0

AGENTS_NEXUS_DIR="$CORE" bash "$APPLY" "$PLUGS" >/dev/null 2>&1
for d in agents skills/foo skills/foo/ref deep/a/b .; do
  check "marker in $d" "$([ -f "$CORE/$d/.overlay-managed.test" ] && echo yes || echo no)" yes
done
check "no marker where no file landed" \
  "$([ -f "$CORE/skills/.overlay-managed.test" ] && echo yes || echo no)" no
check "marker names the overlay" "$(grep -c "'test' overlay" "$CORE/agents/.overlay-managed.test")" 1
check "marker names the source" "$(grep -c "$PLUGS" "$CORE/agents/.overlay-managed.test")" 1
# An unquoted heredoc is required to expand $NAME/$SRC, so a stray backtick would run.
check "no command substitution leaked" \
  "$(grep -c 'overlay-apply.sh' "$CORE/agents/.overlay-managed.test")" 2
check "markers recorded in exclude" \
  "$(grep -c '\.overlay-managed\.test' "$CORE/.git/info/exclude")" 5
check "core stays clean" "$(git -C "$CORE" status --porcelain | grep -c '')" 0

AGENTS_NEXUS_DIR="$CORE" bash "$APPLY" --remove test >/dev/null 2>&1
check "all markers removed" "$(find "$CORE" -name '.overlay-managed.*' | wc -l | tr -d ' ')" 0
check "placed files removed" "$([ -e "$CORE/agents/one.md" ] && echo yes || echo no)" no
check "core clean after remove" "$(git -C "$CORE" status --porcelain | grep -c '')" 0

rm -rf "$SB"
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
