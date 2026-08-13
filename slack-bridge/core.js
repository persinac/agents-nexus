/**
 * Provider-agnostic `/nexus` dispatch.
 *
 * This is the half of the bridge that neither Slack nor Discord owns: a normalized
 * `Command` or `Action` goes in, normalized `Message`s come out through callbacks. No
 * platform types appear in any signature or body — that is what lets one implementation
 * serve both front ends.
 *
 * It lives here rather than in `index.js` for one concrete reason: `index.js` opens a
 * Socket Mode connection at module load, so importing it from a test is impossible. The
 * dispatch was therefore the only part of the seam verified structurally and live but
 * never by unit test. Everything it needs is injected, so a test can drive it with
 * stubs and assert the exact Messages produced.
 *
 * `orchestrator` and the Message constructors are imported directly rather than
 * injected — they are pure and already unit-tested, so parameterizing them would add
 * ceremony without adding testability.
 */
import * as orch from './orchestrator.js';
import { ephemeral as eph } from './providers/types.js';

/**
 * @typedef {Object} NexusCoreDeps
 * @property {boolean}  spawnEnabled
 * @property {string}   nexusChannel          Control channel spawn/restore results post to.
 * @property {string}   spawnAllowlistFile
 * @property {() => Array<{text: string, value: string}>} localAgentOptions
 * @property {(pane: string) => string}       paneName
 * @property {(query: string) => Promise<string>} statusText
 * @property {(localOnly: boolean) => Promise<Object>} agentsSummaryText
 * @property {(pane: string, lines?: string) => Promise<Object>} doPeek
 * @property {(pane: string) => Promise<Object>} doClearCmd
 * @property {(pane: string) => Promise<Object>} doStopCmd
 * @property {(pane: string, onOff: string) => Promise<Object>} doKeepCmd
 * @property {(agent: string, text: string) => Promise<Object>} doMsgCmd
 * @property {(channel: string, triggerId: undefined, repo: string, seed: string, uid: string) => Promise<any>} doSpawn
 * @property {(channel: string, triggerId: undefined, repo: string, uid: string) => Promise<any>} doRestore
 * @property {(uid: string) => string}        nexusMsgPrefix
 * @property {(args: string[]) => Promise<any>} ledgerCmd
 * @property {(msg: string) => void}          [log]
 */

/**
 * Build the dispatch pair against a set of injected dependencies.
 * @param {NexusCoreDeps} deps
 */
