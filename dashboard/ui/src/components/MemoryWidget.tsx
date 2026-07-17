import { useCallback, useEffect, useState } from 'react';

// ── Types ──────────────────────────────────────────────────────────────────

interface HealthStats {
  events_1h: number;
  events_24h: number;
  notes_total: number;
  notes_embedded: number;
  last_event: { ts: string; type: string; repo: string | null } | null;
  last_note: { ts: string; title: string; content: string } | null;
  error?: string;
}

interface MemoryNote {
  title: string;
  content: string;
  tags: string[];
  created_at: string;
  access_count: number;
  project: string;
}

interface MemoryWidgetProps {
  onClose: () => void;
}

// ── Constants ──────────────────────────────────────────────────────────────

const BASE = 20; // base font size in px — scale everything relative to this

const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';
const FULL  = '#fff';
const GREEN = '#89d185';
const AMBER = '#cca700';
const RED   = '#f14c4c';
const BLUE  = 'var(--pixel-accent)';

// ── Helpers ────────────────────────────────────────────────────────────────

function ageLabel(tsStr: string): string {
  if (!tsStr) return '';
  try {
    const secs = Math.floor((Date.now() - new Date(tsStr).getTime()) / 1000);
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  } catch {
    return '';
  }
}

function trunc(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…';
}

function apiBase(): string {
  return `http://${window.location.hostname || 'localhost'}:8420`;
}

// ── Sub-components ─────────────────────────────────────────────────────────

function StatRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, fontSize: BASE, padding: '2px 0', lineHeight: 1.5 }}>
      <span style={{ color: DIM, minWidth: 90, flexShrink: 0 }}>{label}</span>
      <span style={{ color: FULL }}>{children}</span>
    </div>
  );
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span style={{
      display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
      background: ok ? GREEN : RED, marginRight: 3, verticalAlign: 'middle',
    }} />
  );
}

