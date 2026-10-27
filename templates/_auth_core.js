// Shared sign-in session (localStorage 'agv-auth'), used by every page that
// talks to the server: dashboard.html includes it, review.html includes it.
// One copy, so a change to the token rules cannot land on one page and not the
// other — which is exactly what happened while dashboard.html kept its own.
//
// Optional hooks, set by the including page:
//   Auth.onChange = () => {...}  called after every session change
//   Auth.onLost   = (msg) => {}  called when the server ends the session
const AUTH_KEY = 'agv-auth';
const Auth = {
  data: null,                                   // {access, refresh, accessExp, refreshExp} (ms epoch)
  onChange: null,
  onLost: null,
  _changed() { try { if (typeof this.onChange === 'function') this.onChange(); } catch (e) {} },
  _lost(msg) { try { if (typeof this.onLost === 'function') this.onLost(msg); } catch (e) {} },
  load() {
    try { const d = JSON.parse(localStorage.getItem(AUTH_KEY) || 'null'); this.data = d && d.refresh ? d : null; }
    catch (e) { this.data = null; }
    if (this.data && Date.now() >= this.data.refreshExp) this.data = null;
  },
  save() {
    try { this.data ? localStorage.setItem(AUTH_KEY, JSON.stringify(this.data)) : localStorage.removeItem(AUTH_KEY); }
    catch (e) { /* private mode: session lives until reload */ }
  },
  clear() { this.data = null; this.save(); this._changed(); },
  accessValid(margin = 0) { return !!this.data && Date.now() < this.data.accessExp - margin; },
  refreshValid() { return !!this.data && Date.now() < this.data.refreshExp; },
  headers() { return this.accessValid() ? { Authorization: 'Bearer ' + this.data.access } : {}; },
  async refresh() {
    if (!this.refreshValid()) { this.clear(); return false; }
    try {
      const r = await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: this.data.refresh }) });
      if (r.status === 401) { this.clear(); this._lost('Session ended - sign in again'); return false; }
      if (!r.ok) return this.accessValid();           // server hiccup: keep what we have
      const j = await r.json(), now = Date.now();
      this.data.access = j.access_token;
      this.data.accessExp = now + j.access_expires_in * 1000;
      // refreshExp stays as issued at sign-in: using the token never extends it
      this.save(); this._changed();
      return true;
    } catch (e) { return this.accessValid(); }
  },
  async ensure() {
    if (this.accessValid(20000)) return true;
    if (this.refreshValid()) return (await this.refresh()) && this.accessValid();
    return false;
  },
  async login(password) {
    const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, status: r.status, error: j.error || 'sign-in failed' };
    const now = Date.now();
    this.data = { access: j.access_token, refresh: j.refresh_token,
                  accessExp: now + j.access_expires_in * 1000, refreshExp: now + j.refresh_expires_in * 1000 };
    this.save(); this._changed();
    return { ok: true };
  },
  async logout() {
    const rt = this.data && this.data.refresh;
    this.clear();
    try { await fetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                       body: JSON.stringify({ refresh_token: rt }) }); } catch (e) {}
  },
};
Auth.load();
window.addEventListener('storage', (e) => { if (e.key === AUTH_KEY) { Auth.load(); Auth._changed(); } });
