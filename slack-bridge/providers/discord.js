/**
 * Discord adapter for the provider seam.
 *
 * Mirrors `providers/slack.js`: pure render/parse functions exported standalone and
 * unit-tested, plus a `DiscordAdapter` class with every I/O dependency injected so it is
 * testable without a live app.
 *
 * Three Discord facts drive the shape of this file:
 *
 * 1. **Signatures are over RAW bytes.** Discord signs `timestamp + body` with Ed25519.
 *    Re-serialized JSON will not verify — key order and whitespace are part of the
 *    signed message — so the caller must hand us the untouched request buffer.
 * 2. **3-second deadline, decided before the work happens.** Every real command is
 *    answered with a DEFERRED ack, then a follow-up webhook edit (valid ~15 min). The
 *    catch: ephemerality is fixed by the *deferred* response's flags, before the handler
 *    has produced a Message. `/nexus` is ephemeral on Slack in every branch, so we always
 *    defer ephemeral; a future in-channel reply would need the ack to know in advance.
 * 3. **One custom_id carries what Slack splits in two.** Slack routes on `action_id` and
 *    passes data in `value`; Discord has only `custom_id`. We pack them as `id|value` so
 *    the normalized `Action` looks the same on both sides.
 */
import { createPublicKey, verify as edVerify } from 'crypto';
import { PROVIDERS, parseCommandText } from './types.js';

const API = 'https://discord.com/api/v10';

/** Inbound interaction types (Discord API v10). */
export const INTERACTION = Object.freeze({
  PING: 1, APPLICATION_COMMAND: 2, MESSAGE_COMPONENT: 3, AUTOCOMPLETE: 4, MODAL_SUBMIT: 5,
});
/** Interaction *response* (callback) types. */
export const CALLBACK = Object.freeze({
  PONG: 1,
  CHANNEL_MESSAGE: 4,
  DEFERRED_CHANNEL_MESSAGE: 5,  // "Nexus is thinking…" — follow up within 15 min
  DEFERRED_UPDATE_MESSAGE: 6,   // component ack, original message left as-is for now
  UPDATE_MESSAGE: 7,
});
/** `MessageFlags.EPHEMERAL` — the Discord equivalent of Slack's `response_type:'ephemeral'`. */
export const EPHEMERAL_FLAG = 64;

// ── Ed25519 request verification ─────────────────────────────────────────────
// A raw 32-byte Ed25519 public key has no ASN.1 wrapper, but Node's KeyObject API only
// ingests SPKI DER. This fixed 12-byte prefix is that wrapper for id-Ed25519 (RFC 8410):
// SEQUENCE(0x2a) { SEQUENCE(0x05){ OID 1.3.101.112 }, BIT STRING(0x21) { 0x00 || key } }.
const SPKI_ED25519_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

/** @param {string} hex 64-char hex — Discord's "Public Key" from the app's General page. */
export function publicKeyFromHex(hex) {
  const raw = Buffer.from(String(hex || ''), 'hex');
  // Buffer.from(...,'hex') truncates silently on invalid input instead of throwing, so
  // the length check is the only thing standing between a typo'd key and a confusing
  // "every request is a 401".
  if (raw.length !== 32) throw new Error(`ed25519 public key must be 32 bytes, got ${raw.length}`);
  return createPublicKey({ key: Buffer.concat([SPKI_ED25519_PREFIX, raw]), format: 'der', type: 'spki' });
}

/**
 * Verify a detached Ed25519 signature. Returns false rather than throwing on any
 * malformed input — a bad signature and a malformed one are the same 401.
 * @param {{publicKey: string|Object, signature: string|Buffer, message: Buffer|string}} p
 */
export function verifyEd25519({ publicKey, signature, message }) {
  try {
    const sig = Buffer.isBuffer(signature) ? signature : Buffer.from(String(signature || ''), 'hex');
    if (sig.length !== 64) return false;
    const key = typeof publicKey === 'string' ? publicKeyFromHex(publicKey) : publicKey;
    const msg = Buffer.isBuffer(message) ? message : Buffer.from(String(message ?? ''), 'utf8');
    // `null` algorithm is required for Ed25519 — the hash is part of the scheme.
    return edVerify(null, msg, key, sig);
  } catch { return false; }
}

