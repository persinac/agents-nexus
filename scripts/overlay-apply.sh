#!/usr/bin/env bash
# overlay-apply.sh — snap a private "plugs" overlay repo into this (public) core checkout.
#
# The public agents-nexus core is standalone-capable; every org/personal specific is a
# seam. An OVERLAY is a private repo that fills those seams: it carries a `files/` tree
# (mirroring paths under the core repo root) plus an `overlay.toml` manifest of post-copy
# steps. This script fetches an overlay and layers it in — with ZERO baked-in knowledge of
# what any particular overlay contains (the path list lives only in the overlay repo).
#
# Usage:
#   scripts/overlay-apply.sh <git-url|local-path> [--ref BRANCH] [--dry-run] [--force]
#   scripts/overlay-apply.sh --status            # show the applied overlay + tracked paths
#   scripts/overlay-apply.sh --remove            # un-apply (remove copied files + exclude lines)
#
#   <git-url|local-path>  where the overlay repo lives (cloned/pulled into ./overlay/)
#   --ref BRANCH          overlay branch/tag/sha to checkout (default: the repo's default)
#   --dry-run             print what would be copied/run; touch nothing
#   --force               overwrite core files that already differ (default: skip + warn)
#
# Safety model — why this never leaks a private identifier back into the public export:
#   The export (export-public.sh) is `git archive HEAD` = TRACKED FILES ONLY. Every path an
#   overlay drops is recorded in .git/info/exclude (a per-clone, never-committed ignore list),
#   so overlay files stay UNTRACKED and are invisible to the export. The core repo therefore
#   cannot re-emit overlay content no matter how many times it is exported.
#
# overlay.toml (in the overlay repo root) — minimal TOML this script understands:
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
OVERLAY_DIR="$NEXUS_DIR/overlay"
EXCLUDE="$NEXUS_DIR/.git/info/exclude"
MANIFEST="$OVERLAY_DIR/overlay.toml"
STAMP="$NEXUS_DIR/.overlay-applied"   # gitignored; records applied source + copied paths

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; OKC=$'\033[32m'; WARNC=$'\033[33m'; ERRC=$'\033[31m'; Z=$'\033[0m'
else B=""; D=""; OKC=""; WARNC=""; ERRC=""; Z=""; fi
say(){ printf '%s\n' "$*" >&2; }
die(){ printf '%s\n' "${ERRC}error:${Z} $*" >&2; exit 1; }

SRC=""; REF=""; DRY=0; FORCE=0; ACTION="apply"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref)     REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --force)   FORCE=1; shift ;;
    --status)  ACTION="status"; shift ;;
    --remove)  ACTION="remove"; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)        die "unknown flag: $1" ;;
    *)         SRC="$1"; shift ;;
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
    # newline-terminate each value: _strip emits no trailing \n, so a bare `while read`
    # consumer would never see a lone/last value (and multiples would concatenate).
    case "$line" in *=*) k="$(_strip "${line%%=*}")"; [ "$k" = "$key" ] && { _strip "${line#*=}"; printf '\n'; };; esac
  done < "$f"
}

expand_home(){ case "$1" in "~/"*) printf '%s/%s' "$HOME" "${1#\~/}";; "~") printf '%s' "$HOME";; *) printf '%s' "$1";; esac; }

# ── --status ─────────────────────────────────────────────────────────────────
if [ "$ACTION" = "status" ]; then
  if [ -f "$STAMP" ]; then
    say "${B}overlay applied${Z}"
    sed 's/^/  /' "$STAMP" >&2
  else
    say "${D}no overlay applied${Z}"
  fi
  exit 0
fi

# ── --remove (un-apply: delete copied files + strip our exclude block) ───────
if [ "$ACTION" = "remove" ]; then
  [ -f "$STAMP" ] || { say "${D}nothing to remove (no .overlay-applied stamp)${Z}"; exit 0; }
  while IFS= read -r rel; do
    case "$rel" in "# "*|"") continue ;; esac
    run rm -f "$NEXUS_DIR/$rel"
  done < "$STAMP"
  # strip the marked block from .git/info/exclude
  if [ -f "$EXCLUDE" ]; then
    run python3 - "$EXCLUDE" <<'PY'
import sys
p=sys.argv[1]; lines=open(p).read().split("\n"); out=[]; skip=False
for ln in lines:
    if ln.strip()=="# >>> agents-nexus overlay >>>": skip=True; continue
    if ln.strip()=="# <<< agents-nexus overlay <<<": skip=False; continue
    if not skip: out.append(ln)
