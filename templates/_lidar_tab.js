// ── LiDAR tab · obstacle-safety zones ────────────────────────────
// The sliders are built from /api/lidar's `limits`, which the server
// also clamps against — the range shown here is a hint, not the
// authority. Sector 0 is straight ahead and the plot runs clockwise.
const LIDAR_KEYS = ['lidar_stop_dist_m', 'lidar_slow_dist_m',
                    'lidar_slow_v_mps', 'lidar_stale_ms', 'lidar_sector_deg'];
let lidarLimits = null, lidarDirty = false, lidarSetAt = 0;

function fmtVal(key, v) {
  const lim = lidarLimits && lidarLimits[key];
  const unit = lim ? lim.unit : '';
  if (key === 'lidar_sector_deg') return `${Math.round(v)}${unit}`;
  if (key === 'lidar_stale_ms') return `${Math.round(v)} ${unit}`;
  return `${Number(v).toFixed(2)} ${unit}`;
}

function buildLidarSliders(cfg) {
  if (!cfg || !cfg.limits) return;
  lidarLimits = cfg.limits;
  const host = $('lidar-sliders');
  host.innerHTML = '';
  for (const key of LIDAR_KEYS) {
    const lim = cfg.limits[key];
    if (!lim) continue;
    const row = document.createElement('div');
    row.className = 'lidar-row';
    row.innerHTML =
      `<label for="lidar-in-${key}">${lim.label}</label>` +
      `<input id="lidar-in-${key}" type="range" min="${lim.min}" max="${lim.max}" ` +
      `step="${lim.step}" value="${cfg[key]}" aria-label="${lim.label}">` +
      `<span class="val" id="lidar-val-${key}"></span>`;
    host.appendChild(row);
    const input = row.querySelector('input');
    const val = row.querySelector('.val');
    val.textContent = fmtVal(key, cfg[key]);
    input.addEventListener('input', () => {
      val.textContent = fmtVal(key, input.value);
      lidarDirty = true;
      $('lidar-apply-msg').textContent = 'unsaved changes';
    });
  }
}

async function loadLidarConfig() {
  try {
    const r = await fetch('/api/lidar');
    if (!r.ok) return;
    buildLidarSliders(await r.json());
  } catch (e) { /* the tab just stays at defaults */ }
}

$('lidar-apply').addEventListener('click', async () => {
  if (!lidarLimits) return;
  const payload = {};
  for (const key of LIDAR_KEYS) {
    const el = $('lidar-in-' + key);
    if (el) payload[key] = Number(el.value);
  }
  $('lidar-apply-msg').textContent = 'saving…';
  const r = await authFetch('/api/lidar', payload);
  if (!r.ok) { $('lidar-apply-msg').textContent = 'not saved'; toast('LiDAR zones not changed', 'err'); return; }
  const saved = await r.json();
  buildLidarSliders(saved);            // echoes back the clamped values
  lidarDirty = false;
  $('lidar-apply-msg').textContent = 'saved';
});

const lidarBox = $('lidar-toggle');
lidarBox.addEventListener('change', async () => {
  const want = lidarBox.checked;
  lidarSetAt = Date.now();
  if (!want && !(await requireAuth('Sign in to turn LiDAR obstacle safety OFF.'))) { lidarBox.checked = true; return; }
  const r = await authFetch('/api/lidar', { enabled: want });
  if (!r.ok) { lidarBox.checked = true; toast('LiDAR setting not changed', 'err'); }
});

