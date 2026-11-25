/* ============================================================
   PHANTOM NETWORK — broadcast.js (v1.4.0 Phantom Broadcast)

   Household announcement center:
   * compose: text + tone profile + optional spoken text (local TTS)
   * targets: computers, hosted phones, saved groups, everyone
   * profiles: 7 built-ins + user-defined, quick announce buttons
   * quiet hours: shown honestly; emergency override requires the
     urgency field — the companion still respects device participation
   * schedules: HH:MM daily or weekday-specific repeating announcements
   * history: per-target delivery states + acknowledgments
   ============================================================ */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtTime } from '../ui.js';

let cleanupFns = [];
function cleanup() { cleanupFns.forEach(f => { try { f(); } catch {} }); cleanupFns = []; }

export async function render(container) {
  cleanup();
  let profiles = [];
  let groups = [];
  let history = [];
  let schedules = [];
  let quietInfo = { quiet_hours: '' };

  /* ---------------- controls ---------------- */
  const textIn = el('textarea', { rows: '3', placeholder:
    'Announcement text…', style: { width: '100%' } });
  const speakIn = el('input', { placeholder:
    'Spoken text (local system voice only — optional)' });
  const toneSel = el('select', {}, el('option', { value: '' }, 'No tone'));
  const urgSel = el('select', {},
    el('option', { value: 'normal' }, 'Normal'),
    el('option', { value: 'attention' }, 'Attention'),
    el('option', { value: 'emergency' }, 'Emergency (overrides quiet hours — use sparingly)'));
  const ackChk = el('input', { type: 'checkbox' });
  const targetList = el('div', { class: 'row', style: { flexWrap: 'wrap',
    gap: '6px', maxHeight: '160px', overflow: 'auto' } });
  const groupSel = el('select', {}, el('option', { value: '' }, '— groups —'));
  const sendBtn = el('button', { class: 'btn violet' }, '📣 Announce');
  const profRow = el('div', { class: 'row', style: { flexWrap: 'wrap',
    gap: '8px' } });
  const newProfName = el('input', { placeholder: 'profile name',
    style: { maxWidth: '160px' } });
  const newProfTone = toneSel.cloneNode(true);
  const newProfVoice = el('input', { placeholder: 'spoken text',
    style: { maxWidth: '200px' } });
  const addProfBtn = el('button', { class: 'btn' }, '＋ Save profile');
  const schedTime = el('input', { placeholder: 'HH:MM', style: { maxWidth: '90px' } });
  const schedText = el('input', { placeholder: 'announcement text',
    style: { maxWidth: '220px' } });
  const schedDays = el('input', { placeholder: 'days: mon,wed or blank=daily',
    style: { maxWidth: '190px' } });
  const addSchedBtn = el('button', { class: 'btn' }, '＋ Schedule');
  const schedList = el('div', {});
  const histList = el('div', {});
  const quietNote = el('div', { class: 'small dim' }, '');

  const selected = new Set();   // target keys

  /* ---------------- data ---------------- */
  async function loadAll() {
    try {
      const [p, g, h, s] = await Promise.all([
        api.get('', '/api/broadcast/profiles'),
        api.get('', '/api/broadcast/groups'),
        api.get('', '/api/broadcast/history?limit=60'),
        api.get('', '/api/broadcast/schedules'),
      ]);
      profiles = p.profiles || []; groups = g.groups || [];
      history = h.history || []; schedules = s.schedules || [];
      quietInfo.quiet_hours = p.quiet_hours || '';
      render();
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function loadTargets() {
    try {
      const [peers, mob] = await Promise.all([
        api.peers(),
        api.get('', '/api/mobile/clients').catch(() => ({ clients: [] })),
      ]);
      state.peers = (peers && peers.peers) || state.peers;
      window.__pn_mobile_clients = mob.clients || [];
      renderTargets();
    } catch {}
  }

  function targetDefs() {
    const me = state.self && state.self.device_id;
    const out = [];
    for (const p of state.peers) {
      if (p.device_id === me || p.status !== 'online') continue;
      out.push({ key: 'c:' + p.device_id, kind: 'computer',
                 device_id: p.device_id, label: '💻 ' + p.display_name });
    }
    out.push({ key: 'c:me', kind: 'computer', device_id: me,
               label: '💻 This device' });
    for (const c of window.__pn_mobile_clients || []) {
      out.push({ key: 'm:' + c.client_id, kind: 'mobile',
                 client_id: c.client_id, host_device_id: c.host_device_id,
                 label: '📱 ' + c.name });
    }
    return out;
  }

  function renderTargets() {
    targetList.innerHTML = '';
    for (const t of targetDefs()) {
      const chip = el('button', {
        class: 'chip' + (selected.has(t.key) ? ' sel' : ''),
        onclick: () => {
          selected.has(t.key) ? selected.delete(t.key) : selected.add(t.key);
          renderTargets();
        },
      }, t.label);
      targetList.appendChild(chip);
    }
  }

  function renderProfiles() {
    profRow.innerHTML = '';
    for (const p of profiles) {
      profRow.appendChild(el('button', { class: 'btn small-btn',
        onclick: () => {
          textIn.value = textIn.value || p.voice || '';
          speakIn.value = p.voice || '';
          toneSel.value = p.tone || '';
          toast(`Profile "${p.name}" loaded — pick targets and announce`,
                'info');
        } }, `${p.urgency === 'attention' ? '⚠ ' : ''}${p.name}`));
    }
  }

  function renderGroups() {
    groupSel.innerHTML = '';
    groupSel.appendChild(el('option', { value: '' }, '— groups —'));
    for (const g of groups) {
      groupSel.appendChild(el('option', { value: g.id }, g.name));
    }
  }

  function renderSchedules() {
    schedList.innerHTML = '';
    if (!schedules.length) {
      schedList.appendChild(el('span', { class: 'dim small' },
        'No scheduled announcements.'));
      return;
    }
    for (const s of schedules) {
      schedList.appendChild(el('div', { class: 'row small',
        style: { gap: '10px', borderBottom: '1px solid #221c2b',
                 padding: '4px 0' } },
        el('b', { class: 'mono' }, s.time),
        el('span', { class: 'dim' }, (s.days || []).join(',')
          || 'daily'), el('span', {}, s.text || s.tone || ''),
        el('button', { class: 'btn danger small-btn', onclick: async () => {
          try {
            await api.del('', '/api/broadcast/schedule/' + s.id);
            await loadAll();
          } catch (e) { toast(errText(e), 'err'); }
        } }, '✕')));
    }
  }

  function renderHistory() {
    histList.innerHTML = '';
    if (!history.length) {
      histList.appendChild(el('span', { class: 'dim small' },
        'No announcements yet.'));
      return;
    }
    for (const h of history.slice(0, 30)) {
      const okCount = (h.results || []).filter(r =>
        r.state.startsWith('delivered')).length;
      const who = (h.targets || []).length;
      histList.appendChild(el('div', { style: { borderBottom:
        '1px solid #221c2b', padding: '6px 0' } },
        el('div', { class: 'row', style: { gap: '10px',
          alignItems: 'baseline' } },
          el('b', {}, h.text ? (h.text.length > 60 ? h.text.slice(0, 60) +
            '…' : h.text) : `[tone: ${h.tone}]`),
          el('span', { class: 'small mono dim' }, fmtTime(h.ts)),
          el('span', { class: 'small' },
            `${okCount}/${who} delivered`),
          h.quiet ? el('span', { class: 'small', style: { color:
            'var(--warn)' } }, 'quiet hours: sound suppressed') : null,
          h.ack ? el('span', { class: 'small' },
            `acks: ${(h.acknowledged || []).length}`) : null)));
    }
  }

  function renderQuiet() {
    quietNote.textContent = quietInfo.quiet_hours
      ? `Quiet hours active window: ${quietInfo.quiet_hours} — tones and ` +
        'spoken audio are suppressed during this window (visual delivery ' +
        'only). Emergency urgency overrides this only for the sender ' +
        'device; silenced devices stay silent.'
      : 'No quiet-hours window configured (Settings).';
  }

  function render() {
    renderTargets(); renderProfiles(); renderGroups();
    renderSchedules(); renderHistory(); renderQuiet();
  }

  /* ---------------- actions ---------------- */
  async function send(kind) {
    const targets = targetDefs().filter(t => selected.has(t.key))
      .map(({ key, ...rest }) => rest);
    if (!targets.length) { toast('Select at least one target', 'err'); return; }
    const body = {
      text: textIn.value.trim(), tone: toneSel.value,
      voice: speakIn.value.trim(), urgency: urgSel.value,
      ack: ackChk.checked, targets,
    };
    if (!body.text && !body.tone && !body.voice) {
      toast('Type text, pick a tone or spoken text', 'err'); return;
    }
    sendBtn.disabled = true;
    try {
      const r = await api.post('', '/api/broadcast/announce', body);
      const bad = (r.announcement.results || []).filter(x =>
        !x.state.startsWith('delivered')).length;
      toast(`Announced to ${r.announcement.results.length - bad} device(s)` +
        (bad ? ` — ${bad} unavailable (shown honestly in history)` : ''),
        bad ? 'info' : 'ok');
      await loadAll();
    } catch (e) { toast(errText(e), 'err'); }
    sendBtn.disabled = false;
  }

  sendBtn.onclick = () => send('announce');
  groupSel.onchange = () => {
    const g = groups.find(x => x.id === groupSel.value);
    if (!g) return;
    selected.clear();
    for (const m of g.members || []) {
      selected.add(m.startsWith('mob_') ? 'm:' + m : 'c:' + m);
    }
    if (!selected.size) toast('Group has no members I can resolve here',
      'info');
    renderTargets();
  };

  addProfBtn.onclick = async () => {
    if (!newProfName.value.trim()) { toast('Profile name required', 'err'); return; }
    try {
      await api.post('', '/api/broadcast/profile', {
        name: newProfName.value.trim(), tone: newProfTone.value,
        voice: newProfVoice.value.trim(),
      });
      newProfName.value = newProfVoice.value = '';
      await loadAll();
      toast('Profile saved', 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  };

  addSchedBtn.onclick = async () => {
    const targets = targetDefs().filter(t => selected.has(t.key))
      .map(({ key, ...rest }) => rest);
    if (!targets.length) { toast('Select targets for the schedule', 'err'); return; }
    try {
      await api.post('', '/api/broadcast/schedule', {
        time: schedTime.value.trim(), text: schedText.value.trim(),
        days: schedDays.value.split(',').map(s => s.trim()).filter(Boolean),
        targets,
      });
      schedTime.value = schedText.value = schedDays.value = '';
      await loadAll();
      toast('Schedule saved — fires at local time', 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  };

  /* ---------------- layout ---------------- */
  container.append(
    el('div', { class: 'card' },
      el('h2', {}, '📣 Phantom Broadcast'),
      el('p', { class: 'dim small' },
        'Household announcements: text banners, notification tones and ' +
        'LOCAL system-voice speech (no cloud TTS). Delivery respects ' +
        'each device\u2019s participation settings and quiet hours — ' +
        'nothing is overridden automatically.'),
      quietNote,
      el('div', { style: { marginTop: '8px' } }, textIn),
      el('div', { class: 'row', style: { marginTop: '6px', gap: '8px',
        flexWrap: 'wrap' } },
        toneSel, speakIn, urgSel,
        el('label', { class: 'small' }, ' ask for acknowledgment ', ackChk)),
      el('div', { class: 'row', style: { marginTop: '6px', gap: '8px',
        flexWrap: 'wrap' } }, groupSel, sendBtn),
      el('h3', { style: { marginTop: '14px' } }, 'Targets'),
      targetList,
      el('h3', { style: { marginTop: '14px' } }, 'Profiles'),
      profRow,
      el('div', { class: 'row', style: { marginTop: '8px', gap: '8px',
        flexWrap: 'wrap' } }, newProfName, newProfTone, newProfVoice,
        addProfBtn),
      el('h3', { style: { marginTop: '14px' } }, 'Schedules'),
      schedList,
      el('div', { class: 'row', style: { marginTop: '8px', gap: '8px',
        flexWrap: 'wrap' } }, schedTime, schedText, schedDays, addSchedBtn)),
    el('div', { class: 'card' },
      el('h3', {}, 'Announcement history'),
      histList));

  /* populate tones */
  const TONES = [['', 'No tone'], ['chime', 'chime'], ['beacon', 'beacon'],
    ['soft', 'soft'], ['success', 'success'], ['alarm', 'alarm']];
  for (const sel of [toneSel, newProfTone]) {
    sel.innerHTML = '';
    for (const [v, label] of TONES) {
      sel.appendChild(el('option', { value: v }, label));
    }
  }

  await Promise.all([loadAll(), loadTargets()]);
  const refresh = setInterval(() => { loadAll(); }, 15000);
  cleanupFns.push(() => clearInterval(refresh));
}
