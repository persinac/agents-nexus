#!/usr/bin/env bash
# substrate — the tmux <-> herdr substrate seam for the agents-nexus fleet.
#
# ONE cli, TWO backends, switched by $NEXUS_SUBSTRATE (default: herdr). Every
# fleet consumer (bash scripts, hooks, the slack-bridge, the arbiter, the
# conductor, runner.py) routes its substrate operations through here instead of
# calling tmux directly, so a single flag flips the whole fleet between the
# battle-tested tmux backend and herdr — and back — with no code change.
#
#   NEXUS_SUBSTRATE=herdr  (default) → the verified herdr op-map (docs/herdr-spike.md)
#   NEXUS_SUBSTRATE=tmux             → the legacy tmux commands (DEPRECATED fallback; set to roll back)
#
# Verbs:
#   spawn <name> <cwd> <cmd...> [--print] [--workspace <label>] [--split right|down] [--tab <id>] [--focus]
#     --focus: switch the client to the new agent (interactive picker); default no-focus (fan-out)
#   send  <dest> <text>                      deliver a line (literal + Enter; bare digit = no Enter)
#   send-keys <dest> <enter|escape|literal>  raw key(s) — permission/elicitation responses
#   report-state <pane> <working|idle> | needs-input <pane> <wait_type>
#   rename <pane> <name>                     name the window/pane, lock auto-rename
#   keep <tgt> <0|1>                         reaper-protect toggle (@keep)
#   cohort <tgt> <label> | cohort <tgt> --release
#   tag-orchestrator <pane>                  mark as the command post (@orchestrator)
#   register <pane> <name> [cwd] [ws]        write the $NEXUS_TMUX_DIR/registry/<handle> entry
#   deregister <pane>                        remove the registry entry (idempotent)
#   kill <win>                               close the window/pane
#   workspace-create <label> [cwd] [--focus] · workspace-list · workspace-close <label|id> · workspace-of <pane>
#     (labeled herdr buckets; --workspace on spawn resolves-or-creates. tmux: label recorded, flat)
#   query                                    list agents: index|name|@waiting|path|command|@wait_type
#   pane-opt <pane> <@name>                  read one window option value
#
# NOTE (deployment): $NEXUS_TMUX_DIR/* (default ~/.tmux) are SYMLINKS into this repo working tree, so
# editing a live script changes the running fleet immediately. This file is new
# (nothing links to it yet) and safe to iterate on until install.sh symlinks it.
set -euo pipefail

BACKEND="${NEXUS_SUBSTRATE:-herdr}"
# Fleet install root (registry dir lives here). Self-default: substrate.sh does not
# source env.sh, and runs in stripped-env contexts (herdr hooks), so resolve it here.
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
SESSION="${TMUX_AGENT_SESSION:-${TMUX_SESSION:-agents}}"
ENTER_DELAY="${SLACK_A2A_ENTER_DELAY:-0.4}"
# herdr metadata that has no native home (epochs, orchestration tags) lives in a
# per-pane sidecar the read daemon also consults. Kept tiny + local.
HERDR_STATE="${NEXUS_HERDR_STATE:-$HOME/.config/herdr/nexus-state}"

die() { echo "substrate: $*" >&2; exit 2; }
now() { date +%s; }

# ── helpers ──────────────────────────────────────────────────────────────────
_is_digit() { [[ "$1" =~ ^[0-9]$ ]]; }

