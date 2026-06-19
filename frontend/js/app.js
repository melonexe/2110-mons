import { updatePpmBar, updatePhaseBar, formatDb } from './meters.js';

const WS_URL   = `ws://${location.host}/ws/meters`;
const WS_AUDIO = `ws://${location.host}/ws/audio`;
const API       = `${location.protocol}//${location.host}/api`;

// ── State ─────────────────────────────────────────────────────────

let socket       = null;
let channelEls   = [];
let numChannels  = 0;
let loudnessCh   = 0;  // 0-indexed

// Audio player
let audioCtx     = null;
let workletNode  = null;
let gainNode     = null;
let audioSocket  = null;
let listening    = false;

// ── Boot ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadDevices();
  bindControls();
  connectWs();
  bindSdpModal();
  bindListenControls();
});

// ── Device enumeration ────────────────────────────────────────────

async function loadDevices() {
  try {
    const r = await fetch(`${API}/devices`);
    const { devices } = await r.json();
    const sel = document.getElementById('device-select');
    sel.innerHTML = '<option value="default">default</option>';
    for (const d of devices) {
      const opt = document.createElement('option');
      opt.value       = d.device_string;
      opt.textContent = `${d.card_name} — ${d.device_string}`;
      sel.appendChild(opt);
    }
  } catch {
    setStatus('Cannot reach backend — is it running?', 'error');
  }
}

// ── Monitor controls ──────────────────────────────────────────────

function bindControls() {
  document.getElementById('btn-start').addEventListener('click', startMonitor);
  document.getElementById('btn-stop').addEventListener('click', stopMonitor);
  document.getElementById('btn-reset-clip').addEventListener('click', () => {
    fetch(`${API}/reset-clip`, { method: 'POST' });
    channelEls.forEach(({ clipDot }) => clipDot.classList.remove('clipped'));
  });
  document.getElementById('btn-reset-int').addEventListener('click', () => {
    fetch(`${API}/reset-integrated`, { method: 'POST' });
  });
  document.getElementById('loudness-ch').addEventListener('change', e => {
    loudnessCh = Math.max(0, parseInt(e.target.value, 10) - 1);
  });
}