open(p,"w").write("\n".join(out))
PY
  fi
  run rm -f "$STAMP"
  say "${OKC}✓${Z} overlay removed (copied files deleted, exclude block stripped)"
  exit 0
fi

# ── apply ─────────────────────────────────────────────────────────────────────
[ -n "$SRC" ] || die "no overlay source. Usage: overlay-apply.sh <git-url|local-path> [--ref B] [--dry-run]"

say "${B}▸ fetch overlay${Z} ${D}($SRC)${Z}"
if [ -d "$OVERLAY_DIR/.git" ]; then
  run git -C "$OVERLAY_DIR" fetch --quiet --all || die "overlay fetch failed"
  [ -n "$REF" ] && run git -C "$OVERLAY_DIR" checkout --quiet "$REF"
  run git -C "$OVERLAY_DIR" pull --quiet --ff-only || say "  ${WARNC}(pull not fast-forward — using current overlay checkout)${Z}"
elif [ -d "$SRC/.git" ] || [ -f "$SRC/overlay.toml" ]; then
  # local path: clone if it's a git repo, else symlink-free copy of the dir
  if [ -d "$SRC/.git" ]; then
    run git clone --quiet ${REF:+--branch "$REF"} "$SRC" "$OVERLAY_DIR" || die "overlay clone failed"
  else
    run mkdir -p "$OVERLAY_DIR"; run cp -R "$SRC/." "$OVERLAY_DIR/"
  fi
else
  run git clone --quiet ${REF:+--branch "$REF"} "$SRC" "$OVERLAY_DIR" || die "overlay clone failed"
fi

# In dry-run with no pre-existing overlay/, we can't read a manifest — describe intent + stop.
if [ "$DRY" = 1 ] && [ ! -f "$MANIFEST" ]; then
  say "  ${D}[dry-run] would clone $SRC → $OVERLAY_DIR, then copy its files/ tree + run overlay.toml steps${Z}"
  exit 0
fi
[ -f "$MANIFEST" ] || die "overlay has no overlay.toml at its root ($MANIFEST)"

FILES_SUB="$(toml_scalar "$MANIFEST" files_dir || echo files)"
FILES_ROOT="$OVERLAY_DIR/$FILES_SUB"
[ -d "$FILES_ROOT" ] || die "overlay files dir not found: $FILES_ROOT (files_dir=\"$FILES_SUB\")"

# ── 1. copy the files/ tree into the core, tracking every path we place ──────
say "${B}▸ layer files${Z} ${D}($FILES_SUB/ → repo root)${Z}"
COPIED=()   # repo-relative paths we placed
SKIPPED=0
while IFS= read -r abs; do
  rel="${abs#"$FILES_ROOT"/}"
  dst="$NEXUS_DIR/$rel"
  if [ -e "$dst" ] && ! cmp -s "$abs" "$dst" 2>/dev/null; then
    if git -C "$NEXUS_DIR" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
      # A TRACKED core file differs — refuse unless --force (protects the neutral templates).
      if [ "$FORCE" = 1 ]; then
        say "  ${WARNC}overwrite (tracked, --force):${Z} $rel"
      else
        say "  ${WARNC}skip (tracked core file differs; --force to overwrite):${Z} $rel"; SKIPPED=$((SKIPPED+1)); continue
      fi
    fi
  fi
  run mkdir -p "$(dirname "$dst")"
  run cp "$abs" "$dst"
  COPIED+=("$rel")
done < <(find "$FILES_ROOT" -type f | LC_ALL=C sort)
say "  ${OKC}✓${Z} ${#COPIED[@]} file(s) placed${SKIPPED:+, $SKIPPED skipped}"

# ── 2. template copied files matching each [[template]].glob (__HOME__/__NODE_BIN__) ──
NODE_BIN="$(dirname "$(command -v node 2>/dev/null || echo /usr/local/bin/node)")"
_tmpl_one(){ # sed a single copied file in place
  local dst="$1"
  run perl -i -pe "s{__HOME__}{$HOME}g; s{__NODE_BIN__}{$NODE_BIN}g" "$dst"
}
while IFS= read -r glob; do
  [ -n "$glob" ] || continue
  say "${B}▸ template${Z} ${D}($glob)${Z}"
  for rel in "${COPIED[@]}"; do
    case "$rel" in $glob) _tmpl_one "$NEXUS_DIR/$rel"; say "  ${D}templated:${Z} $rel" ;; esac
  done