# Resolve a BARE slot/name to its recorded backend via the registry `SUBSTRATE=` field
# (written by `register`). Echoes tmux|herdr on the first NAME=/SLOT= match, else nothing.
# Mixed-fleet correctness: a bare target may live on the OTHER backend than the caller's
# global $BACKEND, and — unlike a %N/wN:pN handle — its shape carries no backend hint.
_sub_from_registry() {  # <slot-or-name> → tmux|herdr (empty if unknown)
  local want="$1" f nm sl sub
  [ -d "$NEXUS_TMUX_DIR/registry" ] || return 0
  for f in "$NEXUS_TMUX_DIR/registry"/*; do
    [ -f "$f" ] || continue
    nm=""; sl=""; sub=""
    while IFS='=' read -r k v; do
      case "$k" in NAME) nm="$v" ;; SLOT) sl="$v" ;; SUBSTRATE) sub="$v" ;; esac
    done < "$f"
    if [ "$want" = "$nm" ] || [ "$want" = "$sl" ]; then
      case "$sub" in tmux|herdr) printf '%s' "$sub"; return 0 ;; esac
    fi
  done
}

# Mixed fleet (tmux + herdr agents coexisting): address each target on ITS OWN backend, inferred
# from the handle shape — NOT the caller's global $BACKEND. Else a herdr-mode caller runs
# `herdr pane get %3` on a tmux handle (or a tmux caller `tmux display-message` on wN:pN),
# mis-pruning/mis-delivering across backends. A BARE slot/name has no shape hint, so consult
# the target's recorded `SUBSTRATE=` in the registry; fall back to $BACKEND only when unknown
# (pure-fleet or a pre-SUBSTRATE= entry → same value as before, so pure-tmux stays identical).
_backend_for() {  # <handle> → tmux|herdr
  case "$1" in
    %[0-9]*|@[0-9]*)             echo tmux ;;    # tmux pane (%N) / window (@N) id
    w[A-Za-z0-9]*:p[A-Za-z0-9]*) echo herdr ;;   # herdr pane handle wN:pN
    *:*)                         echo tmux ;;     # tmux session:index target
    *)  local s; s="$(_sub_from_registry "$1" || true)"; echo "${s:-$BACKEND}" ;;
  esac
}

# map herdr semantic agent_status → our @waiting domain
_herdr_wait_of() {
  case "$1" in
    working) echo 0 ;;
    blocked) echo 1 ;;
    idle|done) echo 2 ;;
    *) echo "" ;;
  esac
}

_herdr_sidecar_set() {  # <pane> <key> <value>
  mkdir -p "$HERDR_STATE"
  local f="$HERDR_STATE/${1//[:\/]/_}"
  # rewrite the key line, keep the rest
  { [ -f "$f" ] && grep -v "^$2=" "$f" || true; echo "$2=$3"; } > "$f.tmp" && mv "$f.tmp" "$f"
}

# ── herdr workspace (bucket) helpers ─────────────────────────────────────────
# A workspace is a labeled herdr bucket. Labels may contain '/' (category/slug).
_herdr_ws_id_for_label() {  # <label> → workspace_id (empty if none)
  herdr workspace list 2>/dev/null | python3 -c '
import sys, json
want = sys.argv[1]
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
for w in d.get("result", {}).get("workspaces", []):
    if w.get("label") == want: print(w.get("workspace_id", "")); break
' "$1" || true
}
_herdr_ws_resolve_or_create() {  # <label> [cwd] → workspace_id (creates the bucket if absent)
  local id; id="$(_herdr_ws_id_for_label "$1")"
  if [ -z "$id" ]; then
    id="$(herdr workspace create --label "$1" --cwd "${2:-$HOME}" --no-focus 2>/dev/null | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
r = d.get("result", {})
print((r.get("root_pane") or {}).get("workspace_id", "") or r.get("workspace_id", ""))
' || true)"
  fi
  printf '%s' "$id"
}
_herdr_ws_label_for_id() {  # <workspace_id> → label (empty if unknown)
  herdr workspace list 2>/dev/null | python3 -c '
import sys, json
want = sys.argv[1]
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
for w in d.get("result", {}).get("workspaces", []):
    if w.get("workspace_id") == want: print(w.get("label", "")); break
' "$1" || true
}

# ── verbs ──────────────────────────────────────────────────────────────────
verb="${1:-}"; shift || true
case "$verb" in

spawn)  # spawn <name> <cwd> <cmd...> [--print] [--workspace <label>] [--split right|down] [--tab <id>] [--focus]
  print=0; ws_label=""; split=""; tab=""; focus=0; args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --print)     print=1; shift ;;
      --workspace) ws_label="${2:-}"; shift 2 ;;
      --split)     split="${2:-}"; shift 2 ;;
      --tab)       tab="${2:-}"; shift 2 ;;
      --focus)     focus=1; shift ;;
      *)           args+=("$1"); shift ;;
    esac
  done
  name="${args[0]:?spawn needs <name>}"; cwd="${args[1]:?spawn needs <cwd>}"
  cmd="${args[*]:2}"
  if [ "$BACKEND" = herdr ]; then
    # --env PATH inherits the CALLER's PATH (the launchd server's is stripped). Propagate the
    # backend + workspace label so the agent's OWN sub-processes (hooks, agent-send, registry)
    # use herdr and record their bucket. Build ONE always-populated arg array — bash 3.2
    # errors on an empty "${arr[@]}" under set -u, so never leave a conditionally-empty array.
    # Everything AFTER the name (name is swapped in the uniquify loop below).
    tailargs=(--cwd "$cwd")
    if [ -n "$ws_label" ]; then
      ws_id="$(_herdr_ws_resolve_or_create "$ws_label" "$cwd")"
      if [ -n "$ws_id" ]; then
        tailargs+=(--workspace "$ws_id" --split "${split:-down}")
        [ -n "$tab" ] && tailargs+=(--tab "$tab")
      fi
      tailargs+=(--env NEXUS_WORKSPACE="$ws_label")
    fi
    # Focus is OPT-IN. Background fan-out (Conductor workers, skill tasks) wants --no-focus so it
    # doesn't yank the user's view; the INTERACTIVE picker passes --focus because the user chose a
    # repo to go work in — without it the agent spawns into a workspace the client isn't viewing
    # and "the window never opens." Into a NEW bucket, `agent start --focus` alone doesn't switch
    # the client (the bucket was created with --no-focus), so also focus the workspace explicitly.
    if [ "$focus" = 1 ]; then tailargs+=(--focus); else tailargs+=(--no-focus); fi
    tailargs+=(--env PATH="$PATH" --env HOME="$HOME" --env NEXUS_SUBSTRATE=herdr --env NEXUS_TMUX_DIR="$NEXUS_TMUX_DIR" --)
    # herdr agent names are GLOBALLY unique — spawning a repo/`general` whose name is already a
    # live agent returns `agent_name_taken`, which used to `die` here → the picker aborted with NO
    # window (the reported "spawned general into interactive/daily, nothing showed up"). Auto-
    # uniquify: try <name>, <name>-2, <name>-3… so an interactive spawn always lands a window.
    # (open-claude still sets the friendly registry NAME + pane label; only the herdr id is
    # suffixed.) Only retry on the name collision — real errors still die.
    # Run the command through a shell (sh -c) — NOT bare argv. The command string may carry an inline
    # `env VAR='multi word value' prog` prefix (e.g. SEED_PROMPT); herdr execs the post-`--` argv WITHOUT
    # a shell, so unquoted `$cmd` word-splits and shatters quoted values — the swarm-bg "dead shell"
    # bug (env read `SEED_PROMPT='Run` as an assignment, tried to exec the next word, failed → login
    # shell). `sh -c "$cmd"` makes herdr parse the command line exactly like the tmux branch, whose
    # `tmux new-window "$cmd"` already runs `/bin/sh -c`. So both backends are now identical.
    try="$name"; sfx=1; out=""; spawned=0
    while :; do
      out=$(herdr agent start "$try" "${tailargs[@]}" sh -c "$cmd" 2>&1) && { spawned=1; break; }
      case "$out" in
        *agent_name_taken*) sfx=$((sfx + 1)); [ "$sfx" -gt 20 ] && break; try="$name-$sfx" ;;
        *) break ;;
      esac
    done
    [ "$spawned" = 1 ] || die "herdr spawn: $out"
    if [ "$focus" = 1 ] && [ -n "${ws_id:-}" ]; then herdr workspace focus "$ws_id" >/dev/null 2>&1 || true; fi
    # NB: `[ x = y ] && echo` as the LAST statement leaks a non-zero exit when false — which
    # made non-`--print` spawns return 1 despite succeeding (callers double-spawned). Use `if`.
    if [ "$print" = 1 ]; then echo "$out"; fi
  else
    # tmux has no workspaces: record the label via a NEXUS_WORKSPACE env prefix (the registry
    # still carries the bucket) and keep flat windows. Byte-identical when --workspace absent.
    [ -n "$ws_label" ] && cmd="env NEXUS_WORKSPACE=$ws_label $cmd"
    # tmux new-window inherits the tmux SERVER's env, not ours — so a RELOCATED install
    # ($NEXUS_TMUX_DIR moved off ~/.tmux) must pass it through, or the spawned open-claude.sh
    # self-defaults back to ~/.tmux. Only when relocated, so the default spawn stays byte-identical.
    [ "$NEXUS_TMUX_DIR" != "$HOME/.tmux" ] && cmd="env NEXUS_TMUX_DIR=$NEXUS_TMUX_DIR $cmd"
    if [ "$print" = 1 ]; then
      tmux new-window -dP -F '#{pane_id}	#{window_index}' -t "$SESSION:" -n "$name" -c "$cwd" "$cmd"
    else
      tmux new-window -d -t "$SESSION:" -n "$name" -c "$cwd" "$cmd"
    fi
  fi
  ;;

send)  # send <dest> <text>  — bus delivery: literal paste + delayed Enter; bare digit → no Enter
  dest="${1:?send needs <dest>}"; text="${2:?send needs <text>}"
  if [ "$(_backend_for "$dest")" = herdr ]; then
    if _is_digit "$text"; then herdr pane send-keys "$dest" "$text"
    else herdr pane send-text "$dest" "$text"; sleep "$ENTER_DELAY"; herdr pane send-keys "$dest" enter; fi
  else
    if _is_digit "$text"; then tmux send-keys -t "$dest" "$text"
    else tmux send-keys -l -t "$dest" "$text"; sleep "$ENTER_DELAY"; tmux send-keys -t "$dest" Enter; fi
  fi
  ;;

send-keys)  # send-keys <dest> <enter|escape|literal>
  dest="${1:?}"; key="${2:?}"
  if [ "$(_backend_for "$dest")" = herdr ]; then
    case "$key" in
      enter) herdr pane send-keys "$dest" enter ;;
      escape) herdr pane send-keys "$dest" esc ;;
      *) herdr pane send-text "$dest" "$key" ;;
    esac
  else
    case "$key" in
      enter) tmux send-keys -t "$dest" Enter ;;
      escape) tmux send-keys -t "$dest" Escape ;;
      *) tmux send-keys -t "$dest" -l "$key" ;;
    esac
  fi
  ;;

report-state)  # report-state <pane> working|idle [ts]  |  report-state needs-input <pane> <wait_type> [ts]
  # ts is passed by callers that must keep an exact epoch (the @wait_since byte-match
  # stale-guard in the slack-bridge); defaults to now() when omitted.
  if [ "${1:-}" = needs-input ]; then
    pane="${2:?}"; wtype="${3:-}"; ts="${4:-$(now)}"
    if [ "$BACKEND" = herdr ]; then
      herdr pane report-agent "$pane" --source nexus-hook --agent claude --state blocked --custom-status "needs input: $wtype" >/dev/null
      herdr pane report-metadata "$pane" --source nexus-hook --state-label "blocked=$wtype" >/dev/null
      _herdr_sidecar_set "$pane" wait_since "$ts"; _herdr_sidecar_set "$pane" wait_type "$wtype"
    else
      tmux set-window-option -t "$pane" @waiting 1
      tmux set-option -w -t "$pane" @wait_since "$ts"
      tmux set-option -w -t "$pane" @wait_type "$wtype"
    fi
  else
    pane="${1:?}"; state="${2:?working|idle}"; ts="${3:-$(now)}"
    if [ "$BACKEND" = herdr ]; then
      case "$state" in
        working) herdr pane report-agent "$pane" --source nexus-hook --agent claude --state working >/dev/null; _herdr_sidecar_set "$pane" last_tool "$ts" ;;
        idle)    herdr pane report-agent "$pane" --source nexus-hook --agent claude --state idle >/dev/null ;;
        *) die "report-state: unknown state '$state'" ;;
      esac
    else
      case "$state" in
        working) tmux set-window-option -t "$pane" @waiting 0; tmux set-window-option -t "$pane" @last_tool "$ts"; tmux set-option -wu -t "$pane" @wait_since 2>/dev/null || true ;;
        idle)    tmux set-window-option -t "$pane" @waiting 2 ;;
        *) die "report-state: unknown state '$state'" ;;
      esac
    fi
  fi
  ;;

rename)  # rename <pane> <name>
  pane="${1:?}"; name="${2:?}"
  if [ "$BACKEND" = herdr ]; then
    herdr pane rename "$pane" "$name" >/dev/null
  else
    tmux rename-window -t "$pane" "$name"
    tmux set-window-option -t "$pane" automatic-rename off
  fi
  ;;

keep)  # keep <tgt> <0|1>
  tgt="${1:?}"; val="${2:?0|1}"
  if [ "$BACKEND" = herdr ]; then _herdr_sidecar_set "$tgt" keep "$val"
  else tmux set-window-option -t "$tgt" @keep "$val"; fi
  ;;

cohort)  # cohort <tgt> <label>  |  cohort <tgt> --release
  tgt="${1:?}"; label="${2:?<label>|--release}"
  if [ "$label" = --release ]; then
    if [ "$BACKEND" = herdr ]; then _herdr_sidecar_set "$tgt" cohort ""
    else tmux set-window-option -u -t "$tgt" @cohort 2>/dev/null || true; tmux set-window-option -u -t "$tgt" @cohort_since 2>/dev/null || true; fi
  else
    if [ "$BACKEND" = herdr ]; then _herdr_sidecar_set "$tgt" cohort "$label"; _herdr_sidecar_set "$tgt" cohort_since "$(now)"
    else tmux set-window-option -t "$tgt" @cohort "$label"; tmux set-window-option -t "$tgt" @cohort_since "$(now)"; fi
  fi
  ;;

tag-orchestrator)  # tag-orchestrator <pane>
  pane="${1:?}"
  if [ "$BACKEND" = herdr ]; then _herdr_sidecar_set "$pane" orchestrator 1
  else tmux set-window-option -t "$pane" @orchestrator 1; fi
  ;;

register)  # register <pane> <name> [cwd] [ws]  → write the canonical registry entry
  # ONE writer of the ~/.tmux/registry/<handle> format, so every agent that reaches the
  # fleet — open-claude picker launches AND seam-spawned agents (conductor orchestrator +
  # workers, skill tasks) — is enumerable by the reaper / peers / name→handle resolution.
  # Without this only open-claude registered, so seam-spawned herdr agents were a "second
  # roster" invisible to the registry-driven consumers. SLOT/SUBSTRATE are backend-derived;
  # WORKSPACE falls back to the pane's bucket (herdr) when not passed. Idempotent (overwrite).
  pane="${1:?register needs <pane>}"; rname="${2:?register needs <name>}"
  rcwd="${3:-$PWD}"; rws="${4:-}"
  slot="$("$0" pane-field "$pane" '#{window_index}' 2>/dev/null || true)"
  [ -n "$rws" ] || rws="${NEXUS_WORKSPACE:-$("$0" workspace-of "$pane" 2>/dev/null || true)}"
  mkdir -p "$NEXUS_TMUX_DIR/registry"
  printf 'SLOT=%s\nNAME=%s\nCWD=%s\nAT=%s\nPANE_ID=%s\nWORKSPACE=%s\nSUBSTRATE=%s\n' \
    "${slot:-$pane}" "$rname" "$rcwd" "$(now)" "$pane" "$rws" "$BACKEND" \
    > "$NEXUS_TMUX_DIR/registry/${pane}"
  ;;

deregister)  # deregister <pane>  → remove the registry entry (idempotent)
  # For agents WITHOUT a tmux pane-died hook (headless python: conductor / workers), which
  # must self-deregister on exit so their entry doesn't go stale. claude agents keep using
  # agent-deregister.sh via the pane-died hook; this is the same effect, callable directly.
  pane="${1:?deregister needs <pane>}"
  rm -f "$NEXUS_TMUX_DIR/registry/${pane}"
  ;;

kill)  # kill <win>
  win="${1:?}"
  if [ "$(_backend_for "$win")" = herdr ]; then herdr pane close "$win" >/dev/null
  else tmux kill-window -t "$win"; fi
  ;;

workspace-create)  # workspace-create <label> [cwd] [--focus] → prints workspace_id (herdr); no-op (tmux)
  # --focus switches the client to the new bucket. The shared resolve helper always creates with
  # --no-focus (right for spawn's background fan-out), so focus is a SEPARATE opt-in step here —
  # the interactive bucket-creator (nexus-workspace-new.sh, prefix+shift+b) passes it, otherwise
  # the bucket is made but the client never moves to it and it looks like nothing happened.
  wsc_focus=0; wsc_args=()
  for a in "$@"; do case "$a" in --focus) wsc_focus=1 ;; *) wsc_args+=("$a") ;; esac; done
  wl="${wsc_args[0]:?workspace-create needs <label>}"
  if [ "$BACKEND" = herdr ]; then
    wsc_id="$(_herdr_ws_resolve_or_create "$wl" "${wsc_args[1]:-$HOME}")"
    [ -n "$wsc_id" ] || exit 1
    [ "$wsc_focus" = 1 ] && { herdr workspace focus "$wsc_id" >/dev/null 2>&1 || true; }
    printf '%s' "$wsc_id"
  fi
  ;;

workspace-list)  # workspace-list → lines: <id>\t<label>\t<pane_count> (herdr); empty (tmux)
  if [ "$BACKEND" = herdr ]; then
    herdr workspace list 2>/dev/null | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
for w in d.get("result", {}).get("workspaces", []):
    print("%s\t%s\t%s" % (w.get("workspace_id",""), w.get("label",""), w.get("pane_count","")))
' || true
  fi
  ;;

workspace-close)  # workspace-close <label|id> → herdr closes the bucket; tmux kills WORKSPACE=-matched windows
  wt="${1:?workspace-close needs <label|id>}"
  if [ "$BACKEND" = herdr ]; then
    wid="$(herdr workspace list 2>/dev/null | python3 -c '
import sys, json
want = sys.argv[1]
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
for w in d.get("result", {}).get("workspaces", []):
    if w.get("label") == want or w.get("workspace_id") == want:
        print(w.get("workspace_id","")); break
' "$wt" || true)"
    [ -n "$wid" ] && herdr workspace close "$wid" >/dev/null 2>&1 || true
  else
    # tmux-degrade: close = kill every window whose registry WORKSPACE= matches (full or
    # slug); never the caller's own pane.
    self="${TMUX_PANE:-}"
    for f in "$NEXUS_TMUX_DIR/registry"/*; do
      [ -f "$f" ] || continue
      ew="$(grep '^WORKSPACE=' "$f" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      [ -n "$ew" ] || continue
      { [ "$ew" = "$wt" ] || [ "${ew##*/}" = "${wt##*/}" ]; } || continue
      pid="$(grep '^PANE_ID=' "$f" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      { [ -n "$pid" ] && [ "$pid" != "$self" ]; } || continue
      win="$(tmux display-message -t "$pid" -p '#{window_id}' 2>/dev/null || true)"
      [ -n "$win" ] && tmux kill-window -t "$win" 2>/dev/null || true
    done
  fi
  ;;

workspace-of)  # workspace-of <pane> → prints the pane's workspace label (fallback deriver)
  wp="${1:?workspace-of needs <pane>}"
  if [ "$BACKEND" = herdr ]; then _herdr_ws_label_for_id "${wp%%:*}"
  else tmux show-options -wqv -t "$wp" @workspace 2>/dev/null || true; fi
  ;;