/**
 * Verify a Discord interaction request: the signed message is `timestamp || rawBody`.
 * @param {{publicKey: string|Object, signature: string, timestamp: string, rawBody: Buffer|string}} p
 */
export function verifyInteractionSignature({ publicKey, signature, timestamp, rawBody }) {
  if (!publicKey || !signature || !timestamp || rawBody == null) return false;
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(String(rawBody), 'utf8');
  const message = Buffer.concat([Buffer.from(String(timestamp), 'utf8'), body]);
  return verifyEd25519({ publicKey, signature, message });
}

// ── custom_id packing ────────────────────────────────────────────────────────
// Slack: {action_id:'nx:do:peek', value:'w1:p2'}. Discord: one custom_id, ≤100 chars.
export const packCustomId = (id, value) =>
  (value == null || value === id ? String(id) : `${id}|${value}`).slice(0, 100);

/** Inverse of {@link packCustomId}. No separator → value mirrors the id, matching the
 *  `button()` builder's `value = value ?? id` default. */
export function unpackCustomId(customId) {
  const s = String(customId || '');
  const i = s.indexOf('|');
  return i === -1 ? { actionId: s, value: s } : { actionId: s.slice(0, i), value: s.slice(i + 1) };
}

// ── text dialect: Slack mrkdwn → Discord markdown ────────────────────────────
// `Message.text` is authored in Slack's dialect (that surface came first). Discord
// renders `:shortcode:` literally and treats a single `*` as italic, so status output
// arrives as ":large_green_circle:2" in what should be *nexus fleet*.
const SLACK_EMOJI = Object.freeze({
  large_green_circle: '🟢', large_yellow_circle: '🟡', white_circle: '⚪',
  red_circle: '🔴', large_blue_circle: '🔵',
  warning: '⚠️', lock: '🔒', rocket: '🚀', sparkles: '✨', zzz: '💤',
  information_source: 'ℹ️', hourglass_flowing_sand: '⏳', eyes: '👀', bulb: '💡',
  white_check_mark: '✅', heavy_check_mark: '✔️', x: '❌',
  leftwards_arrow_with_hook: '↩️', pushpin: '📌', round_pushpin: '📍',
  bar_chart: '📊', broom: '🧹', octagonal_sign: '🛑',
  // Workspace-custom Slack emoji have no Unicode equivalent; map them to something
  // recognisable rather than leaking the raw shortcode.
  'nexus-relay': '📡', 'nexus-presence': '📶', 'nexus-env': '🧩',
});

// Apply `fn` only to the parts of `s` that are NOT inside a code span/block, so nothing
// rewrites the contents of `\`nx:do:peek\`` or a fenced block.
function mapOutsideCode(s, fn) {
  const parts = s.split(/(```[\s\S]*?```|`[^`\n]*`)/g);
  return parts.map((p, i) => (i % 2 === 1 ? p : fn(p))).join('');
}

const emojify = (t) => t.replace(/:([a-z0-9_+-]+):/g, (m, name) =>
  // Known keys ONLY. A blanket strip would eat the `:do:` inside action ids like
  // `nx:do:peek`, silently corrupting text.
  Object.prototype.hasOwnProperty.call(SLACK_EMOJI, name) ? SLACK_EMOJI[name] : m);

// Slack bold is *one* asterisk; Discord's is two (one is italic). Anchored to word
// boundaries so arithmetic and glob patterns are left alone.
const boldify = (t) => t.replace(/(^|[\s(])\*(?!\s)([^*\n]+?)(?<!\s)\*(?=$|[\s.,;:!?)])/g, '$1**$2**');

/** Translate Slack-dialect text into Discord markdown. Safe on already-plain text. */
export function slackTextToDiscord(s) {
  if (s == null) return s;
  return mapOutsideCode(String(s), (t) => boldify(emojify(t)));
}

// Discord hard-rejects a message over 2000 characters with a 400 — the whole reply is
// lost, not trimmed. Slack has no comparable limit, so text that has always been fine
// there (a 40-line `peek` capture is ~3kB) silently fails here. Cap on our side.
export const MAX_CONTENT = 2000;
const TRUNC = '\n… truncated';

export function capContent(s) {
  const str = String(s ?? '');
  if (str.length <= MAX_CONTENT) return str;
  let out = str.slice(0, MAX_CONTENT - TRUNC.length) + TRUNC;
  // Cutting mid-code-block would leave the fence unclosed and swallow the marker into
  // an unterminated code span, so balance it — still within the limit because the
  // slice above reserved room.
  if ((out.match(/```/g) || []).length % 2 === 1) {
    out = out.slice(0, MAX_CONTENT - TRUNC.length - 4) + TRUNC + '\n```';
  }
  return out;
}

