#!/usr/bin/env python3
"""memory-graph.py — export the agent-memory knowledge graph as nodes + links.

Companion to memory-search.py. Reads agents.memory_nodes / memory_links /
memory_entities directly via DATABASE_URL (parameterized — never interpolates
input into SQL) and emits a force-graph-ready JSON payload:

  {"nodes": [{id,type,label,...}], "links": [{source,target,type,confidence}],
   "meta": {...}}

The graph is bipartite-ish: note nodes (memory_nodes) plus entity nodes
(memory_entities — files / [[wikilinks]] / @mentions). `mentions` links go
note->entity; `temporal` links go note->note (to_entity holds a node id). We
materialise every link target so there are no dangling edges.

Usage:
  memory-graph.py [--project NAME|all] [--limit N] [--format json]

Fail-open: prints {"nodes":[],"links":[]} on any error.
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path

ENV_FILE = Path(os.environ.get("AGENTS_NEXUS_DIR", Path.home() / "repos/agents-nexus")) / ".env"

# source-ish extensions used to guess entity_type when not recorded
_FILE_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def db_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    url = re.sub(r"[&?]search_path=[^&]*", "", url)
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def _entity_type(name: str, recorded: str | None) -> str:
    if recorded:
        return recorded
    if name.startswith("@"):
        return "mention"
    if "/" in name or _FILE_RE.search(name):
        return "file"
    return "wikilink"


def build_graph(project: str, limit: int) -> dict:
    url = db_url()
    if not url:
        return {"nodes": [], "links": []}
    all_projects = project == "" or project.lower() == "all"
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # 1. seed note set (most recently touched)
                if all_projects:
                    cur.execute(
                        """
                        SELECT id, title, content, tags, project, created_at
                        FROM agents.memory_nodes
                        ORDER BY COALESCE(last_accessed, created_at) DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, title, content, tags, project, created_at
                        FROM agents.memory_nodes
                        WHERE project = %s
                        ORDER BY COALESCE(last_accessed, created_at) DESC
                        LIMIT %s
                        """,
                        (project, limit),
                    )
                seed = cur.fetchall()
                nodes: dict[str, dict] = {}
                for r in seed:
                    nodes[r[0]] = {
                        "id": r[0],
                        "type": "note",
                        "label": (r[1] or (r[2] or "")[:48] or r[0]),
                        "title": r[1] or "",
                        "content": (r[2] or "")[:800],
                        "tags": list(r[3]) if r[3] else [],
                        "project": r[4] or "",
                        "created_at": str(r[5]) if r[5] else "",
                    }
                if not nodes:
                    return {"nodes": [], "links": [], "meta": {"notes": 0, "entities": 0, "links": 0, "project": project}}

                # 2. links out of the seed notes
                seed_ids = list(nodes.keys())
                cur.execute(
                    """
                    SELECT from_node, to_entity, link_type, confidence
                    FROM agents.memory_links
                    WHERE from_node = ANY(%s)
                    """,
                    (seed_ids,),
                )
                raw_links = cur.fetchall()
                targets = {r[1] for r in raw_links}

                # 3. which targets are notes (temporal edges) vs entities (mentions)?
                target_notes: set[str] = set()
                if targets:
                    cur.execute(
                        "SELECT id, title, content, tags, project, created_at FROM agents.memory_nodes WHERE id = ANY(%s)",
                        (list(targets),),
                    )
                    for r in cur.fetchall():
                        target_notes.add(r[0])
                        nodes.setdefault(r[0], {
                            "id": r[0],
                            "type": "note",
                            "label": (r[1] or (r[2] or "")[:48] or r[0]),
                            "title": r[1] or "",
                            "content": (r[2] or "")[:800],
                            "tags": list(r[3]) if r[3] else [],
                            "project": r[4] or "",
                            "created_at": str(r[5]) if r[5] else "",
                        })

                entity_names = [t for t in targets if t not in target_notes]
                recorded_types: dict[str, str] = {}
                if entity_names:
                    cur.execute(
                        "SELECT name, entity_type FROM agents.memory_entities WHERE name = ANY(%s)",
                        (entity_names,),
                    )
                    recorded_types = {r[0]: r[1] for r in cur.fetchall()}
                for name in entity_names:
                    nodes.setdefault(name, {
                        "id": name,
                        "type": "entity",
                        "label": name,
                        "entity_type": _entity_type(name, recorded_types.get(name)),
                    })

        links = [
            {"source": fn, "target": te, "type": lt or "reference", "confidence": float(cf) if cf is not None else 1.0}
            for (fn, te, lt, cf) in raw_links
            if fn in nodes and te in nodes
        ]
        note_ct = sum(1 for n in nodes.values() if n["type"] == "note")
        return {
            "nodes": list(nodes.values()),
            "links": links,
            "meta": {
                "notes": note_ct,
                "entities": len(nodes) - note_ct,
                "links": len(links),
                "project": project,
                "limit": limit,
            },
        }
    except Exception:
        return {"nodes": [], "links": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="all")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--format", choices=["json"], default="json")
    args = parser.parse_args()

    load_env()
    print(json.dumps(build_graph(args.project, args.limit)))


if __name__ == "__main__":
    main()
