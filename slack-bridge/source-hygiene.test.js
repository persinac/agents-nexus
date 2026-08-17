// Source hygiene: no raw control bytes in tracked JS sources.
//
// WHY THIS EXISTS. index.js once carried a single raw NUL byte — a deliberate
// delimiter inside a template literal:
//
//     const idKey = (a) => `${ws}<NUL>${name}`;
//
// Semantically fine. Operationally corrosive: GNU grep sniffs for a NUL, decides
// the file is BINARY, and prints "Binary file index.js matches" — or, piped or with
// -q/-c, prints NOTHING and exits 1. That is byte-identical to "no matches." Every
// agent that greps this file to check whether a flag or endpoint exists gets a
// confident, wrong "it isn't there," and reasons forward from it. It cost this repo
// three wrong conclusions in one session (a claimed deployed-vs-tree drift, a claimed
// dead SLACK_PRESENCE_FQDN flag, and a claimed non-live FQDN presence) before anyone
// suspected the tool rather than the code.
//
// The fix is to spell the byte as an escape (a six-character `\u0000`), which is identical at runtime
// and invisible to grep's binary heuristic. This test keeps it that way.
//
// Prefer the CONSTRUCTED form — String.fromCharCode(0) — over a typed escape. An escape
// written by an LLM has a habit of arriving at the filesystem as the raw byte it denotes;
// that is exactly how tmux-scripts/nx-kv.mjs acquired one twice on the day this was written.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');

// The bridge is where it bit us, but the trap is not bridge-specific: the fleet scripts
// are read by agents the same way and mislead them the same way. nx-kv.mjs reintroduced
// a raw NUL on the day this test was written, in the very line that splits /proc environ
// — and it lived outside this directory, so the first version of this test missed it.
const ROOTS = [__dirname, resolve(REPO_ROOT, 'tmux/mac/tmux-scripts')].filter(existsSync);

const SKIP_DIRS = new Set(['node_modules', '.git', 'coverage', 'dist', 'build']);

function jsFiles(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) jsFiles(p, out);
    else if (entry.endsWith('.js') || entry.endsWith('.mjs')) out.push(p);
  }
  return out;
}

// The bytes that make grep call a text file "binary". NUL is the one that actually
// bit us; the other C0 controls are in the same class and have no business in source.
// TAB (0x09), LF (0x0a), CR (0x0d) are legitimate whitespace and are allowed.
const FORBIDDEN = new Set([...Array(32).keys()].filter((c) => c !== 9 && c !== 10 && c !== 13));

test('no raw control bytes in JS sources (they make grep treat the file as binary)', () => {
  const offenders = [];
  const files = ROOTS.flatMap((root) => jsFiles(root));
  for (const file of files) {
    const buf = readFileSync(file);
    for (let i = 0; i < buf.length; i++) {
      if (!FORBIDDEN.has(buf[i])) continue;
      const line = buf.subarray(0, i).toString('utf8').split('\n').length;
      offenders.push(
        `${relative(REPO_ROOT, file)}:${line} — raw 0x${buf[i].toString(16).padStart(2, '0')} ` +
        `at byte ${i}; build it instead (String.fromCharCode(0)) or write an escape`,
      );
      break;   // one report per file is enough to act on
    }
  }
  assert.deepEqual(offenders, [], `\n${offenders.join('\n')}\n`);
  assert.ok(files.length > 0, 'found no JS sources to scan — the roots are wrong');
});