// ── render: Message → Discord payload ────────────────────────────────────────
const MAX_ROWS = 5;
const MAX_BUTTONS_PER_ROW = 5;
// Discord ButtonStyle: 1 PRIMARY, 2 SECONDARY, 3 SUCCESS, 4 DANGER.
const BUTTON_STYLE = { primary: 1, danger: 4 };

/**
 * Render a normalized Message → a Discord interaction/message payload.
 * `m.blocks` (Slack Block Kit) is deliberately ignored: a Message carrying both renders
 * rich on Slack and from `components` here; one carrying only `blocks` degrades to its
 * `text`. See the `blocks` note in types.js.
 * @param {import('./types.js').Message} m
 */
export function messageToDiscord(m = {}) {
  const out = {};
  // Convert dialect first, then cap — emoji substitution changes the length.
  if (m.text != null) out.content = capContent(slackTextToDiscord(m.text));
  if (m.ephemeral) out.flags = EPHEMERAL_FLAG;
  const rows = componentsToRows(m.components);
  if (rows.length) out.components = rows;
  return out;
}

function componentsToRows(components) {
  const rows = [];
  for (const row of components || []) {
    let buttons = [];
    for (const el of row.elements || []) {
      if (el && el.type === 'select') {
        // A string select must occupy its action row alone (Discord rejects a mixed row
        // with 50035), whereas Slack happily renders buttons and a select side by side.
        // So flush pending buttons, then give the select its own row.
        if (buttons.length) { rows.push(...chunkButtons(buttons)); buttons = []; }
        rows.push({ type: 1, components: [selectToDiscord(el)] });
      } else if (el && el.type === 'button') {
        buttons.push(buttonToDiscord(el));
      }
    }
    if (buttons.length) rows.push(...chunkButtons(buttons));
  }
  // Over-cap rows are dropped rather than sent: Discord 400s the whole message if it
  // gets a 6th row, which would lose the text too.
  return rows.slice(0, MAX_ROWS);
}

function chunkButtons(buttons) {
  const rows = [];
  for (let i = 0; i < buttons.length; i += MAX_BUTTONS_PER_ROW) {
    rows.push({ type: 1, components: buttons.slice(i, i + MAX_BUTTONS_PER_ROW) });
  }
  return rows;
}

function buttonToDiscord(el) {
  return {
    type: 2,
    style: BUTTON_STYLE[el.style] || 2,
    label: String(el.text ?? '').slice(0, 80),
    custom_id: packCustomId(el.id, el.value),
  };
}

function selectToDiscord(el) {
  return {
    type: 3,
    custom_id: packCustomId(el.id, null),
    placeholder: String(el.placeholder || 'Select').slice(0, 150),
    // Discord caps a string select at 25 options; Slack allows 100. Truncate rather
    // than 400 the whole payload.
    options: (el.options || []).slice(0, 25).map((o) => ({
      label: String(o.text ?? '').slice(0, 100),
      value: String(o.value ?? '').slice(0, 100),
    })),
  };
}

// ── parse: interaction → Command / Action ────────────────────────────────────
// In a guild the invoker is at `member.user`; in a DM it is at `user`. Getting this
// wrong yields undefined user ids only in one of the two contexts.
const invokerId = (i) => (i.member && i.member.user && i.member.user.id) || (i.user && i.user.id);