async function startMonitor(device, numCh, sampleRate) {
  device      = device     || document.getElementById('device-select').value;
  numCh       = numCh      || parseInt(document.getElementById('ch-count').value, 10) || 2;
  sampleRate  = sampleRate || parseInt(document.getElementById('sample-rate').value, 10) || 48000;

  await fetch(`${API}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device, num_channels: numCh, sample_rate: sampleRate }),
  });

  buildChannelStrips(numCh);
  rebuildListenPairs(numCh);
  document.getElementById('btn-start').classList.add('active');
  document.getElementById('btn-stop').classList.remove('active');
}

async function stopMonitor() {
  await fetch(`${API}/stop`, { method: 'POST' });
  document.getElementById('btn-start').classList.remove('active');
  document.getElementById('btn-stop').classList.add('active');
}

// ── Channel strips ────────────────────────────────────────────────

function buildChannelStrips(n) {
  const panel = document.getElementById('meter-panel');
  panel.querySelectorAll('.channel-strip, .phase-row').forEach(el => el.remove());
  channelEls  = [];
  numChannels = n;

  for (let i = 0; i < n; i++) {
    const strip = document.createElement('div');
    strip.className = 'channel-strip';
    strip.innerHTML = `
      <div class="ch-label">CH${i + 1}</div>
      <div class="ppm-track">
        <div class="ppm-bar"></div>
        <div class="ppm-hold"></div>
      </div>
      <div class="readouts">
        <div class="readout-row">
          <span class="readout-label">PK</span>
          <span class="readout-value pk-val">-∞</span>
        </div>
        <div class="readout-row">
          <span class="readout-label">RMS</span>
          <span class="readout-value rms-val">-∞</span>
        </div>
      </div>
      <div class="clip-dot" title="Clip (click to reset)"></div>
    `;
    panel.appendChild(strip);

    const clipDot = strip.querySelector('.clip-dot');
    clipDot.addEventListener('click', () => {
      fetch(`${API}/reset-clip`, { method: 'POST' });
      channelEls.forEach(e => e.clipDot.classList.remove('clipped'));
    });

    channelEls.push({
      bar:    strip.querySelector('.ppm-bar'),
      hold:   strip.querySelector('.ppm-hold'),
      pkVal:  strip.querySelector('.pk-val'),
      rmsVal: strip.querySelector('.rms-val'),
      clipDot,
    });

    if (i % 2 === 1) {
      const phRow = document.createElement('div');
      phRow.className = 'phase-row channel-strip';
      phRow.innerHTML = `
        <div class="ch-label" style="font-size:9px;color:#555">ϕ</div>
        <div class="phase-bar-track">
          <div class="phase-bar-fill" style="left:50%;width:0%"></div>
        </div>
        <div class="readouts">
          <div class="readout-row">
            <span class="readout-label">CORR</span>
            <span class="phase-val readout-value">0.00</span>
          </div>
        </div>
        <div></div>
      `;
      panel.appendChild(phRow);
    }
  }

  document.getElementById('loudness-ch').max = n;
}

// ── Meter WebSocket ───────────────────────────────────────────────

function connectWs() {
  socket = new WebSocket(WS_URL);

  socket.addEventListener('open', () => setStatus('Connected', 'connected'));

  socket.addEventListener('message', e => {
    const data = JSON.parse(e.data);
    if (data.error) { setStatus(`Error: ${data.error}`, 'error'); return; }
    renderMeters(data);
    renderStreams(data.streams || []);
  });

  socket.addEventListener('close', () => {
    setStatus('Disconnected — retrying…', 'error');
    setTimeout(connectWs, 2000);
  });

  socket.addEventListener('error', () => setStatus('WebSocket error', 'error'));
}

function renderMeters(data) {
  const channels = data.channels || [];
  const phases   = data.phase_pairs || [];

  channels.forEach((ch, i) => {
    if (i >= channelEls.length) return;
    const el = channelEls[i];
    updatePpmBar(el.bar, el.hold, ch.peak_db, ch.peak_hold_db);
    el.pkVal.textContent  = formatDb(ch.peak_db);
    el.rmsVal.textContent = formatDb(ch.rms_db);
    el.pkVal.className    = 'readout-value pk-val ' + dbClass(ch.peak_db);
    if (ch.clip) el.clipDot.classList.add('clipped');
  });

  const phaseRows = document.querySelectorAll('.phase-row');
  phases.forEach((pair, idx) => {
    if (idx >= phaseRows.length) return;
    const fill = phaseRows[idx].querySelector('.phase-bar-fill');
    const val  = phaseRows[idx].querySelector('.phase-val');
    updatePhaseBar(fill, pair.value);
    val.textContent = pair.value.toFixed(2);
  });

  const ch = channels[loudnessCh];
  if (ch) renderLoudness(ch);
}

function renderLoudness(ch) {
  setLoudnessCard('lk-m',  ch.loudness_m,  'LUFS M');
  setLoudnessCard('lk-st', ch.loudness_st, 'LUFS ST');
  setLoudnessCard('lk-i',  ch.loudness_i,  'LUFS I');
}

function setLoudnessCard(id, value, unit) {
  const el = document.getElementById(id);
  if (!el) return;
  const valEl = el.querySelector('.loudness-value');
  valEl.textContent = value <= -99 ? '-∞' : value.toFixed(1);
  valEl.className   = 'loudness-value ' + loudnessClass(value);
  const unitEl = el.querySelector('.loudness-unit');
  if (unitEl) unitEl.textContent = unit;
}

function renderStreams(streams) {
  const section = document.getElementById('streams-section');
  section.innerHTML = '';

  if (!streams.length) {
    section.innerHTML = '<p class="no-streams">Listening for SAP announcements…</p>';
    return;
  }

  for (const s of streams) {
    const entry = document.createElement('div');
    entry.className = 'stream-entry';
    entry.innerHTML = `
      <div class="stream-name" title="${esc(s.session_name)}">${esc(s.session_name)}</div>
      <div class="stream-meta">${s.encoding||'?'} · ${s.sample_rate||'?'} Hz · ${s.channels||'?'} ch · ${s.multicast_addr||s.source_ip}</div>
    `;
    entry.addEventListener('click', () => {
      if (s.channels)     document.getElementById('ch-count').value    = s.channels;
      if (s.sample_rate)  document.getElementById('sample-rate').value = s.sample_rate;
    });
    section.appendChild(entry);
  }
}

// ── Listen / audio player ─────────────────────────────────────────

function bindListenControls() {
  document.getElementById('btn-listen').addEventListener('click', toggleListen);
  document.getElementById('listen-pair').addEventListener('change', onPairChange);
  document.getElementById('listen-vol').addEventListener('input', onVolumeChange);
}

function rebuildListenPairs(n) {
  const sel = document.getElementById('listen-pair');
  sel.innerHTML = '';
  for (let i = 0; i + 1 < n; i += 2) {
    const opt = document.createElement('option');
    opt.value       = `${i},${i+1}`;
    opt.textContent = `CH ${i+1}+${i+2}`;
    sel.appendChild(opt);
  }
}

async function toggleListen() {
  if (listening) {
    stopListen();
  } else {
    await startListen();
  }
}

async function startListen() {
  // AudioContext must be created on user gesture
  if (!audioCtx) {
    audioCtx = new AudioContext({ sampleRate: 48000 });
    await audioCtx.audioWorklet.addModule('/js/audio-worklet.js');
  }
  if (audioCtx.state === 'suspended') await audioCtx.resume();

  gainNode    = audioCtx.createGain();
  gainNode.gain.value = parseFloat(document.getElementById('listen-vol').value);
  gainNode.connect(audioCtx.destination);

  workletNode = new AudioWorkletNode(audioCtx, 'aes67-audio-receiver');
  workletNode.connect(gainNode);

  workletNode.port.onmessage = ({ data }) => {
    if (data.type === 'level') updateBufIndicator(data.available);
  };

  audioSocket = new WebSocket(WS_AUDIO);
  audioSocket.binaryType = 'arraybuffer';

  audioSocket.onmessage = ({ data }) => {
    workletNode.port.postMessage(data, [data]);
  };

  audioSocket.onopen = () => {
    // Tell server which channel pair to send
    const pair = document.getElementById('listen-pair').value.split(',').map(Number);
    fetch(`${API}/audio/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ left: pair[0], right: pair[1] }),
    });
  };

  audioSocket.onclose = () => { if (listening) stopListen(); };

  listening = true;
  const btn = document.getElementById('btn-listen');
  btn.textContent = '⏹ Stop';
  btn.classList.add('listening');
  document.getElementById('listen-state').textContent = 'Buffering…';
}