export function createNexusCore(deps) {
  const {
    spawnEnabled, nexusChannel, spawnAllowlistFile,
    localAgentOptions, paneName, statusText, agentsSummaryText,
    doPeek, doClearCmd, doStopCmd, doKeepCmd, doMsgCmd, doSpawn, doRestore,
    nexusMsgPrefix, ledgerCmd,
    log = (m) => console.error(m),
  } = deps;

  const panelAgents = () =>
    localAgentOptions().map((o) => ({ name: o.text, pane: o.value, label: o.text }));

  /**
   * A normalized `Command` in, normalized `Message`s out through `reply`.
   * The callback is named `reply` rather than `respond` deliberately: `respond()` in
   * index.js is Slack-specific, and shadowing it here would let an accidental use slip
   * through unnoticed.
   * @param {import('./providers/types.js').Command} command
   * @param {(m: import('./providers/types.js').Message) => Promise<void>} reply
   */
  async function dispatchNexusCommand(command, reply) {
    const { name: sub, args, rawArgs, userId: uid, channelId } = command;
    const chan = channelId || nexusChannel || '';
    try {
      if (!sub || sub === 'home' || sub === 'help') {
        await reply(orch.fleetPanel({ agents: panelAgents(), spawnEnabled }));
        return;
      }
      if (sub === 'status') { await reply(eph(await statusText(args.join(' ')))); return; }
      if (sub === 'agents') { await reply(await agentsSummaryText(/--local\b/.test(rawArgs))); return; }
      if (sub === 'peek') { await reply(await doPeek(args[0], args[1])); return; }
      if (sub === 'clear') { await reply(await doClearCmd(args[0])); return; }
      if (sub === 'stop') { await reply(await doStopCmd(args[0])); return; }
      if (sub === 'keep') { await reply(await doKeepCmd(args[0], args[1])); return; }
      if (sub === 'msg' || sub === 'message') {
        const agent = args[0] || '';
        const msgText = rawArgs.slice(agent.length).trim();
        const full = msgText ? nexusMsgPrefix(uid) + msgText : msgText;
        await reply(await doMsgCmd(agent, full));
        return;
      }
      if (sub === 'spawn') {
        if (!spawnEnabled) { await reply(eph(':lock: spawn disabled (`SLACK_SPAWN_ENABLED=0`).')); return; }
        const repo = args[0];
        const seed = rawArgs.slice((repo || '').length).trim();
        const allow = orch.loadAllowlist(spawnAllowlistFile);
        const names = orch.allowlistEntries(allow).map((e) => e.name);
        if (!repo) {
          await reply(eph(names.length ? `spawnable: ${names.join(', ')}\nusage: \`/nexus spawn <repo> [seed]\`` : 'no spawnable repos configured.'));
          return;
        }
        // Validate the name BEFORE the optimistic "spawning…" — otherwise a bad name reads
        // as success: the rocket goes over response_url (visible) while doSpawn's rejection
        // goes to the control channel via chat.postMessage (which the invoker may not be watching).
        const match = orch.matchAllowlist(allow, repo);
        if (!match) {
          const hint = orch.suggestSpawnName(repo, names);
          await reply(eph(`:warning: \`${repo}\` isn't a spawnable repo.${hint ? ` Did you mean \`${hint}\`?` : ''}${names.length ? ` Spawnable: ${names.join(', ')}` : ''}`));
          return;
        }
        await reply(eph(`:rocket: spawning \`${match.name}\`…`));
        // Post the spawn result to the reachable control channel — chat.postMessage to
        // the invoking channel fails channel_not_found when the bot isn't a member.
        await doSpawn(nexusChannel || chan, undefined, match.name, seed, uid);
        return;
      }
      if (sub === 'restore') {
        if (!spawnEnabled) { await reply(eph(':lock: restore disabled.')); return; }
        const repo = args[0];
        if (repo) { await reply(eph(`:leftwards_arrow_with_hook: restoring \`${repo}\`…`)); await doRestore(nexusChannel || chan, undefined, repo, uid); return; }
        const dormant = await ledgerCmd(['list', '--state', 'dormant', '--json']);
        const names = (Array.isArray(dormant) ? dormant : []).map((d) => d.repo || d.name);
        await reply(eph(names.length ? `dormant: ${names.join(', ')}\nusage: \`/nexus restore <repo>\`` : 'no dormant agents.'));
        return;
      }
      await reply(eph(`Unknown \`${sub}\`. Try \`/nexus\` (panel), or: status · agents · peek · clear · stop · keep · msg · spawn · restore`));
    } catch (e) {
      log(`[nexus] slash ${sub} failed: ${e.message}`);
      await reply(eph(`:warning: ${e.message}`));
    }
  }

  /**
   * Panel component activation.
   *
   * `edit`/`post` is the portable form of Slack's `replace_original`:
   *   edit → replace the message the component lives on (Slack `replace_original:true`,
   *          Discord `PATCH /webhooks/…/messages/@original`)
   *   post → add a new message beside it (Slack `replace_original:false`,
   *          Discord follow-up webhook POST)
   * Both providers have both semantics natively; only the wire spelling differs.
   *
   * @param {import('./providers/types.js').Action} action
   * @param {{edit: (m: Object) => Promise<void>, post: (m: Object) => Promise<void>}} out
   */
  async function dispatchNexusAction(action, { edit, post }) {
    if (!action) return;
    const id = action.actionId || '';
    try {
      if (id === 'nx:pick') {
        // `action.value` carries the choice on both providers — Slack's adapter reads it
        // out of `selected_option`, Discord's out of `data.values[0]`.
        const pane = action.value;
        const picked = pane ? { name: paneName(pane), pane } : null;
        await edit(orch.fleetPanel({ agents: panelAgents(), picked, spawnEnabled }));
        return;
      }
      if (id.startsWith('nx:do:')) {
        const act = id.slice('nx:do:'.length);
        const pane = action.value;
        let res;
        if (act === 'fleetstatus') res = await statusText('all');
        else if (act === 'status') res = await statusText(paneName(pane));
        else if (act === 'peek') res = (await doPeek(pane, '40')).text;
        else if (act === 'clear') res = (await doClearCmd(pane)).text;
        else if (act === 'stop') res = (await doStopCmd(pane)).text;
        else if (act === 'keepon') res = (await doKeepCmd(pane, 'on')).text;
        else if (act === 'keepoff') res = (await doKeepCmd(pane, 'off')).text;
        else res = `unknown action ${act}`;
        await post(eph(res));
        return;
      }
    } catch (e) {
      log(`[nexus] panel action ${id} failed: ${e.message}`);
      await post(eph(`:warning: ${e.message}`));
    }
  }

  return { dispatchNexusCommand, dispatchNexusAction };
}
