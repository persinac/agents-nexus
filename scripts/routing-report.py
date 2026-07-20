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

No secrets required: it queries ClickHouse via `docker exec langfuse-clickhouse`.
Cost figures use the editable PRICES table below (Langfuse has no prices set for
these models) and are ESTIMATES — see the caveats printed at the end.

Usage:  python3 scripts/routing-report.py [WINDOW]     # WINDOW e.g. "24 HOUR" (default), "7 DAY"
"""

import re
import subprocess
import sys

# ── EDIT ME: real Anthropic list prices, USD per 1M tokens ───────────────────
PRICES = {
    "claude-opus-4-8":  {"input": 15.0, "output": 75.0},
    "claude-sonnet-5":  {"input": 3.0,  "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
}
CACHE_READ_MULT = 0.10    # cache-read priced ~0.1x base input
CACHE_WRITE_MULT = 1.25   # cache-creation priced ~1.25x base input

CH = ["docker", "exec", "langfuse-clickhouse", "clickhouse-client"]


def ch(query: str) -> list[list[str]]:
    """Run a ClickHouse query, return rows (list of columns). TSV, no header."""
    out = subprocess.run(CH + ["-q", query + " FORMAT TabSeparated"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"clickhouse query failed:\n{out.stderr.strip()}")
    return [ln.split("\t") for ln in out.stdout.splitlines() if ln.strip()]


def price(model: str, u: dict) -> float:
    """$ for a turn's usage buckets at `model`'s rates (fuzzy model match)."""
    p = PRICES.get(model)
    if p is None:  # tolerate dated ids: match on tier keyword
        for k, v in PRICES.items():
            tier = k.split("-")[1]  # opus|sonnet|haiku
            if tier in (model or "").lower():
                p = v
                break
    if p is None:
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
    window = sys.argv[1] if len(sys.argv) > 1 else "24 HOUR"
    if not re.fullmatch(r"\d+\s+(MINUTE|HOUR|DAY|WEEK)", window, re.I):
        sys.exit(f"bad WINDOW {window!r} — use e.g. '24 HOUR', '7 DAY'")
    W = f"start_time > now() - INTERVAL {window} AND metadata['routing'] != ''"
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
    print(f"\n[B] request mix + token volume (output = the real cost driver)")
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

    print("\n--- caveats ---")
    print("  * $ uses the editable PRICES table (Langfuse has no model prices set); verify rates.")
    print("  * Downgrades lose the requested model's prompt cache (cache is per-model), so a real")
    print("    downgraded turn's buckets skew to fresh input — [C] is a same-tokens approximation.")
    print("  * Difficulty currently counts FULL context size incl. cache-read (cheap); the real cost")
    print("    driver is output tokens. Consider weighting difficulty toward generation, not context.\n")


if __name__ == "__main__":
    main()
