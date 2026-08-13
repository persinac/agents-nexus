/**
 * Notifier — outbound fan-out across registered providers (task 4.1).
 *
 * Inbound is already provider-agnostic: a Slack slash command and a Discord interaction
 * both become a `Command` and hit the same dispatch. Outbound is not — `doSpawn`,
 * permission cards, done-pings and presence alerts all call `web.chat.postMessage`
 * directly, so a `/nexus spawn` issued from Discord acknowledges in Discord and then
 * reports its result into Slack. This is the seam that closes that.
 *
 * Two properties matter more than anything else here:
 *
 * 1. **One provider failing must never suppress the others.** A Discord outage cannot be
 *    allowed to swallow a permission card that also needed to reach Slack. Every send is
 *    settled independently and failures are returned, not thrown.
 * 2. **Default to the originating provider only.** Fan-out is opt-in per call site.
 *    Broadcasting everything everywhere would double every notification the moment a
 *    second provider is registered — a regression disguised as a feature.
 */

/**
 * @typedef {Object} FanoutResult
 * @property {string}  provider
 * @property {boolean} ok
 * @property {string}  [error]     Message only — never the payload.
 * @property {boolean} [skipped]   True when no target was configured for that provider.
 */

export class Notifier {
  /**
   * @param {Object} [deps]
   * @param {Array<import('./types.js').NexusProvider>} [deps.providers]
   * @param {(msg: string) => void} [deps.log]
   */
  constructor({ providers = [], log } = {}) {
    /** @type {Map<string, import('./types.js').NexusProvider>} */
    this.providers = new Map();
    this.log = log || (() => {});
    for (const p of providers) this.register(p);
  }

  /**
   * Register a provider. Ignores anything falsey or unnamed so a caller can pass a
   * conditionally-constructed adapter without guarding at every call site. Re-registering
   * the same name replaces it, which keeps startup idempotent.
   * @returns {boolean} whether it was registered
   */
  register(provider) {
    if (!provider || !provider.name) return false;
    this.providers.set(provider.name, provider);
    return true;
  }

  /** Registered provider names, in insertion order. */
  get names() { return [...this.providers.keys()]; }

  /** @returns {import('./types.js').NexusProvider|undefined} */
  get(name) { return this.providers.get(name); }

  /**
   * Resolve which providers a call should reach.
   * - `providers: ['slack']` → exactly those (unknown names dropped)
   * - `origin: 'discord'`    → just the originating one (the DEFAULT)
   * - `all: true`            → every registered provider
   * Explicit `providers` wins over `all`, which wins over `origin`.
   * @returns {string[]}
   */
  resolve({ providers, origin, all } = {}) {
    if (Array.isArray(providers) && providers.length) {
      return providers.filter((n) => this.providers.has(n));
    }
    if (all) return this.names;
    if (origin && this.providers.has(origin)) return [origin];
    return [];
  }

  /**
   * Send one Message to the resolved providers.
   *
   * `targets` maps provider name → channel identifier, because a Slack channel id means
   * nothing to Discord. A provider with no target is REPORTED as skipped rather than
   * silently dropped — a missing target is a config bug, and silence is how those
   * survive for months.
   *
   * Never throws: a provider that rejects yields `{ok:false, error}`.
   *
   * @param {import('./types.js').Message} m
   * @param {{providers?: string[], origin?: string, all?: boolean, targets?: Record<string,string>}} [opts]
   * @returns {Promise<FanoutResult[]>}
   */
  async fanout(m, opts = {}) {
    const targets = opts.targets || {};
    const names = this.resolve(opts);
    if (!names.length) return [];

    const settled = await Promise.allSettled(names.map(async (name) => {
      const target = targets[name];
      if (!target) return { provider: name, ok: false, skipped: true };
      await this.providers.get(name).send(target, m);
      return { provider: name, ok: true };
    }));

    const results = settled.map((s, i) => (s.status === 'fulfilled'
      ? s.value
      : { provider: names[i], ok: false, error: String((s.reason && s.reason.message) || s.reason) }));

    for (const r of results) {
      if (r.skipped) this.log(`notify: no target configured for ${r.provider} — skipped`);
      else if (!r.ok) this.log(`notify: ${r.provider} send failed: ${r.error}`);
    }
    return results;
  }
}