query)  # → lines of  index|name|@waiting|path|command|@wait_type   (the arbiter enumeration contract)
  if [ "$BACKEND" = herdr ]; then
    # Prefer the substrated read-daemon (cached, no per-call subprocess); fall back
    # to a direct herdr query when the daemon is down.
    if out=$(curl -sf -m 1 "http://127.0.0.1:${SUBSTRATED_PORT:-8422}/windows" 2>/dev/null); then printf '%s' "$out"; else
    herdr agent list 2>/dev/null | python3 -c '
import sys, json, os
state=os.environ.get("NEXUS_HERDR_STATE", os.path.expanduser("~/.config/herdr/nexus-state"))
wmap={"working":"0","blocked":"1","idle":"2","done":"2"}
def sidecar(pane,key):
    f=os.path.join(state, pane.replace(":","_").replace("/","_"))
    try:
        for ln in open(f):
            if ln.startswith(key+"="): return ln.split("=",1)[1].strip()
    except OSError: pass
    return ""
try: agents=json.load(sys.stdin)["result"]["agents"]
except Exception: agents=[]
for a in agents:
    pid=a.get("pane_id",""); wt=(a.get("state_labels") or {}).get("blocked","")
    print("|".join([pid, a.get("agent") or "", wmap.get(a.get("agent_status"),""),
                     a.get("foreground_cwd") or "", "claude", wt]))
