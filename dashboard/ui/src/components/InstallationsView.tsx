import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchInstallations, fetchSparkIndexInfo, type InstallationInfo, type SparkIndexInfo } from '../hooks/useCommandCenterData.js';

const GREEN = '#89d185';
const AMBER = '#d9a441';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';

const WEEK = 7 * 86400;
const MONTH = 30 * 86400;

const cellStyle: React.CSSProperties = {
  padding: '4px 10px', fontSize: 16, whiteSpace: 'nowrap',
};

type SortCol = 'name' | 'indexedAt' | 'lastRemoteTs' | 'ageSeconds';

interface Props { onClose: () => void }

function formatAge(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function staleColor(ageSeconds: number | null): string {
  if (ageSeconds == null) return DIM;
  if (ageSeconds > MONTH) return RED;
  if (ageSeconds > WEEK) return AMBER;
  return GREEN;
}

const btnStyle: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid transparent',
  color: 'var(--pixel-text)', fontSize: 18, padding: '2px 8px',
  cursor: 'pointer', borderRadius: 0,
};

export function InstallationsView({ onClose }: Props) {
  const [rows, setRows] = useState<InstallationInfo[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [sortCol, setSortCol] = useState<SortCol>('ageSeconds');
  const [sortDesc, setSortDesc] = useState(true);
  const [filter, setFilter] = useState('');
  const [indexInfo, setIndexInfo] = useState<SparkIndexInfo | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const [data, info] = await Promise.all([fetchInstallations(), fetchSparkIndexInfo()]);
    setRows(data);
    setIndexInfo(info);
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggleSort = (col: SortCol) => {
    if (col === sortCol) {
      setSortDesc((d) => !d);
    } else {
      setSortCol(col);
      // Sensible default direction: names ascending, everything else descending.
      setSortDesc(col !== 'name');
    }
  };

  const visible = useMemo(() => {
    const all = rows ?? [];
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? all.filter((r) => r.name.toLowerCase().includes(q) || r.relPath.toLowerCase().includes(q))
      : all;
    const dir = sortDesc ? -1 : 1;
    return [...filtered].sort((a, b) => {
      let av: string | number;
      let bv: string | number;
      if (sortCol === 'name') { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
      else if (sortCol === 'indexedAt') { av = a.indexedAt; bv = b.indexedAt; }
      else if (sortCol === 'lastRemoteTs') { av = a.lastRemoteTs; bv = b.lastRemoteTs; }
      else { av = a.ageSeconds ?? -1; bv = b.ageSeconds ?? -1; }
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [rows, filter, sortCol, sortDesc]);

  const total = rows?.length ?? 0;
  const arrow = (col: SortCol) => (col === sortCol ? (sortDesc ? ' ▼' : ' ▲') : '');

  const columns: { key: SortCol; label: string }[] = [
    { key: 'name', label: 'Installation' },
    { key: 'indexedAt', label: 'Last Indexed' },
    { key: 'lastRemoteTs', label: 'Indexed Commit' },
    { key: 'ageSeconds', label: 'Status' },
  ];

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
        display: 'flex', alignItems: 'center', padding: '10px 16px',
        borderBottom: '2px solid var(--pixel-border)', flexShrink: 0,
      }}>
        <span style={{ fontSize: 22, color: 'var(--pixel-accent)', fontWeight: 'bold' }}>
          INSTALLATIONS ({total})
        </span>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter…"
          style={{
            marginLeft: 16, background: 'var(--pixel-btn-bg)',
            border: '2px solid var(--pixel-border)', color: 'var(--pixel-text)',
            fontSize: 16, padding: '2px 8px', borderRadius: 0, width: 220,
          }}
        />
        {filter.trim() && (
          <span style={{ marginLeft: 10, fontSize: 14, color: DIM }}>
            showing {visible.length} of {total}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          {loading && <span style={{ fontSize: 14, color: DIM }}>refreshing...</span>}
          <button onClick={() => void load()} style={btnStyle}>Refresh</button>
          <button onClick={onClose} style={btnStyle}>Close</button>
        </span>
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
        {rows == null ? (
          <div style={{ color: DIM, fontSize: 18 }}>Loading…</div>
        ) : total === 0 ? (
          <div style={{ color: DIM, fontSize: 18 }}>No installations found</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--pixel-border)' }}>
                {columns.map((c) => (
                  <th
                    key={c.key}
                    onClick={() => toggleSort(c.key)}
                    style={{ ...cellStyle, color: DIM, textAlign: 'left', fontWeight: 'normal', cursor: 'pointer', userSelect: 'none' }}
                  >
                    {c.label}{arrow(c.key)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={r.relPath} style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                  <td style={cellStyle}>
                    <span style={{ color: 'var(--pixel-text)' }}>{r.name}</span>
                    {r.relPath !== r.name && (
                      <span style={{ color: DIM, fontSize: 13, marginLeft: 8 }}>{r.relPath}</span>
                    )}
                  </td>
                  <td style={{ ...cellStyle, color: MID }}>
                    {r.indexedAt ? new Date(r.indexedAt).toLocaleString() : '—'}
                    <span style={{ color: DIM, marginLeft: 8 }}>({formatAge(r.ageSeconds)})</span>
                  </td>
                  <td style={{ ...cellStyle, color: MID }}>
                    {r.lastRemoteTs > 0 ? new Date(r.lastRemoteTs * 1000).toLocaleDateString() : '—'}
                  </td>
                  <td style={cellStyle}>
                    <span style={{
                      display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                      background: staleColor(r.ageSeconds),
                    }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
