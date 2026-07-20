/**
 * NatsTransport — the NATS + JetStream A2A transport for the slack-bridge.
 *
 * This is the `nats` half of the pluggable bus seam (see docs/agent-bus-roadmap.md
 * and openspec/changes/nats-a2a-bus-transport). It replaces Slack `chat.postMessage`
 * + Socket Mode as the medium for AGENT-TO-AGENT traffic only; the human notify/reply
 * leg stays on Slack in index.js. Routing, the delivery layer (send-keys / SDK inbox),
 * and the `@waiting` idle-gate are unchanged — this module only PUBLISHES and SUBSCRIBES.
 *
 * Mapping (all subject/key math is the tested codec in orchestrator.js):
 *   - Addressing : an A2A message publishes to `<prefix>.<host>.<workspace>.<name>`;
 *                  a bridge subscribes only its own host subtree (`<prefix>.<host>.>`),
 *                  so the broker routes to the owning host — no fleet-wide fan-out.
 *   - Durable inbox : a JetStream stream persists A2A messages; an offline recipient's
 *                     messages wait in the stream and drain from the durable consumer's
 *                     cursor on reconnect. The stream (bounded retention) is the audit log.
 *   - Presence : a JetStream KV bucket keyed by FQDN with a bucket-wide TTL; a bridge
 *                upserts its live agents on a heartbeat and any bridge reads the bucket
 *                to build reachability + resolve bare names. TTL expiry = departure.
 *
 * Uses the nats.js v3 scoped packages (@nats-io/transport-node + /jetstream + /kv),
 * imported dynamically by index.js ONLY when NEXUS_BUS_TRANSPORT=nats, so a Slack-only
 * bridge never needs them installed.
 *
 * NOT YET integration-tested against a live broker (no nats-server in the authoring
 * environment). The pure subject/key/envelope math is unit-tested in orchestrator.js;
 * the JetStream/KV wiring is written to the nats.js v3 API and validated with `node --check`.
 */
import { connect, credsAuthenticator } from '@nats-io/transport-node';
import {
  jetstream,
  jetstreamManager,
  AckPolicy,
  DeliverPolicy,
  RetentionPolicy,
  DiscardPolicy,
} from '@nats-io/jetstream';
import { Kvm } from '@nats-io/kv';
import { readFileSync } from 'fs';
import {
  fqdnToSubject,
  subjectToFqdn,
  hostSubjectFilter,
  fqdnToKvKey,
  kvKeyToFqdn,
} from '../orchestrator.js';

const enc = new TextEncoder();
const dec = new TextDecoder();

// Build connect() auth options from whatever the operator provided: a creds file
// (NKEY/JWT — the scale path), a plain token, or user/pass. v3 still accepts bare
// token / user+pass in ConnectionOptions; a creds file needs the authenticator.
function authOptions({ credsFile, token, user, pass }) {
  if (credsFile) {
    const creds = readFileSync(credsFile);
    return { authenticator: credsAuthenticator(new Uint8Array(creds)) };
  }
  if (token) return { token };
  if (user) return { user, pass };
  return {};
}

