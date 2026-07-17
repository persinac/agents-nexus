/**
 * Development launcher — starts both the bridge server and the Vite UI dev server.
 */

import { spawn } from 'child_process';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

console.log('Starting Pixel Dashboard...\n');

// Start bridge server
const server = spawn('node', ['index.js'], {
  cwd: join(root, 'server'),
  stdio: 'inherit',
  shell: true,
});

// Start Vite dev server
const ui = spawn('npm', ['run', 'dev'], {
  cwd: join(root, 'ui'),
  stdio: 'inherit',
  shell: true,
});

process.on('SIGINT', () => {
  server.kill();
  ui.kill();
  process.exit(0);
});

server.on('exit', (code) => {
  console.log(`Bridge server exited (${code})`);
  ui.kill();
  process.exit(code || 0);
});

ui.on('exit', (code) => {
  console.log(`UI server exited (${code})`);
  server.kill();
  process.exit(code || 0);
});
