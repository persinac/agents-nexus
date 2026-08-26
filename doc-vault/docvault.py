#!/usr/bin/env python3
"""doc-vault — index, browse, and search agent-authored HTML docs.

Stdlib only. Subcommands:
    init      write config.json + create the database
    crawl     discover docs under the configured roots and deposit them
    put       deposit a single file (the deposit-point CLI)
    serve     run the browse/search server
    stats     print what the vault holds
    reindex   re-extract text/metadata for everything already deposited
    refile    move a doc to a different collection
    tag       add or remove tags on a doc
    forget    remove a doc from the vault

Two axes, deliberately different in kind:

  collection — a CLOSED, curated vocabulary (see config "collections"). Every doc
               is in exactly one. This is the browsable hierarchy, and it stays
               short enough to scan.
  tags       — an OPEN, namespaced set. A doc has any number. Auto-derived where
               it can be (ticket:, repo:, branch:, mr:, date:) and free-form
               otherwise. This is the cross-cutting axis.

The vault is the durable copy. Crawl and put both COPY the file in, so a doc
survives its origin being deleted, pruned, or rewritten by a worktree cleanup.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from fnmatch import fnmatch
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Code is committed to a public repo; the vault is machine state that must not be.
CODE_DIR = Path(__file__).resolve().parent
_VAULT_ENV = os.environ.get("DOCVAULT_HOME")
VAULT_IS_EXPLICIT = bool(_VAULT_ENV)
VAULT = Path(_VAULT_ENV or "~/doc-vault").expanduser().resolve()
DOCS_DIR = VAULT / "docs"
DB_PATH = VAULT / "index.db"
CONFIG_PATH = VAULT / "config.json"

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "roots": [
        {"path": "~", "depth": 1},
        {"path": "~/investigations", "depth": 3},
        {"path": "~/Downloads", "depth": 1},
        {"path": "~/.claude/mr-walkthrough", "depth": 3},
        {"path": "~/repos", "depth": 4},
        {"path": "~/garner/repos", "depth": 6},
        {"path": "~/obs-garner", "depth": 5},
    ],
    # The closed vocabulary. A doc lands in exactly one of these; anything the
    # rules below cannot place falls to default_collection.
    "collections": ["investigations", "log-sift", "notes", "merge-requests",
                    "ideation", "r&d"],
    "default_collection": "notes",
    # Ordered, first match wins. Each rule keys on exactly one of:
    #   "match" — path substring
    #   "name"  — filename glob, case-insensitive
    #   "title" — document title glob, case-insensitive
    "collection_rules": [
        # Title-keyed, and first: log-sift output is scattered across
        # ~/investigations, ~/Downloads and the log-sift repo itself, so no path
        # or filename rule identifies it. The tool signs its own titles
        # ("Chat <uuid> — log-sift"), which is the only reliable discriminator.
        {"title": "*log-sift", "collection": "log-sift"},
        {"match": "/.claude/mr-walkthrough/", "collection": "merge-requests"},
        {"name": "*walkthrough*", "collection": "merge-requests"},
        {"match": "/ideation/", "collection": "ideation"},
        {"name": "*ideation*", "collection": "ideation"},
        {"match": "/investigations/", "collection": "investigations"},
        {"name": "dossier-sift-*", "collection": "investigations"},
        {"name": "engsup-*", "collection": "investigations"},
        {"match": "/sales-demo-seams/", "collection": "investigations"},
        {"match": "/garner/repos/platform/", "collection": "r&d"},
        {"name": "*eval*report*", "collection": "r&d"},
        {"name": "*-case.html", "collection": "r&d"},
    ],
    # Directory names pruned during the walk. Cheap and cuts the tree hard.
    "prune_dirs": [
        "node_modules", ".git", ".venv", "venv", "site-packages", "__pycache__",
        ".cache", "Library", "dist", "build", "coverage", "htmlcov", ".next",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "templates", "llm_eval",
        "test-reporters", "pwa-manifest", "examples", "static", ".terraform",
        "target", "vendor", ".idea", ".gradle",
    ],
    # Substrings that reject a path outright.
    "deny_path": [
        "/.claude/plugins/",
        "/dashboard/ui/",
        "/report/",
        "/Google Drive/",
        "/tests/data/",
        "/test/fixtures/",
    ],
    # Filename globs (matched case-insensitively) that reject a file outright.
    "deny_name": [
        "index.html", "report.html", "test-report.html", "eval_report_*.html",
        "template.html", "*.template.html", "*_template.html", "*-template.html",
        "*_base.html", "*-base.html",
        "workflow-viewer.html", "exports_dashboard*.html",
        "splash.html", "icon.html",
    ],
    # Paths always accepted, skipping the content score.
    "always_allow": [],
    # The junk observed on this machine was 0B / 403B / 921B; real docs start
    # around 30KB. 4KB clears the junk with room for a genuinely short brief.
    "min_bytes": 4096,
    "max_bytes": 8_388_608,
    "score_threshold": 5,
    # Seconds a file must be untouched before the watch loop will capture it.
    "settle_seconds": 45,
    "port": 8310,
    # See the "cloudflare access" section. Disabled means `serve` behaves
    # exactly as it always has: one unauthenticated loopback listener.
    "access": {},
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


# ---------------------------------------------------------------------------
# cloudflare access
# ---------------------------------------------------------------------------
# The tunnel must point at access["port"], never cfg["port"]: cfg["port"] is
# ungated, and cloudflared connects from localhost so no source check can tell
# the two apart. Design rationale and the stdlib-crypto tradeoff are in README.


class AccessDenied(Exception):
    """Request carried no usable Access identity. Always answered as 403."""


DEFAULT_ACCESS = {
    "enabled": False,
    "team": "",
    # Without this, a JWT minted for ANY application in the team verifies here.
    "aud": "",
    "allow": [],
    "port": 8311,
    "certs_url": "",
    "leeway": 60,
    # Extra root added on top of the system trust for the JWKS fetch only.
    # Needed where WARP/Gateway re-signs outbound TLS: python's CA store is not
    # the macOS keychain, so the fetch fails and the gate 403s every request.
    "ca_bundle": "",
}


def access_config(cfg: dict) -> dict:
    """Merge the access block over its defaults (load_config is a shallow update)."""
    acfg = dict(DEFAULT_ACCESS)
    acfg.update(cfg.get("access") or {})
    return acfg


def access_misconfig(acfg: dict) -> list[str]:
    """Names of settings that must be present before the gated port may open."""
    missing = [k for k in ("team", "aud") if not str(acfg.get(k) or "").strip()]
    if not acfg.get("allow"):
        missing.append("allow")
    return missing


def _certs_url(acfg: dict) -> str:
    if acfg["certs_url"]:
        return acfg["certs_url"]
    return f"https://{acfg['team']}.cloudflareaccess.com/cdn-cgi/access/certs"


def _issuer(acfg: dict) -> str:
    return f"https://{acfg['team']}.cloudflareaccess.com"


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class _JwksCache:
    """The team's published signing keys, refreshed when a kid is unrecognised."""

    TTL = 3600
    # Floor between fetches, so a stream of bogus kids cannot become a fetch loop.
    MIN_REFRESH = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched = 0.0

    def _fetch(self, url: str, ca_bundle: str = "") -> None:
        ctx = ssl.create_default_context()
        if ca_bundle:
            ctx.load_verify_locations(cafile=os.path.expanduser(ca_bundle))
        with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
            body = json.loads(resp.read().decode())
        keys: dict[str, tuple[int, int]] = {}
        for k in body.get("keys", []):
            if k.get("kty") != "RSA" or not k.get("kid"):
                continue
            try:
                n = int.from_bytes(_b64u_decode(k["n"]), "big")
                e = int.from_bytes(_b64u_decode(k["e"]), "big")
            except Exception:
                continue
            if n and e:
                keys[k["kid"]] = (n, e)
        if not keys:
            raise AccessDenied("certs endpoint published no usable RSA keys")
        self._keys = keys
        self._fetched = time.time()

    def get(self, kid: str, url: str, ca_bundle: str = "") -> tuple[int, int]:
        with self._lock:
            now = time.time()
            need = kid not in self._keys or (now - self._fetched) > self.TTL
            if need and (not self._keys or (now - self._fetched) > self.MIN_REFRESH):
                try:
                    self._fetch(url, ca_bundle)
                except AccessDenied:
                    raise
                except Exception as exc:
                    raise AccessDenied(f"cannot reach certs endpoint: {exc!r}") from exc
            if kid not in self._keys:
                raise AccessDenied("token kid is not published by the team")
            return self._keys[kid]


