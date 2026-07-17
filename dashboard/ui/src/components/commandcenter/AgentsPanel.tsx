import { useCallback, useState } from 'react';

import type { AgentInfo } from '../../hooks/useCommandCenterData.js';

const GREEN = '#89d185';
const AMBER = '#cca700';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';
const MID   = 'rgba(255,255,255,0.65)';

const statusColor: Record<string, string> = { active: GREEN, waiting: AMBER, permission: RED };

function apiBase() {
  return `http://${window.location.hostname || 'localhost'}:8420`;
}

function formatUptime(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const actionBtn: React.CSSProperties = {
  padding: '4px 14px', fontSize: 16, cursor: 'pointer',
  border: '2px solid transparent', borderRadius: 0,
};

interface Props {
  agents: AgentInfo[] | null;
  onRefresh: () => void;
}

export function AgentsPanel({ agents, onRefresh }: Props) {
  const [responding, setResponding] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const handleRespond = useCallback(async (agentId: number, action: 'approve' | 'deny') => {
    setResponding(agentId);
    try {
      await fetch(`${apiBase()}/api/agents/${agentId}/respond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      setTimeout(onRefresh, 500);
    } catch { /* ignore */ }
    setResponding(null);
  }, [onRefresh]);

  if (!agents || agents.length === 0) {
    return <div style={{ color: DIM, fontSize: 18 }}>No agents running</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {agents.map((a) => {
        const isPermission = a.status === 'permission';
        const isExpanded = expanded === a.id || isPermission;
        const pending = a.pendingTool;

        return (
          <div
            key={a.id}
            onClick={() => !isPermission && setExpanded(expanded === a.id ? null : a.id)}
            style={{
              background: isPermission ? 'rgba(241, 76, 76, 0.08)' : 'rgba(255,255,255,0.05)',
              border: `2px solid ${isPermission ? 'rgba(241, 76, 76, 0.3)' : 'var(--pixel-border)'}`,
              padding: '8px 12px',
              cursor: isPermission ? 'default' : 'pointer',
            }}
          >
            {/* Header row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: isExpanded ? 6 : 0 }}>
              <span style={{
                width: 8, height: 8, borderRadius: '50%',
                background: statusColor[a.status] || AMBER,
                display: 'inline-block', flexShrink: 0,
                ...(isPermission ? { animation: 'none', boxShadow: `0 0 6px ${RED}` } : {}),
              }} />
              <span style={{ fontSize: 20, color: '#fff', fontWeight: 'bold' }}>#{a.id}</span>
              <span style={{ fontSize: 18, color: 'var(--pixel-accent)' }}>{a.name}</span>
              <span style={{ fontSize: 14, color: DIM, marginLeft: 'auto' }}>
                {a.status} · {formatUptime(a.uptime)}
              </span>
            </div>

            {/* Active tool (non-permission) */}
            {!isPermission && a.lastTool && a.status === 'active' && (
              <div style={{ fontSize: 14, color: DIM, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {a.lastTool.status}
              </div>
            )}

            {/* Expanded details */}
            {isExpanded && !isPermission && (
              <div style={{ fontSize: 14, color: DIM }}>
                {a.cwd}
              </div>
            )}

            {/* Permission prompt */}
            {isPermission && pending && (
              <div style={{ marginTop: 4 }}>
                <div style={{ fontSize: 16, color: MID, marginBottom: 4 }}>
                  {pending.status}
                </div>

                {pending.command && (
                  <div style={{
                    fontSize: 13, color: MID, padding: '4px 8px', marginBottom: 6,
                    background: 'rgba(0,0,0,0.3)', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
                  }}>
                    $ {pending.command}
                  </div>
                )}

                {pending.file && !pending.command && (
                  <div style={{ fontSize: 13, color: DIM, marginBottom: 6 }}>
                    {pending.file}
                  </div>
                )}

                {pending.description && !pending.command && (
                  <div style={{ fontSize: 13, color: DIM, marginBottom: 6 }}>
                    {pending.description}
                  </div>
                )}

                <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleRespond(a.id, 'approve'); }}
                    disabled={responding === a.id}
                    style={{
                      ...actionBtn,
                      background: 'rgba(89, 200, 133, 0.2)',
                      color: GREEN,
                      border: `2px solid ${GREEN}`,
                      opacity: responding === a.id ? 0.5 : 1,
                    }}
                  >
                    Allow
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleRespond(a.id, 'deny'); }}
                    disabled={responding === a.id}
                    style={{
                      ...actionBtn,
                      background: 'rgba(241, 76, 76, 0.2)',
                      color: RED,
                      border: `2px solid ${RED}`,
                      opacity: responding === a.id ? 0.5 : 1,
                    }}
                  >
                    Deny
                  </button>
                </div>
              </div>
            )}

            {/* Permission state but no tool detail yet */}
            {isPermission && !pending && (
              <div style={{ fontSize: 14, color: AMBER, marginTop: 4 }}>
                {a.waitType === 'elicitation_dialog' ? 'Waiting for input...' : 'Waiting for permission...'}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