'
    fi
  else
    tmux list-windows -t "$SESSION" -F "#{window_index}|#{window_name}|#{@waiting}|#{pane_current_path}|#{pane_current_command}|#{@wait_type}" 2>/dev/null || true
  fi
  ;;

pane-opt)  # pane-opt <pane> <@name>  → the option value (empty if unset)
  pane="${1:?}"; opt="${2:?}"
  if [ "$(_backend_for "$pane")" = herdr ]; then
    enc="${pane//%/%25}"
    if val=$(curl -sf -m 1 "http://127.0.0.1:${SUBSTRATED_PORT:-8422}/pane?id=${enc}&opt=${opt}" 2>/dev/null); then printf '%s' "$val"; else
    case "$opt" in
      @waiting) herdr pane get "$pane" 2>/dev/null | python3 -c 'import sys,json; s=json.load(sys.stdin)["result"]["pane"]["agent_status"]; print({"working":"0","blocked":"1","idle":"2","done":"2"}.get(s,""))' 2>/dev/null || echo "" ;;
      *) # sidecar-backed options (@wait_since/@last_tool/@keep/@cohort/@orchestrator/@wait_type)
         key="${opt#@}"
         f="$HERDR_STATE/${pane//[:\/]/_}"; [ -f "$f" ] && (grep "^$key=" "$f" | cut -d= -f2-) || echo "" ;;
    esac
    fi
  else
    tmux show-options -wqv -t "$pane" "$opt" 2>/dev/null || true
  fi
  ;;

