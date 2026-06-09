import { useState } from 'react';

import { searchMemory, type MemoryHit } from '../../hooks/useCommandCenterData.js';

const ACCENT = 'var(--pixel-accent)';
const DIM = 'rgba(255,255,255,0.4)';
const MID = 'rgba(255,255,255,0.65)';

type Mode = 'semantic' | 'keyword';

function formatAge(iso: string): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const secs = Math.floor((Date.now() - t) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

const inputStyle: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid var(--pixel-border)',
  color: 'var(--pixel-text)', fontSize: 15, padding: '3px 8px', borderRadius: 0,
};

const btnBase: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid transparent',
  color: 'var(--pixel-text)', fontSize: 15, padding: '3px 10px',
  cursor: 'pointer', borderRadius: 0,
};

const btnActive: React.CSSProperties = {
  ...btnBase, background: 'var(--pixel-active-bg)', border: `2px solid ${ACCENT}`,
};

export function MemoryPanel() {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<Mode>('semantic');
  const [project, setProject] = useState('all');
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const run = async () => {
    const q = query.trim();
    if (!q || loading) return;
    setLoading(true);
    const results = await searchMemory(q, mode, project.trim() || 'all', 10);
    setHits(results);
    setExpanded(new Set());
    setLoading(false);
  };

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void run(); }}
          placeholder="search memory notes…"
          style={{ ...inputStyle, flex: 1, minWidth: 180 }}
        />
        <button style={mode === 'semantic' ? btnActive : btnBase} onClick={() => setMode('semantic')} title="Embedding similarity (slower)">semantic</button>
        <button style={mode === 'keyword' ? btnActive : btnBase} onClick={() => setMode('keyword')} title="Literal text/tag match (fast)">keyword</button>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void run(); }}
          placeholder="project (all)"
          style={{ ...inputStyle, width: 130 }}
          title="Project to scope to, or 'all'"
        />
        <button style={btnBase} onClick={() => void run()} disabled={loading}>
          {loading ? '…' : 'Search'}
        </button>
      </div>

      {hits == null ? (
        <div style={{ color: DIM, fontSize: 15 }}>
          Search the agent-memory store. Semantic uses embeddings; keyword matches text/tags.
        </div>
      ) : hits.length === 0 ? (
        <div style={{ color: DIM, fontSize: 15 }}>No matches.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {hits.map((h) => {
            const isOpen = expanded.has(h.id);
            const long = h.content.length > 280;
            return (
              <div
                key={h.id}
                onClick={() => long && toggle(h.id)}
                style={{
                  border: '1px solid var(--pixel-border)', padding: '6px 10px',
                  cursor: long ? 'pointer' : 'default',
                  background: isOpen ? 'rgba(255,255,255,0.04)' : 'transparent',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                  {long && <span style={{ fontSize: 12, color: DIM, width: 10 }}>{isOpen ? '▾' : '▸'}</span>}
                  <span style={{ fontSize: 15, color: 'var(--pixel-text)' }}>
                    {h.title || h.content.slice(0, 60)}
                  </span>
                  {h.project && <span style={{ fontSize: 12, color: ACCENT }}>[{h.project}]</span>}
                  {h.tags.map((t) => (
                    <span key={t} style={{ fontSize: 12, color: DIM }}>#{t}</span>
                  ))}
                  <span style={{ fontSize: 12, color: DIM, marginLeft: 'auto' }}>{formatAge(h.created_at)}</span>
                </div>
                <div style={{ fontSize: 13, color: MID, marginTop: 3, whiteSpace: 'pre-wrap' }}>
                  {isOpen || !long ? h.content : `${h.content.slice(0, 280)}…`}
                </div>
                {long && (
                  <div style={{ fontSize: 11, color: DIM, marginTop: 3 }}>
                    {isOpen ? 'click to collapse' : 'click to expand'}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
