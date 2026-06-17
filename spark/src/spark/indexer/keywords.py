"""Domain-keyword extractor — surfaces in-code vocabulary into repo summaries.

'The index holds every answer; one must only know the word to ask.'
— 127 Guilty Spark

Semantic + BM25 search over repo *summaries* (monitor-logs) drives Stage-1
routing in `spark`/`spark_deep`. Prose in README/CLAUDE.md is already embedded,
but domain identifiers that live only in code — data-source enums, event types,
status constants — are not. A query like "which repo ingests POE" then fails to
route, because the summary never names POE even though `SOURCE_POE = "poe"` sits
in `constants.py`.

This module performs a bounded AST scan of a repo's Python files and pulls out
two high-signal, low-noise families of identifier:

  * module-level UPPER_SNAKE_CASE constants bound to string literals
    (e.g. ``SOURCE_POE = "poe"``  ->  ``poe``)
  * ``Enum`` member values (e.g. ``class Source(str, Enum): POE = "poe"``)

The resulting tokens are appended to the summary chunk as a ``Keywords:`` line,
so both the vector index and the full-text (BM25) index can match the bare term.
AST-only by design: it captures the authoritative "these are our values"
declarations while ignoring the noise a blind text scan would pull in.
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

from spark.config import SparkConfig

logger = logging.getLogger("spark.keywords")

# Tokens that are structurally identifier-like but carry no domain signal.
# These are technical/generic vocabulary that appears across unrelated repos and
# adds no Stage-1 discriminative power — filtering it keeps summaries focused on
# the business terms that actually distinguish one repo from another.
_NOISE = frozenset({
    # booleans / sentinels / config flags
    "true", "false", "none", "null", "nil", "void", "default", "none_", "todo",
    "fixme", "true_", "false_", "y", "n", "yes", "no", "on", "off", "auto",
    # environments / log levels
    "dev", "prod", "stage", "staging", "test", "testing", "local", "debug",
    "info", "warning", "warn", "error", "errors", "critical", "trace", "fatal",
    # http verbs / schemes / serialization
    "get", "post", "put", "patch", "delete", "head", "options", "http", "https",
    "ws", "wss", "tcp", "udp", "grpc", "rest", "json", "yaml", "yml", "csv",
    "txt", "xml", "html", "toml", "ini", "utf-8", "utf8", "utf-16", "ascii",
    "latin-1", "base64", "hex", "gzip", "deflate", "identity", "zip", "gz",
    "tar", "zstd", "lz4", "br",
    # primitive / schema types
    "string", "str", "integer", "int", "float", "double", "decimal", "number",
    "numeric", "bool", "boolean", "bytes", "byte", "binary", "char", "date",
    "datetime", "time", "timestamp", "object", "array", "list", "dict", "map",
    "set", "tuple", "struct", "record", "enum", "any", "type", "types", "kind",
    # parser/stream events (ijson, sax, websocket frames)
    "start", "end", "start_map", "end_map", "map_key", "start_array",
    "end_array", "open", "opening", "close", "closed", "closing", "connect",
    "connected", "disconnect", "ping", "pong", "text", "continuation", "frame",
    # generic states / results / verbs
    "input", "output", "result", "results", "success", "succeeded", "failure",
    "failed", "pending", "queued", "running", "active", "inactive", "enabled",
    "disabled", "unknown", "other", "others", "all", "empty", "valid",
    "invalid", "ok", "status", "create", "created", "update", "updated",
    "read", "write", "append", "first", "last", "asc", "desc", "left", "right",
    # architectures / platforms
    "aarch64", "aarc64", "arm64", "arm", "amd64", "x86", "x86_64", "i386",
    "linux", "darwin", "windows", "macos", "win32", "posix", "loongarch64",
    "ppc64", "ppc64le", "s390x", "riscv64", "riscv", "mips", "mips64",
    "mips64le", "sparc", "sparc64", "wasm",
    # http digest-auth / crypto field names (RFC 7616)
    "qop", "realm", "nonce", "cnonce", "opaque", "algorithm", "domain",
    # PEP 508 / packaging environment markers (vendored packaging/pip code)
    "python_version", "python_full_version", "implementation_name",
    "implementation_version", "platform_release", "platform_version",
    "platform_machine", "platform_system", "platform_python_implementation",
    "os_name", "sys_platform", "extra", "extras", "dependency_groups", "marker",
    # ubiquitous identifiers / vcs
    "id", "ids", "uuid", "name", "names", "value", "values", "key", "keys",
    "version", "v1", "v2", "v3", "main", "master", "head", "origin",
})

# A keyword must look like a word/identifier token (letters, digits, _.- and a
# single internal space), start with a letter, and be a sane length.
_MIN_LEN = 2
_MAX_LEN = 40


def _is_uppersnake(name: str) -> bool:
    """True for ALL_CAPS / UPPER_SNAKE constant names (at least one letter)."""
    return name.isupper() and any(c.isalpha() for c in name) and "__" not in name[:2]


def _clean_token(value: str) -> str | None:
    """Normalize a candidate string literal to a keyword, or None if not word-like."""
    tok = value.strip()
    if not (_MIN_LEN <= len(tok) <= _MAX_LEN):
        return None
    if tok[0] in "/.{$%#@<":  # paths, templates, format strings, urls-ish
        return None
    if "://" in tok or "/" in tok or "\\" in tok or "\n" in tok:
        return None
    if not tok[0].isalpha():
        return None
    # word-like: letters/digits plus at most light separators, <=1 internal space
    if not all(c.isalnum() or c in "_-. " for c in tok):
        return None
    if tok.count(" ") > 1:
        return None
    if tok.count("-") > 2:  # bucket/DNS/host-like identifiers, not jargon
        return None
    low = tok.lower()
    if low in _NOISE:
        return None
    if low.isdigit():
        return None
    return low


def _string_literals(node: ast.AST) -> list[str]:
    """Collect plain string-constant values directly under an assignment value
    (a bare string, or a list/tuple/set of strings)."""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
    return out


def _is_enum_class(node: ast.ClassDef) -> bool:
    """Heuristic: a class whose base name ends in 'Enum' (Enum, IntEnum, StrEnum, str+Enum)."""
    for base in node.bases:
        base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if base_name and base_name.endswith("Enum"):
            return True
    return False


def _extract_from_source(source: str) -> list[str]:
    """Return candidate keyword tokens from one Python source string."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    found: list[str] = []

    def _consider_assign(targets: list[ast.expr], value: ast.AST, require_upper: bool) -> None:
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if require_upper and not any(_is_uppersnake(n) for n in names):
            return
        for literal in _string_literals(value):
            cleaned = _clean_token(literal)
            if cleaned:
                found.append(cleaned)

    # Module-level UPPER_SNAKE string constants.
    for node in tree.body:
        if isinstance(node, ast.Assign):
            _consider_assign(node.targets, node.value, require_upper=True)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_uppersnake(node.target.id) and node.value is not None:
                for literal in _string_literals(node.value):
                    cleaned = _clean_token(literal)
                    if cleaned:
                        found.append(cleaned)
        elif isinstance(node, ast.ClassDef) and _is_enum_class(node):
            # Enum members: take string *values* only. The member-name fallback was
            # dropped — it doubled noise from technical enums (websocket opcodes,
            # JSON type schemas) while domain enums already carry the term as a value
            # (e.g. `class Source(str, Enum): POE = "poe"`).
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    _consider_assign(stmt.targets, stmt.value, require_upper=False)
    return found