/**
 * Application-command interaction (type 2) → normalized Command.
 * Mirrors `/nexus <args>` — a single string option named `args`, matching the Slack
 * slash surface rather than native Discord subcommands (see the change's non-goals).
 * @param {Object} i
 * @returns {import('./types.js').Command}
 */
export function interactionToCommand(i = {}) {
  const options = (i.data && i.data.options) || [];
  const argsOpt = options.find((o) => o && o.name === 'args');
  return {
    ...parseCommandText(argsOpt ? argsOpt.value : ''),
    userId: invokerId(i),
    channelId: i.channel_id,
    replyToken: i.token,
    provider: PROVIDERS.DISCORD,
  };
}

/**
 * Message-component interaction (type 3) → normalized Action, or null.
 * @param {Object} i
 * @returns {import('./types.js').Action|null}
 */
export function interactionToAction(i = {}) {
  if (!i || i.type !== INTERACTION.MESSAGE_COMPONENT) return null;
  const d = i.data || {};
  if (!d.custom_id) return null;
  const { actionId, value } = unpackCustomId(d.custom_id);
  // A string select reports the chosen option in `values`; its custom_id carries no
  // payload. This is the Discord analogue of Slack's `selected_option.value`.
  const selected = Array.isArray(d.values) && d.values.length ? d.values[0] : null;
  return {
    actionId,
    value: selected != null ? selected : value,
    userId: invokerId(i),
    replyToken: i.token,
    provider: PROVIDERS.DISCORD,
  };
}

/** The `/nexus` application-command definition (bulk-overwrite payload). */
export const NEXUS_COMMAND = Object.freeze({
  name: 'nexus',
  description: 'Nexus fleet control — status, agents, peek, msg, spawn…',
  options: [Object.freeze({
    type: 3,                        // STRING
    name: 'args',
    description: 'e.g. status · agents --local · peek <agent> 40 · msg <agent> hello',
    required: false,
  })],
});

/**
 * @implements {import('./types.js').NexusProvider}
 */
export class DiscordAdapter {
  /**
   * @param {Object} deps
   * @param {string} [deps.publicKey]  Ed25519 public key (hex) from the app's General page.
   * @param {string} [deps.botToken]   Bot token, for command registration + follow-ups.
   * @param {string} [deps.appId]      Application id — part of the follow-up webhook URL.
   * @param {string} [deps.guildId]    Optional. Register `/nexus` to this guild instead of
   *                                    globally: guild commands appear immediately, global
   *                                    ones take up to an hour to propagate. Set it while
   *                                    iterating, unset it to ship.
   * @param {string} [deps.path]       HTTP path the interactions endpoint is mounted at.
   * @param {typeof fetch} [deps.fetchImpl]
   * @param {(msg:string)=>void} [deps.log]
   */
  constructor({ publicKey, botToken, appId, guildId, path = '/discord/interactions', fetchImpl, log } = {}) {
    this.name = PROVIDERS.DISCORD;
    this.publicKey = publicKey || '';
    this.botToken = botToken || '';
    this.appId = appId || '';
    this.guildId = guildId || '';
    this.path = path;
    this.fetch = fetchImpl || ((...a) => fetch(...a));
    this.log = log || (() => {});
    this._onCommand = null;
    this._onAction = null;
    this._key = null;
  }

  /** Fully configured? An unconfigured adapter is inert — never started, never routed. */
  get enabled() { return Boolean(this.publicKey && this.botToken && this.appId); }

  onCommand(handler) { this._onCommand = handler; }
  onAction(handler) { this._onAction = handler; }

  /** Parse the public key once, so a malformed one fails loudly at startup. */
  key() {
    if (!this._key) this._key = publicKeyFromHex(this.publicKey);
    return this._key;
  }

  /** Where `/nexus` gets registered: one guild (instant) or globally (~1h to propagate). */
  commandsUrl() {
    return this.guildId
      ? `${API}/applications/${this.appId}/guilds/${this.guildId}/commands`
      : `${API}/applications/${this.appId}/commands`;
  }

