#!/usr/bin/env bash
# overlay-apply.sh — snap private "plugs" overlays into this (public) core checkout.
#
# The public agents-nexus core is standalone-capable; every org/personal specific is a
# seam. An OVERLAY is a private repo that fills those seams: it carries a `files/` tree
# (mirroring paths under the core repo root) plus an `overlay.toml` manifest of post-copy
# steps. This script fetches an overlay and layers it in — with ZERO baked-in knowledge of
# what any particular overlay contains (the path list lives only in the overlay repo).
#
# COMPOSABLE: multiple NAMED overlays coexist on one box (e.g. an `org` overlay for shared
# hooks/configs AND a `personal` overlay for your workflow). Each is fetched, applied,
# listed, and removed independently. The name comes from a REQUIRED `name = "..."` field in
# the overlay's own overlay.toml, and keys the clone dir, the stamp, and the exclude block.
#
# Usage:
#   scripts/overlay-apply.sh <git-url|local-path> [--ref BRANCH] [--dry-run] [--force]
#   scripts/overlay-apply.sh --status [<name>]   # list all applied overlays, or detail one
#   scripts/overlay-apply.sh --remove <name>     # un-apply one overlay (restores shadowed files)
#   scripts/overlay-apply.sh --remove --all      # un-apply every overlay (newest-first)
#
#   <git-url|local-path>  where the overlay repo lives (staged, then placed at overlay/<name>/)
#   --ref BRANCH          overlay branch/tag/sha to checkout (default: the repo's default)
#   --dry-run             print what would be copied/run; touch nothing
#   --force               overwrite TRACKED core files that already differ (default: skip+warn)
#
# Multiple overlays: run once per overlay (they compose). If two overlays place the SAME core
# path, LAST-APPLIED WINS (a warning names both). Removing the winner RESTORES the other
# overlay's version of that path if it still owns it.
#
# Safety model — why this never leaks a private identifier back into the public export:
#   The export (export-public.sh) is `git archive HEAD` = TRACKED FILES ONLY. Every path an
#   overlay drops is recorded in .git/info/exclude (a per-clone, never-committed ignore list)
#   under a per-overlay marker block, so overlay files stay UNTRACKED and are invisible to the
#   export. The core repo cannot re-emit overlay content no matter how many overlays apply.
#
# overlay.toml (in the overlay repo root) — minimal TOML this script understands:
#   name = "example"               # REQUIRED — [A-Za-z0-9._-]; keys clone/stamp/exclude
#   files_dir = "files"            # dir (relative to overlay root) whose tree mirrors core paths
#   [[symlink]]                    # 0+  create ~/… symlinks (e.g. repoint conductor config)
#     link = "~/.tmux/conductor.yaml"
#     target = "config/conductor.personal.yaml"   # relative to CORE root (i.e. a copied file)
#   [[env]]                        # 0+  merge KEY=VAL into the active profile .env (kept if present)
#     key = "SOME_KEY"
#     value = "default"
#   [[template]]                   # 0+  glob of copied files to sed __HOME__/__NODE_BIN__ in-place
#     glob = "launchd/*.plist"
set -u

