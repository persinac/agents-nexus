/// <reference types="vite/client" />

declare module 'virtual:agent-sources' {
  const config: Record<string, { palette?: number }>;
  export default config;
}
