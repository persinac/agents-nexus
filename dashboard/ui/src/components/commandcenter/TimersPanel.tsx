import { Fragment, useEffect, useState } from 'react';

import { fetchTimerLog, type TimerInfo, type TimerLog } from '../../hooks/useCommandCenterData.js';

const GREEN = '#89d185';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';

const cellStyle: React.CSSProperties = {
  padding: '3px 8px', fontSize: 16, whiteSpace: 'nowrap',
};

function resultColor(result: string): string {
  if (result === 'success') return GREEN;
  if (result === 'failed' || result.startsWith('exit-code')) return RED;
  return DIM;
}

interface Props { timers: TimerInfo[] | null }

interface LogState { status: 'loading' | 'ready' | 'error'; data?: TimerLog }

export function TimersPanel({ timers }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, LogState>>({});

  useEffect(() => {
    if (!expanded) return;
    if (logs[expanded]) return;
    setLogs(prev => ({ ...prev, [expanded]: { status: 'loading' } }));
    fetchTimerLog(expanded, 200).then(data => {
      setLogs(prev => ({
        ...prev,
        [expanded]: data ? { status: 'ready', data } : { status: 'error' },
      }));
    });
  }, [expanded, logs]);

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
          {timers.map((t) => {
            const isOpen = expanded === t.name;
            const log = logs[t.name];
            return (
              <Fragment key={t.name}>
                <tr
                  onClick={() => setExpanded(isOpen ? null : t.name)}
                  style={{
                    borderBottom: isOpen ? 'none' : '1px solid rgba(255,255,255,0.06)',
                    cursor: 'pointer',
                    background: isOpen ? 'rgba(255,255,255,0.05)' : 'transparent',
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
                      {isOpen ? '▼' : '▶'}
                    </span>
                    {t.name}
                  </td>
                  <td style={{ ...cellStyle, color: DIM }}>{t.leftUntil || t.nextRun}</td>
                  <td style={{ ...cellStyle, color: DIM }}>{t.lastRun || '—'}</td>
                  <td style={{ ...cellStyle, color: resultColor(t.result) }}>
                    {t.result}
                  </td>
                </tr>
                {isOpen && (
                  <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    <td />
                    <td colSpan={4} style={{ padding: '4px 8px 10px', fontSize: 14, color: MID }}>
                      {t.description && <div>{t.description}</div>}
                      {t.nextRun && (
                        <div style={{ marginTop: 4, fontSize: 13, color: DIM }}>
                          Next: {t.nextRun}
                        </div>
                      )}
                      <LogSection state={log} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LogSection({ state }: { state: LogState | undefined }) {
  if (!state || state.status === 'loading') {
    return <div style={{ marginTop: 8, fontSize: 12, color: DIM }}>Loading log…</div>;
  }
  if (state.status === 'error') {
    return <div style={{ marginTop: 8, fontSize: 12, color: RED }}>Failed to load log</div>;
  }
  const log = state.data!;
  if (log.error) {
    return <div style={{ marginTop: 8, fontSize: 12, color: RED }}>Error: {log.error}</div>;
  }
  if (!log.path) {
    return <div style={{ marginTop: 8, fontSize: 12, color: DIM }}>{log.note || 'No log path configured for this job.'}</div>;
  }
  if (!log.content) {
    return (
      <div style={{ marginTop: 8, fontSize: 12, color: DIM }}>
        <div>{log.note || 'Log is empty.'}</div>
        <div style={{ marginTop: 2 }}>Path: <code>{log.path}</code></div>
      </div>
    );
  }
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 12, color: DIM, marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
        <span>Log: <code>{log.path}</code></span>
        {log.mtime && <span>updated {new Date(log.mtime).toLocaleString()}</span>}
      </div>
      <pre style={{
        margin: 0,
        padding: '8px 10px',
        background: 'rgba(0,0,0,0.35)',
        border: '1px solid rgba(255,255,255,0.08)',
        borderRadius: 3,
        maxHeight: 280,
        overflow: 'auto',
        fontSize: 12,
        lineHeight: 1.4,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        color: MID,
      }}>
        {log.content}
      </pre>
    </div>
  );
}