# ── repo root (canonicalize scripts/ → root through any symlink) ─────────────
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd "$(dirname "$_src")" && pwd -P)"; _tgt="$(readlink "$_src")"
  case "$_tgt" in /*) _src="$_tgt" ;; *) _src="$_dir/$_tgt" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$_src")" && pwd -P)"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
OVERLAY_ROOT="$NEXUS_DIR/overlay"          # container; each overlay lives at overlay/<name>/
INCOMING="$OVERLAY_ROOT/.incoming"         # staging dir (name unknown until manifest is read)
EXCLUDE="$NEXUS_DIR/.git/info/exclude"
LEGACY_STAMP="$NEXUS_DIR/.overlay-applied" # pre-composable single-overlay stamp (migrated)

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; OKC=$'\033[32m'; WARNC=$'\033[33m'; ERRC=$'\033[31m'; Z=$'\033[0m'
else B=""; D=""; OKC=""; WARNC=""; ERRC=""; Z=""; fi
say(){ printf '%s\n' "$*" >&2; }
warn(){ printf '%s\n' "  ${WARNC}$*${Z}" >&2; }
die(){ printf '%s\n' "${ERRC}error:${Z} $*" >&2; exit 1; }

SRC=""; REF=""; DRY=0; FORCE=0; ACTION="apply"; ARG_NAME=""; ALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref)     REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --force)   FORCE=1; shift ;;
    --all)     ALL=1; shift ;;
    --status)  ACTION="status"; shift ;;
    --remove)  ACTION="remove"; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)        die "unknown flag: $1" ;;
    *)         if [ "$ACTION" = apply ]; then SRC="$1"; else ARG_NAME="$1"; fi; shift ;;
  esac
done

run(){ if [ "$DRY" = 1 ]; then say "    ${D}[dry-run] $*${Z}"; else "$@"; fi; }

# ── tiny TOML readers (flat scalars + repeated [[table]] blocks) ─────────────
_strip(){ local v="$1"; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
          case "$v" in \"*\") v="${v#\"}"; v="${v%\"}";; \'*\') v="${v#\'}"; v="${v%\'}";; esac; printf '%s' "$v"; }
toml_scalar(){ # toml_scalar FILE KEY  → first top-level `key = value`
  local f="$1" key="$2" line k
  [ -f "$f" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"; case "$line" in \[*) continue;; esac
    case "$line" in *=*) k="$(_strip "${line%%=*}")"; [ "$k" = "$key" ] && { _strip "${line#*=}"; return 0; };; esac
  done < "$f"; return 1
}
toml_blocks(){ # toml_blocks FILE TABLE KEY  → value of KEY for each [[TABLE]] block, ONE PER LINE
  local f="$1" table="$2" key="$3" line k inblk=0
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      "[[$table]]"*) inblk=1; continue ;;
      \[*)           inblk=0; continue ;;
    esac
    [ "$inblk" = 1 ] || continue
    case "$line" in *=*) k="$(_strip "${line%%=*}")"; [ "$k" = "$key" ] && { _strip "${line#*=}"; printf '\n'; };; esac
  done < "$f"
}

expand_home(){ case "$1" in "~/"*) printf '%s/%s' "$HOME" "${1#\~/}";; "~") printf '%s' "$HOME";; *) printf '%s' "$1";; esac; }

# ── name + path helpers ──────────────────────────────────────────────────────
valid_name(){ # 0 if $1 is a safe overlay name
  case "$1" in
    ""|.|..|.incoming) return 1 ;;
    *[!A-Za-z0-9._-]*) return 1 ;;
    *) return 0 ;;
  esac
}
clone_dir(){ printf '%s/%s' "$OVERLAY_ROOT" "$1"; }
stamp_path(){ printf '%s/.overlay-applied.%s' "$NEXUS_DIR" "$1"; }
list_applied(){ # print applied overlay names, one per line (empty if none)
  local s n
  for s in "$NEXUS_DIR"/.overlay-applied.*; do
    [ -e "$s" ] || continue
    n="${s##*/.overlay-applied.}"
    printf '%s\n' "$n"
  done
}
stamp_field(){ # stamp_field NAME FIELD  → value from the stamp header `# FIELD: value`
  local sp; sp="$(stamp_path "$1")"; [ -f "$sp" ] || return 1
  sed -n "s/^# $2: //p" "$sp" | head -1
}
stamp_paths(){ # stamp_paths NAME  → the recorded rel paths (skip header comments)
  local sp; sp="$(stamp_path "$1")"; [ -f "$sp" ] || return 0
  while IFS= read -r rel; do case "$rel" in "#"*|"") continue ;; *) printf '%s\n' "$rel" ;; esac; done < "$sp"
}
# path_owner_excluding REL EXCL  → the applied overlay (other than EXCL) with the highest
# applied_at that lists REL and still has the file in its clone. Empty if none.
path_owner_excluding(){
  local rel="$1" excl="$2" n best="" best_at=-1 at fdir
  while IFS= read -r n; do
    [ -n "$n" ] || continue; [ "$n" = "$excl" ] && continue
    stamp_paths "$n" | grep -qxF "$rel" || continue
    fdir="$(toml_scalar "$(clone_dir "$n")/overlay.toml" files_dir 2>/dev/null || echo files)"
    [ -f "$(clone_dir "$n")/$fdir/$rel" ] || continue
    at="$(stamp_field "$n" applied_at 2>/dev/null || echo 0)"; case "$at" in ''|*[!0-9]*) at=0;; esac
    if [ "$at" -ge "$best_at" ]; then best_at="$at"; best="$n"; fi
  done < <(list_applied)
  printf '%s' "$best"
}

