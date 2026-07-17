#!/usr/bin/env python3
"""Conductor DB layer (Slice A) — CRUD + event log over agents.missions / mission_subtasks
/ mission_events. The mission record is the source of truth (resumable + audit); the
mission_events log doubles as "logs progress" and stitches to the knowledge graph via
memory_*.mission_id."""
import os
import urllib.parse
import uuid

import psycopg
from psycopg.types.json import Jsonb

REPO = os.environ.get("AGENTS_NEXUS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JSONB = {"plan", "verdict", "result", "payload"}   # columns adapted as JSONB


def _dsn(url=None):
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        try:
            for ln in open(os.path.join(REPO, ".env"), errors="replace"):
                if ln.startswith("DATABASE_URL="):
                    url = ln.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    if not url:
        raise RuntimeError("DATABASE_URL not set and not found in .env")
    # libpq rejects `search_path` as a URI query param — strip it; we SET it after
    # connect and fully-qualify table names, so nothing depends on it anyway.
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != "search_path"]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def _wrap(col, val):
    return Jsonb(val) if col in _JSONB and val is not None else val


class Db:
    def __init__(self, url=None):
        self.conn = psycopg.connect(_dsn(url), autocommit=True)
        self.conn.execute("SET search_path TO agents, public")

    def close(self):
        self.conn.close()

    def _rows(self, cur):
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    # ── missions ────────────────────────────────────────────────────────────
    def create_mission(self, goal, type="building", route="conductor", repos=None,
                        datasources=None, created_by=None, device="", project="agents-nexus"):
        mid = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO missions (id, goal, type, route, status, repos, datasources,"
            " created_by, device, project, started_at)"
            " VALUES (%s,%s,%s,%s,'planning',%s,%s,%s,%s,%s, now())",
            (mid, goal, type, route, repos or [], datasources or [], created_by, device, project),
        )
        self.log_event(mid, "created", {"goal": goal})
        return mid

    def update_mission(self, mid, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = now()"
        self.conn.execute(f"UPDATE missions SET {sets} WHERE id = %s",
                          [_wrap(k, v) for k, v in fields.items()] + [mid])

    def get_mission(self, mid):
        cur = self.conn.execute("SELECT * FROM missions WHERE id = %s", (mid,))
        rows = self._rows(cur)
        return rows[0] if rows else None

    def finish_mission(self, mid, status):
        """Terminal state (done|failed|escalated) — stamps finished_at."""
        self.conn.execute(
            "UPDATE missions SET status = %s, finished_at = now(), updated_at = now() WHERE id = %s",
            (status, mid))

    def find_mission(self, prefix):
        """Resolve a mission by full id or id prefix (most recent match)."""
        rows = self._rows(self.conn.execute(
            "SELECT * FROM missions WHERE id LIKE %s ORDER BY created_at DESC LIMIT 1", (prefix + "%",)))
        return rows[0] if rows else None

    # ── subtasks ────────────────────────────────────────────────────────────
    def create_subtask(self, mid, subtask_key, goal, profile, repo=None, depends_on=None, effort="high"):
        sid = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO mission_subtasks (id, mission_id, subtask_key, goal, repo, profile,"
            " depends_on, effort) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (sid, mid, subtask_key, goal, repo, profile, depends_on or [], effort),
        )
        return sid

    def update_subtask(self, sid, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = now()"
        self.conn.execute(f"UPDATE mission_subtasks SET {sets} WHERE id = %s",
                          [_wrap(k, v) for k, v in fields.items()] + [sid])

    def list_subtasks(self, mid):
        return self._rows(self.conn.execute(
            "SELECT * FROM mission_subtasks WHERE mission_id = %s ORDER BY created_at", (mid,)))

    def get_subtask(self, sid):
        rows = self._rows(self.conn.execute("SELECT * FROM mission_subtasks WHERE id = %s", (sid,)))
        return rows[0] if rows else None

    # ── events ──────────────────────────────────────────────────────────────
    def log_event(self, mid, event_type, payload=None, subtask_id=None):
        self.conn.execute(
            "INSERT INTO mission_events (id, mission_id, subtask_id, event_type, payload)"
            " VALUES (%s,%s,%s,%s,%s)",
            (uuid.uuid4().hex, mid, subtask_id, event_type, Jsonb(payload or {})),
        )

    def list_events(self, mid):
        return self._rows(self.conn.execute(
            "SELECT event_type, subtask_id, ts, payload FROM mission_events"
            " WHERE mission_id = %s ORDER BY ts", (mid,)))
