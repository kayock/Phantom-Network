/* ============================================================
   PHANTOM NETWORK — state.js
   Client-side global state: self identity, peer registry cache,
   config, device helpers.  Views read from here; app.js keeps it
   refreshed (peers poll every 5 s + SSE-triggered refreshes).
   ============================================================ */

import { api } from './api.js';

const listeners = {};

function emit(topic) {
  (listeners[topic] || []).forEach(fn => {
    try { fn(); } catch (e) { console.error('state listener', e); }
  });
}

export const state = {
  self: null,
  peers: [],            // array of peer dicts from /api/peers
  config: null,
  favorites: [],        // [{device, path, label}]
  chatUnread: {},
  eventsSeen: 0,

  token() {
    try { return localStorage.getItem('pn_token') || ''; } catch { return ''; }
  },
  setToken(t) {
    try { t ? localStorage.setItem('pn_token', t) : localStorage.removeItem('pn_token'); } catch {}
  },

  on(topic, fn) {
    (listeners[topic] = listeners[topic] || []).push(fn);
    return () => { listeners[topic] = listeners[topic].filter(f => f !== fn); };
  },

  peerById(id) {
    return this.peers.find(p => p.device_id === id) || null;
  },

  displayName(idOrPeer) {
    if (!idOrPeer) return '?';
    if (typeof idOrPeer === 'string') {
      if (this.self && idOrPeer === this.self.device_id) return this.self.name + ' (this device)';
      const p = this.peerById(idOrPeer);
      return p ? p.display_name : idOrPeer;
    }
    return idOrPeer.display_name || idOrPeer.name;
  },

  devices() {
    /* Every browsable device: self first, then online participating peers. */
    const selfEntry = this.self ? {
      device_id: 'self', display_name: this.self.name + ' (this device)',
      name: this.self.name, online: true, self: true,
      os: this.self.os, addresses: this.self.addresses,
    } : null;
    const peers = this.peers
      .filter(p => p.participating && p.status === 'online')
      .map(p => ({ ...p, online: p.status === 'online', device_id: p.device_id }));
    return selfEntry ? [selfEntry, ...peers] : peers;
  },

  isSelf(id) { return !id || id === 'self' || id === this.self?.device_id; },

  async loadSelf() {
    this.self = await api.identity();
    emit('self');
  },

  async loadPeers() {
    const data = await api.peers();
    this.peers = data.peers || [];
    emit('peers');
  },

  async loadConfig() {
    this.config = (await api.config()).config;
    try {
      this.favorites = JSON.parse(localStorage.getItem('pn_favorites') || '[]');
    } catch { this.favorites = []; }
    emit('config');
  },

  saveFavorites() {
    try { localStorage.setItem('pn_favorites', JSON.stringify(this.favorites)); } catch {}
    emit('favorites');
  },

  toggleFavorite(device, path, label) {
    const i = this.favorites.findIndex(f => f.device === device && f.path === path);
    if (i >= 0) this.favorites.splice(i, 1);
    else this.favorites.push({ device, path, label: label || path.split(/[\\/]/).pop() || path });
    this.saveFavorites();
  },

  isFavorite(device, path) {
    return this.favorites.some(f => f.device === device && f.path === path);
  },

  /* persistent UI prefs (pane devices, dirs, hidden toggles...) */
  uiPrefs: {},
  loadPrefs() {
    try { this.uiPrefs = JSON.parse(localStorage.getItem('pn_prefs') || '{}'); }
    catch { this.uiPrefs = {}; }
    return this.uiPrefs;
  },
  savePrefs() {
    try { localStorage.setItem('pn_prefs', JSON.stringify(this.uiPrefs)); } catch {}
  },
};
