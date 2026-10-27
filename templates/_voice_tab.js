/* _voice_tab.js — the Voice tab. Display only.
 *
 * Polls /api/voice/status (published by the separate voice process) and
 * renders it. Every string that came from speech or a model is untrusted,
 * so rows are built with textContent — never innerHTML — and a transcript
 * like "<img onerror=...>" renders as text.
 *
 * The one write is "Listen now" / "Speak test": a password-gated POST that
 * drops a command file for the voice process. Neither verb can move the
 * rover.
 */
(function() {
  const $ = (id) => document.getElementById(id);
  const LANES = [['mic', 'Microphone'], ['stt', 'Speech to text'], ['tts', 'Speech out'],
                 ['narrator', 'Narrator'], ['parser', 'Command parser']];
  let lastStatus = null, inflight = false;
  let player = null, playingId = null, playingUrl = null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const clock = (t) => { if (!t) return '—'; const d = new Date(t * 1000);
    return d.toTimeString().slice(0, 8); };
  const ms = (v) => v == null ? '—' : (v >= 1000 ? (v / 1000).toFixed(1) + ' s' : v + ' ms');
  const num = (v) => v == null ? '—' : String(v);

  function confBar(conf, floor, below) {
    const w = el('span', 'conf' + (below ? ' low' : ''));
    const fill = document.createElement('i'); fill.style.width = Math.round(Math.max(0, Math.min(1, conf)) * 100) + '%';
    const mark = document.createElement('b'); mark.style.left = Math.round(floor * 100) + '%';
    w.append(fill, mark); return w;
  }

  function fillList(listId, rows, emptyText, build) {
    const ol = $(listId); ol.textContent = '';
    if (!rows.length) { const li = el('li', 'empty', emptyText); ol.append(li); return; }
    for (const r of rows) ol.append(build(r));
  }

  function renderLanes(s) {
    const box = $('voice-lanes'); box.textContent = '';
    for (const [key, title] of LANES) {
      // The microphone is checked by the dashboard itself, so it still has
      // an answer when the voice process is off.
      const lane = (s.lanes || {})[key] || (key === 'mic' ? micAsLane() : null);
      const state = lane ? (lane.state || (lane.ok ? 'ok' : 'down')) : 'unknown';
      const tile = el('div', 'voice-lane ' + state);
      tile.append(el('h3', null, title),
                  el('div', 'st', lane ? state : (s.running ? 'no data' : 'voice process off')));
      if (lane) {
        const bits = [lane.backend, lane.model, lane.device, lane.tool,
          lane.cached != null ? lane.cached + '/' + lane.expected + ' phrases' : null,
          lane.product].filter(Boolean);
        if (bits.length) tile.append(el('div', 'meta', bits.join(' · ')));
        if (lane.reason) tile.append(el('div', 'why', lane.reason));
      }
      box.append(tile);
    }
  }

  function renderTranscripts(s, floor) {
    const rows = s.transcripts || [];
    const redacted = !!s.redacted;
    $('voice-redact').hidden = !(redacted && s.running);
    const top = rows.find((r) => r.text) || null;
    $('voice-last-text').textContent = top ? top.text : '—';
    $('voice-last-text').className = 'voice-last-text' + (top ? '' : ' empty');
    $('voice-last-meta').textContent = top
      ? [top.confidence.toFixed(2) + ' confidence', top.source, top.language, ms(top.latency_ms),
         clock(top.t), top.below_floor ? 'BELOW FLOOR — discarded' : ''].filter(Boolean).join(' · ')
      : (redacted ? 'sign in to see transcripts' : 'nothing heard yet');
    fillList('voice-transcripts', rows,
      redacted ? 'hidden — sign in' : 'No transcripts yet. Press “Listen now”.', (r) => {
        const li = el('li'); li.append(el('span', 't', clock(r.t)));
        const mid = el('span', 'txt' + (r.error ? ' err' : ''));
        if (r.error) mid.textContent = r.error;
        else { mid.append(confBar(r.confidence, floor, r.below_floor), document.createTextNode(r.text)); }
        li.append(mid, el('span', 'tag ' + (r.error ? 'bad' : r.below_floor ? 'warn' : 'good'),
          r.error ? 'error' : r.confidence.toFixed(2)));
        return li; });
  }

  function renderNarration(s) {
    fillList('voice-narration', s.narration || [],
      s.redacted ? 'hidden — sign in' : 'Nothing narrated yet.', (r) => {
        const li = el('li'); li.append(el('span', 't', clock(r.t)),
          el('span', 'txt', r.text + '  (' + r.event + ')'),
          el('span', 'tag ' + (r.played ? 'good' : 'bad'), r.played ? 'played' : 'played: no'));
        return li; });
  }

  function renderParses(s) {
    const cls = { accepted: 'good', rejected: 'warn', refused: 'warn', clarify: 'warn',
                  not_understood: 'warn', throttled: 'bad', backend_error: 'bad' };
    fillList('voice-parses', s.parses || [],
      s.redacted ? 'hidden — sign in' : 'No commands parsed yet.', (r) => {
        const li = el('li'); li.append(el('span', 't', clock(r.t)),
          el('span', 'txt', r.text + (r.action ? '  → ' + r.action : '') + (r.error ? '  — ' + r.error : '')),
          el('span', 'tag ' + (cls[r.outcome] || ''), (r.lane === 'rules' ? 'rules · ' : r.lane === 'llm' ? 'llm · ' : '') + r.outcome));
        return li; });
  }

  function stopNote() {
    if (player) { player.onended = null; player.pause(); player = null; }
    if (playingUrl) { URL.revokeObjectURL(playingUrl); playingUrl = null; }
    playingId = null;
    if (lastStatus) renderNotes(lastStatus);
  }

  async function playNote(id) {
    const msg = $('voice-notes-msg');
    if (playingId === id) { stopNote(); return; }
    stopNote();
    msg.textContent = 'loading…';
    try {
      if (!Auth.accessValid(5000) && Auth.refreshValid()) await Auth.refresh();
      const r = await fetch('/api/voice/notes/' + encodeURIComponent(id) + '.wav',
                            { cache: 'no-store', headers: Auth.headers() });
      if (!r.ok) throw new Error(r.status === 401 ? 'sign in to play' : 'note is gone');
      playingUrl = URL.createObjectURL(await r.blob());
      player = new Audio(playingUrl);
      playingId = id;
      player.onended = stopNote;
      await player.play();
      msg.textContent = '';
      renderNotes(lastStatus);
    } catch (e) {
      msg.textContent = e.message || 'could not play';
      stopNote();
    }
  }

  function renderNotes(s) {
    const gateWhy = (n) => n.gate ? 'passed the loudness gate' : 'too quiet — never sent to speech-to-text';
    fillList('voice-notes', s.notes || [],
      s.redacted ? 'hidden — sign in' : 'No captures yet. Press “Listen now”.', (n) => {
        const li = el('li'); li.append(el('span', 't', clock(n.t)));
        const mid = el('span', 'txt');
        const heard = !n.gate ? 'not transcribed'
          : !n.resolved ? 'transcribing…'
          : n.error ? n.error
          : '“' + n.text + '”' + (n.confidence != null ? ' (' + n.confidence.toFixed(2) + ')' : '');
        mid.append(document.createTextNode(heard),
          el('span', 'sub', n.seconds + ' s · peak ' + Math.round(n.peak_rms) + ' · ' + gateWhy(n)));
        const btn = el('button', null, playingId === n.id ? 'Stop' : 'Play');
        btn.type = 'button';
        btn.setAttribute('aria-label', (playingId === n.id ? 'Stop' : 'Play') + ' voice note from ' + clock(n.t));
        btn.addEventListener('click', () => playNote(n.id));
        li.append(mid, btn);
        return li; });
  }

  function renderMeter(s) {
    const m = s.meter || {}, peak = m.peak_rms || 0, thr = m.threshold || 250;
    const max = Math.max(thr * 4, peak);
    $('voice-meter-bar').style.width = Math.min(100, peak / max * 100) + '%';
    $('voice-meter-thr').style.left = (thr / max * 100) + '%';
    $('voice-meter-val').textContent = 'peak ' + Math.round(peak) + ' / gate ' + Math.round(thr);
  }

  function renderLatency(s) {
    const l = s.latency || {};
    for (const [key, id] of [['stt_ms', 'stt'], ['parse_ms', 'parse']]) {
      const v = l[key] || {};
      $('voice-lat-' + id + '-p50').textContent = ms(v.p50);
      $('voice-lat-' + id + '-p95').textContent = ms(v.p95);
      $('voice-lat-' + id + '-n').textContent = num(v.n);
      $('voice-lat-' + id + '-t').textContent = num(v.throttled);
    }
  }

  function renderCommand(s) {
    const c = s.command || {}, busy = c.state === 'running' || inflight;
    $('voice-listen').disabled = busy || !s.running;
    $('voice-speak').disabled = busy || !s.running;
    $('voice-ask').disabled = busy || !s.running;
    $('voice-report').disabled = busy || !s.running;
    if (c.state && c.state !== 'idle' && c.state !== 'unavailable' && !inflight) {
      $('voice-cmd-msg').textContent = c.verb + ': ' + c.state + (c.detail ? ' — ' + c.detail : '');
    }
  }

  function render(s) {
    lastStatus = s;
    const running = !!s.running;
    const lanes = Object.values(s.lanes || {});
    const anyDown = lanes.some((l) => l.state === 'down');
    const anyDeg = lanes.some((l) => l.state === 'degraded');
    const chip = $('voice-chip');
    chip.className = 'pill ' + (!running ? 'voice-off' : anyDown || anyDeg ? 'voice-warn' : 'voice-ok');
    chip.textContent = !running ? 'offline' : anyDown ? 'lane down' : anyDeg ? 'degraded' : 'ready';
    $('voice-dot').className = 'dot' + (running ? ' live' : ' dead');
    $('voice-proc').textContent = running ? 'running' : 'not running';
    $('voice-pid').textContent = running ? num(s.pid) : '—';
    $('voice-age').textContent = s.age_s != null ? s.age_s.toFixed(1) + ' s' : '—';
    const keys = s.keys || {};
    $('voice-keys').textContent = running
      ? Object.keys(keys).map((k) => k.replace('_API_KEY', '').toLowerCase() + (keys[k] ? ' ✓' : ' ✗')).join('  ') : '—';
    $('voice-offline').hidden = running;
    if (!running) $('voice-offline-msg').textContent = s.age_s != null
      ? 'Last update was ' + s.age_s.toFixed(0) + ' s ago.' : 'No voice_status.json found.';
    // A red dot only when it's running AND broken; "not running" is a normal state.
    const dot = $('dot-voice');
    if (dot) dot.className = 'tabdot' + (running && anyDown ? ' on' : '');
    renderLanes(s);
    const floor = ((s.lanes || {}).stt || {}).confidence_floor;
    const f = typeof floor === 'number' ? floor : 0.55;
    $('voice-floor').textContent = f.toFixed(2);
    renderTranscripts(s, f); renderNarration(s); renderParses(s); renderNotes(s);
    renderMeter(s); renderLatency(s); renderCommand(s);
  }

  async function refresh() {
    try {
      if (!Auth.accessValid(5000) && Auth.refreshValid()) await Auth.refresh();
      const r = await fetch('/api/voice/status', { cache: 'no-store', headers: Auth.headers() });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      render(await r.json());
    } catch (e) {
      render({ running: false, stale: true, age_s: null });
      $('voice-offline-msg').textContent = 'Dashboard did not answer /api/voice/status.';
    }
  }

  async function send(verb) {
    if (!(await requireAuth('Sign in to use the microphone.'))) return;
    inflight = true;
    for (const id of ['voice-listen', 'voice-speak', 'voice-ask', 'voice-report']) $(id).disabled = true;
    $('voice-cmd-msg').textContent = 'sending…';
    try {
      const body = (verb === 'listen' || verb === 'ask') ? { verb, seconds: Number($('voice-secs').value) } : { verb };
      const r = await authFetch('/api/voice/command', body);
      const j = await r.json().catch(() => ({}));
      $('voice-cmd-msg').textContent = r.ok ? ({ listen: 'listening…', ask: 'listening for your question…', report: 'reporting…' }[verb] || 'speaking…')
        : (j.error || 'not sent');
    } catch (e) { $('voice-cmd-msg').textContent = 'not sent'; }
    inflight = false; refresh();
  }

  $('voice-listen').addEventListener('click', () => send('listen'));
  $('voice-speak').addEventListener('click', () => send('speak_test'));
  $('voice-ask').addEventListener('click', () => send('ask'));
  $('voice-report').addEventListener('click', () => send('report'));
  $('voice-signin').addEventListener('click', () => { if (typeof openLogin === 'function') openLogin(); });

  // ── Microphone card ─────────────────────────────────────────────
  // Connection comes from /api/voice/mic (USB bus, nothing opened). "Working"
  // is only claimed after a real capture: the last test, or the last capture
  // the voice process made.
  let lastMic = null, micBusy = false;
  const MIC_STATE = {
    working: ['ok', 'working'], silent: ['bad', 'silent'], busy: ['warn', 'busy'],
    missing: ['bad', 'not found'], no_tool: ['bad', 'no recorder'], error: ['bad', 'error'] };
  const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t);
    return s < 90 ? Math.round(s) + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : 'over an hour ago'; };

  // Best evidence of "working": a recent test, else what the voice process last captured.
  function micEvidence() {
    const t = lastMic && lastMic.last_test;
    if (t) return { state: t.state, ok: !!t.ok, peak: t.peak_rms, msg: t.message, t: t.t, src: 'test' };
    const m = lastStatus && lastStatus.running && lastStatus.meter;
    if (m && m.peak_rms > 3) return { state: 'working', ok: true, peak: m.peak_rms,
      msg: 'Audio arrived on the last voice capture.', t: null, src: 'voice' };
    return null;
  }
  function micAsLane() {
    if (!lastMic) return null;
    if (lastMic.connected === null) return { ok: false, state: 'down', reason: lastMic.error || 'cannot inspect audio devices' };
    if (!lastMic.connected) return { ok: false, state: 'down', reason: 'C270 not found on USB (' + lastMic.expected_usb_id + ')' };
    const ev = micEvidence(), d = lastMic.device || {};
    const base = { product: d.product, device: d.plughw };
    if (!ev) return { ok: true, state: 'degraded', reason: 'connected, not tested yet', ...base };
    return ev.ok ? { ok: true, state: 'ok', reason: '', ...base }
                 : { ok: false, state: 'down', reason: ev.msg, ...base };
  }

  function renderMic() {
    const m = lastMic; if (!m) return;
    const chip = $('mic-chip'), head = $('mic-headline'), ev = micEvidence();
    let cls, chipText, headText;
    if (m.connected === null) { cls = 'bad'; chipText = 'unknown'; headText = m.error || 'Cannot inspect audio devices.'; }
    else if (!m.connected) {
      cls = 'bad'; chipText = 'not found';
      headText = 'No microphone found. Expected USB ' + m.expected_usb_id + ' (Logitech C270). Re-plug it.';
    } else {
      const d = m.device || {}, name = (m.cards.find((c) => c.selected) || {}).name || d.product || 'USB microphone';
      if (!ev) { cls = 'warn'; chipText = 'connected'; headText = name + ' is connected — not tested yet.'; }
      else if (ev.ok) { cls = 'ok'; chipText = 'working'; headText = name + ' is connected and working.'; }
      else { cls = (MIC_STATE[ev.state] || ['bad'])[0]; chipText = (MIC_STATE[ev.state] || [0, 'problem'])[1];
             headText = name + ' is connected, but: ' + ev.msg; }
    }
    chip.className = 'pill ' + (cls === 'ok' ? 'voice-ok' : cls === 'warn' ? 'voice-warn' : 'voice-off');
    if (cls === 'bad') chip.style.background = '#7f1d1d', chip.style.color = '#fecaca'; else { chip.style.background = ''; chip.style.color = ''; }
    chip.textContent = chipText;
    head.textContent = headText; head.className = 'mic-headline ' + cls;

    const d = m.device;
    $('mic-product').textContent = d ? d.product + ' (card ' + d.card + ')' : '—';
    $('mic-usb').textContent = d ? d.usb_id : '—';
    $('mic-alsa').textContent = d ? d.plughw : '—';
    $('mic-arecord').textContent = m.arecord ? 'installed' : 'MISSING';
    $('mic-last').textContent = ev ? (ev.src === 'test' ? 'test ' + ago(ev.t) : 'from voice capture') : 'not tested';
    $('mic-peak').textContent = ev ? 'peak ' + Math.round(ev.peak) : '—';
    $('mic-level-bar').style.width = ev ? Math.min(100, ev.peak / 40) + '%' : '0%';
    $('mic-level-bar').style.background = ev && !ev.ok ? '#f87171' : '';

    const ul = $('mic-cards'); ul.textContent = '';
    if (!m.cards.length) ul.append(el('li', null, 'No capture devices reported by arecord.'));
    for (const c of m.cards) {
      const li = el('li', c.selected ? 'sel' : ''); li.append(el('span', 'mono', 'card ' + c.card),
        el('span', null, c.name));
      if (c.selected) li.append(el('span', 'tag', 'used by voice'));
      ul.append(li);
    }
    if (lastStatus) renderLanes(lastStatus);
  }

  async function micRefresh() {
    try {
      const r = await fetch('/api/voice/mic', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      lastMic = await r.json();
    } catch (e) {
      lastMic = { connected: null, error: 'Dashboard did not answer /api/voice/mic.', cards: [], device: null };
    }
    renderMic();
  }

  $('mic-test').addEventListener('click', async () => {
    if (micBusy) return;
    if (!(await requireAuth('Sign in to test the microphone.'))) return;
    micBusy = true; $('mic-test').disabled = true; $('mic-test-msg').textContent = 'Recording… speak or tap the mic.';
    try {
      const r = await authFetch('/api/voice/mic/test', {});
      const j = await r.json().catch(() => ({}));
      $('mic-test-msg').textContent = r.ok ? j.message : (j.error || 'Test did not run.');
    } catch (e) { $('mic-test-msg').textContent = 'Test did not run.'; }
    micBusy = false; $('mic-test').disabled = false; micRefresh();
  });

  micRefresh();
  setInterval(() => { if (!$('tab-voice').hidden && !micBusy) micRefresh(); }, 3000);

  // ── speaker ────────────────────────────────────────────────────────
  let lastOut = null;

  function renderOut() {
    const o = lastOut;
    const sel = $('out-device');
    if (!o) { $('out-chip').textContent = 'unknown'; $('out-chip').className = 'pill voice-off';
             $('out-headline').textContent = 'Dashboard did not answer /api/voice/output.'; return; }

    const outs = o.outputs || [];
    const chip = $('out-chip');
    if (!outs.length) {
      chip.textContent = 'none found'; chip.className = 'pill voice-off';
      $('out-headline').textContent = o.error || 'No playback devices were found.';
    } else if (o.state === 'missing') {
      chip.textContent = 'selected speaker is gone'; chip.className = 'pill voice-off';
      $('out-headline').textContent = 'Tarka is pointed at a speaker that is not connected.';
    } else if (o.default_present === false && o.selected === 'auto') {
      chip.textContent = 'default is missing'; chip.className = 'pill voice-off';
      $('out-headline').textContent =
        'The machine default is "' + (o.default || 'unset') + '", which is not connected right now. Pick a speaker below.';
    } else if (o.state === 'connected') {
      chip.textContent = 'selected'; chip.className = 'pill voice-ok';
      $('out-headline').textContent = 'Speaking through the selected speaker.';
    } else {
      chip.textContent = 'auto'; chip.className = 'pill voice-ok';
      $('out-headline').textContent = 'Following the machine default.';
    }

    // Rebuild only when the device list actually changed, so an in-progress
    // selection is not thrown away by the poll.
    const signature = outs.map(x => x.id + '|' + x.state).join(',') + '#' + (o.selected || '');
    if (sel.dataset.sig !== signature) {
      sel.dataset.sig = signature;
      sel.textContent = '';
      const auto = document.createElement('option');
      auto.value = 'auto';
      auto.textContent = 'Auto — follow the machine default';
      sel.appendChild(auto);
      outs.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = d.label + ' (' + d.kind + (d.state && d.state !== 'unknown' ? ', ' + d.state.toLowerCase() : '') + ')';
        sel.appendChild(opt);
      });
      sel.value = o.selected || 'auto';
    }
    $('out-selected').textContent = o.selected || 'auto';
    $('out-default').textContent = o.default || '—';

    const ul = $('out-cards'); ul.textContent = '';
    if (!outs.length) ul.append(el('li', null, 'No playback devices found.'));
    for (const d of outs) {
      const used = d.id === o.selected;
      const li = el('li', used ? 'sel' : '');
      li.append(el('span', 'mono', d.kind), el('span', null, d.label));
      if (used) li.append(el('span', 'tag', 'used by voice'));
      else if (d.id === o.default) li.append(el('span', 'tag', 'machine default'));
      ul.append(li);
    }
  }

  async function outRefresh() {
    try {
      const r = await fetch('/api/voice/output', { cache: 'no-store' });
      lastOut = await r.json();
      if (!r.ok) lastOut = { outputs: [], error: (lastOut && lastOut.error) || 'could not list outputs' };
    } catch (e) {
      lastOut = { outputs: [], error: 'Dashboard did not answer /api/voice/output.' };
    }
    renderOut();
  }

  $('out-apply').addEventListener('click', async () => {
    if (!(await requireAuth('Sign in to change the speaker.'))) return;
    $('out-msg').textContent = 'Applying…';
    try {
      const r = await authFetch('/api/voice/output', { device: $('out-device').value });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { $('out-msg').textContent = j.error || 'Could not change the speaker.'; return; }
      lastOut = j;
      $('out-msg').textContent = 'Speaker set to ' + (j.selected || 'auto') + '.';
      renderOut();
    } catch (e) { $('out-msg').textContent = 'Could not change the speaker.'; }
  });

  $('out-test').addEventListener('click', async () => {
    const device = $('out-device').value;
    if (!(await requireAuth('Sign in to test the speaker.'))) return;
    $('out-msg').textContent = 'Playing a test clip on ' + device + '…';
    try {
      // Apply first, so the test proves the device that was just chosen.
      const r = await authFetch('/api/voice/output', { device });
      if (r.ok) lastOut = await r.json();
      renderOut();
      // Then probe it here rather than queueing a phrase for the voice
      // process: only this side can say whether audio actually left the sink.
      const t = await authFetch('/api/voice/output/test', { device });
      const j = await t.json().catch(() => ({}));
      if (j.audible) {
        $('out-msg').textContent = 'Played, and the speaker took it. ' + (j.reason || '');
      } else {
        // Say why it failed — a paired-but-off Bluetooth speaker is the case
        // that otherwise just looks like "no sound".
        $('out-msg').textContent = 'No sound: ' + (j.reason || 'the speaker did not play');
      }
    } catch (e) { $('out-msg').textContent = 'Test did not run.'; }
  });

  outRefresh();
  setInterval(() => { if (!$('tab-voice').hidden) outRefresh(); }, 5000);

  // Fast while the tab is open, slow in the background so the tab dot stays honest.
  function tick() {
    const visible = !$('tab-voice').hidden;
    refresh().finally(() => setTimeout(tick, visible ? 1000 : 5000));
  }
  tick();
  window.voiceTabRefresh = refresh;
})();
