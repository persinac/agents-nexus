import type { ServiceInfo } from '../../hooks/useCommandCenterData.js';

const GREEN = '#89d185';
const AMBER = '#cca700';
const RED   = '#f14c4c';
const DIM   = 'rgba(255,255,255,0.4)';

function healthColor(status: string, health: string): string {
  if (health === 'healthy') return GREEN;
  if (health === 'unhealthy') return RED;
  if (status === 'running') return AMBER;
  return RED;
}

const cellStyle: React.CSSProperties = {
  padding: '3px 8px', fontSize: 16, whiteSpace: 'nowrap',
  overflow: 'hidden', textOverflow: 'ellipsis',
};

interface Props { services: ServiceInfo[] | null }

export function ServicesPanel({ services }: Props) {
  if (!services || services.length === 0) {
    return <div style={{ color: DIM, fontSize: 18 }}>No containers found</div>;
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--pixel-border)' }}>
            {['', 'Name', 'Status', 'Uptime'].map((h) => (
              <th key={h} style={{ ...cellStyle, color: DIM, textAlign: 'left', fontWeight: 'normal' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {services.map((s) => (
            <tr key={s.name} style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
              <td style={cellStyle}>
                <span style={{
                  width: 8, height: 8, borderRadius: '50%',
                  background: healthColor(s.status, s.health),
                  display: 'inline-block',
                }} />
              </td>
              <td style={{ ...cellStyle, color: '#fff' }}>{s.name}</td>
              <td style={{ ...cellStyle, color: DIM }}>{s.health !== 'none' ? s.health : s.status}</td>
              <td style={{ ...cellStyle, color: DIM }}>{s.uptime}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