function stopListen() {
  audioSocket?.close();
  audioSocket = null;
  workletNode?.disconnect();
  workletNode = null;
  gainNode?.disconnect();
  gainNode = null;
  listening = false;

  const btn = document.getElementById('btn-listen');
  btn.textContent = '▶ Listen';
  btn.classList.remove('listening');
  document.getElementById('listen-state').textContent = 'Stopped';
  updateBufIndicator(0);
}

function onPairChange() {
  if (!listening) return;
  const pair = document.getElementById('listen-pair').value.split(',').map(Number);
  fetch(`${API}/audio/pair`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ left: pair[0], right: pair[1] }),
  });
}

function onVolumeChange(e) {
  if (gainNode) gainNode.gain.value = parseFloat(e.target.value);
}

function updateBufIndicator(available) {
  const fill  = document.getElementById('listen-buf-fill');
  const label = document.getElementById('listen-state');
  const pct   = Math.min(100, (available / 4096) * 100);
  fill.style.width = pct + '%';
  if (available === 0) {
    label.textContent = listening ? 'Buffering…' : 'Stopped';
  } else if (available < 4096) {
    label.textContent = 'Buffering…';
  } else {
    label.textContent = 'Playing';
  }
}

// ── SDP modal ─────────────────────────────────────────────────────

function bindSdpModal() {
  const modal      = document.getElementById('sdp-modal');
  const btnOpen    = document.getElementById('btn-sdp');
  const btnClose   = document.getElementById('btn-sdp-close');
  const btnCancel  = document.getElementById('btn-sdp-cancel');
  const btnParse   = document.getElementById('btn-sdp-parse');
  const btnSub     = document.getElementById('btn-sdp-subscribe');

  btnOpen.addEventListener('click',   () => { modal.style.display = 'flex'; clearSdpModal(); });
  btnClose.addEventListener('click',  () => { modal.style.display = 'none'; });
  btnCancel.addEventListener('click', () => { modal.style.display = 'none'; });

  btnParse.addEventListener('click', parseSdp);
  btnSub.addEventListener('click',   subscribeSdp);

  // Close on backdrop click
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
}

function clearSdpModal() {
  document.getElementById('sdp-info').style.display   = 'none';
  document.getElementById('sdp-result').style.display = 'none';
  document.getElementById('btn-sdp-subscribe').disabled = true;
}

