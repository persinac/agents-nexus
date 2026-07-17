import ForceGraph from 'force-graph';
import { useEffect, useRef, useState } from 'react';

import {
  fetchMemoryGraph,
  type MemoryGraph,
  type MemoryGraphLink,
  type MemoryGraphNode,
} from '../hooks/useCommandCenterData.js';

const ACCENT = 'var(--pixel-accent)';
const DIM = 'rgba(255,255,255,0.4)';
const MID = 'rgba(255,255,255,0.65)';

// Canvas fillStyle can't read CSS vars, so node colors are concrete hex.
const NOTE_COLOR = '#cca700'; // gold — curated notes
const ENTITY_COLOR = '#6c9ef8'; // blue — files / [[wikilinks]] / @mentions
const LINK_COLOR = 'rgba(255,255,255,0.13)';

interface Props { onClose: () => void }

const inputStyle: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid var(--pixel-border)',
  color: 'var(--pixel-text)', fontSize: 16, padding: '4px 10px', borderRadius: 0,
};
const btnBase: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid transparent',
  color: 'var(--pixel-text)', fontSize: 16, padding: '4px 12px', cursor: 'pointer', borderRadius: 0,
};

function formatAge(iso: string): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const secs = Math.floor((Date.now() - t) / 1000);
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function MemoryGraphView({ onClose }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<ForceGraph<MemoryGraphNode, MemoryGraphLink> | null>(null);
  const [project, setProject] = useState('all');
  const [limit, setLimit] = useState(150);
  const [data, setData] = useState<MemoryGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<MemoryGraphNode | null>(null);

  const load = async (p = project, n = limit) => {
    setLoading(true);
    setSelected(null);
    const g = await fetchMemoryGraph(p.trim() || 'all', n);
    setData(g);
    setLoading(false);
  };

  // initial load
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, []);

  // (re)build the force graph whenever data changes
  useEffect(() => {
    const el = containerRef.current;
    if (!el || !data) return;
    const graph = new ForceGraph<MemoryGraphNode, MemoryGraphLink>(el)
      .width(el.clientWidth)
      .height(el.clientHeight)
      .backgroundColor('rgba(0,0,0,0)')
      .nodeId('id')
      .nodeRelSize(4)
      .nodeVal((n) => (n.type === 'entity' ? 1.5 : 4))
      .nodeColor((n) => (n.type === 'entity' ? ENTITY_COLOR : NOTE_COLOR))
      .nodeLabel((n) => n.label)
      .linkColor(() => LINK_COLOR)
      .linkWidth(1)
      .onNodeClick((n) => setSelected(n))
      .graphData({
        // force-graph mutates node/link objects (x/y, resolves source/target),
        // so hand it shallow copies, not our React state.
        nodes: data.nodes.map((n) => ({ ...n })),
        links: data.links.map((l) => ({ ...l })),
      });
    graphRef.current = graph;

    const onResize = () => { graph.width(el.clientWidth).height(el.clientHeight); };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      graph._destructor();
      el.innerHTML = '';
      graphRef.current = null;
    };
  }, [data]);

  const meta = data?.meta;
  const degree = selected
    ? (data?.links.filter((l) => l.source === selected.id || l.target === selected.id).length ?? 0)
    : 0;

  return (
    <div
      className="mono-font"
      style={{
        position: 'absolute', inset: 0, zIndex: 50,
        background: 'var(--pixel-bg)', display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
        borderBottom: '2px solid var(--pixel-border)', flexShrink: 0, flexWrap: 'wrap',
      }}>
        <span style={{ fontSize: 22, color: ACCENT, fontWeight: 'bold', marginRight: 8 }}>MEMORY GRAPH</span>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void load(); }}
          placeholder="project (all)"
          style={{ ...inputStyle, width: 160 }}
          title="Project to scope to, or 'all'"
        />
        <input
          type="number"
          value={limit}
          min={10}
          max={500}
          onChange={(e) => setLimit(Math.max(10, Math.min(500, parseInt(e.target.value || '150', 10))))}
          style={{ ...inputStyle, width: 90 }}
          title="Max notes to seed the graph"
        />
        <button style={btnBase} onClick={() => void load()} disabled={loading}>{loading ? '…' : 'Reload'}</button>
        {/* legend */}
        <span style={{ fontSize: 13, color: DIM, display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8 }}>
          <span style={{ width: 10, height: 10, background: NOTE_COLOR, display: 'inline-block', borderRadius: '50%' }} /> note
          <span style={{ width: 10, height: 10, background: ENTITY_COLOR, display: 'inline-block', borderRadius: '50%', marginLeft: 8 }} /> entity
        </span>
        {meta && (
          <span style={{ fontSize: 13, color: DIM }}>
            {meta.notes} notes · {meta.entities} entities · {meta.links} links
          </span>
        )}
        <button style={{ ...btnBase, marginLeft: 'auto' }} onClick={onClose}>Close</button>
      </div>

      {/* Body: graph canvas + detail panel */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
          {/* force-graph mounts a <canvas> here imperatively — keep it free of React children */}
          <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
          {loading && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: DIM, fontSize: 16, pointerEvents: 'none' }}>
              loading graph…
            </div>
          )}
          {data && data.nodes.length === 0 && !loading && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: DIM, fontSize: 16, pointerEvents: 'none' }}>
              No memory nodes for this scope. Try project “all”.
            </div>
          )}
        </div>

        {selected && (
          <div style={{
            width: 360, flexShrink: 0, borderLeft: '2px solid var(--pixel-border)',
            padding: 16, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 8,
          }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span style={{ fontSize: 12, color: selected.type === 'entity' ? ENTITY_COLOR : NOTE_COLOR }}>
                {selected.type === 'entity' ? `entity · ${selected.entity_type ?? '?'}` : 'note'}
              </span>
              <span style={{ fontSize: 12, color: DIM, marginLeft: 'auto' }}>{degree} link{degree === 1 ? '' : 's'}</span>
              <button style={{ ...btnBase, padding: '0 8px', fontSize: 13 }} onClick={() => setSelected(null)}>×</button>
            </div>
            <div style={{ fontSize: 17, color: 'var(--pixel-text)', wordBreak: 'break-word' }}>
              {selected.title || selected.label}
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {selected.project && <span style={{ fontSize: 12, color: ACCENT }}>[{selected.project}]</span>}
              {(selected.tags ?? []).map((t) => <span key={t} style={{ fontSize: 12, color: DIM }}>#{t}</span>)}
              {selected.created_at && <span style={{ fontSize: 12, color: DIM }}>{formatAge(selected.created_at)}</span>}
            </div>
            {selected.content && (
              <div style={{ fontSize: 14, color: MID, whiteSpace: 'pre-wrap', lineHeight: 1.5, marginTop: 4 }}>
                {selected.content}
              </div>
            )}
            {selected.type === 'entity' && (
              <div style={{ fontSize: 13, color: DIM, marginTop: 4 }}>
                Referenced by {degree} note{degree === 1 ? '' : 's'} in the graph.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
