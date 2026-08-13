/**
 * Provider seam — normalized, platform-agnostic model for the `/nexus` surface.
 *
 * An adapter (Slack, Discord, …) translates a platform's slash command / interaction
 * into a normalized `Command`/`Action`, hands it to the shared `/nexus` dispatch, and
 * renders the resulting `Message` back to that platform. NO platform SDK types appear
 * here — that is the whole point of the seam.
 *
 * @typedef {Object} Message
 * @property {string}  [text]        Plain-text body (fully portable across providers).
 * @property {boolean} [ephemeral]   Only the invoking user sees it (Slack `ephemeral`
 *                                    / Discord `flags:64`). Default: visible in channel.
 * @property {Row[]}   [components]   Optional interactive rows (buttons / one select each).
 * @property {any[]}   [blocks]       ESCAPE HATCH: a provider-native rich payload (Slack
 *                                    Block Kit) for surfaces Slack renders more richly
 *                                    than the portable model can express — currently the
 *                                    fleet panel, which uses sections/context/dividers
 *                                    that have no cross-provider equivalent.
 *                                    A Message may carry BOTH `blocks` and `components`:
 *                                    `messageToBlockKit` prefers `blocks` (so Slack stays
 *                                    byte-identical), while `messageToDiscord` ignores
 *                                    them and renders `components`. Adapters that can
 *                                    render neither fall back to `text`, which every
 *                                    Message has.
 *
 * @typedef {Object} Command   A slash invocation, provider-agnostic.
 * @property {string}   name        Subcommand: home|status|agents|peek|clear|stop|keep|msg|spawn|restore.
 * @property {string[]} args        Whitespace-split tokens after the subcommand.
 * @property {string}   rawArgs     Everything after the subcommand (internal spacing intact).
 * @property {string}   userId
 * @property {string}   channelId
 * @property {string}   replyToken  Opaque token the adapter replies through (Slack `response_url`).
 * @property {string}   provider    One of {@link PROVIDERS}.
 *
 * @typedef {Object} Action    An interactive component activation.
 * @property {string} actionId
 * @property {string} value
 * @property {Object} [state]       Form/view state (values keyed by block/action id).
 * @property {string} userId
 * @property {string} replyToken
 * @property {string} provider
 *
 * @typedef {Object} Row
 * @property {'row'} type
 * @property {Array<Object>} elements
 *
 * @typedef {Object} NexusProvider   The contract every adapter implements.
 * @property {() => Promise<void>}                              start      Connect / register commands.
 * @property {(handler: (c: Command) => any) => void}          onCommand  Register the command sink.
 * @property {(handler: (a: Action, raw?: any) => any) => void} onAction  Register the action sink.
 * @property {(replyToken: string, m: Message) => Promise<void>} reply    Answer a command/interaction.
 * @property {(target: string, m: Message) => Promise<void>}    send       Proactive post to a channel.
 */

export const PROVIDERS = Object.freeze({ SLACK: 'slack', DISCORD: 'discord' });

// ── Message constructors ─────────────────────────────────────────────────────
// `message()` normalizes an object into a Message, dropping absent/falsey fields so
// two equivalent messages compare deep-equal (important for byte-identical rendering).

/** @param {Partial<Message>} m @returns {Message} */
export function message(m = {}) {
  const out = {};
  if (m.text != null) out.text = String(m.text);
  if (m.ephemeral) out.ephemeral = true;
  if (m.components && m.components.length) out.components = m.components;
  if (m.blocks && m.blocks.length) out.blocks = m.blocks;
  return out;
}

/** In-channel text message. */
export const text = (s, opts = {}) => message({ ...opts, text: s });
/** Ephemeral (invoker-only) text message. */
export const ephemeral = (s, opts = {}) => message({ ...opts, text: s, ephemeral: true });

// ── Shared command-text parsing ──────────────────────────────────────────────
// Both providers hand us one free-text string (`/nexus <this>`), so the split into
// subcommand + args lives here rather than in either adapter. That is what makes
// `/nexus msg agent hello  there` behave identically on Slack and Discord instead of
// by coincidence — the two adapters differ only in where they dig the string out of.
/**
 * @param {string} input  Everything the user typed after `/nexus`.
 * @returns {{name: string, args: string[], rawArgs: string}}
 */
export function parseCommandText(input) {
  const raw = String(input || '').trim();
  const first = raw.split(/\s+/)[0] || '';
  // Empty invocation is the panel. Lowercasing only the subcommand keeps arg casing
  // intact, and `first.length` still indexes correctly because case-folding ASCII
  // never changes length.
  const name = (first || 'home').toLowerCase();
  const rawArgs = raw.slice(first.length).trim();
  return { name, args: rawArgs ? rawArgs.split(/\s+/) : [], rawArgs };
}

// ── Component builders (minimal: buttons + a single select per row) ──────────
export const button = ({ id, text: t, value, style }) => ({
  type: 'button', id, text: t, value: value ?? id, ...(style ? { style } : {}),
});
export const select = ({ id, placeholder, options }) => ({
  type: 'select', id, placeholder, options: options || [],
});
export const actionRow = (...elements) => ({ type: 'row', elements });
