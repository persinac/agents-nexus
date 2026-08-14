#!/usr/bin/env python3
"""
routing-report — calibration view for nexus-proxy model routing.

Reads the `routing` metadata the proxy tags onto every /v1/messages generation
(requested/served model, difficulty, action, retries) plus token usage from
Langfuse's ClickHouse, and prints:

  A. routing activity      — difficulty x action, downgrade rate, resilience
  B. request mix + volumes  — where the token spend actually is
  C. realized savings       — $ saved on turns that actually downgraded
  D. headroom / what-if     — opus->sonnet opportunity (all + low-risk subset)
  E. per-agent spend        — list-$ per sess/<agent> + cache-safe sonnet-pin headroom

No secrets required: it queries ClickHouse via `docker exec langfuse-clickhouse`.
Cost figures use Anthropic list prices (PRICES below / routing-prices.json). For
personal subscription traffic they are a NOTIONAL list-value / quota proxy, not
cash — see the caveats printed at the end.

Usage:  python3 scripts/routing-report.py [WINDOW]     # WINDOW e.g. "24 HOUR" (default), "7 DAY"
"""

import json
import os.path
import re
import subprocess
import sys

# ── Prices: Anthropic list, USD per 1M tokens ────────────────────────────────
# Personal sessions run on the subscription (direct-Anthropic OAuth), so $ here
# is a NOTIONAL list-value / quota-consumption proxy, not cash — only work-gateway
# traffic (work-*) is real per-token spend. Override without editing code via
# scripts/routing-prices.json (see load_prices). Current 2026-07-20; Sonnet 5 is
# on introductory pricing ($2/$10) through 2026-08-31, then $3/$15.
# Populated exclusively from scripts/routing-prices.json by load_prices(). Kept
# empty on purpose: a hardcoded copy here silently diverged from the JSON — both
# tables were missing claude-opus-5 while agreeing with each other, so nothing
# flagged it. One table, one place to update.
PRICES: dict[str, dict[str, float]] = {}
CACHE_READ_MULT = 0.10    # overwritten from the JSON; see load_prices()
CACHE_WRITE_MULT = 1.25   # overwritten from the JSON; see load_prices()

CH = ["docker", "exec", "langfuse-clickhouse", "clickhouse-client"]


def load_prices() -> None:
    """Load scripts/routing-prices.json — the only price table. Exits if it is
    unreadable: there is no built-in fallback any more, so continuing would
    price every model at zero and print a confidently empty report."""
    global CACHE_READ_MULT, CACHE_WRITE_MULT
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routing-prices.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read the price table {path}: {exc}")
    for m, p in (data.get("models") or {}).items():
        if isinstance(p, dict) and "input" in p and "output" in p:
            PRICES[m] = {"input": float(p["input"]), "output": float(p["output"])}
    if "cache_read_mult" in data:
        CACHE_READ_MULT = float(data["cache_read_mult"])
    if "cache_write_mult" in data:
        CACHE_WRITE_MULT = float(data["cache_write_mult"])
    if not PRICES:
        sys.exit(f"the price table {path} lists no models — refusing to report "
                 "every model at zero cost")


def ch(query: str) -> list[list[str]]:
    """Run a ClickHouse query, return rows (list of columns). TSV, no header."""
    out = subprocess.run(CH + ["-q", query + " FORMAT TabSeparated"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"clickhouse query failed:\n{out.stderr.strip()}")
    return [ln.split("\t") for ln in out.stdout.splitlines() if ln.strip()]


_WARNED: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"  ! {msg}", file=sys.stderr)


def price(model: str, u: dict) -> float:
    """$ for a turn's usage buckets at `model`'s rates.

    Exact match, then a same-tier substitute — but the substitute now ANNOUNCES
    itself. It used to be silent, which is how claude-opus-5 read as correctly
    priced for months: it fell through to claude-opus-4-8, and the two happen to
    share $5/$25. The figure was right by coincidence, and would have become a
    plausible wrong number the first time an Opus shipped at a new price.
    The nightly snapshot deliberately has no equivalent fallback — see
    resolve_price() there — because its output is the durable record."""
    p = PRICES.get(model)
    if p is None:  # tolerate dated ids: match on tier keyword
        for k, v in PRICES.items():
            tier = k.split("-")[1]  # opus|sonnet|haiku
            if tier in (model or "").lower():
                p = v
                _warn_once(model, f"{model} has no price entry — using {k} rates "
                                  f"(${v['input']}/${v['output']}). Add it to "
                                  f"routing-prices.json.")
                break
    if p is None:
        _warn_once(model, f"{model} has no price entry and no same-tier match — "
                          f"counting it as $0. Add it to routing-prices.json.")
        return 0.0
    inp, out = p["input"], p["output"]
    return (
        u.get("input", 0) * inp
        + u.get("cache_creation_input_tokens", 0) * inp * CACHE_WRITE_MULT
        + u.get("cache_read_input_tokens", 0) * inp * CACHE_READ_MULT
        + u.get("output", 0) * out
    ) / 1_000_000


