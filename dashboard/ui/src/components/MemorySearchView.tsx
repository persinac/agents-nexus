import { useState } from 'react';

import { searchMemory, type MemoryHit } from '../hooks/useCommandCenterData.js';

const ACCENT = 'var(--pixel-accent)';
const DIM = 'rgba(255,255,255,0.4)';
const MID = 'rgba(255,255,255,0.65)';
const HILITE = 'rgba(204,167,0,0.40)';

type Mode = 'semantic' | 'keyword';

interface Props { onClose: () => void }

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

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Highlight occurrences of any query term (case-insensitive) in text.
function highlight(text: string, terms: string[]): React.ReactNode {
  if (!terms.length || !text) return text;
  const lower = new Set(terms.map((t) => t.toLowerCase()));
  const re = new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'gi');
  const parts = text.split(re);
  return parts.map((p, i) =>
    lower.has(p.toLowerCase())
      ? <mark key={i} style={{ background: HILITE, color: 'inherit', borderRadius: 2 }}>{p}</mark>
      : <span key={i}>{p}</span>,
  );
}

const inputStyle: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid var(--pixel-border)',
  color: 'var(--pixel-text)', fontSize: 16, padding: '4px 10px', borderRadius: 0,
};

const btnBase: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid transparent',
  color: 'var(--pixel-text)', fontSize: 16, padding: '4px 12px',
  cursor: 'pointer', borderRadius: 0,
};

const btnActive: React.CSSProperties = {
  ...btnBase, background: 'var(--pixel-active-bg)', border: `2px solid ${ACCENT}`,
};

export function MemorySearchView({ onClose }: Props) {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<Mode>('semantic');
  const [project, setProject] = useState('all');
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [terms, setTerms] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const run = async () => {
    const q = query.trim();
    if (!q || loading) return;
    setLoading(true);
    const results = await searchMemory(q, mode, project.trim() || 'all', 15);
    setHits(results);
    setTerms(q.split(/\s+/).filter((t) => t.length >= 2));
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
    <div
      className="mono-font"
      style={{
        position: 'absolute', inset: 0, zIndex: 50,
        background: 'var(--pixel-bg)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
        borderBottom: '2px solid var(--pixel-border)', flexShrink: 0, flexWrap: 'wrap',
      }}>
        <span style={{ fontSize: 22, color: ACCENT, fontWeight: 'bold', marginRight: 8 }}>
          MEMORY SEARCH
        </span>
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void run(); }}
          placeholder="search memory notes…"
          style={{ ...inputStyle, flex: 1, minWidth: 220 }}
        />
        <button style={mode === 'semantic' ? btnActive : btnBase} onClick={() => setMode('semantic')} title="Embedding similarity (slower)">semantic</button>
        <button style={mode === 'keyword' ? btnActive : btnBase} onClick={() => setMode('keyword')} title="Literal text/tag match (fast)">keyword</button>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void run(); }}
          placeholder="project (all)"
          style={{ ...inputStyle, width: 140 }}
          title="Project to scope to, or 'all'"
        />
        <button style={btnBase} onClick={() => void run()} disabled={loading}>
          {loading ? '…' : 'Search'}
        </button>
        <button style={btnBase} onClick={onClose}>Close</button>
      </div>

      {/* Results */}
      <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
        {hits == null ? (
          <div style={{ color: DIM, fontSize: 16 }}>
            Search the agent-memory store. Semantic uses embeddings; keyword matches text/tags. Matched terms are highlighted.
          </div>
        ) : hits.length === 0 ? (
          <div style={{ color: DIM, fontSize: 16 }}>No matches.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 1000 }}>
            <div style={{ fontSize: 13, color: DIM }}>{hits.length} result{hits.length === 1 ? '' : 's'}</div>
            {hits.map((h) => {
              const isOpen = expanded.has(h.id);
              const long = h.content.length > 320;
              const shown = isOpen || !long ? h.content : `${h.content.slice(0, 320)}…`;
              return (
                <div
                  key={h.id}
                  onClick={() => long && toggle(h.id)}
                  style={{
                    border: '1px solid var(--pixel-border)', padding: '8px 12px',
                    cursor: long ? 'pointer' : 'default',
                    background: isOpen ? 'rgba(255,255,255,0.04)' : 'transparent',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                    {long && <span style={{ fontSize: 12, color: DIM, width: 10 }}>{isOpen ? '▾' : '▸'}</span>}
                    <span style={{ fontSize: 16, color: 'var(--pixel-text)' }}>
                      {highlight(h.title || h.content.slice(0, 60), terms)}
                    </span>
                    {h.project && <span style={{ fontSize: 12, color: ACCENT }}>[{h.project}]</span>}
                    {h.tags.map((t) => (
                      <span key={t} style={{ fontSize: 12, color: DIM }}>#{t}</span>
                    ))}
                    <span style={{ fontSize: 12, color: DIM, marginLeft: 'auto' }}>{formatAge(h.created_at)}</span>
                  </div>
                  <div style={{ fontSize: 14, color: MID, marginTop: 4, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
                    {highlight(shown, terms)}
                  </div>
                  {long && (
                    <div style={{ fontSize: 11, color: DIM, marginTop: 4 }}>
                      {isOpen ? 'click to collapse' : 'click to expand'}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