set-opt)  # set-opt <pane> <@name> [value]   — value omitted = unset (generic window option; e.g. @last_surface)
  pane="${1:?}"; opt="${2:?}"
  if [ "$BACKEND" = herdr ]; then
    _herdr_sidecar_set "$pane" "${opt#@}" "${3-}"
  else
    if [ $# -ge 3 ]; then tmux set-window-option -t "$pane" "$opt" "$3"
    else tmux set-option -wu -t "$pane" "$opt" 2>/dev/null || true; fi
  fi
  ;;

pane-field)  # pane-field <target> '<tmux-format>'  → display-message passthrough (#{window_index}, #{pane_id}, #W, …)
  tgt="${1:?}"; fmt="${2:?}"
  if [ "$BACKEND" = herdr ]; then
    # tmux is the flag=tmux-exercised path in P2; herdr field mapping is refined in P3.
    herdr pane get "$tgt" 2>/dev/null | _FMT="$fmt" python3 -c '
import sys, json, os
fmt=os.environ.get("_FMT","").strip()
try: p=json.load(sys.stdin)["result"]["pane"]
except Exception: p={}
m={"#{pane_id}":p.get("pane_id",""),"#{window_index}":p.get("pane_id",""),
   "#{pane_current_path}":p.get("foreground_cwd",""),"#{pane_current_command}":"claude","#W":p.get("agent","")}