# ── exclude-block edit (parameterized on overlay name) ───────────────────────
strip_exclude_block(){ # strip_exclude_block NAME  → remove that overlay's block from EXCLUDE
  [ -f "$EXCLUDE" ] || return 0
  run python3 - "$EXCLUDE" "$1" <<'PY'
import sys
p, name = sys.argv[1], sys.argv[2]
beg = f"# >>> agents-nexus overlay:{name} >>>"
end = f"# <<< agents-nexus overlay:{name} <<<"
lines = open(p).read().split("\n"); out=[]; skip=False
for ln in lines:
    if ln.strip()==beg: skip=True; continue
    if ln.strip()==end: skip=False; continue
    if not skip: out.append(ln)
while out and out[-1]=="": out.pop()
open(p,"w").write("\n".join(out)+("\n" if out else ""))
PY
}
write_exclude_block(){ # write_exclude_block NAME PATH...  → replace that overlay's block
  local name="$1"; shift
  mkdir -p "$(dirname "$EXCLUDE")"; touch "$EXCLUDE"
  run python3 - "$EXCLUDE" "$name" "$@" <<'PY'
import sys
p, name = sys.argv[1], sys.argv[2]; copied = sys.argv[3:]
beg = f"# >>> agents-nexus overlay:{name} >>>"
end = f"# <<< agents-nexus overlay:{name} <<<"
lines = open(p).read().split("\n"); out=[]; skip=False
for ln in lines:
    if ln.strip()==beg: skip=True; continue
    if ln.strip()==end: skip=False; continue
    if not skip: out.append(ln)
while out and out[-1]=="": out.pop()
out.append(beg)
out.append(f"# (auto-managed by scripts/overlay-apply.sh — do not edit; `--remove {name}` clears it)")
for rel in copied: out.append("/"+rel)
out.append(end)
open(p,"w").write("\n".join(out)+"\n")
PY
}

# ── legacy migration (pre-composable single overlay → name 'legacy') ─────────
migrate_legacy(){
  [ -f "$LEGACY_STAMP" ] || return 0
  [ -f "$(stamp_path legacy)" ] && return 0   # already migrated
  say "${WARNC}migrating pre-composable overlay → name 'legacy'${Z}"
  if [ -d "$OVERLAY_ROOT" ] && [ ! -d "$(clone_dir legacy)" ] && [ -f "$OVERLAY_ROOT/overlay.toml" ]; then
    local tmp="$NEXUS_DIR/.overlay-migrate.$$"
    mv "$OVERLAY_ROOT" "$tmp" && mkdir -p "$OVERLAY_ROOT" && mv "$tmp" "$(clone_dir legacy)"
  fi
  { echo "# agents-nexus overlay — applied"
    echo "# name: legacy"
    echo "# source: $(sed -n 's/^# source: //p' "$LEGACY_STAMP" | head -1)"
    echo "# applied_at: 0"
    echo "# schema: 2"
    while IFS= read -r rel; do case "$rel" in "#"*|"") continue ;; *) printf '%s\n' "$rel" ;; esac; done < "$LEGACY_STAMP"
  } > "$(stamp_path legacy)"
  rm -f "$LEGACY_STAMP"
  if [ -f "$EXCLUDE" ]; then
    python3 - "$EXCLUDE" <<'PY'