async function parseSdp() {
  const text = document.getElementById('sdp-input').value.trim();
  if (!text) return;

  const r    = await fetch(`${API}/sdp/parse`, { method: 'POST', body: text });
  const data = await r.json();

  const info = document.getElementById('sdp-info');
  info.style.display = 'grid';
  info.innerHTML = '';

  if (data.error) {
    info.innerHTML = `<div style="color:var(--red);grid-column:1/-1">${esc(data.error)}</div>`;
    return;
  }

  const p = data.primary || {};
  const rows = [
    ['Session',    data.session_name + (data.is_redundant ? '<span class="sdp-info-badge">DUP</span>' : '')],
    ['Origin IP',  data.origin_addr],
    ['Encoding',   p.encoding || '—'],
    ['Sample rate',p.sample_rate ? p.sample_rate + ' Hz' : '—'],
    ['Channels',   p.channels || '—'],
    ['Multicast',  p.multicast_addr || '—'],
    ['Source (SSM)', p.source_addr || '—'],
    ['Port (pri)', p.port || '—'],
    ['Pkt time',   p.ptime_ms != null ? p.ptime_ms + ' ms' : '—'],
    ['PTP domain', p.ptp_domain != null ? p.ptp_domain : '—'],
    ['GM clock',   p.ptp_gmid || '—'],
  ];

  rows.forEach(([k, v]) => {
    info.insertAdjacentHTML('beforeend',
      `<div class="sdp-info-row">
        <span class="sdp-info-key">${k}</span>
        <span class="sdp-info-val">${v}</span>
      </div>`);
  });

  document.getElementById('btn-sdp-subscribe').disabled = false;
}

async function subscribeSdp() {
  const text    = document.getElementById('sdp-input').value.trim();
  const result  = document.getElementById('sdp-result');
  const btnSub  = document.getElementById('btn-sdp-subscribe');

  btnSub.disabled   = true;
  result.className  = 'sdp-result warn';
  result.textContent = 'Contacting Merging RAVENNA daemon…';
  result.style.display = 'block';

  try {
    const r    = await fetch(`${API}/sdp/subscribe`, { method: 'POST', body: text });
    const data = await r.json();

    if (data.status === 'subscribed') {
      result.className  = 'sdp-result success';
      result.textContent = data.message;

      // Auto-populate device selector and start monitoring
      await loadDevices();
      const parsed = data.parsed?.primary;
      if (parsed) {
        document.getElementById('ch-count').value = parsed.channels || 8;
        const rateEl = document.getElementById('sample-rate');
        if (parsed.sample_rate) {
          rateEl.value = String(parsed.sample_rate);
          if (!rateEl.querySelector(`option[value="${parsed.sample_rate}"]`)) {
            const opt = document.createElement('option');
            opt.value = parsed.sample_rate;
            opt.textContent = `${parsed.sample_rate / 1000} kHz`;
            rateEl.appendChild(opt);
            rateEl.value = String(parsed.sample_rate);
          }
        }
        // Select the hinted ALSA device if it appears in the list
        if (data.alsa_hint) {
          const sel = document.getElementById('device-select');
          const match = [...sel.options].find(o => o.value.includes('RAVENNA') || o.value === data.alsa_hint);
          if (match) sel.value = match.value;
        }
      }

      // Close modal after a moment so user can read the message
      setTimeout(() => {
        document.getElementById('sdp-modal').style.display = 'none';
      }, 3000);

    } else if (data.status === 'pending_manual') {
      result.className = 'sdp-result warn';
      result.innerHTML = esc(data.message) +
        (data.manual_info
          ? `<br><br><strong>Multicast:</strong> ${data.manual_info.multicast}:${data.manual_info.port} &nbsp;
             <strong>Source:</strong> ${data.manual_info.source} &nbsp;
             <strong>${data.manual_info.channels} ch @ ${data.manual_info.sample_rate} Hz</strong>`
          : '');
      btnSub.disabled = false;

    } else {
      result.className  = 'sdp-result error';
      result.textContent = data.error || data.message || 'Unknown error.';
      btnSub.disabled = false;
    }
  } catch (e) {
    result.className  = 'sdp-result error';
    result.textContent = `Request failed: ${e.message}`;
    btnSub.disabled = false;
  }
}

// ── Utilities ─────────────────────────────────────────────────────

function dbClass(db) {
  if (db >= -3)  return 'over';
  if (db >= -12) return 'warn';
  return '';
}

function loudnessClass(lufs) {
  if (lufs >= -9)  return 'over';
  if (lufs >= -16) return 'warn';
  return '';
}

function setStatus(msg, state) {
  document.getElementById('status-text').textContent = msg;
  document.getElementById('status-dot').className = 'status-dot ' + (state || '');
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