print(m.get(fmt,""))' 2>/dev/null || true
  else
    tmux display-message -t "$tgt" -p "$fmt" 2>/dev/null || true
  fi
  ;;

list-panes)  # list-panes  → one pane/agent handle per line (liveness enumeration)
  if [ "$BACKEND" = herdr ]; then
    herdr pane list 2>/dev/null | python3 -c 'import sys,json
try:
    for p in json.load(sys.stdin)["result"]["panes"]: print(p.get("pane_id",""))
except Exception: pass' 2>/dev/null || true
  else
    tmux list-panes -a -F '#{pane_id}' 2>/dev/null || true
  fi
  ;;

capture)  # capture <target> [lines]  → pane text (peek-summary)
  tgt="${1:?}"
  if [ "$BACKEND" = herdr ]; then
    herdr pane read "$tgt" --source recent-unwrapped ${2:+--lines "$2"} 2>/dev/null || true   # herdr `pane read` emits plain text, not JSON
  else
    tmux capture-pane -t "$tgt" -p 2>/dev/null || true
  fi
  ;;

pane-focused)  # pane-focused <target>  → 1 if the human's client is currently on this pane, else 0
  tgt="${1:?}"
  if [ "$BACKEND" = herdr ]; then
    herdr pane get "$tgt" 2>/dev/null | python3 -c 'import sys,json