import sys
p=sys.argv[1]
beg="# >>> agents-nexus overlay >>>"; end="# <<< agents-nexus overlay <<<"
nbeg="# >>> agents-nexus overlay:legacy >>>"; nend="# <<< agents-nexus overlay:legacy <<<"
lines=open(p).read().split("\n"); out=[]
for ln in lines:
    if ln.strip()==beg: out.append(nbeg); continue
    if ln.strip()==end: out.append(nend); continue
    out.append(ln)
open(p,"w").write("\n".join(out))
PY
  fi
  say "  ${OKC}✓${Z} migrated → 'legacy' (list: --status; remove: --remove legacy)"
}

# ── template one file (reused by apply + restore) ────────────────────────────
NODE_BIN="$(dirname "$(command -v node 2>/dev/null || echo /usr/local/bin/node)")"
_tmpl_one(){ run perl -i -pe "s{__HOME__}{$HOME}g; s{__NODE_BIN__}{$NODE_BIN}g" "$1"; }
_apply_templates(){ # _apply_templates NAME REL...  → template any REL matching NAME's [[template]] globs
  local name="$1"; shift
  local man; man="$(clone_dir "$name")/overlay.toml"; [ -f "$man" ] || return 0
  local glob rel
  while IFS= read -r glob; do
    [ -n "$glob" ] || continue
    for rel in "$@"; do case "$rel" in $glob) _tmpl_one "$NEXUS_DIR/$rel" ;; esac; done
  done < <(toml_blocks "$man" template glob)
}

# ══ --status ═════════════════════════════════════════════════════════════════
if [ "$ACTION" = "status" ]; then
  migrate_legacy
  if [ -n "$ARG_NAME" ]; then
    sp="$(stamp_path "$ARG_NAME")"
    [ -f "$sp" ] || die "no overlay named '$ARG_NAME' applied"
    say "${B}overlay: $ARG_NAME${Z}"; sed 's/^/  /' "$sp" >&2; exit 0
  fi
  names="$(list_applied)"
  if [ -z "$names" ]; then say "${D}no overlays applied${Z}"; exit 0; fi
  say "${B}applied overlays${Z}"
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    src="$(stamp_field "$n" source 2>/dev/null)"
    cnt="$(stamp_paths "$n" | grep -c .)"
    say "  ${OKC}$n${Z}  ${D}(${cnt} files · ${src:-?})${Z}"
  done <<< "$names"
  say "${D}detail: --status <name>   remove: --remove <name>${Z}"
  exit 0
fi

# ══ --remove ═════════════════════════════════════════════════════════════════
do_remove(){ # do_remove NAME
  local name="$1" sp; sp="$(stamp_path "$name")"
  [ -f "$sp" ] || { warn "no overlay named '$name' applied"; return 0; }
  say "${B}▸ remove overlay${Z} ${D}($name)${Z}"
  local rel other fdir
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    other="$(path_owner_excluding "$rel" "$name")"
    if [ -n "$other" ]; then
      fdir="$(toml_scalar "$(clone_dir "$other")/overlay.toml" files_dir 2>/dev/null || echo files)"
      run cp "$(clone_dir "$other")/$fdir/$rel" "$NEXUS_DIR/$rel"
      _apply_templates "$other" "$rel"
      say "  ${OKC}restored${Z} $rel ${D}from '$other'${Z}"
    else
      run rm -f "$NEXUS_DIR/$rel"
    fi
  done < <(stamp_paths "$name")
  strip_exclude_block "$name"
  run rm -rf "$(clone_dir "$name")"
  run rm -f "$sp"
  say "  ${OKC}✓${Z} removed overlay '$name'"
}
if [ "$ACTION" = "remove" ]; then
  migrate_legacy
  if [ "$ALL" = 1 ]; then
    names="$(while IFS= read -r n; do [ -n "$n" ] || continue; printf '%s %s\n' "$(stamp_field "$n" applied_at 2>/dev/null || echo 0)" "$n"; done < <(list_applied) | sort -rn | awk '{print $2}')"
    [ -n "$names" ] || { say "${D}no overlays applied${Z}"; exit 0; }
    while IFS= read -r n; do [ -n "$n" ] && do_remove "$n"; done <<< "$names"
    exit 0
  fi
  if [ -z "$ARG_NAME" ]; then
    names="$(list_applied)"; count="$(printf '%s\n' "$names" | grep -c .)"
    [ "$count" = 0 ] && { say "${D}nothing to remove${Z}"; exit 0; }
    [ "$count" = 1 ] && { do_remove "$(printf '%s' "$names" | head -1)"; exit 0; }
    die "multiple overlays applied — name one (or --all):"$'\n'"$(printf '%s' "$names" | sed 's/^/  /')"
  fi
  do_remove "$ARG_NAME"
  exit 0