_JWKS = _JwksCache()

# DigestInfo(SHA-256) DER prefix, RFC 8017 section 9.2 note 1.
_SHA256_DER = bytes.fromhex("3031300d060960864801650304020105000420")


def _rsa_sha256_verify(n: int, e: int, sig: bytes, signed: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    t = _SHA256_DER + hashlib.sha256(signed).digest()
    if k < len(t) + 11:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify_access_jwt(token: str, acfg: dict) -> dict:
    """Return the token's claims, or raise AccessDenied. Fails closed throughout."""
    if not str(acfg.get("aud") or "").strip():
        raise AccessDenied("access.aud is not configured")

    parts = token.split(".")
    if len(parts) != 3:
        raise AccessDenied("malformed token")
    try:
        header = json.loads(_b64u_decode(parts[0]))
        claims = json.loads(_b64u_decode(parts[1]))
        sig = _b64u_decode(parts[2])
    except Exception as exc:
        raise AccessDenied(f"undecodable token: {exc!r}") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AccessDenied("token header or payload is not an object")

    # Pinned, not read from the header: "none" would skip verification and
    # "HS256" would make us treat a public key as an HMAC secret.
    if header.get("alg") != "RS256":
        raise AccessDenied(f"unexpected alg {header.get('alg')!r}")
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise AccessDenied("token has no kid")

    n, e = _JWKS.get(kid, _certs_url(acfg), acfg.get("ca_bundle") or "")
    if not _rsa_sha256_verify(n, e, sig, f"{parts[0]}.{parts[1]}".encode()):
        raise AccessDenied("signature does not verify")

    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if acfg["aud"] not in auds:
        raise AccessDenied("token was minted for a different Access application")
    if claims.get("iss") != _issuer(acfg):
        raise AccessDenied(f"unexpected issuer {claims.get('iss')!r}")

    now = time.time()
    leeway = int(acfg.get("leeway") or 0)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise AccessDenied("token has no usable exp")
    if now > exp + leeway:
        raise AccessDenied("token has expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and not isinstance(nbf, bool) and now < nbf - leeway:
        raise AccessDenied("token is not valid yet")

    allow = {str(a).strip().lower() for a in (acfg.get("allow") or [])}
    if not allow:
        raise AccessDenied("access.allow is empty")
    email = str(claims.get("email") or "").strip().lower()
    if email not in allow:
        raise AccessDenied(f"{email or '<no email claim>'} is not on the allowlist")
    return claims


def access_token_from_headers(headers) -> str:
    """The JWT Access puts on a proxied request: header first, then its cookie."""
    tok = headers.get("Cf-Access-Jwt-Assertion")
    if tok and tok.strip():
        return tok.strip()
    for part in (headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "CF_Authorization" and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    title_key    TEXT NOT NULL,
    lede         TEXT NOT NULL DEFAULT '',
    collection   TEXT NOT NULL DEFAULT 'notes',
    vault_name   TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    origin_mtime TEXT NOT NULL,
    added_at     TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'crawl'
);
CREATE TABLE IF NOT EXISTS origins (
    doc_id  INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    path    TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (doc_id, path)
);
CREATE TABLE IF NOT EXISTS tags (
    doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    tag    TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_docs_collection ON docs(collection);
CREATE INDEX IF NOT EXISTS idx_docs_title_key ON docs(title_key);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, lede, body, tokenize='porter unicode61'
);
"""


def db() -> sqlite3.Connection:
    # sqlite reports a missing dir as "unable to open database file", which
    # reads like corruption rather than a wrong DOCVAULT_HOME.
    if not VAULT.is_dir():
        raise RuntimeError(
            f"doc-vault data dir does not exist: {VAULT} "
            f"(set DOCVAULT_HOME, or run `docvault.py init` to create it)")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_vault() -> None:
    """Create the data dir on first run, but never silently for an explicit path.

    init_db() creates the whole tree, so a typo'd DOCVAULT_HOME would otherwise
    yield an empty vault that looks exactly like having lost every doc.
    """
    if VAULT.is_dir():
        return
    if VAULT_IS_EXPLICIT:
        sys.exit(f"doc-vault: DOCVAULT_HOME={VAULT} does not exist.\n"
                 f"  Create it deliberately if that is really where the vault should live, "
                 f"or unset DOCVAULT_HOME to use ~/doc-vault.")
    VAULT.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_vault()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return local_iso(time.time())


def local_iso(epoch: float) -> str:
    """Naive LOCAL-time ISO string.

    Deliberately not UTC. Every consumer of these timestamps is a human in one
    timezone, and UTC crosses the day boundary at 17:00-18:00 local: a doc
    written at 23:06 on the 24th would be stamped and tagged date:20260825,
    which is simply the wrong answer to "when did I write this".
    """
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "td", "th", "pre", "blockquote", "figcaption",
}


class TextExtractor(HTMLParser):
    """Pull the title and visible prose out of an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._title_done = False
        self._skip_depth = 0
        self._chunks: list[str] = []
        self._headings: list[str] = []
        self._in_heading = False
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title" and self._skip_depth == 0 and not self._title_done:
            # Only the document <title>. Inline SVG diagrams carry their own
            # <title> accessibility labels, which would otherwise be appended.
            self._in_title = True
        elif tag in {"h1", "h2", "p"} and self._skip_depth == 0:
            self._in_heading = tag in {"h1", "h2"}
            self._heading_buf = []
        if tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag in {"h1", "h2"} and self._in_heading:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self._headings.append(text)
            self._in_heading = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self._chunks.append(data)
            if self._in_heading:
                self._heading_buf.append(data)

    @property
    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(ln.split()) for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)

    @property
    def headings(self) -> list[str]:
        return self._headings


GENERIC_TITLES = {
    "", "document", "index", "untitled", "html", "page", "test", "report",
    "react app", "vite app", "app",
}

# An emitted-but-unfilled `doc-vault theme` skeleton. The watch loop caught one
# of these mid-authoring once and indexed "TITLE — replace me" as a real doc,
# leaving a stale twin of the finished page under the same origin path. A hard
# reject, not a score penalty: a skeleton otherwise scores full marks, because
# structurally it IS a well-formed document.
SKELETON_TITLE_RE = re.compile(r"replace me|^title\b.*—", re.I)

TICKET_RE = re.compile(r"\b((?:FC|ENGSUP|PLAT|DATA|SEC|OPS)-\d{2,6})\b")

# An unrendered template names itself with its own placeholder: the observed ones
# titled themselves "__TITLE__", "Team Performance — Week of __WEEKLABEL__" and
# "${npi} ${specialty} ${ggr} Diag".
#
# Deliberately checked against the TITLE ONLY. Scanning the body for the same
# markers is worse than useless here: a review deck quotes real source code, so
# `${...}` and `{{...}}` show up legitimately, and the decks emit their code as
# `<div class="ln"><span class="si">` token soup rather than <pre>/<code>, so
# there is no reliable region to exclude. Every real template on this machine is
# caught by filename (see deny_name) and again by this title check.
TITLE_TEMPLATE_RE = re.compile(r"\{\{|\{%|<%|\$\{|__[A-Z][A-Z0-9_]{2,}__")


def slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return out or "untitled"


def extract(raw_bytes: bytes) -> dict:
    """Parse a document into the fields the index needs."""
    text_html = raw_bytes.decode("utf-8", errors="replace")
    parser = TextExtractor()
    try:
        parser.feed(text_html)
        parser.close()
    except Exception:
        pass  # malformed HTML still yields whatever was parsed before the error

    title = " ".join(parser.title.split())
    body = parser.text

    if not title and parser.headings:
        title = parser.headings[0]

    # Lede: first line of prose long enough to be a sentence, skipping any
    # line that merely repeats the title.
    lede = ""
    for line in body.splitlines():
        if len(line) < 40:
            continue
        if title and line.lower().startswith(title.lower()[:24]):
            continue
        lede = line[:280]
        break
    if not lede:
        lede = " ".join(body.split())[:280]

    return {
        "title": title,
        "body": body,
        "lede": lede,
        "tickets": sorted(set(TICKET_RE.findall(text_html))),
        "headings": parser.headings[:40],
        "html": text_html,
    }


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def glob_hit(name: str, patterns: list[str]) -> str | None:
    low = name.lower()
    for pat in patterns:
        if fnmatch(low, pat.lower()):
            return pat
    return None


def classify(path: Path, raw: bytes, meta: dict, cfg: dict) -> tuple[bool, int, list[str]]:
    """Score a candidate. Returns (accepted, score, reasons)."""
    reasons: list[str] = []
    spath = str(path)

    for allow in cfg.get("always_allow", []):
        if os.path.expanduser(allow) in spath:
            return True, 99, ["always_allow"]

    for deny in cfg["deny_path"]:
        if deny in spath:
            return False, 0, [f"deny_path {deny}"]

    hit = glob_hit(path.name, cfg["deny_name"])
    if hit:
        return False, 0, [f"deny_name {hit}"]

    if SKELETON_TITLE_RE.search(meta["title"] or ""):
        return False, 0, ["unfilled theme skeleton"]

    size = len(raw)
    if size < cfg["min_bytes"]:
        return False, 0, [f"too small ({size}B)"]
    if size > cfg["max_bytes"]:
        return False, 0, [f"too large ({size}B)"]

    score = 0
    title = meta["title"]
    if title and title.strip().lower() not in GENERIC_TITLES:
        score += 2
        reasons.append("+2 real title")
    else:
        reasons.append("+0 generic/missing title")

    score += 2
    reasons.append("+2 size in range")

    lowered = meta["html"].lower()

    style_len = sum(len(m) for m in re.findall(r"<style\b[^>]*>(.*?)</style>", lowered, re.S))
    if style_len >= 500:
        score += 1
        reasons.append("+1 inline stylesheet")

    text_len = len(meta["body"])
    if text_len >= 1500:
        score += 2
        reasons.append(f"+2 prose ({text_len}c)")
    elif text_len < 400:
        score -= 2
        reasons.append(f"-2 almost no prose ({text_len}c)")

    if len(meta["headings"]) >= 3:
        score += 1
        reasons.append("+1 has section headings")

    tpl = TITLE_TEMPLATE_RE.findall(title)
    if tpl:
        score -= 6
        reasons.append(f"-6 unrendered placeholder in title {tpl[:2]}")

    # An app shell pulls its logic from a local bundle; an authored doc inlines
    # everything it needs.
    local_scripts = re.findall(r"<script[^>]+src=[\"'](?!https?:)([^\"']+)", lowered)
    if local_scripts:
        score -= 3
        reasons.append(f"-3 local script bundle ({local_scripts[0][:40]})")

    return score >= cfg["score_threshold"], score, reasons


# ---------------------------------------------------------------------------
# collection + tags
# ---------------------------------------------------------------------------

def resolve_collection(path: Path, cfg: dict, title: str = "") -> str:
    """Place a doc in the closed collection vocabulary. First rule wins."""
    spath = str(path)
    low_title = (title or "").strip().lower()
    for rule in cfg.get("collection_rules", []):
        if "match" in rule and rule["match"] in spath:
            return rule["collection"]
        if "name" in rule and fnmatch(path.name.lower(), rule["name"].lower()):
            return rule["collection"]
        if "title" in rule and low_title and fnmatch(low_title, rule["title"].lower()):
            return rule["collection"]
    return cfg.get("default_collection", "notes")


# Garner worktrees are named "<group>_<repo>--<branch>", e.g.
# "search_concierge_svc-chatbot--amex-demo".
WORKTREE_RE = re.compile(r"^(?P<base>.+?)(?:--(?P<branch>.+))?$")
# Walkthrough decks title themselves "<repo>!<number> walkthrough".
MR_TITLE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]*)!(\d+)")
DATE_DASHED_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
DATE_COMPACT_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def find_repo(path: Path) -> tuple[str | None, str | None]:
    """Best-effort (repo, branch) for a path. Either may be None."""
    parts = path.parts
    if ".worktrees" in parts:
        i = parts.index(".worktrees")
        if i + 1 < len(parts):
            m = WORKTREE_RE.match(parts[i + 1])
            base = m.group("base") if m else parts[i + 1]
            branch = m.group("branch") if m else None
            # "search_concierge_svc-chatbot" -> "svc-chatbot"
            return base.split("_")[-1], branch

    for parent in path.parents:
        if (parent / ".git").exists():
            return parent.name, None
    return None, None