try: print("1" if json.load(sys.stdin)["result"]["pane"].get("focused") else "0")
except Exception: print("0")' 2>/dev/null || echo 0
  else
    # focused == the active pane of the active window (where the client’s keystrokes land)
    case "$(tmux display-message -t "$tgt" -p '#{window_active}#{pane_active}' 2>/dev/null)" in
      11) echo 1 ;; *) echo 0 ;;
    esac
  fi
  ;;

pane-visible)  # pane-visible <target> [lines]  → CURRENT on-screen text (reflects in-progress input)
  tgt="${1:?}"
  if [ "$BACKEND" = herdr ]; then
    herdr pane read "$tgt" --source visible ${2:+--lines "$2"} 2>/dev/null || true   # herdr `pane read` emits plain text, not JSON
  else
    if [ -n "${2:-}" ]; then tmux capture-pane -t "$tgt" -p 2>/dev/null | tail -n "$2" || true
    else tmux capture-pane -t "$tgt" -p 2>/dev/null || true; fi
  fi
  ;;

clients)  # clients ['<fmt>']  → attached clients (human-typing guard)
  if [ "$BACKEND" = herdr ]; then
    echo ""   # herdr: no TUI-attached-client analog when headless → fail-open (P3 documents this)
  else
    tmux list-clients ${1:+-F "$1"} 2>/dev/null || true
  fi
  ;;

has-session)  # has-session  → exit 0 if the fleet substrate is up
  if [ "$BACKEND" = herdr ]; then
    herdr status server >/dev/null 2>&1
  else
    tmux has-session -t "$SESSION" 2>/dev/null
  fi
  ;;

pane-alive)  # pane-alive <handle>  → exit 0 if the pane/agent is alive, 1 if gone.
  # Authoritative liveness for registry pruning — works for both tmux %panes and
  # herdr handles (wN:pN). The tmux-only `display-message -t <pane>` probe that
  # callers used before returns non-zero for a herdr handle, which wrongly prunes
  # live herdr agents; route liveness through here instead.
  h="${1:?}"
  if [ "$(_backend_for "$h")" = herdr ]; then
    herdr pane get "$h" 2>/dev/null | grep -q '"pane_id"'
  else
    # Definitive: the pane id must be in the live pane list. `display-message -t <bad>` can
    # exit 0 for a non-existent pane (→ under-prune), so enumerate instead.
    tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx "$h"
  fi
  ;;

""|-h|--help|help)
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  ;;

*) die "unknown verb '$verb' (try: substrate --help)" ;;
esac