function NoteCard({ note }: { note: MemoryNote }) {
  const [expanded, setExpanded] = useState(false);
  const preview = note.content.length > 180 ? note.content.slice(0, 180) + '…' : note.content;
  const truncated = note.content.length > 180;

  return (
    <div style={{ border: '1px solid var(--pixel-border)', padding: '7px 9px', background: 'var(--pixel-btn-bg)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 3, flexWrap: 'wrap' }}>
        <span style={{ fontSize: BASE, color: FULL, fontWeight: 600, flex: 1, minWidth: 0 }}>
          {trunc(note.title || note.content.slice(0, 40), 44)}
        </span>
        {note.project && (
          <span style={{ fontSize: BASE - 3, color: DIM, border: '1px solid var(--pixel-border)', padding: '0 4px' }}>
            {note.project}
          </span>
        )}
      </div>
      <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginBottom: 4, alignItems: 'center' }}>
        {note.tags.map((t) => (
          <span key={t} style={{ fontSize: BASE - 3, color: BLUE }}>#{t}</span>
        ))}
        {note.created_at && (
          <span style={{ fontSize: BASE - 3, color: DIM, marginLeft: 'auto' }}>
            {ageLabel(note.created_at)}
          </span>
        )}
      </div>
      <div style={{ fontSize: BASE - 1, color: MID, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
        {expanded ? note.content : preview}
      </div>
      {truncated && (
        <button
          onClick={() => setExpanded((v) => !v)}
          style={{ marginTop: 4, padding: '1px 6px', fontSize: BASE - 3, color: BLUE, background: 'transparent', border: `1px solid ${BLUE}`, borderRadius: 0, cursor: 'pointer' }}
        >
          {expanded ? 'less' : 'more'}
        </button>
      )}
    </div>
  );
}

// ── Main widget ────────────────────────────────────────────────────────────

export function MemoryWidget({ onClose }: MemoryWidgetProps) {
  const [stats, setStats] = useState<HealthStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [notesExpanded, setNotesExpanded] = useState(false);
  const [notes, setNotes] = useState<MemoryNote[]>([]);
  const [notesLoading, setNotesLoading] = useState(false);
  const [notesError, setNotesError] = useState(false);
  const [notesFetched, setNotesFetched] = useState(false);

  const fetchStats = useCallback(() => {
    fetch(`${apiBase()}/api/memory/stats`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: HealthStats) => { setStats(data); setStatsLoading(false); })
      .catch(() => {
        setStats({ error: 'unreachable', events_1h: 0, events_24h: 0, notes_total: 0, notes_embedded: 0, last_event: null, last_note: null });
        setStatsLoading(false);
      });
  }, []);

  useEffect(() => {
    fetchStats();
    const id = setInterval(fetchStats, 30_000);
    return () => clearInterval(id);
  }, [fetchStats]);

  const fetchNotes = useCallback(() => {
    setNotesLoading(true);
    setNotesError(false);
    fetch(`${apiBase()}/api/memory`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: MemoryNote[]) => { setNotes(data); setNotesLoading(false); setNotesFetched(true); })
      .catch(() => { setNotesError(true); setNotesLoading(false); });
  }, []);

  const handleToggleNotes = () => {
    const next = !notesExpanded;
    setNotesExpanded(next);
    if (next && !notesFetched) fetchNotes();
  };

  const dbOk = stats !== null && !stats.error;

  return (
    <div className="mono-font" style={{
      position: 'absolute', top: 8, right: 8, width: 560,
      maxHeight: 'calc(100% - 70px)', display: 'flex', flexDirection: 'column',
      zIndex: 45, gap: 4, fontSize: BASE,
    }}>
      {/* ── Health panel ────────────────────────────────────────────────── */}
      <div style={{ background: 'var(--pixel-bg)', border: '2px solid var(--pixel-border)', boxShadow: 'var(--pixel-shadow)', flexShrink: 0 }}>

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 9px', borderBottom: '1px solid var(--pixel-border)' }}>
          <span style={{ fontSize: BASE, color: FULL, fontWeight: 700, flex: 1, letterSpacing: '0.02em' }}>
            memory health
          </span>
          <span style={{ fontSize: BASE - 3, color: DIM, display: 'flex', alignItems: 'center', gap: 6 }}>
            <span><StatusDot ok={dbOk} />db</span>
            <span><StatusDot ok={dbOk} />mcp</span>
          </span>
          <button onClick={fetchStats} title="Refresh"
            style={{ fontSize: BASE, color: DIM, background: 'transparent', border: 'none', cursor: 'pointer', padding: '0 2px' }}>
            ↺
          </button>
          <button onClick={onClose} title="Close"
            style={{ fontSize: BASE, color: DIM, background: 'transparent', border: 'none', cursor: 'pointer', padding: '0 2px' }}>
            ✕
          </button>
        </div>

        {/* Stats body */}
        <div style={{ padding: '7px 10px' }}>
          {statsLoading && <div style={{ fontSize: BASE - 2, color: DIM }}>Loading…</div>}
          {!statsLoading && stats?.error && <div style={{ fontSize: BASE - 2, color: RED }}>DB unreachable</div>}
          {!statsLoading && stats && !stats.error && (<>
            <StatRow label="events">
              <strong>{stats.events_1h}</strong>
              <span style={{ color: DIM }}>/1h{'  '}</span>
              <span style={{ color: MID }}>{stats.events_24h}</span>
              <span style={{ color: DIM }}>/24h</span>
            </StatRow>
            <StatRow label="notes">
              <strong>{stats.notes_total}</strong>
              {'  '}
              <span style={{ color: stats.notes_embedded === stats.notes_total ? GREEN : AMBER, fontSize: BASE }}>
                emb {stats.notes_embedded}/{stats.notes_total}
              </span>
            </StatRow>
            {stats.last_note && (
              <StatRow label="last note">
                {trunc(stats.last_note.title || stats.last_note.content, 20)}
                {'  '}
                <span style={{ color: DIM, fontSize: BASE }}>{ageLabel(stats.last_note.ts)}</span>
              </StatRow>
            )}
            {stats.last_event && (
              <StatRow label="last event">
                {trunc(stats.last_event.type, 14)}
                {'  '}
                <span style={{ color: DIM, fontSize: BASE }}>
                  {trunc(stats.last_event.repo || '—', 10)}{'  '}{ageLabel(stats.last_event.ts)}
                </span>
              </StatRow>
            )}
          </>)}
        </div>

        {/* Drill-in button */}
        <div style={{ borderTop: '1px solid var(--pixel-border)', padding: '5px 8px' }}>
          <button onClick={handleToggleNotes} style={{
            width: '100%', fontSize: BASE, padding: '4px 0',
            background: notesExpanded ? 'var(--pixel-active-bg)' : 'var(--pixel-btn-bg)',
            color: notesExpanded ? FULL : MID,
            border: `1px solid ${notesExpanded ? 'var(--pixel-accent)' : 'var(--pixel-border)'}`,
            borderRadius: 0, cursor: 'pointer', textAlign: 'center',
          }}>
            {notesExpanded ? 'hide notes ▲' : `view ${stats && !stats.error ? stats.notes_total : ''} notes ▼`}
          </button>
        </div>
      </div>

      {/* ── Notes list ──────────────────────────────────────────────────── */}
      {notesExpanded && (
        <div style={{
          flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 4,
          background: 'var(--pixel-bg)', border: '2px solid var(--pixel-border)',
          boxShadow: 'var(--pixel-shadow)', padding: 6,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
            <span style={{ fontSize: BASE - 2, color: DIM }}>
              {notesFetched ? `${notes.length} notes` : ''}
            </span>
            {notesFetched && (
              <button onClick={fetchNotes} title="Refresh"
                style={{ fontSize: BASE, color: DIM, background: 'transparent', border: 'none', cursor: 'pointer' }}>
                ↺
              </button>
            )}
          </div>
          {notesLoading && <div style={{ fontSize: BASE - 2, color: DIM, padding: '6px 0' }}>Loading…</div>}
          {notesError && <div style={{ fontSize: BASE, color: RED, padding: '6px 0' }}>Could not fetch notes.</div>}
          {!notesLoading && !notesError && notes.length === 0 && (
            <div style={{ fontSize: BASE - 2, color: DIM, padding: '6px 0' }}>No notes found.</div>
          )}
          {!notesLoading && notes.map((note, i) => <NoteCard key={i} note={note} />)}
        </div>
      )}
    </div>
  );
}
