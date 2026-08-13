/**
 * Slack adapter for the provider seam.
 *
 * The pure render/parse functions (`messageToBlockKit`, `slashToCommand`,
 * `interactiveToAction`) are exported standalone and unit-tested — they are the
 * byte-for-byte boundary between the normalized model and Slack's wire format. The
 * `SlackAdapter` class wires them onto a Socket Mode socket + WebClient (both injected,
 * so it is testable without a live Slack connection).
 *
 * Behavior target when this replaces the inline handlers in index.js (Phase 2.2):
 * IDENTICAL to today — ack empty immediately (Socket Mode WS on this box delivers
 * envelopes ~2.6s late, so we never ack-with-payload), then reply over `response_url`.
 *
 * `view_submission` (modal) is the one Slack-shaped escape hatch on this class: a modal
 * submit must ack WITH a response (close / validation errors), so the adapter must not
 * pre-ack it, and there is no normalized equivalent in the model yet. `onViewSubmission`
 * therefore hands the handler the RAW body and the RAW ack and lets it own both. Without
 * a handler registered the envelope is dropped, unacked — the pre-2.2 behavior.
 */
import { PROVIDERS, parseCommandText } from './types.js';

/**
 * Render a normalized Message → a Slack `response_url` / chat payload.
 * - `ephemeral` → `response_type:'ephemeral'` (else `'in_channel'`)
 * - `text`      → `text`
 * - `blocks`    → passed through verbatim (escape hatch for Block-Kit-native surfaces)
 * - `components`→ rendered to Block Kit `actions` blocks (when no raw `blocks` present)
 * @param {import('./types.js').Message} m
 */
export function messageToBlockKit(m = {}) {
  const payload = { response_type: m.ephemeral ? 'ephemeral' : 'in_channel' };
  if (m.text != null) payload.text = String(m.text);
  if (m.blocks && m.blocks.length) payload.blocks = m.blocks;
  else if (m.components && m.components.length) payload.blocks = m.components.map(rowToBlock);
  return payload;
}

function rowToBlock(row) {
  return { type: 'actions', elements: (row.elements || []).map(elToBlockKit) };
}
function elToBlockKit(el) {
  if (el.type === 'button') {
    const b = { type: 'button', text: { type: 'plain_text', text: el.text }, action_id: el.id, value: el.value };
    if (el.style) b.style = el.style;
    return b;
  }
  if (el.type === 'select') {
    return {
      type: 'static_select', action_id: el.id,
      placeholder: { type: 'plain_text', text: el.placeholder || 'Select' },
      options: (el.options || []).map((o) => ({ text: { type: 'plain_text', text: o.text }, value: o.value })),
    };
  }
  return el; // unknown element — pass through
}

/**
 * Parse a Slack `slash_commands` envelope body → normalized Command.
 * Empty text defaults to the `home` subcommand (the panel), matching index.js.
 * @param {Object} body
 * @returns {import('./types.js').Command}
 */
export function slashToCommand(body = {}) {
  return {
    ...parseCommandText(body.text),   // shared with Discord — see types.js
    userId: body.user_id,
    channelId: body.channel_id,
    replyToken: body.response_url,
    provider: PROVIDERS.SLACK,
  };
}

/**
 * Parse a Slack interactive body → normalized Action, or null if it is not a
 * `block_actions` (e.g. `view_submission`, which index.js handles separately).
 * @param {Object} body
 * @returns {import('./types.js').Action|null}
 */
export function interactiveToAction(body = {}) {
  if (!body || body.type !== 'block_actions') return null;
  const a = (body.actions || [])[0];
  if (!a) return null;
  return {
    actionId: a.action_id,
    // A static_select reports its choice in `selected_option`, and leaves `value`
    // undefined — reading only `value` silently drops the picked agent. This mirrors
    // Discord's `data.values[0]`, so both providers yield the same Action shape.
    value: a.selected_option ? a.selected_option.value : a.value,
    state: body.state && body.state.values,
    userId: body.user && body.user.id,
    replyToken: body.response_url,
    provider: PROVIDERS.SLACK,
  };
}

const defaultPost = async (url, payload) => {
  await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
};

/**
 * @implements {import('./types.js').NexusProvider}
 */
export class SlackAdapter {
  /**
   * @param {Object} deps
   * @param {Object} [deps.socket]  Socket Mode client (`.on(event, handler)`).
   * @param {Object} [deps.web]     WebClient (`.chat.postMessage`).
   * @param {(url:string,payload:Object)=>Promise<void>} [deps.post]  response_url POST (injectable for tests).
   * @param {(msg:string)=>void} [deps.log]
   */
  constructor({ socket, web, post, log } = {}) {
    this.name = PROVIDERS.SLACK;
    this.socket = socket;
    this.web = web;
    this.post = post || defaultPost;
    this.log = log || (() => {});
    this._onCommand = null;
    this._onAction = null;
    this._onViewSubmission = null;
  }

  onCommand(handler) { this._onCommand = handler; }
  onAction(handler) { this._onAction = handler; }
  /** Raw Slack modal submits: `(body, ack) => …`. The handler owns the ack. */
  onViewSubmission(handler) { this._onViewSubmission = handler; }

  async start() {
    if (!this.socket) return;
    // Ack empty FIRST (zero computation) so it lands in Slack's 3s window despite the
    // slow WS; all real output then goes over response_url. Identical to index.js today.
    this.socket.on('slash_commands', async ({ body, ack }) => {
      try { await ack(); } catch { /* already acked */ }
      // The raw envelope rides along as a second arg (same as `onAction`) purely so the
      // still-Slack-owned wiring layer can log what the user literally typed. The core
      // handler only ever reads the normalized Command.
      if (this._onCommand) await this._onCommand(slashToCommand(body), body);
    });
    this.socket.on('interactive', async ({ body, ack }) => {
      const first = (body && body.actions && body.actions[0]) || null;
      this.log(`interactive type=${(body && body.type) || '-'} action=${(first && first.action_id) || '-'}`);
      // Modal submits ack WITH a response, so the handler owns the ack and we must not
      // pre-ack. No handler → drop it unacked (pre-2.2 behavior).
      if (body && body.type === 'view_submission') {
        if (this._onViewSubmission) await this._onViewSubmission(body, ack);
        return;
      }
      try { await ack(); } catch { /* already acked */ }
      const action = interactiveToAction(body);
      if (action && this._onAction) await this._onAction(action, body);
    });
  }

  /** Answer a command/interaction over its response_url (ack already sent). */
  async reply(replyToken, m) {
    if (!replyToken) return;
    await this.post(replyToken, messageToBlockKit(m));
  }

  /** Proactive post to a channel via chat.postMessage. */
  async send(channel, m) {
    if (!this.web || !channel) return;
    const p = messageToBlockKit(m);
    await this.web.chat.postMessage({ channel, text: p.text || ' ', ...(p.blocks ? { blocks: p.blocks } : {}) });
  }
}