def extract_domain_keywords(repo_dir: Path, config: SparkConfig) -> list[str]:
    """Scan a repo's Python files for domain identifiers (enum/constant values).

    Bounded by `config.exclude_dirs`, `config.max_file_size`, and
    `config.max_files_per_installation`. Returns a frequency-then-alpha sorted,
    deduped, capped list of lower-cased keyword tokens. Never raises.
    """
    if not getattr(config, "summary_keywords_enabled", True):
        return []

    cap = getattr(config, "summary_keywords_max", 40)
    counts: dict[str, int] = {}
    files_scanned = 0
    try:
        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if d not in config.exclude_dirs]
            for filename in sorted(files):
                if not filename.endswith(".py"):
                    continue
                fpath = Path(root) / filename
                try:
                    if fpath.stat().st_size > config.max_file_size:
                        continue
                    source = fpath.read_text(errors="replace")
                except OSError:
                    continue
                for tok in _extract_from_source(source):
                    counts[tok] = counts.get(tok, 0) + 1
                files_scanned += 1
                if files_scanned >= config.max_files_per_installation:
                    break
            if files_scanned >= config.max_files_per_installation:
                break
    except Exception as exc:  # never let keyword extraction break indexing
        logger.warning("keyword extraction failed for %s: %s", repo_dir, exc)
        return []

    # Most-repeated terms first (a value reused across files is more salient),
    # then alphabetical for stable output.
    ordered = sorted(counts, key=lambda t: (-counts[t], t))
    return ordered[:cap]