done < <(toml_blocks "$MANIFEST" template glob)

# ── 3. record copied paths in .git/info/exclude (keep the core tree clean) ───
if [ "$DRY" != 1 ] && [ "${#COPIED[@]}" -gt 0 ]; then
  mkdir -p "$(dirname "$EXCLUDE")"; touch "$EXCLUDE"
  # strip any prior overlay block, then re-emit a fresh one
  python3 - "$EXCLUDE" "${COPIED[@]}" <<'PY'
import sys
p=sys.argv[1]; copied=sys.argv[2:]
lines=open(p).read().split("\n"); out=[]; skip=False
for ln in lines:
    if ln.strip()=="# >>> agents-nexus overlay >>>": skip=True; continue
    if ln.strip()=="# <<< agents-nexus overlay <<<": skip=False; continue
    if not skip: out.append(ln)
while out and out[-1]=="": out.pop()
out.append("# >>> agents-nexus overlay >>>")
out.append("# (auto-managed by scripts/overlay-apply.sh — do not edit; `--remove` clears it)")
for rel in copied: out.append("/"+rel)
out.append("# <<< agents-nexus overlay <<<")
open(p,"w").write("\n".join(out)+"\n")
PY
  say "  ${OKC}✓${Z} recorded ${#COPIED[@]} path(s) in .git/info/exclude (tree stays clean)"
fi

# ── 4. symlinks (e.g. repoint ~/.tmux/conductor.yaml → the overlay's config) ─
paste_blocks(){ # emit "link<TAB>target" per [[symlink]] block, pairing in order
  local links targets; links="$(toml_blocks "$MANIFEST" symlink link)"; targets="$(toml_blocks "$MANIFEST" symlink target)"
  paste <(printf '%s\n' "$links") <(printf '%s\n' "$targets")
}
if [ -n "$(toml_blocks "$MANIFEST" symlink link)" ]; then
  say "${B}▸ symlinks${Z}"
  while IFS=$'\t' read -r link target; do
    [ -n "$link" ] && [ -n "$target" ] || continue
    linkp="$(expand_home "$link")"; targetp="$NEXUS_DIR/$target"
    if [ ! -e "$targetp" ]; then say "  ${WARNC}skip:${Z} $link → $target (target not present)"; continue; fi
    run mkdir -p "$(dirname "$linkp")"
    run ln -sfn "$targetp" "$linkp"
    say "  ${OKC}✓${Z} $link → $target"
  done < <(paste_blocks)
fi

# ── 5. env merge into the active profile (never clobber an existing key) ─────
if [ -n "$(toml_blocks "$MANIFEST" env key)" ]; then
  PROFILE="$NEXUS_DIR/.env"
  say "${B}▸ env${Z} ${D}(→ $(basename "$PROFILE"), existing keys kept)${Z}"
  keys="$(toml_blocks "$MANIFEST" env key)"; vals="$(toml_blocks "$MANIFEST" env value)"
  while IFS=$'\t' read -r k v; do
    [ -n "$k" ] || continue
    if [ -f "$PROFILE" ] && grep -q "^${k}=" "$PROFILE" 2>/dev/null; then
      say "  ${D}kept:${Z} $k"
    else
      run sh -c "printf '%s=%s\n' \"$k\" \"$v\" >> \"$PROFILE\""; say "  ${OKC}+${Z} $k"
    fi
  done < <(paste <(printf '%s\n' "$keys") <(printf '%s\n' "$vals"))
fi

# ── 6. stamp (gitignored) so --status / --remove know what was applied ───────
if [ "$DRY" != 1 ]; then
  { echo "# agents-nexus overlay — applied"; echo "# source: $SRC${REF:+  ref: $REF}";
    for rel in "${COPIED[@]}"; do echo "$rel"; done; } > "$STAMP"
fi

say ""
say "${OKC}✓ overlay applied.${Z} Re-run the installer's plugin step to pick up any catalog overlay:"
say "  ${D}bash scripts/plugin-install-flow.sh --profile .env${Z}"
say "Inspect: ${D}scripts/overlay-apply.sh --status${Z}   Un-apply: ${D}scripts/overlay-apply.sh --remove${Z}"