fi

# ══ apply ════════════════════════════════════════════════════════════════════
[ -n "$SRC" ] || die "no overlay source. Usage: overlay-apply.sh <git-url|local-path> [--ref B] [--dry-run]"
migrate_legacy

# ── stage: fetch into overlay/.incoming/ (name unknown until we read the manifest) ──
say "${B}▸ fetch overlay${Z} ${D}($SRC)${Z}"
rm -rf "$INCOMING"; mkdir -p "$OVERLAY_ROOT"
if [ -d "$SRC/.git" ]; then
  run git clone --quiet ${REF:+--branch "$REF"} "$SRC" "$INCOMING" || die "overlay clone failed"
elif [ -f "$SRC/overlay.toml" ]; then
  run mkdir -p "$INCOMING"; run cp -R "$SRC/." "$INCOMING/"
else
  run git clone --quiet ${REF:+--branch "$REF"} "$SRC" "$INCOMING" || die "overlay clone failed"
fi

if [ "$DRY" = 1 ] && [ ! -f "$INCOMING/overlay.toml" ]; then
  say "  ${D}[dry-run] would stage $SRC, read its name from overlay.toml, place at overlay/<name>/, copy files/ + run steps${Z}"
  rm -rf "$INCOMING"; exit 0
fi
[ -f "$INCOMING/overlay.toml" ] || { rm -rf "$INCOMING"; die "overlay has no overlay.toml at its root"; }

NAME="$(toml_scalar "$INCOMING/overlay.toml" name || true)"
[ -n "$ARG_NAME" ] && [ "$ARG_NAME" != "$NAME" ] && { rm -rf "$INCOMING"; die "--name '$ARG_NAME' != overlay.toml name '$NAME'"; }
valid_name "$NAME" || { rm -rf "$INCOMING"; die "overlay.toml needs a valid 'name' ([A-Za-z0-9._-]); see overlay.example/overlay.toml (got: '${NAME:-<empty>}')"; }

DEST="$(clone_dir "$NAME")"
if [ "$DRY" = 1 ]; then
  say "  ${D}[dry-run] would place overlay '$NAME' at $DEST${Z}"
  OVERLAY_DIR="$INCOMING"
else
  rm -rf "$DEST"; mv "$INCOMING" "$DEST"; OVERLAY_DIR="$DEST"
fi
MANIFEST="$OVERLAY_DIR/overlay.toml"
say "  ${OKC}✓${Z} overlay ${B}$NAME${Z}"

FILES_SUB="$(toml_scalar "$MANIFEST" files_dir || echo files)"
FILES_ROOT="$OVERLAY_DIR/$FILES_SUB"
[ -d "$FILES_ROOT" ] || die "overlay files dir not found: $FILES_ROOT (files_dir=\"$FILES_SUB\")"

# ── 1. layer the files/ tree into the core (last-wins across overlays) ────────
say "${B}▸ layer files${Z} ${D}($FILES_SUB/ → repo root)${Z}"
COPIED=(); SKIPPED=0
while IFS= read -r abs; do
  rel="${abs#"$FILES_ROOT"/}"
  dst="$NEXUS_DIR/$rel"
  if [ -e "$dst" ] && ! cmp -s "$abs" "$dst" 2>/dev/null; then
    if git -C "$NEXUS_DIR" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
      if [ "$FORCE" = 1 ]; then warn "overwrite (tracked, --force): $rel"
      else warn "skip (tracked core file differs; --force to overwrite): $rel"; SKIPPED=$((SKIPPED+1)); continue; fi
    else
      other="$(path_owner_excluding "$rel" "$NAME")"
      [ -n "$other" ] && warn "conflict: $rel — overlay '$NAME' overwrites '$other' (last wins)"
    fi
  fi
  run mkdir -p "$(dirname "$dst")"
  run cp "$abs" "$dst"
  COPIED+=("$rel")
