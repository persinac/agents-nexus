import agentSourceEntries from 'virtual:agent-sources';

export interface AgentSourceConfig {
  /** Fixed palette index into loadedCharacters[]. Absent = randomized pixel people. */
  palette?: number;
}

/** ID offset added to all relay agent IDs to avoid collision with tmux window indices. */
export const RELAY_ID_OFFSET = 1000;

/** Populated from agentSources.yaml at build time. Sources not listed get randomized pixel people. */
export const AGENT_SOURCE_CONFIGS: Record<string, AgentSourceConfig> = agentSourceEntries;

/** Returns the config for a source, or null (= randomized pixel people). */
export function getAgentSourceConfig(agentSource: string): AgentSourceConfig | null {
  return AGENT_SOURCE_CONFIGS[agentSource] ?? null;
}

/** Relay agents get RELAY_ID_OFFSET added to avoid collision with tmux window indices. */
export function resolveAgentId(rawId: number, agentSource?: string): number {
  return agentSource ? rawId + RELAY_ID_OFFSET : rawId;
}
