import { useCallback, useEffect, useRef, useState } from 'react';

function apiBase() {
  return `http://${window.location.hostname || 'localhost'}:8420`;
}

export interface PendingTool {
  status: string;
  toolName: string | null;
  file?: string;
  command?: string;
  pattern?: string;
  description?: string;
}

export interface AgentInfo {
  id: number;
  name: string;
  status: 'active' | 'waiting' | 'permission';
  cwd: string;
  uptime: number | null;
  lastTool: { toolId: string; status: string } | null;
  pendingTool: PendingTool | null;
  waitType: 'permission_prompt' | 'elicitation_dialog' | null;
}

export interface ServiceInfo {
  name: string;
  status: string;
  health: string;
  uptime: string;
  ports: string;
  image: string;
}

export interface TimerInfo {
  name: string;
  nextRun: string;
  leftUntil: string;
  lastRun: string | null;
  result: string;
  active: boolean;
  description: string | null;
}

export interface CacheEntry {
  project: string;
  file: string;
  size: number;
  mtime: string;
  age: number;
  stale: boolean;
}

export interface CheckpointEntry {
  date: string | null;
  project: string;
  file: string;
  size: number;
  mtime: string;
  branch: string | null;
  changes: string | null;
}

export interface SystemHealth {
  arbiter: boolean;
  docker: boolean;
  tmux: boolean;
  database: boolean;
  agentCount: number;
  containerCount: number;
  timerCount: number;
}

export interface CommandCenterData {
  health: SystemHealth | null;
  agents: AgentInfo[] | null;
  services: ServiceInfo[] | null;
  timers: TimerInfo[] | null;
  cache: CacheEntry[] | null;
  checkpoints: CheckpointEntry[] | null;
  loading: boolean;
  refresh: () => void;
}

async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${apiBase()}${path}`);
    if (!res.ok) return null;
    return await res.json() as T;
  } catch {
    return null;
  }
}

export interface TimerLog {
  label: string;
  path: string | null;
  mtime: string | null;
  content: string;
  note?: string;
  error?: string;
}

export async function fetchTimerLog(label: string, lines = 200): Promise<TimerLog | null> {
  try {
    const params = new URLSearchParams({ label, lines: String(lines) });
    const res = await fetch(`${apiBase()}/api/system/timers/log?${params}`);
    if (!res.ok) return { label, path: null, mtime: null, content: '', error: `HTTP ${res.status}` };
    return await res.json() as TimerLog;
  } catch (err) {
    return { label, path: null, mtime: null, content: '', error: String(err) };
  }
}

export interface InstallationInfo {
  relPath: string;
  name: string;
  indexedAt: string;
  lastRemoteTs: number;
  ageSeconds: number | null;
}

export async function fetchInstallations(): Promise<InstallationInfo[]> {
  return (await fetchJson<InstallationInfo[]>('/api/system/installations')) ?? [];
}

export interface SparkIndexInfo {
  embedder?: string;
  model?: string;
  dim?: number;
  chunks?: number;
  error?: string;
}

export async function fetchSparkIndexInfo(): Promise<SparkIndexInfo | null> {
  return fetchJson<SparkIndexInfo>('/api/system/spark-index');
}

export interface CostDay {
  day: string;
  model: string;
  observations: number;
  total_cost: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
}

export async function fetchCostData(): Promise<CostDay[]> {
  return (await fetchJson<CostDay[]>('/api/system/cost')) ?? [];
}

export interface MemoryHit {
  id: string;
  title: string;
  content: string;
  tags: string[];
  created_at: string;
  project: string;
}

export async function searchMemory(
  query: string,
  mode: 'semantic' | 'keyword',
  project: string,
  limit = 10,
): Promise<MemoryHit[]> {
  const params = new URLSearchParams({ query, mode, project, limit: String(limit) });
  return (await fetchJson<MemoryHit[]>(`/api/system/memory/search?${params}`)) ?? [];
}

export function useCommandCenterData(enabled: boolean): CommandCenterData {
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  const [services, setServices] = useState<ServiceInfo[] | null>(null);
  const [timers, setTimers] = useState<TimerInfo[] | null>(null);
  const [cache, setCache] = useState<CacheEntry[] | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const mountedRef = useRef(true);

  const fetchAll = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    const [h, a, s, t, ca, cp] = await Promise.all([
      fetchJson<SystemHealth>('/api/system/health'),
      fetchJson<AgentInfo[]>('/api/system/agents'),
      fetchJson<ServiceInfo[]>('/api/system/services'),
      fetchJson<TimerInfo[]>('/api/system/timers'),
      fetchJson<CacheEntry[]>('/api/system/cache'),
      fetchJson<CheckpointEntry[]>('/api/checkpoints'),
    ]);
    if (!mountedRef.current) return;
    setHealth(h);
    setAgents(a);
    setServices(s);
    setTimers(t);
    setCache(ca);
    setCheckpoints(cp);
    setLoading(false);
  }, [enabled]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    fetchAll();
    const id = setInterval(fetchAll, 30000);
    return () => clearInterval(id);
  }, [enabled, fetchAll]);

  return { health, agents, services, timers, cache, checkpoints, loading, refresh: fetchAll };
}
