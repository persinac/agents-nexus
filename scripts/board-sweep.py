#!/usr/bin/env python3
"""Board sweep: what is actually ready to be worked, and what is blocked on a human.

Written 2026-09-07 after an overnight run where six workstreams were hand-picked and
five cards from agents nobody tasked never reached the orchestrator at all — including
one reporting that tests write to the production database.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

KEY, TOK = os.environ["TRELLO_API_KEY"], os.environ["TRELLO_TOKEN"]
BOARD = "8rZbo5uA"


def get(path, **kw):
    kw.update(key=KEY, token=TOK)
    url = f"https://api.trello.com/1{path}?{urllib.parse.urlencode(kw)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def made(cid):
    return datetime.fromtimestamp(int(cid[:8], 16), tz=timezone.utc).astimezone()


lists = {l["id"]: l["name"] for l in get(f"/boards/{BOARD}/lists", fields="name")}
cards = get(f"/boards/{BOARD}/cards", filter="open",
            fields="name,desc,shortUrl,labels,idList,dateLastActivity")

# A card is "human-blocked" when its own text says so. These are the phrasings the
# fleet actually used overnight, not invented ones.
BLOCKED = re.compile(
    r"(needs? (a )?(human|alex|owner|decision|sign-?off)|awaiting alex|"
    r"alex (must|needs|decides?)|blocked on|product (call|decision)|"
    r"one-second question|do not (start|merge|apply) )", re.I)
# Minion-eligible = small, mechanical, already specified.
READY = re.compile(r"(loe:s|minion|mechanical|one-line|already specified)", re.I)

by_lane = defaultdict(list)
for c in cards:
    labs = [l.get("name") or l.get("color") for l in c.get("labels", [])]
    blob = f"{c['name']}\n{c.get('desc','')}\n{' '.join(x for x in labs if x)}"
    prio = next((l for l in labs if l and l.startswith("priority:")), "priority:—")
    by_lane[lists.get(c["idList"], "?")].append({
        "name": c["name"], "url": c["shortUrl"], "prio": prio,
        "labels": [x for x in labs if x],
        "blocked": bool(BLOCKED.search(blob)),
        "ready": bool(READY.search(blob)),
        "age_days": (datetime.now(timezone.utc).astimezone() - made(c["id"])).days,
        "new": (datetime.now(timezone.utc).astimezone() - made(c["id"])).days < 2,
    })

print(f"{sum(len(v) for v in by_lane.values())} open cards across {len(by_lane)} lanes\n")
order = {"priority:high": 0, "priority:medium": 1, "priority:low": 2, "priority:—": 3}

blocked_all, ready_all = [], []
for lane, items in sorted(by_lane.items(), key=lambda kv: -len(kv[1])):
    hi = sum(1 for i in items if i["prio"] == "priority:high")
    nw = sum(1 for i in items if i["new"])
    print(f"  {lane:<22} {len(items):>3} cards  ({hi} high, {nw} filed <48h)")
    blocked_all += [i for i in items if i["blocked"]]
    ready_all += [i for i in items if i["ready"] and not i["blocked"]]

print(f"\n{'='*78}\nBLOCKED ON A HUMAN — {len(blocked_all)}\n{'='*78}")
for i in sorted(blocked_all, key=lambda x: order.get(x["prio"], 3))[:14]:
    print(f"  [{i['prio']:<16}] {i['name'][:66]}\n      {i['url']}")

print(f"\n{'='*78}\nREADY TO WORK, NOT BLOCKED — {len(ready_all)}\n{'='*78}")
for i in sorted(ready_all, key=lambda x: order.get(x["prio"], 3))[:14]:
    print(f"  [{i['prio']:<16}] {i['name'][:66]}\n      {i['url']}")