export function createNatsTransport(opts = {}) {
  const {
    url = 'nats://127.0.0.1:4222',
    name = 'nexus-slack-bridge',
    selfHost,
    subjectPrefix = 'nexus.a2a',
    streamName = 'NEXUS_A2A',
    kvBucket = 'nexus_presence',
    // Stream retention = the audit window (also the offline-buffer horizon).
    maxAgeMs = 7 * 24 * 60 * 60 * 1000,
    maxBytes = -1,
    // Consumer redelivery lease. ackWaitMs must comfortably exceed how long the
    // idle-gate might hold a message before delivery; the caller may call working()
    // to extend it. maxDeliver bounds a poison message.
    ackWaitMs = 5 * 60 * 1000,
    maxDeliver = 100,
    // Presence KV bucket TTL (entries expire unless refreshed by the heartbeat).
    presenceTtlMs = 16 * 60 * 1000,
    logger = console,
    ...credsOpts
  } = opts;

  if (!selfHost) throw new Error('NatsTransport: selfHost is required');

  const state = {
    nc: null, js: null, jsm: null, kv: null,
    consuming: false, closed: false,
  };

  async function connectNats() {
    state.nc = await connect({
      servers: url,
      name,
      reconnect: true,
      maxReconnectAttempts: -1,     // reconnect forever — JetStream buffers while we're gone
      waitOnFirstConnect: true,
      ...authOptions(credsOpts),
    });
    state.jsm = await jetstreamManager(state.nc);
    state.js = jetstream(state.nc);
  }

  // Idempotent stream creation: add only if missing, so a shared broker with an
  // already-provisioned stream is not clobbered by a differing config.
  async function ensureStream() {
    const subjects = [`${subjectPrefix}.>`];
    try {
      await state.jsm.streams.info(streamName);
      return;
    } catch {
      // not found → create
    }
    await state.jsm.streams.add({
      name: streamName,
      subjects,
      retention: RetentionPolicy.Limits,
      discard: DiscardPolicy.Old,
      max_age: maxAgeMs * 1_000_000,   // nanoseconds
      max_bytes: maxBytes,
    });
    logger.log?.(`[nats] stream ${streamName} ensured (subjects=${subjects.join(',')}, max_age=${maxAgeMs}ms)`);
  }

  async function ensureKv() {
    // Kvm.create creates the bucket if missing (or opens it); ttl is bucket-wide
    // (per-entry max age), which ages out presence entries we stop refreshing.
    const kvm = new Kvm(state.nc);
    state.kv = await kvm.create(kvBucket, { ttl: presenceTtlMs });
    logger.log?.(`[nats] kv bucket ${kvBucket} ready (ttl=${presenceTtlMs}ms)`);
  }

  // Resolve a target FQDN to its publish subject. `fqdn` is { host, workspace, name };
  // an empty host defaults to selfHost (a bare name with no known owner is treated as
  // host-local — the same "owning host delivers" contract as the Slack bus).
  function subjectFor(fqdn) {
    const f = { host: fqdn.host || selfHost, workspace: fqdn.workspace || '', name: fqdn.name || '' };
    return fqdnToSubject(f, { prefix: subjectPrefix });
  }

  async function connectAndProvision() {
    await connectNats();
    await ensureStream();
    await ensureKv();
    logger.log?.(`[nats] connected ${url} as host=${selfHost}`);
  }

  // Publish a typed A2A envelope to the target's subject. `envelope` is the full envelope
  // ({ v, id, ts, from, to, kind, corr?, reply_to?, body, meta? }); `to` is the address token
  // as typed (kept for delivery), the subject is derived from the resolved fqdn. The payload is
  // the envelope JSON verbatim, so the consuming bridge parses it straight back. A legacy caller
  // passing { to, from, msg } still works — the receiver's parseEnvelope treats it as a `msg`.
  async function publish(fqdn, envelope) {
    if (!state.js) throw new Error('NatsTransport: not connected');
    const subject = subjectFor(fqdn);
    const payload = enc.encode(JSON.stringify(envelope));
    const ack = await state.js.publish(subject, payload);
    return { subject, seq: ack.seq };
  }

  // Subscribe this host's subtree via a durable consumer. `onMessage(envelope, msg, fqdn)`
  // is called per message, where `msg` exposes ack()/working()/nak()/term() so the caller
  // controls acknowledgement timing (ack-on-idle possible later). This module does NOT ack
  // on the caller's behalf — the caller owns the lease.
  async function subscribe(onMessage) {
    if (!state.jsm) throw new Error('NatsTransport: not connected');
    const durable = `bridge_${selfHost}`.replace(/[^A-Za-z0-9_-]/g, '_');
    const filter = hostSubjectFilter(selfHost, { prefix: subjectPrefix });
    try {
      await state.jsm.consumers.info(streamName, durable);
    } catch {
      await state.jsm.consumers.add(streamName, {
        durable_name: durable,
        filter_subject: filter,
        ack_policy: AckPolicy.Explicit,
        deliver_policy: DeliverPolicy.All,
        ack_wait: ackWaitMs * 1_000_000,   // nanoseconds
        max_deliver: maxDeliver,
      });
    }
    logger.log?.(`[nats] durable consumer ${durable} bound (filter=${filter})`);

    const consumer = await state.js.consumers.get(streamName, durable);
    state.consuming = true;
    (async () => {
      const messages = await consumer.consume();
      for await (const m of messages) {
        if (state.closed) break;
        let envelope = null;
        try {
          envelope = JSON.parse(dec.decode(m.data));
        } catch (e) {
          logger.warn?.(`[nats] undecodable message on ${m.subject} — terminating: ${e.message}`);
          m.term();
          continue;
        }
        const fqdn = subjectToFqdn(m.subject, { prefix: subjectPrefix });
        try {
          await onMessage(envelope, m, fqdn);
        } catch (e) {
          logger.error?.(`[nats] onMessage threw for ${envelope?.to}: ${e.message}`);
          m.nak();   // redeliver after ackWait
        }
      }
    })().catch((e) => {
      state.consuming = false;
      logger.error?.(`[nats] consume loop ended: ${e.message}`);
    });
  }

  // Presence — upsert this host's live agents into the KV bucket. `agents` is an array
  // of { workspace, name, pane, ... }; each is keyed by its FQDN (host = selfHost). The
  // bucket TTL ages out entries we stop refreshing (a departed/reaped agent).
  async function presenceUpsert(agents) {
    if (!state.kv) throw new Error('NatsTransport: KV not ready');
    const now = Date.now();
    for (const a of agents) {
      const key = fqdnToKvKey({ host: selfHost, workspace: a.workspace || '', name: a.name });
      const val = enc.encode(JSON.stringify({
        host: selfHost, workspace: a.workspace || '', name: a.name, pane: a.pane || '', ts: now,
      }));
      await state.kv.put(key, val);
    }
  }

  // Read the whole presence bucket back as records [{ host, workspace, name, pane, ts }].
  // Any bridge calls this to build the fleet-wide reachability directory + resolve bare names.
  async function presenceSnapshot() {
    if (!state.kv) throw new Error('NatsTransport: KV not ready');
    const out = [];
    const keys = await state.kv.keys();
    for await (const k of keys) {
      const e = await state.kv.get(k);
      if (!e) continue;
      try {
        out.push(JSON.parse(dec.decode(e.value)));
      } catch {
        const fqdn = kvKeyToFqdn(k);
        if (fqdn) out.push({ ...fqdn, pane: '', ts: 0 });
      }
    }
    return out;
  }

  // Explicit removal (a clean deregister); TTL also handles the crash case.
  async function presenceDelete(fqdn) {
    if (!state.kv) return;
    await state.kv.delete(fqdnToKvKey({ host: selfHost, ...fqdn }));
  }

  function health() {
    return {
      transport: 'nats',
      url,
      connected: !!state.nc && !state.nc.isClosed?.(),
      consuming: state.consuming,
      host: selfHost,
    };
  }

  async function close() {
    state.closed = true;
    if (state.nc) {
      await state.nc.drain().catch(() => {});
    }
  }

  return {
    connect: connectAndProvision,
    publish,
    subscribe,
    presenceUpsert,
    presenceSnapshot,
    presenceDelete,
    subjectFor,
    health,
    close,
  };
}

export default createNatsTransport;
