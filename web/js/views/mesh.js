/* ============================================================
   PHANTOM NETWORK — mesh.js (v1.4.0 Phantom Mesh diagnostics)

   Mesh health and resilience surface:
   * status: failover + mDNS flags, tracked exchange peers, claims
   * recovery: per-peer backoff failures + last errors (honest)
   * discovery sources recap: UDP beacons, manual, exchange (probe
     validated), optional mDNS
   * audit trail: mesh events (expiry, reconciliation, failover) from
     the event log
   ============================================================ */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtTime } from '../ui.js';

let cleanupFns = [];
function cleanup() { cleanupFns.forEach(f => { try { f(); } catch {} }); cleanupFns = []; }

export async function render(container) {
  cleanup();
  const statusEl = el('div', {});
  const recoverEl = el('div', {});
  const claimsEl = el('div', {});
  const auditEl = el('div', {});
  const srcEl = el('div', { class: 'row', style: { flexWrap: 'wrap',
    gap: '8px' } });

  async function load() {
    try {
      const st = await api.get('', '/api/mesh/status');
      renderStatus(st);
      renderRecovery(st.recovery || {});
      renderClaims(st.claims || {});
    } catch (e) {
      statusEl.innerHTML = '';
      statusEl.appendChild(el('span', { style: { color: 'var(--err)' } },
        errText(e)));
    }
    try {
      const ev = await api.get('', '/api/events?limit=100');
      const rows = (ev.events || []).filter(e =>
        (e.topic || e.kind || '').includes('mesh') ||
        (e.text || '').toLowerCase().includes('mesh'));
      auditEl.innerHTML = '';
      if (!rows.length) {
        auditEl.appendChild(el('span', { class: 'dim small' },
          'No mesh audit events yet.'));
      }
      for (const e of rows.slice(0, 25)) {
        auditEl.appendChild(el('div', { class: 'small',
          style: { borderBottom: '1px solid #221c2b', padding: '4px 0' } },
          el('span', { class: 'mono dim' }, fmtTime(e.ts)),
          ' ', e.text || e.event || JSON.stringify(e)));
      }
    } catch {}
  }

  function renderStatus(st) {
    statusEl.innerHTML = '';
    statusEl.append(
      el('div', { class: 'spread' }, el('span', { class: 'dim' },
        'Failover (experimental)'), el('b', {
          style: { color: st.failover_enabled ? 'var(--warn)' : 'var(--ok)' } },
        st.failover_enabled ? 'ENABLED' : 'disabled (default)')),
      el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'mDNS'),
        el('b', { style: { color: st.mdns_enabled ? 'var(--cyan)'
          : 'var(--fg-dim)' } },
        st.mdns_enabled ? 'enabled (probe-validated)' : 'off (UDP + manual active)')),
      el('div', { class: 'spread' }, el('span', { class: 'dim' },
        'Exchange-introduced peers being TTL-tracked'),
        el('b', {}, String(st.exchange_tracked))));
    srcEl.innerHTML = '';
    for (const [name, desc] of [
      ['UDP beacons', 'every 3 s · primary'],
      ['Manual registration', 'user-registered, never auto-removed'],
      ['Peer exchange', 'probe-validated before trust · 30 min TTL'],
      ['Active sweep', 'user-triggered /24 probe'],
      ['mDNS/DNS-SD', st.mdns_enabled ? 'optional · ON' : 'optional · OFF'],
    ]) {
      srcEl.appendChild(el('span', { class: 'chip' }, `◉ ${name} — ${desc}`));
    }
  }

  function renderRecovery(rec) {
    recoverEl.innerHTML = '';
    const ids = Object.keys(rec);
    if (!ids.length) {
      recoverEl.appendChild(el('span', { class: 'dim small' },
        'No peers in recovery — everything answered or nothing known.'));
      return;
    }
    for (const id of ids) {
      const r = rec[id];
      recoverEl.appendChild(el('div', { class: 'small',
        style: { borderBottom: '1px solid #221c2b', padding: '4px 0' } },
        el('b', {}, state.displayName(id)),
        el('span', { class: 'dim mono' }, ` · ${r.fails} failed probes`),
        el('span', { style: { color: 'var(--warn)' } },
          r.last_error ? ` · last error: ${r.last_error}` : '')));
    }
  }

  function renderClaims(claims) {
    claimsEl.innerHTML = '';
    const ids = Object.keys(claims);
    if (!ids.length) {
      claimsEl.appendChild(el('span', { class: 'dim small' },
        'No active failover claims.'));
      return;
    }
    for (const cid of ids) {
      const c = claims[cid];
      claimsEl.appendChild(el('div', { class: 'small',
        style: { borderBottom: '1px solid #221c2b', padding: '4px 0' } },
        el('b', {}, cid),
        el('span', { class: 'dim' },
          ` served by ${c.host_name || c.host} since ${fmtTime(c.ts)}`)));
    }
  }

  container.append(
    el('div', { class: 'card' },
      el('h2', {}, '⌗ Phantom Mesh'),
      el('p', { class: 'dim small' },
        'Resilience layer for the fleet: every peer identity must answer ' +
        'a real probe before it is trusted, exchange-introduced entries ' +
        'expire unless contacted directly, offline peers are re-probed ' +
        'with backoff, duplicate identities are reconciled (newest wins, ' +
        'audit logged), and the experimental mobile failover stays OFF ' +
        'until you enable it. Nothing here edits firewall rules.'),
      statusEl, srcEl),
    el('div', { class: 'card' },
      el('h3', {}, 'Recovery (backoff probe)'),
      recoverEl),
    el('div', { class: 'card' },
      el('h3', {}, 'Failover claims'),
      claimsEl),
    el('div', { class: 'card' },
      el('h3', {}, 'Audit trail'),
      auditEl));

  await load();
  const refresh = setInterval(load, 10000);
  cleanupFns.push(() => clearInterval(refresh));
}
