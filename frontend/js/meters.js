/**
 * PPM bar rendering helpers.
 * dBFS range: -60 to 0, with colour zones.
 */

const DB_MIN = -60;
const DB_MAX = 0;

// Zone thresholds (dBFS)
const ZONE_GREEN_TOP  = -18;
const ZONE_YELLOW_TOP =  -9;
const ZONE_ORANGE_TOP =  -3;

export function dbToPercent(db) {
  const clamped = Math.max(DB_MIN, Math.min(DB_MAX, db));
  return ((clamped - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
}

export function dbToColour(db) {
  if (db >= ZONE_ORANGE_TOP) return 'var(--red)';
  if (db >= ZONE_YELLOW_TOP) return 'var(--orange)';
  if (db >= ZONE_GREEN_TOP)  return 'var(--yellow)';
  return 'var(--green)';
}

/**
 * Update a PPM bar element in-place (no DOM recreation).
 * @param {HTMLElement} bar   - .ppm-bar element
 * @param {HTMLElement} hold  - .ppm-hold element
 * @param {number} peakDb
 * @param {number} holdDb
 */
export function updatePpmBar(bar, hold, peakDb, holdDb) {
  const pct     = dbToPercent(peakDb);
  const holdPct = dbToPercent(holdDb);

  bar.style.width = pct + '%';
  bar.style.background = buildGradient(peakDb);

  hold.style.left = holdPct + '%';
  hold.style.display = holdDb > DB_MIN + 1 ? 'block' : 'none';
}

function buildGradient(db) {
  // Single solid colour avoids repaints of complex gradients
  return dbToColour(db);
}

/**
 * Draw the dB scale ruler onto a canvas element.
 */
export function drawRuler(canvas) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const ticks = [-60, -48, -36, -24, -18, -12, -9, -6, -3, 0];

  ctx.fillStyle = '#555';
  ctx.font = '7px monospace';
  ctx.textAlign = 'center';

  for (const db of ticks) {
    const x = (dbToPercent(db) / 100) * w;
    ctx.fillRect(x, 0, 1, 4);
    if (db % 6 === 0 || db === -18 || db === -9 || db === -3) {
      ctx.fillText(db === 0 ? '0' : db, x, h);
    }
  }
}

/**
 * Render the phase correlation bar.
 * value: -1 (anti-phase) to +1 (in-phase)
 */
export function updatePhaseBar(fill, value) {
  // Map -1..+1 to 0%..100%, centre at 50%
  const centre = 50;
  const halfWidth = Math.abs(value) * 50;
  const left  = value >= 0 ? centre : centre + value * 50;
  fill.style.left  = left + '%';
  fill.style.width = halfWidth + '%';
  fill.style.background = value < 0 ? 'var(--red)'
                         : value < 0.3 ? 'var(--yellow)'
                         : 'var(--green)';
}

export function formatDb(db) {
  if (db <= -99) return '-∞';
  return (db >= 0 ? '+' : '') + db.toFixed(1);
}
