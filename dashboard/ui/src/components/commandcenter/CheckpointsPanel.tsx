import { useState } from 'react';

import type { CacheEntry, CheckpointEntry } from '../../hooks/useCommandCenterData.js';

const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';
const AMBER = '#cca700';

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  return `${(bytes / 1024).toFixed(1)}KB`;
}

const cellStyle: React.CSSProperties = {
  padding: '3px 8px', fontSize: 16, whiteSpace: 'nowrap',
};

interface Props {
  checkpoints: CheckpointEntry[] | null;
  cache: CacheEntry[] | null;
}

export function CheckpointsPanel({ checkpoints, cache }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div>
      <div style={{ fontSize: 16, color: DIM, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
        Checkpoints
      </div>
      {(!checkpoints || checkpoints.length === 0) ? (
        <div style={{ color: DIM, fontSize: 16, marginBottom: 12 }}>None in the last 7 days</div>
      ) : (
        <div style={{ marginBottom: 12 }}>
          {checkpoints.map((cp) => (
            <div
              key={cp.file}
              onClick={() => setExpanded(expanded === cp.file ? null : cp.file)}
              style={{
                padding: '4px 8px', cursor: 'pointer',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
                background: expanded === cp.file ? 'rgba(255,255,255,0.05)' : 'transparent',
              }}
            >
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ fontSize: 14, color: DIM }}>{cp.date}</span>
                <span style={{ fontSize: 16, color: 'var(--pixel-accent)' }}>{cp.project}</span>
                <span style={{ fontSize: 14, color: DIM, marginLeft: 'auto' }}>{formatSize(cp.size)}</span>
              </div>
              {expanded === cp.file && (
                <div style={{ fontSize: 14, color: MID, marginTop: 4 }}>
                  {cp.branch && <div>Branch: {cp.branch}</div>}
                  {cp.changes && <div>Changes: {cp.changes}</div>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div style={{ fontSize: 16, color: DIM, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
        Cache
      </div>
      {(!cache || cache.length === 0) ? (
        <div style={{ color: DIM, fontSize: 16 }}>No cached sessions</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <tbody>
            {cache.map((c) => (
              <tr key={c.file} style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <td style={{ ...cellStyle, color: c.stale ? AMBER : '#fff' }}>{c.project}</td>
                <td style={{ ...cellStyle, color: DIM }}>{formatSize(c.size)}</td>
                <td style={{ ...cellStyle, color: c.stale ? AMBER : DIM }}>{formatAge(c.age)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
