import { useCommandCenterData } from '../hooks/useCommandCenterData.js';
import { AgentsPanel } from './commandcenter/AgentsPanel.js';
import { CheckpointsPanel } from './commandcenter/CheckpointsPanel.js';
import { ServicesPanel } from './commandcenter/ServicesPanel.js';
import { TimersPanel } from './commandcenter/TimersPanel.js';

const GREEN = '#89d185';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';

interface Props { onClose: () => void }

function HealthDot({ ok, label }: { ok: boolean | undefined; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginLeft: 10 }}>
      <span style={{
        width: 8, height: 8, borderRadius: '50%',
        background: ok ? GREEN : ok === false ? RED : DIM,
        display: 'inline-block',
      }} />
      <span style={{ fontSize: 14, color: DIM }}>{label}</span>
    </span>
  );
}

export function CommandCenter({ onClose }: Props) {
  const data = useCommandCenterData(true);

  return (
    <div
      className="mono-font"
      style={{
        position: 'absolute', inset: 0, zIndex: 50,
        background: 'var(--pixel-bg)',
        display: 'flex', flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center',
        padding: '10px 16px',
        borderBottom: '2px solid var(--pixel-border)',
        flexShrink: 0,
      }}>
        <span style={{ fontSize: 22, color: 'var(--pixel-accent)', fontWeight: 'bold' }}>
          NEXUS COMMAND CENTER
        </span>

        <span style={{ marginLeft: 16 }}>
          <HealthDot ok={data.health?.docker} label="docker" />
          <HealthDot ok={data.health?.tmux} label="tmux" />
          <HealthDot ok={data.health?.database} label="db" />
        </span>

        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          {data.loading && (
            <span style={{ fontSize: 14, color: DIM }}>refreshing...</span>
          )}
          <button
            onClick={data.refresh}
            style={{
              background: 'var(--pixel-btn-bg)',
              border: '2px solid transparent',
              color: 'var(--pixel-text)',
              fontSize: 18, padding: '2px 8px',
              cursor: 'pointer', borderRadius: 0,
            }}
          >
            Refresh
          </button>
          <button
            onClick={onClose}
            style={{
              background: 'var(--pixel-btn-bg)',
              border: '2px solid transparent',
              color: 'var(--pixel-text)',
              fontSize: 18, padding: '2px 8px',
              cursor: 'pointer', borderRadius: 0,
            }}
          >
            Close
          </button>
        </span>
      </div>

      {/* Grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gridTemplateRows: '1fr 1fr',
        gap: 0,
        flex: 1,
        overflow: 'hidden',
      }}>
        {/* Agents — top left */}
        <div style={{
          padding: 14,
          borderRight: '1px solid var(--pixel-border)',
          borderBottom: '1px solid var(--pixel-border)',
          overflow: 'auto',
        }}>
          <div style={{ fontSize: 16, color: DIM, marginBottom: 8, textTransform: 'uppercase', letterSpacing: 1 }}>
            Agents ({data.agents?.length ?? 0})
          </div>
          <AgentsPanel agents={data.agents} onRefresh={data.refresh} />
        </div>

        {/* Services — top right */}
        <div style={{
          padding: 14,
          borderBottom: '1px solid var(--pixel-border)',
          overflow: 'auto',
        }}>
          <div style={{ fontSize: 16, color: DIM, marginBottom: 8, textTransform: 'uppercase', letterSpacing: 1 }}>
            Services ({data.services?.length ?? 0})
          </div>
          <ServicesPanel services={data.services} />
        </div>

        {/* Timers — bottom left */}
        <div style={{
          padding: 14,
          borderRight: '1px solid var(--pixel-border)',
          overflow: 'auto',
        }}>
          <div style={{ fontSize: 16, color: DIM, marginBottom: 8, textTransform: 'uppercase', letterSpacing: 1 }}>
            Timers ({data.timers?.length ?? 0})
          </div>
          <TimersPanel timers={data.timers} />
        </div>

        {/* Checkpoints & Cache — bottom right */}
        <div style={{ padding: 14, overflow: 'auto' }}>
          <CheckpointsPanel checkpoints={data.checkpoints} cache={data.cache} />
        </div>
      </div>
    </div>
  );
}
