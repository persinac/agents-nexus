#!/usr/bin/env python3
"""Enumerate recent merges and classify their verifiability.

This is the verifier agent's worklist generator, not the verifier. It answers
"what merged, and did the author say how we would know it worked?" — the agent
then goes and takes the measurement, which needs judgement this script does not have.

See docs/verifier-agent.md. Three outcomes:

  HAS-CLAIM      a VERIFY: line exists -> the agent should run the measurement
  NO-CLAIM-LINE  no VERIFY: line. The change may still be perfectly verifiable from
                 its prose -- this marks a MISSING LINE, not an unverifiable change.
                 Read the body and retro-fit the measurement.
  UNFALSIFIABLE  a VERIFY: line that CANNOT FAIL -> the real finding, and worse than
                 a missing one because it reads as diligence
  TRIVIAL        docs/chore-only diff, nothing to measure

Usage:
  verify-merges.py [--days 2] [--repo wallet-api ...] [--json]
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ORG = "flippin-balls"
REPOS = ["wallet-api", "management-api", "storefront-api", "store-front",
         "flashback-cns", "database", "kiosk", "infrastructure",
         "aws-utils", "pinball-db", "healthcheck"]

VERIFY = re.compile(r"^\s*VERIFY:\s*(?P<claim>.+)$", re.I | re.M)

# A claim that cannot fail is worse than no claim, because it reads as diligence.
# These are the shapes seen on this fleet: restating the diff, or asserting a
# process step rather than an observable outcome.
VACUOUS = re.compile(
    r"^(ci (is )?green|tests? pass(ing|es)?|lint (is )?clean|builds? (ok|clean|fine)|"
    r"code review(ed)?|merged|n/?a|none|no changes?|see (the )?diff)\.?$", re.I)

TRIVIAL_ONLY = re.compile(r"^(docs?|chore|style|ci|test)(\(|:|/)", re.I)


def gh(*args):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def classify(pr):
    m = VERIFY.search(pr.get("body") or "")
    if m:
        claim = m.group("claim").strip()
        if VACUOUS.match(claim):
            return "UNFALSIFIABLE", f"claim cannot fail: {claim!r}"
        return "HAS-CLAIM", claim
    if TRIVIAL_ONLY.match(pr.get("title") or ""):
        return "TRIVIAL", "docs/chore only"
    return "NO-CLAIM-LINE", "no VERIFY: line — read the body and retro-fit the measurement"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--repo", action="append")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    rows = []
    for repo in (a.repo or REPOS):
        raw = gh("pr", "list", "--repo", f"{ORG}/{repo}", "--state", "merged",
                 "--limit", "30", "--json", "number,title,body,mergedAt,url,author")
        if not raw:
            continue
        for pr in json.loads(raw):
            merged = pr.get("mergedAt")
            if not merged:
                continue
            when = datetime.fromisoformat(merged.replace("Z", "+00:00"))
            if when < since:
                continue
            verdict, detail = classify(pr)
            rows.append({"repo": repo, "num": pr["number"], "title": pr["title"],
                         "url": pr["url"], "merged": when.isoformat(),
                         "author": (pr.get("author") or {}).get("login", "?"),
                         "verdict": verdict, "detail": detail})

    rows.sort(key=lambda r: r["merged"], reverse=True)
    if a.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print(f"No merges in the last {a.days} day(s).")
        print("NOTE: that is a real result, not an error — say so rather than reporting nothing.")
        return

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"{len(rows)} merges in the last {a.days} day(s): "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + "\n")

    for r in rows:
        mark = {"HAS-CLAIM": "  ", "UNFALSIFIABLE": ">>",
                "NO-CLAIM-LINE": " ~", "TRIVIAL": "  "}[r["verdict"]]
        print(f"{mark} [{r['verdict']:<14}] {r['repo']}#{r['num']} — {r['title'][:56]}")
        print(f"     {r['detail'][:96]}")
        print(f"     {r['url']}")
    print("\nHAS-CLAIM     -> run the measurement, report CONFIRMED or DRIFTED.")
    print("NO-CLAIM-LINE -> missing line, not a missing proof. Retro-fit the measurement")
    print("                 from the PR prose before calling anything unverifiable.")
    print("UNFALSIFIABLE -> the real finding: a claim that cannot fail.")


if __name__ == "__main__":
    sys.exit(main())