  /**
   * Register `/nexus`. Bulk overwrite (PUT) is idempotent by construction: the body IS
   * the complete command set for that scope, so re-running never duplicates.
   *
   * Guild and global are SEPARATE scopes, not a fallback chain — a command registered to
   * both shows up twice in that guild. Switching from guild-scoped back to global means
   * clearing the guild scope (PUT `[]` to the guild URL), not just unsetting the env var.
   * @returns {Promise<boolean>} true when Discord accepted the definition.
   */
  async start() {
    if (!this.enabled) { this.log('discord: not configured — adapter inert'); return false; }
    this.key();   // throws on a malformed key rather than 401-ing every request later
    const res = await this.fetch(this.commandsUrl(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bot ${this.botToken}` },
      body: JSON.stringify([NEXUS_COMMAND]),
    });
    if (!res || !res.ok) {
      // Never interpolate the response body — a Discord error echoes request headers in
      // some failure modes. Status alone is enough to diagnose (401 bad token, 403 wrong
      // app id or bot not in that guild, 429 rate limit).
      this.log(`discord: command registration failed (HTTP ${res && res.status})`);
      return false;
    }
    this.log(this.guildId
      ? `discord: /nexus registered (app ${this.appId}, guild ${this.guildId} — visible immediately)`
      : `discord: /nexus registered (app ${this.appId}, global — up to 1h to propagate)`);
    return true;
  }

  /**
   * Handle one inbound interaction request.
   *
   * Returns the response to write immediately, plus an optional `work` thunk holding
   * everything that must happen AFTER responding. That split is what keeps us inside
   * Discord's 3s deadline, and it keeps the deferred-then-follow-up dance explicit and
   * testable instead of hidden in a floating promise.
   *
   * @param {{rawBody: Buffer|string, signature: string, timestamp: string}} req
   * @returns {Promise<{status: number, json: Object, work: null|(() => Promise<void>)}>}
   */
  async handleInteraction({ rawBody, signature, timestamp }) {
    if (!this.enabled) return { status: 404, json: { error: 'discord not configured' }, work: null };

    // Discord requires a 401 on bad signatures — it probes with a deliberately invalid
    // one when you save the endpoint URL, and refuses the endpoint if we answer 200.
    if (!verifyInteractionSignature({ publicKey: this.key(), signature, timestamp, rawBody })) {
      // Logged because a silent 401 is undebuggable: Discord's endpoint verification
      // sends BOTH a bad signature (must 401) and a good one (must PONG), and if the
      // configured public key belongs to a different app the good one 401s too — which
      // looks identical from outside. Sizes only, never the signature itself.
      const n = Buffer.isBuffer(rawBody) ? rawBody.length : String(rawBody || '').length;
      // Causes, in rough order of likelihood: DISCORD_PUBLIC_KEY belongs to a different
      // app; a proxy rewrote the body (signatures cover exact bytes); missing headers.
      // body≈10B is a hand-rolled local probe — a genuine Discord PING is hundreds.
      this.log(`discord: signature verify FAILED (sig=${String(signature || '').length}ch ts=${timestamp ? 'set' : 'MISSING'} body=${n}B)${n < 64 ? ' [tiny body — probably a local probe, not Discord]' : ' [Discord-sized body — check DISCORD_PUBLIC_KEY against GET /applications/@me verify_key]'}`);
      return { status: 401, json: { error: 'invalid request signature' }, work: null };
    }

    let i;
    try { i = JSON.parse(Buffer.isBuffer(rawBody) ? rawBody.toString('utf8') : String(rawBody)); }
    catch { return { status: 400, json: { error: 'malformed body' }, work: null }; }

    if (i.type === INTERACTION.PING) {
      // This is the line that proves endpoint verification got through: a signed PING
      // that verified. If the portal still rejects the URL after seeing this, the
      // problem is transport (reachability/timeout), not crypto.
      this.log('discord: PING → PONG (signed ping verified)');
      return { status: 200, json: { type: CALLBACK.PONG }, work: null };
    }

    if (i.type === INTERACTION.APPLICATION_COMMAND) {
      const command = interactionToCommand(i);
      this.log(`discord: /nexus "${command.name}${command.rawArgs ? ' ' + command.rawArgs : ''}" from ${command.userId}`);
      return {
        status: 200,
        // Ephemeral is locked in HERE, before the handler runs — see the file header.
        json: { type: CALLBACK.DEFERRED_CHANNEL_MESSAGE, data: { flags: EPHEMERAL_FLAG } },
        work: async () => { if (this._onCommand) await this._onCommand(command, i); },
      };
    }

    if (i.type === INTERACTION.MESSAGE_COMPONENT) {
      const action = interactionToAction(i);
      this.log(`discord: component ${action ? action.actionId : '-'} from ${action && action.userId}`);
      return {
        status: 200,
        // DEFERRED_UPDATE_MESSAGE: acks without visibly changing the panel, so the
        // follow-up edit is the only thing the user sees move.
        json: { type: CALLBACK.DEFERRED_UPDATE_MESSAGE },
        work: async () => { if (action && this._onAction) await this._onAction(action, i); },
      };
    }

    // Unknown/unsupported (autocomplete, modal submit): ack nothing, 400 so it is
    // visible in logs rather than silently swallowed.
    return { status: 400, json: { error: `unsupported interaction type ${i.type}` }, work: null };
  }

  /**
   * Answer a deferred interaction by editing the placeholder the ack created.
   * `flags` are fixed at defer time, so a Message's `ephemeral` is intentionally not
   * re-sent here — it would be ignored.
   */
  async reply(replyToken, m) {
    if (!replyToken || !this.appId) return;
    const { flags, ...payload } = messageToDiscord(m);   // eslint-disable-line no-unused-vars
    const res = await this.fetch(`${API}/webhooks/${this.appId}/${replyToken}/messages/@original`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res && !res.ok) await this.logHttpFailure('follow-up edit', res, payload);
  }

  /**
   * Report a failed API call with enough detail to act on, without echoing content.
   * Discord's validation errors name the field and the constraint
   * (`{"content":{"_errors":[{"code":"BASE_TYPE_MAX_LENGTH",…}]}}`), which is exactly
   * what you need and safe to log; the raw body is not, since some error paths reflect
   * request data back. Our own payload size goes in too — a length problem is invisible
   * from the status code alone.
   */
  async logHttpFailure(kind, res, payload = {}) {
    let detail = '';
    try {
      if (typeof res.json === 'function') {
        const body = await res.json();
        const errs = body && body.errors ? JSON.stringify(body.errors).slice(0, 300) : '';
        detail = ` code=${(body && body.code) || '?'}${errs ? ' ' + errs : ''}`;
      }
    } catch { /* non-JSON or already-consumed body */ }
    const len = (payload.content || '').length;
    const rows = (payload.components || []).length;
    this.log(`discord: ${kind} failed (HTTP ${res.status}) contentLen=${len} rows=${rows}${detail}`);
  }

  /**
   * Send an ADDITIONAL message on the same interaction, leaving the original intact.
   * This is the Discord half of the portable "edit vs. post" pair: `reply()` edits the
   * placeholder (Slack `replace_original: true`), `followUp()` adds a new message
   * (Slack `replace_original: false`). Unlike `reply`, a follow-up honours `flags`, so
   * an ephemeral Message stays ephemeral here.
   */
  async followUp(replyToken, m) {
    if (!replyToken || !this.appId) return;
    const res = await this.fetch(`${API}/webhooks/${this.appId}/${replyToken}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(messageToDiscord(m)),
    });
    if (res && !res.ok) await this.logHttpFailure('follow-up post', res, messageToDiscord(m));
  }

  /** Proactive post to a channel (bot auth, no interaction involved). */
  async send(channel, m) {
    if (!channel || !this.botToken) return;
    const { flags, ...payload } = messageToDiscord(m);   // eslint-disable-line no-unused-vars
    const res = await this.fetch(`${API}/channels/${channel}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bot ${this.botToken}` },
      body: JSON.stringify(payload),
    });
    if (res && !res.ok) this.log(`discord: postMessage failed (HTTP ${res.status})`);
  }
}
