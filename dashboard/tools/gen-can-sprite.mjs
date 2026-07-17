/**
 * gen-can-sprite.mjs — Generate the AH-noon can pixel character sprite sheet.
 *
 * Outputs a 112×96 PNG (7 frames × 3 directions, 16×32 each) that follows
 * the pixel-dashboard character sprite format:
 *   Row 0 (y  0–31): DOWN  direction — 7 animation frames
 *   Row 1 (y 32–63): UP    direction — 7 animation frames
 *   Row 2 (y 64–95): RIGHT direction — 7 animation frames
 *
 * Usage (from pixel-dashboard/ui/):
 *   node ../tools/gen-can-sprite.mjs
 */

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { PNG } = require('../ui/node_modules/pngjs/lib/png.js');
import { writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dir = dirname(fileURLToPath(import.meta.url));
const OUT   = join(__dir, '../ui/public/assets/characters/char_6.png');

const IMG_W = 112;  // 7 * 16
const IMG_H = 96;   // 3 * 32
const FW    = 16;
const FH    = 32;

// ── Palette ────────────────────────────────────────────────────────────────
// AH-noon can: sky blue top, yellow circle logo, silver-gray bottom.

const T  = [0,   0,   0,   0  ]; // transparent
const R  = [82,  85,  98,  255]; // dark outline
const SL = [158, 162, 172, 255]; // silver rim light
const SD = [108, 111, 122, 255]; // silver rim dark

const B  = [78,  193, 246, 255]; // sky blue (main)
const BH = [138, 220, 255, 255]; // blue highlight (left edge)
const BD = [48,  148, 210, 255]; // blue dark/shadow (right edge)

const Y  = [255, 210, 0,   255]; // yellow (main)
const YH = [255, 238, 110, 255]; // yellow highlight
const YD = [210, 164, 0,   255]; // yellow dark

const G  = [206, 208, 214, 255]; // silver-gray (main)
const GH = [234, 236, 240, 255]; // gray highlight
const GD = [152, 155, 163, 255]; // gray dark

const E  = [30,  32,  42,  255]; // eyes
const SM = [42,  44,  54,  255]; // smile
const LG = [115, 118, 130, 255]; // legs

// ── Canvas helpers ─────────────────────────────────────────────────────────

const img = new PNG({ width: IMG_W, height: IMG_H, filterType: -1 });
img.data.fill(0); // start fully transparent

function setPixel(x, y, [r, g, b, a]) {
  if (x < 0 || x >= IMG_W || y < 0 || y >= IMG_H) return;
  const i = (y * IMG_W + x) * 4;
  img.data[i]     = r;
  img.data[i + 1] = g;
  img.data[i + 2] = b;
  img.data[i + 3] = a;
}

// Returns a setter scoped to a specific frame's top-left corner.
function frameSetter(frameCol, frameRow) {
  const ox = frameCol * FW;
  const oy = frameRow * FH;
  return (x, y, color) => setPixel(ox + x, oy + y, color);
}

// ── Can body: FRONT view (DOWN / UP directions) ────────────────────────────
//
// Body spans cols 2–13 (12px with outline), rows 2–25.
// Yellow oval lives within the blue section at rows 9–15.
//
// Parameters:
//   set      — frame-scoped pixel setter
//   bodyDY   — vertical body offset (–1 = bounce up, +2 = seated)
//   legL     — left-leg X position (null = no legs, e.g. seated)
//   legR     — right-leg X position
//   face     — draw eyes + smile (DOWN only)
//   armStyle — 0 none, 1 typing arms, 2 reading arms

function drawFront(set, bodyDY, legL, legR, face, armStyle) {
  const b = bodyDY;

  // ── top rim ──────────────────────────────────────────
  for (let x = 3; x <= 12; x++) set(x, 2 + b, R);
  set(2, 3 + b, R);
  for (let x = 3; x <= 12; x++) set(x, 3 + b, SL);
  set(13, 3 + b, R);

  // ── blue section (rows 4–17) ─────────────────────────
  for (let r = 4; r <= 17; r++) {
    set(2,  r + b, R);
    set(3,  r + b, BH);
    for (let x = 4; x <= 11; x++) set(x, r + b, B);
    set(12, r + b, BD);
    set(13, r + b, R);
  }

  // ── yellow oval (rows 9–15) ───────────────────────────
  // row 9: narrow top (cols 5–10)
  set(2, 9+b, R); set(3, 9+b, BH); set(4, 9+b, B);
  for (let x = 5; x <= 10; x++) set(x, 9 + b, Y);
  set(11, 9+b, B); set(12, 9+b, BD); set(13, 9+b, R);

  // rows 10–14: full width (cols 4–11)
  for (let r = 10; r <= 14; r++) {
    set(2,  r + b, R); set(3,  r + b, BH);
    for (let x = 4; x <= 11; x++) set(x, r + b, Y);
    set(12, r + b, BD); set(13, r + b, R);
  }
  // highlight top-left of oval
  set(5, 10 + b, YH); set(6, 10 + b, YH);

  // row 15: narrow bottom (cols 5–10)
  set(2, 15+b, R); set(3, 15+b, BH); set(4, 15+b, B);
  for (let x = 5; x <= 10; x++) set(x, 15 + b, Y);
  set(11, 15+b, B); set(12, 15+b, BD); set(13, 15+b, R);

  // ── face (DOWN only) ──────────────────────────────────
  if (face) {
    set(6,  12 + b, E );  // left eye
    set(9,  12 + b, E );  // right eye
    set(6,  13 + b, SM);  // smile left corner
    set(9,  13 + b, SM);  // smile right corner
    set(7,  14 + b, SM);  // smile arc bottom-left
    set(8,  14 + b, SM);  // smile arc bottom-right
  }

  // ── gray section (rows 18–23) ─────────────────────────
  for (let r = 18; r <= 23; r++) {
    set(2,  r + b, R);
    set(3,  r + b, GH);
    for (let x = 4; x <= 11; x++) set(x, r + b, G);
    set(12, r + b, GD);
    set(13, r + b, R);
  }

  // ── bottom rim ────────────────────────────────────────
  set(2, 24 + b, R);
  for (let x = 3; x <= 12; x++) set(x, 24 + b, SL);
  set(13, 24 + b, R);
  for (let x = 3; x <= 12; x++) set(x, 25 + b, R);

  // ── arms ─────────────────────────────────────────────
  if (armStyle === 1) {
    // typing: arms at waist level
    set(1, 16 + b, R); set(1, 17 + b, BH);
    set(14, 16 + b, R); set(14, 17 + b, BD);
  } else if (armStyle === 2) {
    // reading: arms slightly higher
    set(1, 15 + b, R); set(1, 16 + b, BH);
    set(14, 15 + b, R); set(14, 16 + b, BD);
  }

  // ── legs (fixed rows, independent of bodyDY) ──────────
  if (legL !== null) {
    for (let y = 27; y <= 28; y++) {
      set(legL,     y, LG); set(legL + 1, y, LG);
      set(legR,     y, LG); set(legR + 1, y, LG);
    }
  }
}

// ── Can body: PROFILE view (RIGHT direction) ───────────────────────────────
//
// Profile is narrower: cols 4–11 (8px with outline).
// Yellow appears as a full horizontal stripe (no face, no oval rounding).
// Right edge is highlighted (facing viewer); left edge is shadowed (rear).

function drawProfile(set, bodyDY, legX) {
  const b = bodyDY;

  // top rim
  for (let x = 5; x <= 10; x++) set(x, 2 + b, R);
  set(4, 3+b, R);
  for (let x = 5; x <= 10; x++) set(x, 3 + b, SL);
  set(11, 3+b, R);

  // blue section (rows 4–17)
  for (let r = 4; r <= 17; r++) {
    set(4,  r + b, R);
    set(5,  r + b, BD);  // rear = darker
    for (let x = 6; x <= 9; x++) set(x, r + b, B);
    set(10, r + b, BH);  // front edge = lighter
    set(11, r + b, R);
  }

  // yellow stripe (rows 9–15)
  for (let r = 9; r <= 15; r++) {
    set(4,  r + b, R);
    set(5,  r + b, YD);
    for (let x = 6; x <= 9; x++) set(x, r + b, Y);
    set(10, r + b, YH);
    set(11, r + b, R);
  }

  // gray section (rows 18–23)
  for (let r = 18; r <= 23; r++) {
    set(4,  r + b, R);
    set(5,  r + b, GD);
    for (let x = 6; x <= 9; x++) set(x, r + b, G);
    set(10, r + b, GH);
    set(11, r + b, R);
  }

  // bottom rim
  set(4, 24+b, R);
  for (let x = 5; x <= 10; x++) set(x, 24 + b, SL);
  set(11, 24+b, R);
  for (let x = 5; x <= 10; x++) set(x, 25 + b, R);

  // single visible leg in profile
  if (legX !== null) {
    for (let y = 27; y <= 28; y++) {
      set(legX, y, LG); set(legX + 1, y, LG);
    }
  }
}

// ── Frame generation ────────────────────────────────────────────────────────
//
// Frame usage by the UI:
//   walk    → frames 0,1,2,1  (4-frame cycle)
//   typing  → frames 3,4      (2-frame cycle)
//   reading → frames 5,6      (2-frame cycle)

// DOWN direction (row 0)
const downFrames = [
  // [bodyDY, legL, legR, face, armStyle]
  [0,  5, 9, true, 0],   // 0: neutral
  [-1, 4, 10, true, 0],  // 1: walk A — left foot forward, bounce up
  [-1, 6, 8, true, 0],   // 2: walk B — right foot forward, bounce up
  [2, null, null, true, 1], // 3: typing A — seated, arms out
  [2, null, null, true, 1], // 4: typing B — seated, arms slightly lower
  [2, null, null, true, 2], // 5: reading A — seated, arms up
  [2, null, null, true, 2], // 6: reading B — same (subtle)
];

for (let f = 0; f < 7; f++) {
  const [bodyDY, legL, legR, face, arms] = downFrames[f];
  const set = frameSetter(f, 0);
  drawFront(set, bodyDY, legL, legR, face, arms);
}

// UP direction (row 1) — same as DOWN but no face
for (let f = 0; f < 7; f++) {
  const [bodyDY, legL, legR, , arms] = downFrames[f];
  const set = frameSetter(f, 1);
  drawFront(set, bodyDY, legL, legR, false, arms);
}

// RIGHT direction (row 2) — profile view
const rightFrames = [
  // [bodyDY, legX]
  [0,  7],    // 0: neutral
  [-1, 6],    // 1: walk A
  [-1, 8],    // 2: walk B
  [2, null],  // 3: typing
  [2, null],  // 4: typing
  [2, null],  // 5: reading
  [2, null],  // 6: reading
];

for (let f = 0; f < 7; f++) {
  const [bodyDY, legX] = rightFrames[f];
  const set = frameSetter(f, 2);
  drawProfile(set, bodyDY, legX);
}

// ── Write PNG ────────────────────────────────────────────────────────────────

const buf = PNG.sync.write(img);
writeFileSync(OUT, buf);
console.log(`Written: ${OUT} (${IMG_W}×${IMG_H})`);
