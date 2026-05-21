import { useState } from 'react';

import type { TimerInfo } from '../../hooks/useCommandCenterData.js';

const GREEN = '#89d185';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';

const cellStyle: React.CSSProperties = {
  padding: '3px 8px', fontSize: 16, whiteSpace: 'nowrap',
};

function resultColor(result: string): string {
  if (result === 'success') return GREEN;
  if (result === 'failed' || result === 'exit-code') return RED;
  return DIM;
}

interface Props { timers: TimerInfo[] | null }

export function TimersPanel({ timers }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (!timers || timers.length === 0) {
    return <div style={{ color: DIM, fontSize: 18 }}>No timers found</div>;
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--pixel-border)' }}>
            {['', 'Timer', 'Next', 'Last Run', 'Result'].map((h) => (
              <th key={h} style={{ ...cellStyle, color: DIM, textAlign: 'left', fontWeight: 'normal' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {timers.map((t) => (
            <>
              <tr
                key={t.name}
                onClick={() => setExpanded(expanded === t.name ? null : t.name)}
                style={{
                  borderBottom: expanded === t.name ? 'none' : '1px solid rgba(255,255,255,0.06)',
                  cursor: 'pointer',
                  background: expanded === t.name ? 'rgba(255,255,255,0.05)' : 'transparent',
                }}
              >
                <td style={cellStyle}>
                  <span style={{
                    width: 8, height: 8, borderRadius: '50%',
                    background: resultColor(t.result),
                    display: 'inline-block',
                  }} />
                </td>
                <td style={{ ...cellStyle, color: '#fff' }}>
                  <span style={{ marginRight: 6, fontSize: 12, color: DIM }}>
                    {expanded === t.name ? '▼' : '▶'}
                  </span>
                  {t.name}
                </td>
                <td style={{ ...cellStyle, color: DIM }}>{t.leftUntil || t.nextRun}</td>
                <td style={{ ...cellStyle, color: DIM }}>{t.lastRun || '—'}</td>
                <td style={{ ...cellStyle, color: resultColor(t.result) }}>
                  {t.result}
                </td>
              </tr>
              {expanded === t.name && t.description && (
                <tr key={`${t.name}-desc`} style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                  <td />
                  <td colSpan={4} style={{ padding: '4px 8px 8px', fontSize: 14, color: MID }}>
                    {t.description}
                    {t.nextRun && (
                      <div style={{ marginTop: 4, fontSize: 13, color: DIM }}>
                        Next: {t.nextRun}
                      </div>
                    )}
                  </td>
                </tr>
              )}
            </>
          ))}
        </tbody>
      </table>
    </div>
  );
}
