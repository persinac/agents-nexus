#!/usr/bin/env bash
# herdr popup entrypoint: pick TWO panes and swap their positions in the layout.
#
# Wiring: prefix+shift+s (keys.toml) -> action nexus.fleet.swap -> [[panes]] swap
# (zoomed popup, real TTY) -> this script. The swap itself is a first-class herdr
# op: `herdr pane swap --source-pane <id> --target-pane <id>`.
#
# Scope: panes in the tab this picker was invoked from (HERDR_TAB_ID), excluding the
# popup's own pane (HERDR_PANE_ID). If that tab has <2 candidates, falls back to the
# whole fleet. Press Esc in either picker to abort with no changes. The popup closes
# when this script returns.
#
# NEXUS_SWAP_FZF overrides the picker binary (test seam; defaults to fzf).
set -uo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:$PATH"

HERDR="${HERDR_BIN_PATH:-herdr}"
FZF="${NEXUS_SWAP_FZF:-fzf}"

fail() { printf '%s\n' "$*" >&2; sleep 2; exit 1; }

list_json="$("$HERDR" pane list 2>/dev/null)" || fail "could not list panes"

# Candidate rows: "pane_id<TAB>display". Prefer the invoking tab; exclude self.
rows="$(
  NX_TAB="${HERDR_TAB_ID:-}" NX_SELF="${HERDR_PANE_ID:-}" python3 - "$list_json" <<'PY'
import sys, json, os
panes = json.loads(sys.argv[1])["result"]["panes"]
tab  = os.environ.get("NX_TAB") or None
self = os.environ.get("NX_SELF") or None
def disp(p):
    label = p.get("label") or p.get("terminal_title_stripped") or "(shell)"
    return f'{p["pane_id"]}\t{label:<28}  ·  {p.get("workspace_id",""):<4}  ·  {p.get("cwd","")}'
cand = [p for p in panes if p["pane_id"] != self]
same = [p for p in cand if tab and p.get("tab_id") == tab]
for p in (same if len(same) >= 2 else cand):
    print(disp(p))
PY
)"

[ -n "$rows" ] || fail "no panes available to swap"
nrows="$(printf '%s\n' "$rows" | grep -c .)"
[ "$nrows" -ge 2 ] || fail "need at least 2 panes to swap (found $nrows)"

pick() { # prompt in $1; rows on stdin; prints selected pane_id
  $FZF --delimiter=$'\t' --with-nth=2 --height=90% --border=rounded \
       --no-multi --prompt="$1" | cut -f1
}

src="$(printf '%s\n' "$rows" | pick 'swap SOURCE ▸ ')"
[ -n "$src" ] || exit 0   # Esc / cancelled

src_label="$(printf '%s\n' "$rows" | awk -F'\t' -v id="$src" '$1==id{print $2; exit}')"
tgt="$(printf '%s\n' "$rows" | awk -F'\t' -v id="$src" '$1!=id' \
        | pick "swap ${src_label%% *} ⇄ ▸ ")"
[ -n "$tgt" ] || exit 0   # Esc / cancelled

if "$HERDR" pane swap --source-pane "$src" --target-pane "$tgt" >/dev/null 2>&1; then
  printf 'swapped %s ⇄ %s\n' "$src" "$tgt"; sleep 1
else
  fail "swap failed: $src ⇄ $tgt"
fi