done < <(find "$FILES_ROOT" -type f | LC_ALL=C sort)
say "  ${OKC}✓${Z} ${#COPIED[@]} file(s) placed${SKIPPED:+, $SKIPPED skipped}"

# ── 2. template copied files matching each [[template]].glob ──────────────────
while IFS= read -r glob; do
  [ -n "$glob" ] || continue
  say "${B}▸ template${Z} ${D}($glob)${Z}"
  for rel in "${COPIED[@]}"; do
    case "$rel" in $glob) _tmpl_one "$NEXUS_DIR/$rel"; say "  ${D}templated:${Z} $rel" ;; esac
  done
done < <(toml_blocks "$MANIFEST" template glob)

# ── 3. record copied paths in this overlay's named exclude block ─────────────
if [ "$DRY" != 1 ] && [ "${#COPIED[@]}" -gt 0 ]; then
  write_exclude_block "$NAME" "${COPIED[@]}"
  say "  ${OKC}✓${Z} recorded ${#COPIED[@]} path(s) in .git/info/exclude (overlay:$NAME)"
fi

# ── 4. symlinks ──────────────────────────────────────────────────────────────
if [ -n "$(toml_blocks "$MANIFEST" symlink link)" ]; then
  say "${B}▸ symlinks${Z}"
  while IFS=$'\t' read -r link target; do
    [ -n "$link" ] && [ -n "$target" ] || continue
    linkp="$(expand_home "$link")"; targetp="$NEXUS_DIR/$target"
    if [ ! -e "$targetp" ]; then warn "skip: $link → $target (target not present)"; continue; fi
    run mkdir -p "$(dirname "$linkp")"; run ln -sfn "$targetp" "$linkp"
    say "  ${OKC}✓${Z} $link → $target"
  done < <(paste <(toml_blocks "$MANIFEST" symlink link) <(toml_blocks "$MANIFEST" symlink target))
fi

# ── 5. env merge into the active profile (never clobber an existing key) ─────
if [ -n "$(toml_blocks "$MANIFEST" env key)" ]; then
  PROFILE="$NEXUS_DIR/.env"
  say "${B}▸ env${Z} ${D}(→ $(basename "$PROFILE"), existing keys kept)${Z}"
  while IFS=$'\t' read -r k v; do
    [ -n "$k" ] || continue
    if [ -f "$PROFILE" ] && grep -q "^${k}=" "$PROFILE" 2>/dev/null; then say "  ${D}kept:${Z} $k"
    else run sh -c "printf '%s=%s\n' \"$k\" \"$v\" >> \"$PROFILE\""; say "  ${OKC}+${Z} $k"; fi
  done < <(paste <(toml_blocks "$MANIFEST" env key) <(toml_blocks "$MANIFEST" env value))
fi

# ── 6. stamp (v2, gitignored) ────────────────────────────────────────────────
if [ "$DRY" != 1 ]; then
  { echo "# agents-nexus overlay — applied"
    echo "# name: $NAME"
    echo "# source: $SRC${REF:+  ref: $REF}"
    echo "# applied_at: $(date +%s)"
    echo "# schema: 2"
    for rel in "${COPIED[@]}"; do echo "$rel"; done
  } > "$(stamp_path "$NAME")"
fi

say ""
say "${OKC}✓ overlay '$NAME' applied.${Z} Re-run the installer's plugin step to pick up any catalog overlay:"
say "  ${D}bash scripts/plugin-install-flow.sh --profile .env${Z}"
say "Inspect: ${D}overlay-apply.sh --status${Z}  ${D}--status $NAME${Z}   Un-apply: ${D}--remove $NAME${Z}"
