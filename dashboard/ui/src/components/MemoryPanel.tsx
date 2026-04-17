import { useEffect, useState } from 'react';

interface MemoryNote {
  title: string;
  content: string;
  tags: string[];
  created_at: string;
  access_count: number;
  project: string;
}

interface MemoryPanelProps {
  onClose: () => void;
}

const MEMORY_Z = 45;

function ageLabel(createdAt: string): string {
  if (!createdAt) return '';
  try {
    const ts = new Date(createdAt).getTime();
    const secs = Math.floor((Date.now() - ts) / 1000);
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  } catch {
    return '';
  }
}

function NoteCard({ note }: { note: MemoryNote }) {
  const [expanded, setExpanded] = useState(false);
  const preview = note.content.length > 180 ? note.content.slice(0, 180) + '…' : note.content;
  const truncated = note.content.length > 180;
  const age = ageLabel(note.created_at);

  return (
    <div
      style={{
        border: '2px solid var(--pixel-border)',
        padding: '8px 10px',
        background: 'var(--pixel-btn-bg)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap', marginBottom: 4 }}>
        <span style={{ fontSize: '24px', color: 'var(--pixel-text)', fontWeight: 'bold' }}>
          {note.title || note.content.slice(0, 40)}
        </span>
        {note.project && (
          <span
            style={{
              fontSize: '18px',
              color: 'var(--pixel-text-dim)',
              background: 'rgba(255,255,255,0.06)',
              border: '1px solid var(--pixel-border)',
              padding: '1px 5px',
            }}
          >
            {note.project}
          </span>
        )}
        {note.tags.map((t) => (
          <span key={t} style={{ fontSize: '18px', color: 'var(--pixel-accent)' }}>
            #{t}
          </span>
        ))}
        {age && (
          <span style={{ fontSize: '18px', color: 'var(--pixel-text-dim)', marginLeft: 'auto' }}>
            {age}
          </span>
        )}
      </div>
      <div style={{ fontSize: '20px', color: 'var(--pixel-text)', opacity: 0.85, whiteSpace: 'pre-wrap', lineHeight: 1.4 }}>
        {expanded ? note.content : preview}
      </div>
      {truncated && (
        <button
          onClick={() => setExpanded((v) => !v)}
          style={{
            marginTop: 4,
            padding: '2px 8px',
            fontSize: '18px',
            color: 'var(--pixel-accent)',
            background: 'transparent',
            border: '1px solid var(--pixel-accent)',
            borderRadius: 0,
            cursor: 'pointer',
          }}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}

export function MemoryPanel({ onClose }: MemoryPanelProps) {
  const [notes, setNotes] = useState<MemoryNote[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [project, setProject] = useState('general');

  const fetchNotes = (proj: string) => {
    setLoading(true);
    setError(false);
    const host = window.location.hostname || 'localhost';
    fetch(`http://${host}:8420/api/memory?project=${encodeURIComponent(proj)}`)
      .then((r) => {
        if (!r.ok) throw new Error('non-ok');
        return r.json() as Promise<MemoryNote[]>;
      })
      .then((data) => {
        setNotes(data);
        setLoading(false);
      })
      .catch(() => {
        setError(true);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchNotes(project);
  }, [project]);

  return (
    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        background: 'var(--vscode-editor-background)',
        zIndex: MEMORY_Z,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          borderBottom: '2px solid var(--pixel-border)',
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: '28px', color: 'var(--pixel-text)', fontWeight: 'bold', flex: 1 }}>
          Memory
        </span>

        {/* Project filter */}
        <select
          value={project}
          onChange={(e) => setProject(e.target.value)}
          style={{
            fontSize: '22px',
            color: 'var(--pixel-text)',
            background: 'var(--pixel-btn-bg)',
            border: '2px solid var(--pixel-border)',
            borderRadius: 0,
            padding: '3px 6px',
            cursor: 'pointer',
          }}
        >
          <option value="general">All projects</option>
        </select>

        <button
          onClick={() => fetchNotes(project)}
          style={{
            padding: '4px 10px',
            fontSize: '22px',
            color: 'var(--pixel-text)',
            background: 'var(--pixel-btn-bg)',
            border: '2px solid var(--pixel-border)',
            borderRadius: 0,
            cursor: 'pointer',
          }}
          title="Refresh"
        >
          ↺
        </button>

        <button
          onClick={onClose}
          style={{
            padding: '4px 10px',
            fontSize: '22px',
            color: 'var(--pixel-text-dim)',
            background: 'var(--pixel-btn-bg)',
            border: '2px solid var(--pixel-border)',
            borderRadius: 0,
            cursor: 'pointer',
          }}
          title="Close"
        >
          ✕
        </button>
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflow: 'auto', padding: '12px' }}>
        {loading && (
          <div style={{ fontSize: '24px', color: 'var(--pixel-text-dim)', padding: '12px 0' }}>
            Loading…
          </div>
        )}
        {error && (
          <div style={{ fontSize: '24px', color: 'var(--vscode-charts-red, #f14c4c)', padding: '12px 0' }}>
            Could not reach memory server. Is the bridge running?
          </div>
        )}
        {!loading && !error && notes.length === 0 && (
          <div style={{ fontSize: '24px', color: 'var(--pixel-text-dim)', padding: '12px 0' }}>
            No memory notes found.
          </div>
        )}
        {!loading && !error && notes.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {notes.map((note, i) => (
              <NoteCard key={i} note={note} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
