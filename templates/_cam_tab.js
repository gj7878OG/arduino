/* _cam_tab.js — bring the C270 feed tab to life.
 *
 * The browser fetches /cam/mjpeg (multipart/x-mixed-replace) directly
 * via the <img> tag, so the only JS work is the live status pill +
 * dot + frame-age counter, plus one chip in the summary strip.
 *
 * /cam/status is polled every 1s whether or not the tab is open: the
 * strip chip is the only place the operator sees this camera while
 * they work in another tab, so the poll has to keep running. The tab's
 * own fields (resolution, FPS, frame age, the stream <img>) are only
 * touched when the tab is visible.
 */
(function() {
  const $ = (id) => document.getElementById(id);

  function fmtAge(ms) {
    if (ms == null || isNaN(ms)) return '—';
    if (ms < 1000) return ms + ' ms';
    return (ms / 1000).toFixed(1) + ' s';
  }

  function setChip(state) {
    const chip = $('cam-status-chip');
    if (!chip) return;
    chip.classList.remove('cam-pending', 'cam-live', 'cam-stale');
    if (state === 'live') { chip.classList.add('cam-live'); chip.textContent = 'live'; }
    else if (state === 'stale') { chip.classList.add('cam-stale'); chip.textContent = 'stale'; }
    else { chip.classList.add('cam-pending'); chip.textContent = 'offline'; }
  }

  // Summary-strip chip for the USB camera. The AGV pipeline's own camera
  // has a chip next to this one (sum-cam) fed by telemetry; this one is fed
  // from /cam/status so it stays honest when the AGV itself isn't running.
  function setStrip(s, connected, fresh) {
    if (!$('sum-usbcam')) return;
    const txt = fresh ? 'OK' : connected ? 'STALE' : 'NONE';
    const cls = fresh ? 'good' : connected ? 'warn' : 'bad';
    chip('sum-usbcam', txt, cls);
    // The Camera tab dot lights when either camera is not streaming. A preview
    // that is connected but going stale is still streaming, so it does not
    // light it — the amber chip above already says STALE.
    camDot('usb', connected);
    $('sum-usbcam').title = [
      'USB camera',
      s.device || 'no device',
      s.width && s.height ? `${s.width}×${s.height}` : '',
      s.fps != null ? s.fps.toFixed(1) + ' fps' : '',
      connected ? 'frame age ' + fmtAge(s.age_ms) : 'not streaming',
      s.last_error || '',
    ].filter(Boolean).join(' · ');
  }

  function setStripDown() {
    if (!$('sum-usbcam')) return;
    chip('sum-usbcam', 'DOWN', 'bad');
    camDot('usb', false);
    $('sum-usbcam').title = 'USB camera · /cam/status did not answer';
  }

  async function refresh() {
    const tab = $('tab-cam');
    const visible = !!tab && !tab.hidden;
    try {
      const r = await fetch('/cam/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const s = await r.json();

      // Authoritative: connected AND fresh in the last 2 s.
      const connected = !!s.connected;
      const age = s.age_ms;
      const fresh = connected && age != null && age < 2000;

      setStrip(s, connected, fresh);
      if (!visible) return;

      $('cam-dev').textContent    = s.device || '—';
      $('cam-res').textContent    = s.width && s.height ? `${s.width}×${s.height}` : '—';
      $('cam-fps').textContent    = s.fps != null ? s.fps.toFixed(1) : '—';
      $('cam-age').textContent    = connected ? fmtAge(age) : '—';
      $('cam-frames').textContent = s.frame_seq != null ? String(s.frame_seq) : '—';
      $('cam-err').textContent    = s.last_error || '—';

      const dot  = $('cam-dot');
      const img  = $('cam-frame');
      const off  = $('cam-offline');
      const msg  = $('cam-offline-msg');

      if (fresh) {
        dot.classList.add('live'); dot.classList.remove('stale');
        $('cam-status').textContent = 'streaming';
        setChip('live');
        // Use a cache-buster URL only when we transition from offline
        // to live; the server already Cache-Control: no-caches the
        // stream so subsequent reloads stay current.
        if (img.dataset.live !== '1') {
          img.src = '/cam/mjpeg?ts=' + Date.now();
          img.alt = 'USB camera feed (live)';
          img.dataset.live = '1';
          off.classList.add('hidden');
        }
      } else if (connected) {
        dot.classList.remove('live'); dot.classList.add('stale');
        $('cam-status').textContent = 'stale';
        setChip('stale');
      } else {
        dot.classList.remove('live', 'stale');
        $('cam-status').textContent = s.last_error ? 'error' : 'offline';
        setChip(s.last_error ? 'stale' : 'pending');
        if (img.dataset.live === '1') {
          img.dataset.live = '0';
          img.src = '';
          img.alt = 'USB camera feed (offline)';
          off.classList.remove('hidden');
          msg.textContent = s.last_error || 'Re-plug the C270 or check /cam/status.';
        }
      }
    } catch (e) {
      setStripDown();
      if (!visible) return;
      $('cam-status').textContent = 'no dashboard';
      setChip('stale');
    }
  }

  // One timer for the page. It used to be stopped while the tab was hidden,
  // which would have frozen the strip chip at whatever the camera was doing
  // when the operator last looked at it.
  refresh();
  setInterval(refresh, 1000);
})();

/* ── Camera tab · detection overlay, enrolment, roster ──
 *
 * /cam/detections is polled at 4 Hz, and only while this tab is visible: it
 * is what wakes the server-side analytics worker, so a hidden tab costs the
 * Jetson nothing. (The strip chip above keeps polling /cam/status regardless.)
 * Boxes arrive in frame pixels and are drawn on a canvas laid over the <img>,
 * so the MJPEG path is untouched.
 */
(function() {
  const $ = (id) => document.getElementById(id);
  const tab = $('tab-cam');
  if (!tab) return;

  const COL = { known: '#4ade80', unknown: '#f87171', object: '#60a5fa' };
  let settings = null, rosterCount = -1, inflight = false, last = null;
  let people = [], editing = null, voice = { running: false, known: false };
  let greeterBusy = 0;     // saves in flight: their (older) responses must not reset what the operator just changed
  let enrollWasActive = false, thrTimer = null;

  const visible = () => !tab.hidden;
  const pad2 = (n) => String(n).padStart(2, '0');
  const clock = (t) => { const d = new Date(t * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // names are operator input: never innerHTML
    return n;
  }

  // ── overlay canvas ────────────────────────────────────────────────
  const canvas = $('cam-overlay'), ctx = canvas.getContext('2d');
  const wrap = $('cam-frame-wrap');
  function fitCanvas() {
    const dpr = window.devicePixelRatio || 1, r = wrap.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    draw(last);
  }
  if (window.ResizeObserver) new ResizeObserver(fitCanvas).observe(wrap);
  else window.addEventListener('resize', fitCanvas);

  function label(text, x, y, color, dpr) {
    ctx.font = `600 ${12 * dpr}px ui-monospace, monospace`;
    const w = ctx.measureText(text).width + 8 * dpr, h = 17 * dpr;
    const top = Math.max(0, y - h);
    ctx.fillStyle = color; ctx.fillRect(x, top, w, h);
    ctx.fillStyle = '#0b0f15'; ctx.textBaseline = 'middle';
    ctx.fillText(text, x + 4 * dpr, top + h / 2);
  }

  function draw(d) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!d || d.state !== 'running' || !d.w || (d.age_ms != null && d.age_ms > 1500)) return;
    const dpr = window.devicePixelRatio || 1, W = canvas.width, H = canvas.height;
    const s = Math.min(W / d.w, H / d.h);                 // object-fit: contain
    const ox = (W - d.w * s) / 2, oy = (H - d.h * s) / 2;
    ctx.lineWidth = 2 * dpr;
    for (const o of d.objects || []) {
      const [x, y, w, h] = o.box;
      ctx.strokeStyle = COL.object; ctx.strokeRect(ox + x * s, oy + y * s, w * s, h * s);
      label(`${o.name} ${Math.round(o.conf * 100)}%`, ox + x * s, oy + y * s, COL.object, dpr);
    }
    for (const f of d.faces || []) {
      const [x, y, w, h] = f.box, c = f.known ? COL.known : COL.unknown;
      ctx.strokeStyle = c; ctx.strokeRect(ox + x * s, oy + y * s, w * s, h * s);
      const txt = f.known ? `${f.name} ${Math.round(f.sim * 100)}%` : (f.sim == null ? 'Face' : 'Unknown');
      label(txt, ox + x * s, oy + y * s, c, dpr);
    }
  }

  // ── panels ────────────────────────────────────────────────────────
  function renderFaces(d) {
    const ul = $('cam-faces-now');
    const faces = (d && d.state === 'running') ? d.faces || [] : [];
    ul.replaceChildren();
    if (!faces.length) { ul.append(el('li', 'cam-empty', d && d.state === 'idle' ? 'Starting…' : 'No faces')); }
    const thr = d && d.settings ? d.settings.threshold : 0.363;
    for (const f of faces) {
      const li = el('li');
      li.append(el('i', 'cam-id-dot' + (f.known ? ' known' : '')));
      li.append(el('span', 'cam-face-name', f.name || (f.sim == null ? 'Detected' : 'Unknown')));
      if (f.sim != null) {
        const m = el('span', 'cam-meter'), fill = el('i'), mark = el('b');
        fill.style.width = Math.max(0, Math.min(100, f.sim * 100)) + '%';
        mark.style.left = (thr * 100) + '%';
        m.append(fill, mark); li.append(m, el('span', 'mono', f.sim.toFixed(2)));
      }
      ul.append(li);
    }
    const ou = $('cam-objects-now'), objs = d && d.objects ? d.objects : [];
    ou.hidden = !(settings && settings.objects_enabled);
    ou.replaceChildren();
    for (const o of objs) {
      const li = el('li');
      li.append(el('i', 'cam-id-dot known'), el('span', 'cam-face-name', o.name),
                el('span', 'mono', Math.round(o.conf * 100) + '%'));
      ou.append(li);
    }
  }

  function renderEvents(d) {
    const ul = $('cam-events');
    ul.replaceChildren();
    const ev = d && d.events ? d.events : [];
    if (!ev.length) { ul.append(el('li', 'cam-empty', 'Nothing yet')); return; }
    for (const e of ev) {
      const li = el('li');
      li.append(el('time', null, clock(e.t)), el('i', 'cam-id-dot' + (e.name ? ' known' : '')),
                el('span', null, e.name ? `${e.name} · ${e.sim.toFixed(2)}` : 'Unknown face'));
      ul.append(li);
    }
  }

  function renderEnroll(d) {
    const e = d && d.enroll ? d.enroll : { active: false };
    const bar = $('cam-enroll-bar'), msg = $('cam-enroll-msg');
    $('cam-enroll-go').disabled = !!e.active;
    $('cam-enroll-name').disabled = !!e.active;
    $('cam-enroll-cancel').hidden = !e.active;
    bar.hidden = !e.active;
    if (e.active) {
      $('cam-enroll-fill').style.width = (100 * e.got / e.want) + '%';
      msg.textContent = `${e.name}: ${e.got}/${e.want} — ${e.hint}`;
    } else if (e.result === 'saved') {
      msg.textContent = `Saved ${e.saved} samples for ${e.name}.`;
    } else if (e.result) {
      msg.textContent = `Enrolment ${e.result}.`;
    } else {
      msg.textContent = 'One person in view, facing the camera. Turn your head slightly between captures.';
    }
    if (enrollWasActive && !e.active && e.result === 'saved') {
      if (typeof toast === 'function') toast(`Enrolled ${e.name}`, 'info', 3000);
      loadRoster();
    }
    enrollWasActive = !!e.active;
  }

  function renderModels(d) {
    const note = $('cam-model-note'), m = d && d.models ? d.models : {};
    const bad = [];
    if (m.faces && m.faces !== 'ok' && m.faces !== 'off') bad.push('Face model: ' + m.faces);
    if (m.objects && !['ok', 'off', 'loading'].includes(m.objects)) bad.push('Object model: ' + m.objects);
    note.hidden = !bad.length; note.textContent = bad.join(' · ');
    const t = d && d.timing;
    $('cam-perf').textContent = t && d.state === 'running'
      ? `face ${t.faces_ms} ms · embed ${t.embed_ms} ms${settings && settings.objects_enabled ? ` · obj ${t.objects_ms} ms` : ''} · ${t.hz} Hz`
      : '—';
  }

  // Sent with the bearer token when there is one: only a signed-in reader is
  // given the notes, so an editor opened while signed out would otherwise save
  // blank notes over the real ones.
  async function loadRoster() {
    try {
      const r = await fetch('/api/cam/people', { cache: 'no-store', headers: Auth.headers() });
      if (!r.ok) return;
      people = (await r.json()).people;
      rosterCount = people.length;
      $('cam-people-count').textContent = people.length ? `(${people.length})` : '';
      const box = $('cam-roster');
      box.replaceChildren();
      if (!people.length) { box.append(el('p', 'cam-empty', 'Nobody enrolled yet.')); closeProfile(); return; }
      for (const p of people) {
        const card = el('div', 'cam-person' + (editing === p.name ? ' editing' : ''));
        if (p.has_thumb) {
          const img = el('img'); img.alt = p.name; img.loading = 'lazy';
          img.src = `/api/cam/people/${encodeURIComponent(p.name)}/thumb?t=${Math.round(p.enrolled_at || 0)}`;
          card.append(img);
        } else card.append(el('div', 'cam-nothumb', '☺'));
        card.append(el('div', 'nm', p.name),
                    el('div', 'ct', (p.profile && p.profile.role) ? p.profile.role : `${p.samples} samples`));
        if (p.profile && p.profile.greet) card.append(el('div', 'greet-badge', '● greeted'));
        const edit = el('button', 'cam-edit', 'Edit');
        edit.type = 'button'; edit.addEventListener('click', () => openProfile(p.name));
        const call = el('button', 'cam-call', 'Call');
        call.type = 'button'; call.dataset.name = p.name;
        call.addEventListener('click', () => callPerson(p.name));
        const del = el('button', null, '×');
        del.type = 'button'; del.title = `Remove ${p.name}`; del.setAttribute('aria-label', `Remove ${p.name}`);
        del.addEventListener('click', () => removePerson(p.name));
        card.append(edit, call, del);
        box.append(card);
      }
      updateVoiceUi();
    } catch (e) { /* roster just stays as it was */ }
  }

  async function removePerson(name) {
    if (!confirm(`Remove ${name} and all their face samples?`)) return;
    if (!await requireAuth('Sign in to remove a person')) return;
    const r = await authFetch(`/api/cam/people/${encodeURIComponent(name)}`, undefined, 'DELETE');
    if (r.ok) { toast(`Removed ${name}`, 'info', 2500); loadRoster(); }
    else if (r.status !== 401 && r.status !== 429) toast('Could not remove ' + name, 'err');
  }

  // ── controls ──────────────────────────────────────────────────────
  function applySettings(s) {
    settings = s;
    for (const [id, key] of [['cam-t-faces', 'faces_enabled'], ['cam-t-objects', 'objects_enabled']])
      $(id).setAttribute('aria-pressed', s[key] ? 'true' : 'false');
    const lim = s.limits && s.limits.threshold;
    if (lim) { $('cam-thr').min = lim.min; $('cam-thr').max = lim.max; $('cam-thr').step = lim.step; }
    $('cam-thr').value = s.threshold; $('cam-thr-v').textContent = Number(s.threshold).toFixed(2);
    if (greeterBusy) return;
    $('cam-t-greeter').setAttribute('aria-pressed', s.greeter_enabled ? 'true' : 'false');
    // Do not yank a control out from under the operator while they are using it.
    if (document.activeElement !== $('cam-gsim')) { $('cam-gsim').value = s.greet_min_sim; }
    $('cam-gsim-v').textContent = Number(s.greet_min_sim).toFixed(2);
    if (document.activeElement !== $('cam-gcool')) $('cam-gcool').value = s.greet_cooldown_s;
    $('cam-gunk').checked = !!s.greet_unknown;
  }
  async function loadSettings() {
    try { const r = await fetch('/api/cam/settings', { cache: 'no-store' }); if (r.ok) applySettings(await r.json()); }
    catch (e) { /* retried on the next tick */ }
  }
  async function postSettings(patch) {
    try {
      const r = await fetch('/api/cam/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                   body: JSON.stringify(patch) });
      if (r.ok) applySettings(await r.json());
    } catch (e) { toast('Could not save camera settings', 'err'); }
  }
  for (const [id, key] of [['cam-t-faces', 'faces_enabled'], ['cam-t-objects', 'objects_enabled']])
    $(id).addEventListener('click', () => { if (settings) postSettings({ [key]: !settings[key] }); });
  $('cam-thr').addEventListener('input', () => {
    $('cam-thr-v').textContent = Number($('cam-thr').value).toFixed(2);
    clearTimeout(thrTimer);
    thrTimer = setTimeout(() => postSettings({ threshold: Number($('cam-thr').value) }), 200);
  });

  $('cam-enroll-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const name = $('cam-enroll-name').value.trim();
    if (!name) return;
    if (!await requireAuth('Sign in to add a face')) return;
    const r = await authFetch('/api/cam/enroll', { name });
    if (r.ok) { tick(); return; }
    if (r.status === 401 || r.status === 429) return;           // authFetch already told the operator
    const j = await r.json().catch(() => ({}));
    toast(j.error || 'Could not start enrolment', 'err');
  });
  $('cam-enroll-cancel').addEventListener('click', async () => {
    await fetch('/api/cam/enroll/cancel', { method: 'POST' });
    tick();
  });

  // ── profile editor ────────────────────────────────────────────────
  const pf = (id) => $('cam-profile-' + id);
  function previewGreeting() {
    const name = editing || '';
    const g = pf('greeting').value.trim() || `Hello, ${name}.`;
    pf('preview').textContent = pf('greet').checked
      ? `Will say: “${g}”` : 'Will not greet this person (box not ticked).';
  }
  async function openProfile(name) {
    if (!await requireAuth('Sign in to edit details')) return;
    editing = name;
    await loadRoster();                                  // now with the bearer token: real notes
    const p = (people.find((x) => x.name === name) || {}).profile || {};
    $('cam-profile-name').textContent = name;
    pf('role').value = p.role || ''; pf('greeting').value = p.greeting || '';
    pf('notes').value = p.notes || ''; pf('greet').checked = !!p.greet;
    $('cam-profile-form').hidden = false;
    previewGreeting();
    pf('role').focus();
  }
  function closeProfile() {
    editing = null; $('cam-profile-form').hidden = true;
    for (const c of document.querySelectorAll('#cam-roster .cam-person.editing')) c.classList.remove('editing');
  }
  $('cam-profile-close').addEventListener('click', closeProfile);
  for (const id of ['greeting', 'greet']) { pf(id).addEventListener('input', previewGreeting); pf(id).addEventListener('change', previewGreeting); }
  $('cam-profile-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!editing) return;
    if (!await requireAuth('Sign in to save details')) return;
    const r = await authFetch(`/api/cam/people/${encodeURIComponent(editing)}/profile`, {
      role: pf('role').value, greeting: pf('greeting').value, notes: pf('notes').value, greet: pf('greet').checked,
    }, 'PUT');
    if (r.ok) { toast(`Saved details for ${editing}`, 'info', 2500); closeProfile(); loadRoster(); return; }
    if (r.status === 401 || r.status === 429) return;
    const j = await r.json().catch(() => ({}));
    toast(j.error || 'Could not save details', 'err');
  });

  // ── greeter + rover voice ─────────────────────────────────────────
  async function saveGreeter(patch) {
    greeterBusy++;
    try {
      if (!await requireAuth('Sign in to change the greeter')) return;
      const r = await authFetch('/api/cam/settings', patch);
      if (r.ok) {
        applySettings(await r.json());
        if (patch.greeter_enabled && !voice.running) toast('Greeter is on, but the voice process is not running', 'warn', 5000);
      } else if (r.status !== 401 && r.status !== 429) toast('Could not save greeter settings', 'err');
    } finally {
      greeterBusy--;
      if (!greeterBusy && settings) applySettings(settings);   // one render, from the newest state
    }
  }
  $('cam-t-greeter').addEventListener('click', () => { if (settings) saveGreeter({ greeter_enabled: !settings.greeter_enabled }); });
  $('cam-gunk').addEventListener('change', () => saveGreeter({ greet_unknown: $('cam-gunk').checked }));
  let gTimer = null;
  $('cam-gsim').addEventListener('input', () => {
    $('cam-gsim-v').textContent = Number($('cam-gsim').value).toFixed(2);
    clearTimeout(gTimer); gTimer = setTimeout(() => saveGreeter({ greet_min_sim: Number($('cam-gsim').value) }), 400);
  });
  $('cam-gcool').addEventListener('change', () => saveGreeter({ greet_cooldown_s: Number($('cam-gcool').value) }));

  function updateVoiceUi() {
    const dot = $('cam-voice-dot'), line = $('cam-voice-status');
    const faces = (voice.lanes || {}).faces;
    dot.className = 'cam-id-dot' + (voice.running ? (faces && faces.state === 'down' ? ' warn' : ' known') : '');
    if (!voice.known) line.textContent = 'checking…';
    else if (!voice.running) line.textContent = 'Voice process not running (python -m voice.run_phase0)';
    else if (faces && faces.state === 'down') line.textContent = 'Voice running · camera link down: ' + (faces.reason || '');
    else if (faces) line.textContent = `Voice running · camera link ok · greeted ${faces.greeted || 0} so far`;
    else line.textContent = 'Voice running · camera link disabled (--no-faces)';
    const can = voice.running && (!faces || faces.state !== 'down');
    const why = can ? '' : 'The voice process is not running or has no camera link.';
    $('cam-say-who').disabled = !can; $('cam-say-who').title = why || 'Ask the rover to say who it can see right now.';
    for (const b of document.querySelectorAll('#cam-roster .cam-call')) {
      b.disabled = !can; b.title = why || `Ask the rover to call ${b.dataset.name}`;
    }
  }
  async function pollVoice() {
    if (!visible()) return;
    try {
      const r = await fetch('/api/voice/status', { cache: 'no-store', headers: Auth.headers() });
      if (r.ok) { const v = await r.json(); voice = { ...v, running: !!v.running, known: true }; }
    } catch (e) { voice = { running: false, known: true }; }
    updateVoiceUi();
    return voice;
  }

  // Ask the rover to do something, then report what it actually did: the
  // result lives in voice_status.json's `command`, which signed-in readers see.
  async function askRover(body, what) {
    if (!await requireAuth('Sign in to ask the rover')) return;
    const r = await authFetch('/api/voice/command', body);
    const j = await r.json().catch(() => ({}));
    if (r.status !== 202) {
      if (r.status !== 401 && r.status !== 429) toast(j.error || `Could not ask the rover to ${what}`, 'err');
      return;
    }
    toast(`Asked the rover to ${what}…`, 'info', 2500);
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 700));
      const v = await pollVoice();
      const c = v && v.command;
      if (c && c.id === j.id && c.state !== 'running' && c.state !== 'idle') {
        toast(c.state === 'done' ? (c.detail || 'Done') : `Rover: ${c.state}${c.detail ? ' — ' + c.detail : ''}`,
              c.state === 'done' ? 'info' : 'warn', 6000);
        return;
      }
    }
  }
  $('cam-say-who').addEventListener('click', () => askRover({ verb: 'announce_who' }, "say who it can see"));
  function callPerson(name) { return askRover({ verb: 'call_person', name }, `call ${name}`); }

  // ── poll loop ─────────────────────────────────────────────────────
  async function tick() {
    if (!visible() || inflight) return;
    inflight = true;
    try {
      if (!settings) await loadSettings();
      if (rosterCount < 0) await loadRoster();
      const r = await fetch('/cam/detections', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      last = await r.json();
      if (last.people !== rosterCount) loadRoster();            // enrolled/removed elsewhere (CLI, other browser)
      draw(last); renderFaces(last); renderEvents(last); renderEnroll(last); renderModels(last);
    } catch (e) {
      last = null; draw(null);
    } finally { inflight = false; }
  }
  setInterval(tick, 250);
  setInterval(pollVoice, 5000);
  new MutationObserver(() => { if (visible()) pollVoice(); }).observe(tab, { attributes: true, attributeFilter: ['hidden'] });
  new MutationObserver(() => { if (visible()) { fitCanvas(); tick(); } else { last = null; draw(null); } })
    .observe(tab, { attributes: true, attributeFilter: ['hidden'] });
  const devEl = $('cam-device-name');
  setInterval(() => { const d = $('cam-dev'); if (devEl && d && d.textContent !== '—') devEl.textContent = d.textContent; }, 1000);
})();
