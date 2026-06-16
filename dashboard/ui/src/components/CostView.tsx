import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchCostData, type CostDay } from '../hooks/useCommandCenterData.js';

const DIM = 'rgba(255,255,255,0.4)';
const MID = 'rgba(255,255,255,0.65)';
const BAR = '#4ea1d3';

const cellStyle: React.CSSProperties = {
  padding: '4px 10px', fontSize: 16, whiteSpace: 'nowrap',
};

const btnStyle: React.CSSProperties = {
  background: 'var(--pixel-btn-bg)', border: '2px solid transparent',
  color: 'var(--pixel-text)', fontSize: 18, padding: '2px 8px',
  cursor: 'pointer', borderRadius: 0,
};

interface Props { onClose: () => void }

// Stable-ish color per model so the same model reads the same across rows.
function modelColor(model: string): string {
  const palette = ['#89d185', '#d9a441', '#c586c0', '#4ea1d3', '#f14c4c', '#dcdcaa'];
  let h = 0;
  for (let i = 0; i < model.length; i++) h = (h * 31 + model.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}

function fmtUsd(n: number): string {
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function CostView({ onClose }: Props) {
  const [rows, setRows] = useState<CostDay[] | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setRows(await fetchCostData());
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const { visible, maxCost, total, byModel } = useMemo(() => {
    const all = rows ?? [];
    const maxCost = all.reduce((m, r) => Math.max(m, r.total_cost), 0) || 1;
    const total = all.reduce((s, r) => s + r.total_cost, 0);
    const byModel = new Map<string, number>();
    for (const r of all) byModel.set(r.model, (byModel.get(r.model) ?? 0) + r.total_cost);
    // rows already arrive day DESC, cost DESC from the arbiter
    return { visible: all, maxCost, total, byModel };
  }, [rows]);

  const modelSummary = useMemo(
    () => [...byModel.entries()].sort((a, b) => b[1] - a[1]),
    [byModel],
  );

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
        borderBottom: '2px solid var(--pixel-border)', flexShrink: 0, gap: 16,
      }}>
        <span style={{ fontSize: 22, color: 'var(--pixel-accent)', fontWeight: 'bold' }}>
          LLM COST · {fmtUsd(total)}
        </span>
        <span style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          {modelSummary.map(([m, c]) => (
            <span key={m} style={{ fontSize: 13, color: MID, display: 'flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: modelColor(m), display: 'inline-block' }} />
              {m} {fmtUsd(c)}
            </span>
          ))}
        </span>
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
        ) : visible.length === 0 ? (
          <div style={{ color: DIM, fontSize: 18 }}>
            No cost data yet — run <code>task langfuse:cost-snapshot</code>.
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--pixel-border)' }}>
                {['Day', 'Model', 'Cost', '', 'Tokens', 'Obs'].map((h, i) => (
                  <th key={i} style={{ ...cellStyle, color: DIM, textAlign: i >= 4 ? 'right' : 'left', fontWeight: 'normal' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={`${r.day}|${r.model}`} style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                  <td style={{ ...cellStyle, color: MID }}>{r.day}</td>
                  <td style={cellStyle}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: modelColor(r.model), display: 'inline-block', marginRight: 8 }} />
                    <span style={{ color: 'var(--pixel-text)' }}>{r.model}</span>
                  </td>
                  <td style={{ ...cellStyle, color: 'var(--pixel-text)', textAlign: 'right' }}>{fmtUsd(r.total_cost)}</td>
                  <td style={{ ...cellStyle, width: 160 }}>
                    <div style={{ background: 'rgba(255,255,255,0.08)', height: 8, width: 140 }}>
                      <div style={{ background: BAR, height: 8, width: `${Math.max(2, (r.total_cost / maxCost) * 100)}%` }} />
                    </div>
                  </td>
                  <td style={{ ...cellStyle, color: MID, textAlign: 'right' }}>{r.total_tokens.toLocaleString()}</td>
                  <td style={{ ...cellStyle, color: DIM, textAlign: 'right' }}>{r.observations}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