function updateLidarLive(data) {
  // A status frame written just before the operator's own change still carries
  // the old value, and applying it snaps the tick box back off for a frame or
  // two while the server is already on. obstBox has had a guard for this
  // (obstSetAt); for two seconds the operator's choice wins. Everything else
  // in here — the readouts, the action colour, the tab dot — still follows the
  // telemetry, because it uses `enabled` below.
  const settled = Date.now() - lidarSetAt >= 2000;
  const enabled = settled ? (data.lidar_enabled === true) : lidarBox.checked;
  lidarBox.checked = enabled;
  // Read-only mirror of the arm switch, which lives on the Drive tab now.
  // This tab must never look like it is armed when the sensor is not.
  const here = $('lidar-armed-here');
  if (here) here.checked = enabled;
  $('lidar-label').textContent = enabled
    ? 'LiDAR obstacle safety armed' : 'LiDAR obstacle safety OFF (no obstacle sensing)';
  $('lidar-label').style.color = enabled ? '' : '#f87171';

  const act = data.lidar_action || (enabled ? 'CLEAR' : 'OFF');
  const chip = $('lidar-action-chip');
  chip.textContent = enabled ? act : 'OFF';
  chip.className = 'pill ' + (enabled ? act : 'OFF');
  $('lidar-act-now').textContent = enabled ? act : 'OFF';
  $('lidar-act-now').style.color = enabled && act === 'ESTOP' ? '#f87171'
    : (enabled && act === 'SLOW' ? '#fbbf24' : (enabled ? '#4ade80' : '#9ca3af'));

  const front = data.lidar_min_front_m;
  $('lidar-min-front').textContent = (front === null || front === undefined)
    ? '—' : front.toFixed(2) + ' m';
  const age = data.lidar_age_ms;
  $('lidar-age').textContent = (age === null || age === undefined || age > 90000)
    ? '—' : age + ' ms';
  if (data.lidar_estop_reason) {
    $('lidar-age').title = data.lidar_estop_reason;
  } else {
    $('lidar-age').title = '';
  }
  $('lidar-zone-w').textContent = enabled && data.lidar_sector_deg
    ? data.lidar_sector_deg + '° × ' + (data.lidar_sector_mins || []).length : '—';
  $('dot-lidar').className = 'tabdot'
    + (enabled && act === 'ESTOP' ? ' on' : (enabled && data.lidar_estop_reason ? ' warn' : ''));

  updateLidarStatusBar(data);
}
function updateLidarStatusBar(data) {
  // Sensor state: prefer the fresh status.json view, fall back to
  // the legacy "lidar_connected" boolean.
  const conn = data.lidar_connected;
  const fresh = data.updated_at ? (Date.now() / 1000 - data.updated_at) < 3 : false;
  let dotClass = 'dot';
  let sensorTxt = '—';
  if (conn === true && fresh)   { dotClass = 'dot live';  sensorTxt = 'LIVE'; }
  else if (conn === true)        { dotClass = 'dot stale'; sensorTxt = 'STALE'; }
  else if (conn === false)       { dotClass = 'dot dead';  sensorTxt = 'DISCONNECTED'; }
  else                           { dotClass = 'dot';       sensorTxt = 'UNKNOWN'; }
  $('lstat-dot').className = dotClass;
  $('lstat-sensor').textContent = sensorTxt;

  $('lstat-port').textContent    = data.lidar_port || '—';
  $('lstat-baud').textContent    = data.lidar_baud ? data.lidar_baud + ' baud' : '—';
  const n = (data.lidar_sector_mins || []).length;
  const w = data.lidar_sector_deg;
  $('lstat-sectors').textContent = (n && w) ? (n + ' × ' + w + '°') : '—';
  // Hand the sector count to the polar plot below so its spokes track
  // the real sector layout instead of a hard-coded 12.
  window.__lidarSectors = n || 12;

  // Read everything else off `data` directly — these are NOT closures of
  // updateLidarLive(); that function's locals don't exist here.
  const enabled2 = data.lidar_enabled === true;
  const front2 = data.lidar_min_front_m;
  $('lstat-front').textContent   = (front2 === null || front2 === undefined)
                                    ? '—' : front2.toFixed(2) + ' m';
  const act2 = data.lidar_action || (enabled2 ? 'CLEAR' : 'OFF');
  $('lstat-action').textContent  = enabled2 ? act2 : 'OFF';
  $('lstat-action').style.color  = enabled2 && act2 === 'ESTOP' ? '#f87171'
                                 : (enabled2 && act2 === 'SLOW' ? '#fbbf24'
                                   : (enabled2 ? '#4ade80' : '#9ca3af'));
  const age2 = data.lidar_age_ms;
  $('lstat-age').textContent     = (age2 === null || age2 === undefined || age2 > 90000)
                                    ? '—' : age2 + ' ms';
  $('lstat-reason').textContent  = data.lidar_estop_reason || '—';
  // point count is filled in by the points poller (it lives outside)
  $('lstat-pts').textContent = window.__lidarPts || '—';
}