def derive_tags(path: Path, meta: dict, title: str, mtime_iso: str) -> list[str]:
    """Auto-derived, namespaced tags. Free-form tags are added separately."""
    tags: set[str] = set()

    for key in meta.get("tickets", []):
        tags.add(f"ticket:{key}")

    repo, branch = find_repo(path)

    # A walkthrough deck names its repo and MR in the title even when it lives
    # outside any repo (they are written to ~/.claude/mr-walkthrough/).
    m = MR_TITLE_RE.match(title.strip().lower())
    if m:
        repo = repo or m.group(1)
        tags.add(f"mr:{m.group(2)}")

    if repo:
        tags.add(f"repo:{repo}")
    if branch:
        tags.add(f"branch:{branch}")

    # Prefer a date stated in the filename over the file's mtime: a doc copied
    # or rebased carries a new mtime but still describes its original date.
    dm = DATE_DASHED_RE.search(path.name) or DATE_COMPACT_RE.search(path.name)
    if dm:
        tags.add(f"date:{dm.group(1)}{dm.group(2)}{dm.group(3)}")
    elif mtime_iso:
        tags.add(f"date:{mtime_iso[:10].replace('-', '')}")

    return sorted(tags)


TAG_ORDER = {"ticket": 0, "mr": 1, "repo": 2, "branch": 3, "date": 5}


def tag_sort_key(tag: str) -> tuple[int, str]:
    ns = tag.split(":", 1)[0] if ":" in tag else ""
    return (TAG_ORDER.get(ns, 4), tag)


# ---------------------------------------------------------------------------
# deposit
# ---------------------------------------------------------------------------