def bar(n: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    fill = round(width * n / total)
    return "█" * fill + "·" * (width - fill)


def main() -> None:
    load_prices()
    window = sys.argv[1] if len(sys.argv) > 1 else "24 HOUR"
    if not re.fullmatch(r"\d+\s+(MINUTE|HOUR|DAY|WEEK)", window, re.I):
        sys.exit(f"bad WINDOW {window!r} — use e.g. '24 HOUR', '7 DAY'")
    W = f"start_time > now() - INTERVAL {window} AND metadata['routing'] != ''"
    Wo = f"o.start_time > now() - INTERVAL {window} AND o.metadata['routing'] != ''"
    J = "JSONExtractString(metadata['routing'],'{}')"

    print(f"\n=== nexus-proxy routing report — last {window} ===")

    # ── A. activity ──────────────────────────────────────────────────────────
    rows = ch(f"""SELECT {J.format('difficulty')} d, {J.format('action')} a,
                 count(), sum(JSONExtractInt(metadata['routing'],'retries'))
                 FROM observations WHERE {W} GROUP BY d, a ORDER BY count() DESC""")
    total = sum(int(r[2]) for r in rows)
    downgrades = sum(int(r[2]) for r in rows if r[1] == "downgrade")
    retries = sum(int(r[3]) for r in rows)
    print(f"\n[A] activity — {total} tagged turns")
    if not total:
        print("    (no routing-tagged traffic in window)")
    for d, a, n, _ in rows:
        n = int(n)
        print(f"    {d:<9} {a:<12} {n:>6}  {100*n/total:>5.1f}%  {bar(n, total)}")
    print(f"    -> downgrade rate: {100*downgrades/total:.2f}%  |  turns with retries logged: {retries}"
          if total else "")

    # ── B. request mix + token volumes ───────────────────────────────────────
    rows = ch(f"""SELECT {J.format('requested_model')} m, count(),
                 sum(usage_details['output']),
                 sum(usage_details['cache_read_input_tokens']),
                 sum(usage_details['input']+usage_details['cache_creation_input_tokens'])
                 FROM observations WHERE {W} GROUP BY m ORDER BY count() DESC""")
    print(f"\n[B] request mix + token volume (cache-read often dominates cost)")
    print(f"    {'requested':<20}{'turns':>7}{'output_tok':>13}{'cache_read':>13}{'fresh_in':>11}")
    for m, n, o, cr, fi in rows:
        print(f"    {m:<20}{int(n):>7}{int(o):>13,}{int(cr):>13,}{int(fi):>11,}")

    # ── C. realized savings on actual downgrades ─────────────────────────────
    rows = ch(f"""SELECT {J.format('requested_model')}, provided_model_name,
                 usage_details['input'], usage_details['output'],
                 usage_details['cache_read_input_tokens'],
                 usage_details['cache_creation_input_tokens']
                 FROM observations WHERE {W} AND {J.format('action')}='downgrade'""")
    print(f"\n[C] realized downgrade savings — {len(rows)} downgraded turn(s)")
    saved = 0.0
    for req, srv, i, o, cr, cw in rows:
        u = {"input": int(i), "output": int(o),
             "cache_read_input_tokens": int(cr), "cache_creation_input_tokens": int(cw)}
        s = price(req, u) - price(srv, u)
        saved += s
        print(f"    {req} -> {srv}: out={int(o):,} tok  est_saved=${s:.4f}")
    if rows:
        print(f"    -> est. realized savings: ${saved:.4f} (see cache caveat)")
    else:
        print("    (none yet — enable normal->sonnet or loosen thresholds to populate this)")

    # ── D. headroom / what-if: opus -> sonnet ────────────────────────────────
    rows = ch(f"""SELECT
                 count(),
                 sum(usage_details['output']),
                 countIf(usage_details['output'] < 500),
                 sumIf(usage_details['output'], usage_details['output'] < 500),
                 sum(usage_details['input']),
                 sum(usage_details['cache_read_input_tokens']),
                 sum(usage_details['cache_creation_input_tokens'])
                 FROM observations WHERE {W} AND {J.format('requested_model')}='claude-opus-4-8'""")
    print(f"\n[D] headroom — if opus turns had been served by sonnet")
    if rows and rows[0][0] and int(rows[0][0]) > 0:
        n, out_all, n_small, out_small, i_all, cr_all, cw_all = (int(x) for x in rows[0])
        u_all = {"input": i_all, "output": out_all,
                 "cache_read_input_tokens": cr_all, "cache_creation_input_tokens": cw_all}
        opus_cost = price("claude-opus-4-8", u_all)
        sonnet_cost = price("claude-sonnet-5", u_all)
        print(f"    {n} opus turns, {out_all:,} output tok, actual ~${opus_cost:.2f} at opus rates")
        print(f"    theoretical if ALL -> sonnet: ~${sonnet_cost:.2f}  "
              f"(~${opus_cost - sonnet_cost:.2f} max, ignores quality)")
        print(f"    low-risk subset: {n_small} turns with <500 output tok "
              f"({out_small:,} tok) — the safest to try on sonnet")
    else:
        print("    (no opus-requested turns in window)")

    # ── E. per-agent spend + cache-safe sonnet-pin headroom ──────────────────
    # NOT routing-gated, unlike A-D. Those compare requested vs served and so
    # genuinely need metadata['routing']; per-agent SPEND does not care whether
    # proactive downgrade is on. Sharing the gated Wo made E read empty on every
    # box running the default ROUTE_ENABLED=0 — i.e. the one section that answers
    # "what is this fleet costing per agent" was dark for a reason unrelated to it.
    WoE = f"o.start_time > now() - INTERVAL {window}"
    rows = ch(f"""SELECT t.session_id AS agent, o.provided_model_name AS model, count(),
                 sum(o.usage_details['input']), sum(o.usage_details['output']),
                 sum(o.usage_details['cache_read_input_tokens']),
                 sum(o.usage_details['cache_creation_input_tokens'])
                 FROM observations o INNER JOIN traces t ON o.trace_id = t.id
                 WHERE {WoE} AND o.type = 'GENERATION' GROUP BY agent, model""")
    agents: dict = {}
    for agent, model, turns, i, o, cr, cw in rows:
        u = {"input": int(i), "output": int(o),
             "cache_read_input_tokens": int(cr), "cache_creation_input_tokens": int(cw)}
        c = price(model, u)
        a = agents.setdefault(agent or "(untagged)",
                              {"turns": 0, "cost": 0.0, "out": 0, "save": 0.0, "top": ("", 0)})
        a["turns"] += int(turns); a["cost"] += c; a["out"] += int(o)
        if "opus" in (model or ""):  # cache-safe: a pinned agent's cache lives on sonnet
            a["save"] += c - price("claude-sonnet-5", u)
        if int(turns) > a["top"][1]:
            a["top"] = (model, int(turns))
    print(f"\n[E] per-agent list-value $ ({window}) + cache-safe sonnet-pin headroom")
    print(f"    {'agent':<40}{'turns':>6}{'model':>9}{'out/turn':>9}{'list-$':>10}{'pin→sonnet':>14}")
    for agent, a in sorted(agents.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:12]:
        tier = (a["top"][0] or "?").replace("claude-", "").split("-")[0]
        opt = a["out"] // a["turns"] if a["turns"] else 0
        pin = f"save ${a['save']:.2f}" if a["save"] > 0.01 else "—"
        print(f"    {agent[:40]:<40}{a['turns']:>6}{tier:>9}{opt:>9}{'$'+format(a['cost'],'.2f'):>10}{pin:>14}")
    print("    candidates = opus-dominant rows with a pin→sonnet number (whole-session pin keeps cache)")

    print("\n--- caveats ---")
    print("  * $ = Anthropic LIST value (scripts/routing-prices.json). Personal traffic is on the")
    print("    subscription, so it's a quota/budget proxy, not cash; only work-* sessions are real $.")
    print("  * [C]/[D] per-TURN downgrade loses the requested model's per-model cache, so a real")
    print("    downgraded big turn skews to fresh input and can be net-negative — [D] is an upper bound.")
    print("  * [E] pin→sonnet is cache-SAFE: pinning a whole sess/<agent> to sonnet moves its cache")
    print("    onto sonnet (cheaper reads), so that headroom is actually capturable — the real lever.\n")


if __name__ == "__main__":
    main()
