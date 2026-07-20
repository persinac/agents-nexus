"""
Pure routing + resilience policy for nexus-proxy — no I/O, no httpx, stdlib only.

main.py owns the httpx orchestration and calls these functions to decide *what*
to do; this module never performs network I/O, which keeps it unit-testable in
isolation and keeps the hot pass-through path thin.

Invariants encoded here (see docs/model-routing.md):
  * Never cross vendor — the pool is Anthropic-only; downgrades move DOWN the
    size ladder haiku < sonnet < opus, never up, never to another vendor.
  * Difficulty IGNORES tool presence — Claude Code resends its full tool array
    on essentially every stateless turn, so it can't discriminate difficulty.
  * Fail-open — callers treat any exception here as "passthrough the requested
    model"; nothing in this module should ever hard-fail a request.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field

# 5xx / overload statuses that are safe to retry before the first byte. 429 is
# handled separately by the caller: surfaced verbatim for non-streams (Claude
# Code's HTTP-429 backoff works there), but owned proxy-side for streams.
RETRYABLE = frozenset({500, 502, 503, 529})

# Anthropic size ladder. Lower rank = smaller/cheaper.
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}

# Minimum tier that may serve a given difficulty.
_DIFFICULTY_MIN_TIER = {"trivial": 0, "normal": 1, "hard": 2}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Difficulty thresholds (env-overridable; tune from real Langfuse traces). The
# token estimate deliberately counts only messages + system, never tools.
TRIVIAL_TOKENS = _int_env("ROUTE_TRIVIAL_TOKENS", 2000)
TRIVIAL_DEPTH = _int_env("ROUTE_TRIVIAL_DEPTH", 6)
TRIVIAL_MAX_TOKENS = _int_env("ROUTE_TRIVIAL_MAXTOK", 2048)
HARD_TOKENS = _int_env("ROUTE_HARD_TOKENS", 40000)
HARD_DEPTH = _int_env("ROUTE_HARD_DEPTH", 60)
HARD_MAX_TOKENS = _int_env("ROUTE_HARD_MAXTOK", 8192)


@dataclass(frozen=True)
class Model:
    """One Anthropic candidate on the size ladder."""
    model: str   # exact model id sent upstream
    tier: str    # haiku | sonnet | opus
    cost: float  # relative blended $/Mtok — only the ordering matters


# ── difficulty classification ──────────────────────────────────────────────

def _estimate_input_tokens(body: dict) -> int:
    """~tokens of context = (len(json(messages)) + len(system_text)) // 4.

    Tools/tool_result blocks are intentionally excluded: Claude Code sends its
    full tool array on nearly every turn, so counting it would swamp the signal
    and the router would never fire on genuinely small turns.
    """
    try:
        messages = body.get("messages") or []
        system = body.get("system")
        if isinstance(system, str):
            sys_text = system
        elif system:
            sys_text = json.dumps(system, default=str)
        else:
            sys_text = ""
        return (len(json.dumps(messages, default=str)) + len(sys_text)) // 4
    except Exception:
        return 0


def classify_difficulty(body: dict) -> str:
    """Return "trivial" | "normal" | "hard" from context size, turn depth,
    extended-thinking, and max_tokens. Ignores tool presence entirely."""
    est = _estimate_input_tokens(body)
    depth = len(body.get("messages") or [])
    try:
        max_tokens = int(body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        max_tokens = 0

    if body.get("thinking"):
        return "hard"
    if est >= HARD_TOKENS or depth >= HARD_DEPTH or max_tokens >= HARD_MAX_TOKENS:
        return "hard"
    if est <= TRIVIAL_TOKENS and depth <= TRIVIAL_DEPTH and max_tokens <= TRIVIAL_MAX_TOKENS:
        return "trivial"
    return "normal"


# ── candidate pool ─────────────────────────────────────────────────────────

def load_pool() -> list[Model]:
    """The Anthropic size ladder. Static defaults, overridable via ROUTE_POOL
    (inline JSON) or ROUTE_POOL_FILE (path to a JSON array of {model,tier,cost}).

    A future cross-vendor entry would carry a `translate` flag and stay inert
    until a Phase-3 translation sidecar exists — not wired here.
    """
    raw = None
    pf = os.environ.get("ROUTE_POOL_FILE")
    if pf:
        try:
            with open(pf, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            raw = None
    if raw is None and os.environ.get("ROUTE_POOL"):
        try:
            raw = json.loads(os.environ["ROUTE_POOL"])
        except Exception:
            raw = None
    if raw is None:
        cheap = os.environ.get("ROUTE_CHEAP_MODEL", "claude-haiku-4-5")
        raw = [
            {"model": cheap, "tier": "haiku", "cost": 1.0},
            {"model": os.environ.get("ROUTE_SONNET_MODEL", "claude-sonnet-5"), "tier": "sonnet", "cost": 3.0},
            {"model": os.environ.get("ROUTE_OPUS_MODEL", "claude-opus-4-8"), "tier": "opus", "cost": 15.0},
        ]
    pool: list[Model] = []
    for e in raw:
        try:
            tier = e["tier"]
            if tier not in TIER_RANK:
                continue
            pool.append(Model(model=e["model"], tier=tier, cost=float(e["cost"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pool


def _find(pool: list[Model], requested: str | None) -> Model | None:
    """Resolve the requested model to a pool entry: exact id first, else by tier
    keyword in the id (so dated ids like `claude-3-5-haiku-…` map to the haiku
    tier). Unknown → None (caller passes through)."""
    if not requested:
        return None
    for m in pool:
        if m.model == requested:
            return m
    rl = requested.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in rl:
            for m in pool:
                if m.tier == tier:
                    return m
    return None


def select_model(requested, difficulty, pool, cooldowns, downgrade_tiers, now) -> str | None:
    """Proactive down-ladder selection. Downgrade ONLY when `difficulty` is in
    `downgrade_tiers` (default: {"trivial"}): pick the cheapest Anthropic model
    whose tier can serve the difficulty, whose cost ≤ the requested model's, and
    that is not in cooldown. normal/hard and unknown models pass through. Never
    upgrades, never crosses vendor (pool is Anthropic-only)."""
    req = _find(pool, requested)
    if req is None:
        return requested
    if difficulty not in downgrade_tiers:
        return requested
    min_rank = _DIFFICULTY_MIN_TIER.get(difficulty, 0)
    active = cooldowns.active(now)
    candidates = [
        m for m in pool
        if TIER_RANK[m.tier] >= min_rank and m.cost <= req.cost and m.model not in active
    ]
    if not candidates:
        return requested
    best = min(candidates, key=lambda m: (m.cost, TIER_RANK[m.tier]))
    return best.model


def shed_model(current, pool, cooldowns, now) -> str | None:
    """The next cheaper Anthropic model below `current` that is not in cooldown,
    or None if there is none. Used to shed load after retries are exhausted."""
    cur = _find(pool, current)
    if cur is None:
        return None
    active = cooldowns.active(now)
    cheaper = [m for m in pool if m.cost < cur.cost and m.model not in active]
    if not cheaper:
        return None
    return max(cheaper, key=lambda m: m.cost).model


# ── backoff ────────────────────────────────────────────────────────────────

def backoff_delays(attempt: int, retry_after=None, base: float = 0.5, cap: float = 8.0) -> float:
    """Delay (seconds) before the given 0-based retry attempt. Honors an upstream
    Retry-After when present (bounded to 2×cap); otherwise capped jittered
    exponential backoff: min(base * 2**attempt + jitter, cap)."""
    if retry_after is not None:
        try:
            return max(0.0, min(float(retry_after), cap * 2))
        except (TypeError, ValueError):
            pass
    return min(base * (2 ** attempt) + random.uniform(0, base), cap)


# ── cooldowns ──────────────────────────────────────────────────────────────

@dataclass
class Cooldowns:
    """Per-model sliding 429/5xx window shared by resilience + selection: once a
    model logs `threshold` transient failures within `window` seconds it is put
    in cooldown for `window` seconds and skipped by select/shed. In-process and
    in-memory (resets on restart; not shared across replicas) — fine for the
    single container."""
    threshold: int = 2
    window: float = 20.0
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _until: dict[str, float] = field(default_factory=dict)

    def record(self, model: str, status: int, now: float) -> None:
        if not model:
            return
        hits = [t for t in self._hits.get(model, []) if now - t < self.window]
        hits.append(now)
        if len(hits) >= self.threshold:
            self._until[model] = now + self.window
            self._hits[model] = []  # reset once tripped
        else:
            self._hits[model] = hits

    def active(self, now: float) -> set[str]:
        return {m for m, until in self._until.items() if until > now}

    def in_cooldown(self, model: str, now: float) -> bool:
        return self._until.get(model, 0.0) > now
