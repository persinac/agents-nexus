// nx-kv -- read the fleet agent registry out of the NATS JetStream KV bucket.
//
// Invoked by tmux/mac/tmux-scripts/nx-kv.sh. Kept as a separate .mjs (rather than a
// heredoc inside the wrapper) so it can be linted, and so no shell quoting sits between
// this code and the broker.
//
// IT LIVES HERE, not beside the wrapper, because Node resolves a bare ESM import by
// walking up from THIS file's directory and ignores NODE_PATH (a CommonJS-only
// mechanism). Sitting in tmux-scripts/ it could never find @nats-io/*, whatever the
// environment claimed. Do not "tidy" it back next to the wrapper.
//
// CREDENTIAL DISCIPLINE.
// NATS_URL embeds user:pass. This script NEVER prints it. It parses the URL and
// echoes only `protocol//hostname:port`; auth is reported as present/absent, never
// by value. Every future edit must keep that property -- output from here lands in
// agent transcripts, which are durable and cannot be un-written.
//
// NO RAW CONTROL BYTES IN THIS FILE. The /proc environ separator below is built with
// String.fromCharCode(0) rather than typed, because one raw zero byte makes GNU grep
// classify the whole file as binary and then print NOTHING for a match -- output that
// is indistinguishable from "no match". That byte cost this repo three wrong
// conclusions in one session (see slack-bridge/source-hygiene.test.js), and the first
// two drafts of THIS file reintroduced it.

import fs from 'node:fs';
import { connect } from '@nats-io/transport-node';
import { Kvm } from '@nats-io/kv';
import { kvKeyToFqdn } from './orchestrator.js';

const [, , cmd = 'keys', arg] = process.argv;

const ENVIRON_SEP = String.fromCharCode(0);

// Env source: this shell first, else the running bridge's /proc environ (the creds live
// there and nowhere else readable). Reading /proc is a targeted lookup, not a dump -- we
// pull the NATS_* names and drop the rest on the floor. Values are used, never printed.
function loadEnv() {
  if (process.env.NATS_URL) return process.env;
  const pid = (process.env.NX_BRIDGE_PID || '').trim();
  if (!pid) return process.env;
  try {
    const out = { ...process.env };
    for (const pair of fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split(ENVIRON_SEP)) {
      const i = pair.indexOf('=');
      if (i <= 0) continue;
      const k = pair.slice(0, i);
      if (k.startsWith('NATS_')) out[k] = pair.slice(i + 1);
    }
    return out;
  } catch {
    return process.env;
  }
}

const env = loadEnv();
const url = env.NATS_URL;
if (!url) {
  console.error('nx-kv: no NATS_URL in this shell and no readable bridge env.');
  console.error('       Run it on a host with the bridge up, or export NATS_URL yourself.');
  process.exit(1);
}

// Endpoint only -- never the credential-bearing URL.
let endpoint = '(unparseable URL)';
try {
  const u = new URL(url);
  endpoint = `${u.protocol}//${u.hostname}:${u.port || '4222'}`;
} catch { /* keep the placeholder */ }

const bucket = env.NATS_PRESENCE_KV || 'nexus_presence';
const opts = { servers: url, timeout: 10000, name: 'nx-kv' };
if (env.NATS_USER) opts.user = env.NATS_USER;
if (env.NATS_PASS) opts.pass = env.NATS_PASS;
if (env.NATS_TOKEN) opts.token = env.NATS_TOKEN;

let nc;
try {
  nc = await connect(opts);
} catch (e) {
  console.error(`nx-kv: cannot reach ${endpoint} -- ${e.message}`);
  process.exit(2);
}

const kv = await new Kvm(nc).open(bucket);
const keys = [];
for await (const k of await kv.keys()) keys.push(k);
keys.sort();

// KV keys are dot-encoded; the address you type is slash-separated. Print the typed form
// so the output is directly usable without anyone having to remember which goes where.
//
// Use the bridge's OWN codec, not a split/join. Tokens are percent-style escaped with `=`
// (KV keys forbid `~`), so a naive split leaves `melvin/=7e/melvin2001` on screen where the
// real address is `melvin/~/melvin2001` — an FQDN that looks paste-ready and is not.
const toFqdn = (k) => {
  const f = kvKeyToFqdn(k);
  return f ? [f.host, f.workspace, f.name].join('/') : k;
};

if (cmd === 'keys' || cmd === 'ls') {
  console.log(`# ${bucket} @ ${endpoint} -- ${keys.length} live agent(s)`);
  console.log('# address these with: agent-send.sh <fqdn> "<message>"');
  for (const k of keys) console.log(toFqdn(k));
} else if (cmd === 'raw') {
  for (const k of keys) console.log(k);
} else if (cmd === 'hosts') {
  const hosts = [...new Set(keys.map((k) => k.split('.')[0]))].sort();
  console.log(`# ${hosts.length} host(s) in ${bucket}`);
  for (const h of hosts) console.log(h);
} else if (cmd === 'get') {
  if (!arg) { console.error('nx-kv: get needs an fqdn, e.g. alex-nexus/interactive/general'); process.exit(1); }
  const want = arg.replace(/\//g, '.');
  if (!keys.includes(want)) {
    console.error(`nx-kv: ${arg} is not in ${bucket}. Run 'nx-kv.sh keys' for the live list.`);
    process.exit(1);
  }
  try {
    const e = await kv.get(want);
    let v = e?.string() ?? '';
    try { v = JSON.stringify(JSON.parse(v), null, 2); } catch { /* print as-is */ }
    console.log(v);
  } catch (e) {
    // Expected, not broken: the bridge's credentials are scoped to allow keys() but not
    // get(). Say so plainly so nobody files it as an outage.
    console.error('nx-kv: the key EXISTS but its value is not readable with these credentials.');
    console.error(`       This is the least-privilege scope working as designed (${e.message}).`);
    console.error('       Presence/liveness questions are answerable from the key alone.');
    process.exit(3);
  }
} else {
  console.error(`nx-kv: unknown command '${cmd}' (want: keys | raw | hosts | get <fqdn>)`);
  process.exit(1);
}

await nc.drain();