def deposit(path: Path, cfg: dict, *, source: str = "crawl",
            collection: str | None = None, tags: list[str] | None = None,
            title: str | None = None, conn: sqlite3.Connection | None = None
            ) -> tuple[str, int | None, str]:
    """Copy a file into the vault and index it.

    Returns (status, doc_id, title) where status is added | duplicate.
    """
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    meta = extract(raw)
    doc_title = title or meta["title"] or path.stem.replace("-", " ").title()
    coll = collection or resolve_collection(path, cfg, doc_title)
    mtime = local_iso(path.stat().st_mtime)
    all_tags = sorted(set(derive_tags(path, meta, doc_title, mtime) + list(tags or [])))

    own_conn = conn is None
    conn = conn or db()
    try:
        row = conn.execute("SELECT id FROM docs WHERE content_hash = ?", (content_hash,)).fetchone()
        if row:
            doc_id = row["id"]
            conn.execute(
                "INSERT OR REPLACE INTO origins (doc_id, path, seen_at) VALUES (?,?,?)",
                (doc_id, str(path), now_iso()),
            )
            # A second location can contribute tags the first one could not.
            conn.executemany("INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?,?)",
                             [(doc_id, t) for t in all_tags])
            conn.commit()
            return "duplicate", doc_id, doc_title

        vault_name = f"{slugify(doc_title)[:60]}-{content_hash[:8]}.html"
        shutil.copy2(path, DOCS_DIR / vault_name)

        cur = conn.execute(
            """INSERT INTO docs (content_hash, title, title_key, lede, collection,
                                 vault_name, byte_size, origin_mtime, added_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (content_hash, doc_title, slugify(doc_title), meta["lede"], coll,
             vault_name, len(raw), mtime, now_iso(), source),
        )
        doc_id = cur.lastrowid
        conn.execute(
            "INSERT OR REPLACE INTO origins (doc_id, path, seen_at) VALUES (?,?,?)",
            (doc_id, str(path), now_iso()),
        )
        conn.executemany("INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?,?)",
                         [(doc_id, t) for t in all_tags])
        conn.execute(
            "INSERT INTO docs_fts (rowid, title, lede, body) VALUES (?,?,?,?)",
            (doc_id, doc_title, meta["lede"], meta["body"]),
        )
        conn.commit()
        return "added", doc_id, doc_title
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------

def walk_root(root: Path, max_depth: int, prune: set[str]):
    root = root.resolve()
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        depth = len(here.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in prune]
        for name in filenames:
            if name.endswith((".html", ".htm")):
                yield here / name


def cmd_crawl(args) -> int:
    cfg = load_config()
    init_db()
    prune = set(cfg["prune_dirs"])

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in cfg["roots"]:
        rpath = Path(os.path.expanduser(root["path"]))
        if not rpath.exists():
            print(f"  skip (missing): {rpath}")
            continue
        for f in walk_root(rpath, int(root["depth"]), prune):
            rf = f.resolve()
            if rf not in seen:
                seen.add(rf)
                candidates.append(f)

    accepted: list[tuple[Path, int, str]] = []
    rejected: list[tuple[Path, str]] = []

    for path in sorted(candidates):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            rejected.append((path, f"unreadable: {exc}"))
            continue
        meta = extract(raw)
        ok, score, reasons = classify(path, raw, meta, cfg)
        if ok:
            accepted.append((path, score, meta["title"]))
        else:
            decisive = next((r for r in reasons if r.startswith(("-", "deny", "too", "unread"))),
                            None)
            rejected.append((path, decisive or f"score {score} < {cfg['score_threshold']}"))

    home = str(Path.home())

    def short(p) -> str:
        return str(p).replace(home, "~")

    print(f"\nScanned {len(candidates)} HTML files under {len(cfg['roots'])} roots.")
    print(f"  accepted: {len(accepted)}   rejected: {len(rejected)}\n")

    if args.dry_run:
        print("ACCEPTED (dry run — nothing deposited)")
        for path, score, title in accepted:
            coll = resolve_collection(path, cfg, title)
            print(f"  [{score:>2}] {coll:<15} {title[:44]:<44}  {short(path)}")
        if args.verbose:
            print("\nREJECTED")
            for path, why in rejected:
                print(f"  {why[:44]:<44}  {short(path)}")
        else:
            print(f"\n({len(rejected)} rejected — rerun with --verbose to audit)")
        return 0

    counts = {"added": 0, "duplicate": 0}
    with db() as conn:
        for path, _score, _title in accepted:
            status, _doc_id, title = deposit(path, cfg, source="crawl", conn=conn)
            counts[status] = counts.get(status, 0) + 1
            marker = "+" if status == "added" else "="
            coll = resolve_collection(path, cfg, title)
            print(f"  {marker} {coll:<15} {title[:46]:<46} {short(path)}")

    print(f"\nDeposited {counts['added']} new, {counts['duplicate']} already present "
          f"(deduped by content hash).")
    if rejected and args.verbose:
        print("\nREJECTED")
        for path, why in rejected:
            print(f"  {why[:44]:<44}  {short(path)}")
    elif rejected:
        print(f"{len(rejected)} rejected — rerun with --dry-run --verbose to audit.")
    return 0


# ---------------------------------------------------------------------------
# put / refile / tag / stats / reindex / forget
# ---------------------------------------------------------------------------

def check_collection(coll: str, cfg: dict) -> str:
    """Collections are a closed vocabulary; a typo must not invent a bucket."""
    if coll not in cfg["collections"]:
        raise SystemExit(
            f"error: '{coll}' is not a collection.\n"
            f"  choose one of: {', '.join(cfg['collections'])}\n"
            f"  (edit \"collections\" in {CONFIG_PATH.name} to add one)"
        )
    return coll


def cmd_put(args) -> int:
    cfg = load_config()
    init_db()
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 1

    coll = check_collection(args.collection, cfg) if args.collection else None
    tags = [t.strip() for t in (args.tag or []) if t.strip()]
    tags += [f"ticket:{t.strip().upper()}" for t in (args.ticket or []) if t.strip()]

    status, doc_id, title = deposit(path, cfg, source="put", collection=coll,
                                    tags=tags, title=args.title)
    print(f"{status}: {title}")
    print(f"  http://localhost:{cfg['port']}/doc/{doc_id}")
    if args.move and status == "added":
        path.unlink()
        print(f"  removed origin {path}")
    return 0


def cmd_refile(args) -> int:
    cfg = load_config()
    coll = check_collection(args.collection, cfg)
    with db() as conn:
        row = conn.execute("SELECT title FROM docs WHERE id = ?", (args.doc_id,)).fetchone()
        if not row:
            print(f"no doc {args.doc_id}", file=sys.stderr)
            return 1
        conn.execute("UPDATE docs SET collection = ? WHERE id = ?", (coll, args.doc_id))
        conn.commit()
    print(f"{row['title']} -> {coll}")
    return 0


def cmd_tag(args) -> int:
    added, removed = [], []
    with db() as conn:
        row = conn.execute("SELECT title FROM docs WHERE id = ?", (args.doc_id,)).fetchone()
        if not row:
            print(f"no doc {args.doc_id}", file=sys.stderr)
            return 1
        for raw in args.tags:
            tag = raw[1:] if raw.startswith("+") else raw
            if not tag:
                continue
            conn.execute("INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?,?)",
                         (args.doc_id, tag))
            added.append(tag)
        for raw in (args.rm or []):
            conn.execute("DELETE FROM tags WHERE doc_id = ? AND tag = ?", (args.doc_id, raw))
            removed.append(raw)
        conn.commit()
    print(f"{row['title']}")
    if added:
        print(f"  + {' '.join(added)}")
    if removed:
        print(f"  - {' '.join(removed)}")
    return 0


def cmd_stats(args) -> int:
    cfg = load_config()
    init_db()
    with db() as conn:
        total, size = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_size),0) FROM docs").fetchone()
        print(f"{total} docs, {size / 1024:.0f} KB indexed\n")

        print("collections")
        counts = {r["collection"]: r["n"] for r in conn.execute(
            "SELECT collection, COUNT(*) n FROM docs GROUP BY 1")}
        for c in cfg["collections"]:
            print(f"  {counts.pop(c, 0):>3}  {c}")
        for c, n in sorted(counts.items()):
            print(f"  {n:>3}  {c}   (not in the configured vocabulary)")

        print("\ntags by namespace")
        ns_counts: dict[str, int] = {}
        for r in conn.execute("SELECT tag FROM tags"):
            ns = r["tag"].split(":", 1)[0] if ":" in r["tag"] else "(free)"
            ns_counts[ns] = ns_counts.get(ns, 0) + 1
        for ns, n in sorted(ns_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3}  {ns}")

        print("\ntop tags")
        for r in conn.execute(
                "SELECT tag, COUNT(*) n FROM tags GROUP BY 1 ORDER BY n DESC, tag LIMIT 18"):
            print(f"  {r['n']:>3}  {r['tag']}")

        dupes = conn.execute(
            """SELECT d.title, COUNT(o.path) n FROM docs d JOIN origins o ON o.doc_id = d.id
               GROUP BY d.id HAVING n > 1 ORDER BY n DESC"""
        ).fetchall()
        if dupes:
            print("\nsame content in multiple places")
            for r in dupes:
                print(f"  {r['n']:>3}  {r['title']}")
    return 0


def cmd_reindex(args) -> int:
    """Re-derive text, collection and auto-tags for everything deposited.

    Uses each doc's recorded origins, so path-derived tags come back. Free-form
    tags (anything not in a known auto namespace) are preserved.
    """
    cfg = load_config()
    init_db()
    auto_ns = ("ticket:", "repo:", "branch:", "mr:", "date:")
    n = 0
    with db() as conn:
        for row in conn.execute("SELECT id, vault_name, title FROM docs").fetchall():
            f = DOCS_DIR / row["vault_name"]
            if not f.exists():
                continue
            meta = extract(f.read_bytes())
            origins = [r["path"] for r in conn.execute(
                "SELECT path FROM origins WHERE doc_id = ? ORDER BY path", (row["id"],))]

            conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row["id"],))
            conn.execute("INSERT INTO docs_fts (rowid, title, lede, body) VALUES (?,?,?,?)",
                         (row["id"], row["title"], meta["lede"], meta["body"]))
            conn.execute("UPDATE docs SET lede = ? WHERE id = ?", (meta["lede"], row["id"]))

            if origins:
                primary = Path(origins[0])
                conn.execute("UPDATE docs SET collection = ? WHERE id = ?",
                             (resolve_collection(primary, cfg, row["title"]), row["id"]))
                for ns in auto_ns:
                    conn.execute("DELETE FROM tags WHERE doc_id = ? AND tag LIKE ?",
                                 (row["id"], ns + "%"))
                fresh: set[str] = set()
                for o in origins:
                    op = Path(o)
                    mt = local_iso(op.stat().st_mtime) if op.exists() else ""
                    fresh.update(derive_tags(op, meta, row["title"], mt))
                conn.executemany("INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?,?)",
                                 [(row["id"], t) for t in sorted(fresh)])
            n += 1
        conn.commit()
    print(f"reindexed {n} docs")
    return 0


def cmd_forget(args) -> int:
    with db() as conn:
        row = conn.execute("SELECT vault_name, title FROM docs WHERE id = ?",
                           (args.doc_id,)).fetchone()
        if not row:
            print(f"no doc {args.doc_id}", file=sys.stderr)
            return 1
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (args.doc_id,))
        conn.execute("DELETE FROM docs WHERE id = ?", (args.doc_id,))
        conn.commit()
    f = DOCS_DIR / row["vault_name"]
    if f.exists():
        f.unlink()
    print(f"forgot: {row['title']}")
    return 0


THEME_DIR = CODE_DIR / "theme"
THEME_CSS = THEME_DIR / "garner-doc.css"
THEME_SKELETON = THEME_DIR / "skeleton.html"
THEME_MARKER = 'name="doc-theme"'
THEME_VERSION = "garner-doc/1"


def cmd_theme(args) -> int:
    """Emit a self-contained on-theme skeleton, or report which docs are on it.

    The CSS lives in exactly one file and is inlined at emit time. Docs have to
    be self-contained — they get opened as bare files, not only through the
    server — so an external <link> to the vault would render unstyled anywhere
    else, and a second hand-maintained copy would drift.
    """
    if args.check:
        init_db()
        with db() as conn:
            rows = conn.execute(
                "SELECT id, title, collection, vault_name FROM docs "
                "ORDER BY collection, id").fetchall()
        on, off = [], []
        for r in rows:
            f = DOCS_DIR / r["vault_name"]
            if not f.exists():
                continue
            head = f.read_text(errors="replace")[:4096]
            (on if THEME_MARKER in head else off).append(r)
        print(f"on theme:  {len(on)}")
        for r in on:
            print(f"  {r['id']:>3}  {r['collection']:<15} {r['title'][:52]}")
        print(f"\nbespoke:   {len(off)}")
        for r in off:
            print(f"  {r['id']:>3}  {r['collection']:<15} {r['title'][:52]}")
        print("\nExisting docs are NOT retrofitted. Their class names are their own"
              "\n(58 in one, 8 in another), so a shared stylesheet cannot reach them."
              "\nThe theme applies to docs written from here on.")
        return 0

    for f in (THEME_CSS, THEME_SKELETON):
        if not f.exists():
            print(f"error: missing {f}", file=sys.stderr)
            return 1

    css = THEME_CSS.read_text().rstrip()
    skeleton = THEME_SKELETON.read_text()
    if "/*{{CSS}}*/" not in skeleton:
        print(f"error: {THEME_SKELETON.name} has no /*{{{{CSS}}}}*/ placeholder",
              file=sys.stderr)
        return 1

    if args.wrap:
        # Wrap a body fragment (the <div class="shell">…</div> content) in the
        # canonical head. Lets an author write only content and never hand-copy
        # the stylesheet, which is the one way two copies start to drift.
        frag_path = Path(args.wrap).expanduser()
        if not frag_path.is_file():
            print(f"error: no such fragment: {frag_path}", file=sys.stderr)
            return 1
        head = skeleton.partition("<body>")[0]
        head = head.replace("/*{{CSS}}*/", css)
        if args.doc_title:
            head = re.sub(r"<title>.*?</title>",
                          f"<title>{escape(args.doc_title)}</title>", head, count=1, flags=re.S)
        out = head + "<body>\n" + frag_path.read_text().strip() + "\n</body>\n</html>\n"
    else:
        out = skeleton.replace("/*{{CSS}}*/", css)

    if args.out:
        dest = Path(args.out).expanduser()
        if dest.exists() and not args.force:
            print(f"error: {dest} exists (pass --force to overwrite)", file=sys.stderr)
            return 1
        dest.write_text(out)
        print(f"wrote {dest}  ({len(out)} bytes, {THEME_VERSION})")
        print("Fill in the sections, then:")
        print(f"  doc-vault put {dest} --collection notes --tag <topic>")
    else:
        sys.stdout.write(out)
    return 0


def cmd_init(args) -> int:
    init_db()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        print(f"wrote {CONFIG_PATH}")
    else:
        print(f"config already exists: {CONFIG_PATH}")
    print(f"database ready: {DB_PATH}")
    return 0


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root{
  --bg:#fbfaf8; --panel:#ffffff; --ink:#1a1a1a; --muted:#6b6b6b;
  --line:#e4e1dc; --accent:#8b5e34; --accent-soft:#f2e9e0;
  --mark:#ffe9a8; --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.05);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#14140f; --panel:#1c1c18; --ink:#eceae4; --muted:#9a968c;
    --line:#2e2e28; --accent:#d0a678; --accent-soft:#2a231b;
    --mark:#5c4a1c; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35);
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
header.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px 28px;
  display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.brand{font-family:var(--mono);font-size:13px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);white-space:nowrap}
.brand b{color:var(--accent);font-weight:600}
form.search{flex:1;min-width:240px;display:flex}
input[type=search]{flex:1;padding:9px 14px;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);color:var(--ink);font-size:14px;font-family:var(--sans);
  box-shadow:var(--shadow)}
input[type=search]:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
.count{font-family:var(--mono);font-size:12px;color:var(--muted);white-space:nowrap}
nav.colls{display:flex;gap:6px;flex-wrap:wrap;padding:10px 28px 0;max-width:1140px;margin:0 auto}
nav.colls a{font-family:var(--mono);font-size:12px;padding:4px 10px;border-radius:6px;
  border:1px solid var(--line);color:var(--muted)}
nav.colls a:hover,nav.colls a.on{border-color:var(--accent);color:var(--accent);
  background:var(--accent-soft)}
nav.colls a .n{opacity:.6}
main{max-width:1140px;margin:0 auto;padding:20px 28px 60px}
h2.coll{font-family:var(--mono);font-size:12px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--accent);margin:34px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;align-items:baseline}
h2.coll:first-of-type{margin-top:10px}
h2.coll .n{color:var(--muted);font-size:11px;letter-spacing:.08em}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:15px 17px;box-shadow:var(--shadow);transition:transform .12s,border-color .12s}
.card:hover,.card:focus-visible{transform:translateY(-2px);border-color:var(--accent);outline:none}
.card h3{margin:0 0 6px;font-size:15px;font-weight:600;line-height:1.35}
.card p{margin:0;font-size:13px;color:var(--muted);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.meta{margin-top:11px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;
  font-family:var(--mono);font-size:11px;color:var(--muted)}
.chip{background:var(--accent-soft);color:var(--accent);border-radius:5px;padding:2px 7px;
  font-family:var(--mono);font-size:11px;font-weight:600;white-space:nowrap}
.chip.dim{background:transparent;border:1px solid var(--line);color:var(--muted);font-weight:400}
a.chip:hover{outline:1px solid var(--accent)}
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 2px}
.empty{color:var(--muted);padding:60px 0;text-align:center}
.empty code{font-family:var(--mono);background:var(--panel);border:1px solid var(--line);
  padding:2px 6px;border-radius:5px}
.tagcloud{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 30px}
.tagcloud a{font-family:var(--mono);font-size:12px;padding:4px 9px;border-radius:6px;
  border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.tagcloud a:hover{border-color:var(--accent);color:var(--accent)}
.tagcloud a b{color:var(--ink);font-weight:600}
.viewer-bar{position:sticky;top:0;z-index:10;background:var(--panel);border-bottom:1px solid var(--line);
  padding:11px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.viewer-bar h1{margin:0;font-size:15px;font-weight:600;flex:1;min-width:180px}
.viewer-bar a.btn{font-family:var(--mono);font-size:12px;border:1px solid var(--line);
  border-radius:7px;padding:5px 11px;color:var(--muted);white-space:nowrap}
.viewer-bar a.btn:hover{border-color:var(--accent);color:var(--accent)}
iframe{display:block;width:100%;border:0;background:var(--panel)}
.paths{font-family:var(--mono);font-size:11px;color:var(--muted);padding:8px 20px;
  border-bottom:1px solid var(--line);background:var(--bg);word-break:break-all;
  display:flex;gap:8px;flex-wrap:wrap;align-items:center}
"""

PAGE_JS = """
document.addEventListener('keydown',e=>{
  const box=document.getElementById('q');
  if(e.key==='/'&&document.activeElement!==box){e.preventDefault();box&&box.focus();}
  if(e.key==='Escape'&&document.activeElement===box){box.blur();}
});
"""


def nav_html(active: str = "") -> str:
    cfg = load_config()
    with db() as conn:
        counts = {r["collection"]: r["n"] for r in conn.execute(
            "SELECT collection, COUNT(*) n FROM docs GROUP BY 1")}
        ntags = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
    links = [f'<a href="/" class="{"on" if active == "" else ""}">all '
             f'<span class="n">{sum(counts.values())}</span></a>']
    for c in cfg["collections"]:
        n = counts.get(c, 0)
        if not n:
            continue
        cls = "on" if active == c else ""
        links.append(f'<a class="{cls}" href="/collection/{urllib.parse.quote(c)}">'
                     f'{escape(c)} <span class="n">{n}</span></a>')
    links.append(f'<a class="{"on" if active == "~tags" else ""}" href="/tags">'
                 f'tags <span class="n">{ntags}</span></a>')
    return '<nav class="colls">' + "".join(links) + "</nav>"


def shell(title: str, body: str, *, query: str = "", count: str = "",
          active: str = "") -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{PAGE_CSS}</style></head><body>
<header class="top">
  <a class="brand" href="/">doc<b>vault</b></a>
  <form class="search" action="/search" method="get" role="search">
    <input id="q" type="search" name="q" placeholder="Search titles and full text…  (press / )"
           value="{escape(query)}" autocomplete="off" spellcheck="false">
  </form>
  <span class="count">{escape(count)}</span>
</header>
{nav_html(active)}
<main>{body}</main>
<script>{PAGE_JS}</script></body></html>""".encode()


def chip_html(tag: str, dim: bool = False) -> str:
    cls = "chip dim" if dim else "chip"
    return (f'<a class="{cls}" href="/tag/{urllib.parse.quote(tag)}">'
            f'{escape(tag)}</a>')


def cards_grid(rows) -> str:
    """Cards are anchors, so tag chips live in a sibling row beneath each."""
    cells = []
    for r in rows:
        tags = [t for t in ((r["tags"] or "").split(",") if r["tags"] else []) if t]
        tags.sort(key=tag_sort_key)
        chips = "".join(chip_html(t) for t in tags[:4])
        if len(tags) > 4:
            chips += f'<span class="chip dim">+{len(tags) - 4}</span>'
        snip = r["snip"] if "snip" in r.keys() and r["snip"] else None
        text = snip if snip else escape(r["lede"])
        cells.append(
            f'<div><a class="card" href="/doc/{r["id"]}">'
            f'<h3>{escape(r["title"])}</h3><p>{text}</p>'
            f'<div class="meta">{chips}</div></a></div>'
        )
    return '<div class="grid">' + "".join(cells) + "</div>"


DOC_SELECT = """
SELECT d.*, (SELECT GROUP_CONCAT(t.tag) FROM tags t WHERE t.doc_id = d.id) AS tags
FROM docs d
"""

# Written out in full rather than derived from DOC_SELECT: the snippet() and
# bm25() calls need the FTS table in scope, and splicing them into the shared
# SELECT silently produced a malformed query that fell through to the LIKE path.
SEARCH_SQL = """
SELECT d.id, d.title, d.lede, d.collection, d.byte_size, d.origin_mtime,
       (SELECT GROUP_CONCAT(t.tag) FROM tags t WHERE t.doc_id = d.id) AS tags,
       snippet(docs_fts, 2, '<mark>', '</mark>', '…', 26) AS snip
FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid
WHERE docs_fts MATCH ?
ORDER BY bm25(docs_fts, 8.0, 4.0, 1.0)
LIMIT 60
"""


def render_index() -> bytes:
    cfg = load_config()
    with db() as conn:
        rows = conn.execute(DOC_SELECT + " ORDER BY d.origin_mtime DESC").fetchall()

    if not rows:
        body = ('<div class="empty"><p>The vault is empty.</p>'
                '<p>Backfill it with <code>doc-vault crawl</code>, '
                'or add one with <code>doc-vault put file.html</code>.</p></div>')
        return shell("doc-vault", body)

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["collection"], []).append(r)

    parts = ['<h2 class="coll">Recent<span class="n">last touched</span></h2>',
             cards_grid(rows[:6])]

    ordered = [c for c in cfg["collections"] if c in groups]
    ordered += [c for c in sorted(groups) if c not in cfg["collections"]]
    for coll in ordered:
        items = groups[coll]
        parts.append(f'<h2 class="coll">{escape(coll)}'
                     f'<span class="n">{len(items)} doc{"s" if len(items) != 1 else ""}</span></h2>')
        parts.append(cards_grid(items))

    return shell("doc-vault", "".join(parts),
                 count=f"{len(rows)} docs · {len(groups)} collections")


def render_collection(coll: str) -> bytes:
    with db() as conn:
        rows = conn.execute(DOC_SELECT + " WHERE d.collection = ? ORDER BY d.origin_mtime DESC",
                            (coll,)).fetchall()
    if not rows:
        return shell(f"{coll} — doc-vault",
                     f'<div class="empty">Nothing in <b>{escape(coll)}</b> yet.</div>',
                     active=coll)
    body = (f'<h2 class="coll">{escape(coll)}<span class="n">{len(rows)} docs</span></h2>'
            + cards_grid(rows))
    return shell(f"{coll} — doc-vault", body, count=f"{len(rows)} docs", active=coll)


def render_tag(tag: str) -> bytes:
    with db() as conn:
        rows = conn.execute(
            DOC_SELECT + """ WHERE d.id IN (SELECT doc_id FROM tags WHERE tag = ?)
                             ORDER BY d.origin_mtime DESC""", (tag,)).fetchall()
    if not rows:
        return shell(f"{tag} — doc-vault",
                     f'<div class="empty">No docs tagged <b>{escape(tag)}</b>.</div>')
    body = (f'<h2 class="coll">{escape(tag)}<span class="n">{len(rows)} docs</span></h2>'
            + cards_grid(rows))
    return shell(f"{tag} — doc-vault", body, count=f"{len(rows)} docs")


def render_tags() -> bytes:
    with db() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) n FROM tags GROUP BY 1 ORDER BY tag").fetchall()
    if not rows:
        return shell("tags — doc-vault", '<div class="empty">No tags yet.</div>', active="~tags")

    by_ns: dict[str, list] = {}
    for r in rows:
        ns = r["tag"].split(":", 1)[0] if ":" in r["tag"] else "(free)"
        by_ns.setdefault(ns, []).append(r)

    parts = []
    order = ["ticket", "mr", "repo", "branch", "date", "(free)"]
    for ns in order + [k for k in sorted(by_ns) if k not in order]:
        if ns not in by_ns:
            continue
        items = sorted(by_ns[ns], key=lambda r: (-r["n"], r["tag"]))
        parts.append(f'<h2 class="coll">{escape(ns)}'
                     f'<span class="n">{len(items)} tags</span></h2>')
        cloud = "".join(
            f'<a href="/tag/{urllib.parse.quote(r["tag"])}">{escape(r["tag"])} '
            f'<b>{r["n"]}</b></a>' for r in items)
        parts.append(f'<div class="tagcloud">{cloud}</div>')

    return shell("tags — doc-vault", "".join(parts),
                 count=f"{len(rows)} tags", active="~tags")


def fts_query(raw: str) -> str:
    """Turn a human query into something FTS5 will not choke on."""
    terms = re.findall(r"[\w./-]+", raw)
    if not terms:
        return ""
    return " ".join(f'"{t}"*' if len(t) > 2 else f'"{t}"' for t in terms)


def render_search(q: str) -> bytes:
    q = q.strip()
    if not q:
        return render_index()

    # `tag:foo` / `repo:bar` in the search box jumps to that tag listing.
    if re.fullmatch(r"[a-z]+:[\w./-]+", q) and not q.startswith("tag:"):
        with db() as conn:
            if conn.execute("SELECT 1 FROM tags WHERE tag = ? LIMIT 1", (q,)).fetchone():
                return render_tag(q)
    if q.startswith("tag:"):
        return render_tag(q[4:])

    match = fts_query(q)
    rows = []
    if match:
        with db() as conn:
            try:
                rows = conn.execute(SEARCH_SQL, (match,)).fetchall()
            except sqlite3.OperationalError as exc:
                # Never silent: a broken FTS query looks identical to "no hits".
                print(f"  [search] FTS failed for {match!r}: {exc}", file=sys.stderr, flush=True)
                rows = conn.execute(
                    DOC_SELECT + " WHERE d.title LIKE ? OR d.lede LIKE ? LIMIT 60",
                    (f"%{q}%", f"%{q}%"),
                ).fetchall()

    if not rows:
        body = (f'<div class="empty"><p>Nothing matches <b>{escape(q)}</b>.</p>'
                f'<p><a href="/" style="color:var(--accent)">Back to all docs</a></p></div>')
        return shell(f"{q} — doc-vault", body, query=q, count="0 results")

    body = (f'<h2 class="coll">Results<span class="n">{len(rows)} for “{escape(q)}”</span></h2>'
            + cards_grid(rows))
    return shell(f"{q} — doc-vault", body, query=q, count=f"{len(rows)} results")


def render_doc(doc_id: int) -> bytes | None:
    with db() as conn:
        row = conn.execute(DOC_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        origins = [r["path"] for r in conn.execute(
            "SELECT path FROM origins WHERE doc_id = ? ORDER BY path", (doc_id,))]
        siblings = conn.execute(
            "SELECT id, origin_mtime FROM docs WHERE title_key = ? AND id != ? "
            "ORDER BY origin_mtime DESC", (row["title_key"], doc_id)).fetchall()

    home = str(Path.home())
    tags = sorted([t for t in ((row["tags"] or "").split(",") if row["tags"] else []) if t],
                  key=tag_sort_key)
    chips = "".join(chip_html(t) for t in tags)
    sib = ""
    if siblings:
        links = " · ".join(f'<a href="/doc/{s["id"]}" style="color:var(--accent)">'
                           f'{escape(s["origin_mtime"][:10])}</a>' for s in siblings)
        sib = f'<span>other versions: {links}</span>'
    paths = " ".join(f"<span>{escape(p.replace(home, '~'))}</span>" for p in origins)

    body = f"""<div class="viewer-bar">
  <a class="btn" href="/">← all</a>
  <a class="btn" href="/collection/{urllib.parse.quote(row["collection"])}">{escape(row["collection"])}</a>
  <h1>{escape(row["title"])}</h1>
  <a class="btn" href="/raw/{doc_id}" target="_blank">open raw ↗</a>
</div>
<div class="paths">{chips}{paths} {sib}</div>
<iframe src="/raw/{doc_id}" title="{escape(row["title"])}"></iframe>"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(row["title"])} — doc-vault</title><style>{PAGE_CSS}
body{{overflow:hidden}} .paths{{max-height:70px;overflow:auto}}
iframe{{height:calc(100vh - 49px - 36px)}}</style></head>
<body>{body}<script>{PAGE_JS}</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "docvault"

    def log_message(self, fmt, *args):
        if os.environ.get("DOCVAULT_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                return self._send(render_index())
            if path == "/search":
                return self._send(render_search(params.get("q", [""])[0]))
            if path == "/tags":
                return self._send(render_tags())

            if path.startswith("/tag/"):
                return self._send(render_tag(path[len("/tag/"):]))
            if path.startswith("/collection/"):
                return self._send(render_collection(path[len("/collection/"):]))

            m = re.fullmatch(r"/doc/(\d+)", path)
            if m:
                page = render_doc(int(m.group(1)))
                if page is None:
                    return self._send(shell("not found", '<div class="empty">No such doc.</div>'),
                                      code=404)
                return self._send(page)

            m = re.fullmatch(r"/raw/(\d+)", path)
            if m:
                with db() as conn:
                    row = conn.execute("SELECT vault_name FROM docs WHERE id = ?",
                                       (int(m.group(1)),)).fetchone()
                if not row:
                    return self._send(b"not found", "text/plain", 404)
                f = DOCS_DIR / row["vault_name"]
                if not f.exists():
                    return self._send(b"vault file missing", "text/plain", 410)
                return self._send(f.read_bytes())

            if path == "/api/docs":
                with db() as conn:
                    rows = conn.execute(
                        DOC_SELECT + " ORDER BY d.origin_mtime DESC").fetchall()
                payload = json.dumps(
                    [{k: r[k] for k in ("id", "title", "collection", "tags",
                                        "origin_mtime", "byte_size")} for r in rows],
                    indent=2).encode()
                return self._send(payload, "application/json")

            self._send(shell("not found", '<div class="empty">No such page.</div>'), code=404)
        except Exception as exc:  # keep the server alive on a bad request
            self._send(f"<pre>{escape(repr(exc))}</pre>".encode(), code=500)


class AuthHandler(Handler):
    """Tunnel-facing handler; gating do_GET covers HEAD, which delegates to it."""

    access_cfg: dict = {}

    def do_GET(self):
        try:
            token = access_token_from_headers(self.headers)
            if not token:
                raise AccessDenied("no Access token on the request")
            claims = verify_access_jwt(token, self.access_cfg)
        except AccessDenied as exc:
            # Reason only -- never the token.
            print(f"  [access] 403 {self.path}: {exc}", flush=True)
            return self._send(
                shell("forbidden", '<div class="empty">Not authorised.</div>'), code=403)
        except Exception as exc:
            print(f"  [access] 403 {self.path}: unexpected {exc!r}", flush=True)
            return self._send(
                shell("forbidden", '<div class="empty">Not authorised.</div>'), code=403)
        self.access_email = str(claims.get("email") or "")
        return super().do_GET()


def watch_loop(interval: int, cfg: dict) -> None:
    """Poll the configured roots and auto-deposit anything new."""
    prune = set(cfg["prune_dirs"])
    settle = int(cfg.get("settle_seconds", 45))
    while True:
        time.sleep(interval)
        try:
            with db() as conn:
                known = {r["path"] for r in conn.execute("SELECT path FROM origins")}
                for root in cfg["roots"]:
                    rpath = Path(os.path.expanduser(root["path"]))
                    if not rpath.exists():
                        continue
                    for f in walk_root(rpath, int(root["depth"]), prune):
                        if str(f) in known:
                            continue
                        try:
                            raw = f.read_bytes()
                            # Let a file settle before capturing it. A doc being
                            # actively authored is caught on a later pass rather
                            # than indexed half-written.
                            if time.time() - f.stat().st_mtime < settle:
                                continue
                        except OSError:
                            continue
                        meta = extract(raw)
                        ok, _score, _why = classify(f, raw, meta, cfg)
                        if ok:
                            status, doc_id, title = deposit(f, cfg, source="watch", conn=conn)
                            if status == "added":
                                print(f"  [watch] +{doc_id} "
                                      f"{resolve_collection(f, cfg, title)}  {title}", flush=True)
        except Exception as exc:
            print(f"  [watch] error: {exc!r}", flush=True)


def cmd_access_selftest(args) -> int:
    """Exercise the real verify path against a throwaway key and a local JWKS fixture."""
    import subprocess
    import tempfile

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    kid, aud = "selftest-kid", "a" * 64
    allowed, stranger = "allowed@example.com", "stranger@example.com"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        key = tmp / "key.pem"
        run = lambda *a: subprocess.run(a, check=True, capture_output=True)
        run("openssl", "genrsa", "-out", str(key), "2048")
        out = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
                             check=True, capture_output=True, text=True).stdout
        modulus = out.strip().split("=", 1)[1]
        text = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-text"],
                              check=True, capture_output=True, text=True).stdout
        if "65537" not in text:
            print("FAIL: selftest key does not use exponent 65537")
            return 1
        n = int(modulus, 16)
        jwks = json.dumps({"keys": [{
            "kty": "RSA", "kid": kid, "alg": "RS256",
            "n": b64u(n.to_bytes((n.bit_length() + 7) // 8, "big")),
            "e": b64u((65537).to_bytes(3, "big")),
        }]}).encode()

        class JwksHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(jwks)))
                self.end_headers()
                self.wfile.write(jwks)

        jwks_srv = ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler)
        threading.Thread(target=jwks_srv.serve_forever, daemon=True).start()
        certs_url = f"http://127.0.0.1:{jwks_srv.server_address[1]}/certs"

        acfg = access_config({"access": {
            "enabled": True, "team": "selftest", "aud": aud,
            "allow": [allowed], "certs_url": certs_url,
        }})

        def mint(alg="RS256", use_kid=kid, **over) -> str:
            header = {"alg": alg, "kid": use_kid, "typ": "JWT"}
            claims = {"aud": [aud], "iss": _issuer(acfg), "email": allowed,
                      "exp": int(time.time()) + 600, "iat": int(time.time())}
            claims.update(over)
            signing_input = f"{b64u(json.dumps(header).encode())}." \
                            f"{b64u(json.dumps(claims).encode())}"
            payload_file, sig_file = tmp / "si.txt", tmp / "si.sig"
            payload_file.write_bytes(signing_input.encode())
            run("openssl", "dgst", "-sha256", "-sign", str(key),
                "-out", str(sig_file), str(payload_file))
            return f"{signing_input}.{b64u(sig_file.read_bytes())}"

        AuthHandler.access_cfg = acfg
        gate = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
        threading.Thread(target=gate.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{gate.server_address[1]}/"

        def status(token: str | None) -> int:
            req = urllib.request.Request(base)
            if token is not None:
                req.add_header("Cf-Access-Jwt-Assertion", token)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code

        good = mint()
        tampered = good.split(".")
        tampered[1] = b64u(json.dumps({"aud": [aud], "iss": _issuer(acfg),
                                       "email": stranger,
                                       "exp": int(time.time()) + 600}).encode())

        cases = [
            ("valid, allowlisted token", good, 200),
            ("no token at all", None, 403),
            ("empty token", "", 403),
            ("not on the allowlist", mint(email=stranger), 403),
            ("expired", mint(exp=int(time.time()) - 3600), 403),
            ("wrong aud", mint(aud=["b" * 64]), 403),
            ("wrong issuer", mint(iss="https://evil.cloudflareaccess.com"), 403),
            ("alg none", mint(alg="none"), 403),
            ("alg HS256", mint(alg="HS256"), 403),
            ("unknown kid", mint(use_kid="not-published"), 403),
            ("no email claim", mint(email=None), 403),
            ("payload swapped after signing", ".".join(tampered), 403),
            ("garbage", "not.a.jwt", 403),
        ]

        failed = 0
        for name, token, want in cases:
            got = status(token)
            ok = got == want
            failed += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {name}: want {want}, got {got}")

        gate.shutdown()
        jwks_srv.shutdown()

    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


def cmd_serve(args) -> int:
    cfg = load_config()
    init_db()
    port = args.port or cfg["port"]

    if args.watch:
        threading.Thread(target=watch_loop, args=(args.watch, cfg), daemon=True).start()
        print(f"watching roots every {args.watch}s for new docs", flush=True)

    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # flush: stdout is block-buffered when redirected to a log file, which
    # otherwise makes the log look empty while the server is running fine.
    print(f"doc-vault serving {n} docs at http://localhost:{port}  (ctrl-c to stop)", flush=True)

    acfg = access_config(cfg)
    if acfg["enabled"]:
        missing = access_misconfig(acfg)
        if missing:
            # Not sys.exit: KeepAlive would crash-loop and take 8310 down too.
            print(f"  [access] enabled but not opening the gated port: "
                  f"missing access.{', access.'.join(missing)}", flush=True)
        else:
            aport = args.access_port or int(acfg["port"])
            AuthHandler.access_cfg = acfg
            auth_httpd = ThreadingHTTPServer(("127.0.0.1", aport), AuthHandler)
            threading.Thread(target=auth_httpd.serve_forever, daemon=True).start()
            print(f"doc-vault access-gated on http://127.0.0.1:{aport}  "
                  f"(team {acfg['team']}, {len(acfg['allow'])} allowed)  "
                  f"<- point the tunnel here, not {port}", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doc-vault", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write config.json and create the database").set_defaults(fn=cmd_init)

    p = sub.add_parser("crawl", help="discover and deposit docs under the configured roots")
    p.add_argument("--dry-run", action="store_true", help="show what would be deposited")
    p.add_argument("--verbose", action="store_true", help="also list rejects with reasons")
    p.set_defaults(fn=cmd_crawl)

    p = sub.add_parser("put", help="deposit one file into the vault")
    p.add_argument("file")
    p.add_argument("--collection", help="one of the configured collections")
    p.add_argument("--tag", action="append", metavar="TAG",
                   help="tag to attach, repeatable (e.g. --tag repo:svc-chatbot --tag demo)")
    p.add_argument("--ticket", action="append", help="shorthand for --tag ticket:KEY")
    p.add_argument("--title", help="override the document title")
    p.add_argument("--move", action="store_true", help="delete the origin after depositing")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("serve", help="run the browse/search server")
    p.add_argument("--port", type=int)
    p.add_argument("--access-port", type=int,
                   help="override access.port for the Cloudflare-Access-gated listener")
    p.add_argument("--watch", type=int, metavar="SECONDS",
                   help="poll the roots and auto-deposit new docs")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("access-selftest",
                       help="prove the Access gate accepts only valid, allowlisted tokens")
    p.set_defaults(fn=cmd_access_selftest)

    p = sub.add_parser("refile", help="move a doc to a different collection")
    p.add_argument("doc_id", type=int)
    p.add_argument("collection")
    p.set_defaults(fn=cmd_refile)

    # Removal is --rm, not "-tag": argparse claims any leading-dash token as an
    # option, so `tag 39 -foo` fails before it ever reaches this command.
    p = sub.add_parser("tag", help="add tags to a doc, or remove them with --rm")
    p.add_argument("doc_id", type=int)
    p.add_argument("tags", nargs="*", help="tags to add (a leading + is optional)")
    p.add_argument("--rm", action="append", metavar="TAG", help="tag to remove, repeatable")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("theme", help="emit an on-theme doc skeleton, or check which docs are on it")
    p.add_argument("-o", "--out", metavar="FILE", help="write the skeleton here (else stdout)")
    p.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    p.add_argument("--check", action="store_true", help="report on-theme vs bespoke docs")
    p.add_argument("--wrap", metavar="FRAGMENT",
                   help="wrap a body fragment in the canonical head instead of emitting the skeleton")
    p.add_argument("--doc-title", metavar="TEXT", help="<title> to use with --wrap")
    p.set_defaults(fn=cmd_theme)

    sub.add_parser("stats", help="what the vault holds").set_defaults(fn=cmd_stats)
    sub.add_parser("reindex", help="re-derive text, collection and auto-tags").set_defaults(fn=cmd_reindex)

    p = sub.add_parser("forget", help="remove a doc from the vault")
    p.add_argument("doc_id", type=int)
    p.set_defaults(fn=cmd_forget)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