// ── LiDAR raw-points polar overlay ────────────────────────────────
// Polls /points.json (written by lidar.PointsPublisher in the
// orchestrator) and draws individual scan returns on a 2-second fading
// canvas. This is the only scan view on the LiDAR tab.
(function setupLidarPolar() {
  const canvas = $('lidar-points');
  if (!canvas || !canvas.getContext) return;
  let   polarMaxM = 9;              // 3–12 m, user-adjustable ring radius
  const ctx = canvas.getContext('2d');
  // Pick the canvas backing-store size each frame so it stays
  // sharp on devices of any pixel ratio AND tracks CSS resizes
  // (the CSS rule gives it `width:100%; aspect-ratio:1380/520`).
  let cssW = 0, cssH = 0, dpr = 1;
  function resize() {
    dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) return false;
    cssW = Math.round(rect.width);
    cssH = Math.round(rect.height);
    // Backing store = cssW × cssH × dpr; drawing happens in CSS
    // pixels via setTransform(dpr,0,0,dpr,…). This is what keeps
    // the rings crisp instead of pixelated.
    canvas.width  = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }
  // Resize whenever the canvas's CSS box changes (including the
  // initial layout pass where cssW starts at 0 because the
  // aspect-ratio rule hasn't applied yet).
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => { resize(); });
    ro.observe(canvas);
  } else {
    window.addEventListener('resize', () => { resize(); });
  }
  // Make sure the first frame after load has the right size even
  // if the observer hasn't fired yet.
  for (let i = 0; i < 8 && cssW < 4; i++) {
    requestAnimationFrame(resize);
  }
  const decayEl = $('lidar-decay');
  const decayV  = $('lidar-decay-v');
  let decayS = parseFloat(decayEl.value);
  decayEl.addEventListener('input', () => {
    decayS = parseFloat(decayEl.value);
    decayV.textContent = decayS.toFixed(1) + ' s';
  });
  // Range slider drives the polar plot's outermost ring (3 – 12 m).
  // The user can grow the canvas to see far walls or shrink it to
  // focus on the area around the rover.
  const rangeEl = $('lidar-range');
  const rangeV  = $('lidar-range-v');
  polarMaxM = parseInt(rangeEl.value, 10);
  rangeV.textContent = polarMaxM + ' m';
  rangeEl.addEventListener('input', () => {
    polarMaxM = parseInt(rangeEl.value, 10);
    rangeV.textContent = polarMaxM + ' m';
  });
  let points = [];
  let lastStop = 0.5, lastSlow = 1.0;

  function frame() {
    ctx.fillStyle = '#0b0f15';
    ctx.fillRect(0, 0, cssW, cssH);
    const cx = cssW / 2, cy = cssH / 2;
    const R = Math.min(cx, cy) - 30;
    if (R < 30) { requestAnimationFrame(frame); return; }
    // One spoke per sensor sector; updateLidarStatusBar keeps this in
    // step with /status so a 45° layout draws 8 spokes, not 12.
    const polarN = window.__lidarSectors || 12;
    // rings
    ctx.strokeStyle = '#233152'; ctx.lineWidth = 1;
    for (let m = 1; m <= polarMaxM; m++) {
      const r = (m / polarMaxM) * R;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 2 * Math.PI); ctx.stroke();
    }
    // spokes
    for (let i = 0; i < polarN; i++) {
      const a = -Math.PI / 2 + (i / polarN) * 2 * Math.PI;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + R * Math.cos(a), cy + R * Math.sin(a));
      ctx.stroke();
    }
    // degree + metre labels
    ctx.fillStyle = '#94a3b8'; ctx.font = '11px system-ui';
    for (const [a, lbl] of [[0,'0°'],[Math.PI/2,'90°'],[Math.PI,'180°'],[3*Math.PI/2,'270°']]) {
      ctx.fillText(lbl, cx + (R + 8) * Math.cos(a - Math.PI/2) - 8,
                   cy + (R + 8) * Math.sin(a - Math.PI/2) + 4);
    }
    for (let m = 1; m <= polarMaxM; m++) {
      ctx.fillText(m + 'm', cx + 4, cy - (m / polarMaxM) * R + 10);
    }
    // shaded stop/slow zones
    ctx.fillStyle = 'rgba(239,68,68,0.10)';
    ctx.beginPath(); ctx.arc(cx, cy, (lastStop / polarMaxM) * R, 0, 2 * Math.PI); ctx.fill();
    ctx.fillStyle = 'rgba(245,158,11,0.07)';
    ctx.beginPath(); ctx.arc(cx, cy, (lastSlow / polarMaxM) * R, 0, 2 * Math.PI); ctx.fill();
    // raw points
    if (points.length) {
      const now = performance.now() / 1000;
      for (const p of points) {
        const age = now - p.ts;
        if (age > decayS) continue;
        const a = ((p.a % 360) * Math.PI) / 180;
        const r = (Math.min(polarMaxM, p.d) / polarMaxM) * R;
        const x = cx + r * Math.cos(a - Math.PI / 2);
        const y = cy + r * Math.sin(a - Math.PI / 2);
        const alpha = Math.max(0, 1 - age / decayS);
        ctx.fillStyle = p.zone === 'stop' ? '#ef4444'
                      : p.zone === 'slow' ? '#f59e0b'
                      : '#22c55e';
        ctx.globalAlpha = alpha * 0.85;
        ctx.beginPath();
        ctx.arc(x, y, 1.5, 0, 2 * Math.PI);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  async function tick() {
    // Four requests a second, for the whole session, for a scan nobody is
    // looking at. Skipped while the tab is hidden; opening the tab costs at
    // most one 250 ms gap before the first frame lands.
    const panel = $('tab-lidar');
    if (panel && panel.hidden) return;
    try {
      const r = await fetch('/points.json?t=' + performance.now(),
                            { cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      if (typeof d.lidar_stop_dist_m === 'number') lastStop = d.lidar_stop_dist_m;
      if (typeof d.lidar_slow_dist_m === 'number') lastSlow = d.lidar_slow_dist_m;
      const now = performance.now() / 1000;
      // Map server-side t values (seconds, relative to first point
      // in the window) onto performance.now() seconds. The window
      // is recent enough (<2s) that anchoring to "now" loses at
      // most one frame of clock drift.
      const lastT = d.points && d.points.length ? d.points[d.points.length - 1].t : 0;
      const anchor = now - lastT;
      points = (d.points || []).map((p) => ({
        a: p.a, d: p.d, zone: p.zone,
        ts: anchor + p.t,
      }));
      // Surface the rolling point count in the new status bar.
      window.__lidarPts = String(d.n || 0);
      const ptsEl = document.getElementById('lstat-pts');
      if (ptsEl) ptsEl.textContent = window.__lidarPts;
    } catch (e) { /* ignore */ }
  }
  setInterval(tick, 250);
  tick();
})();