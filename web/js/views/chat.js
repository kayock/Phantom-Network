/* PHANTOM NETWORK — chat.js (GhostChat) */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, promptDialog, confirmDialog, fmtTime, fmtSize,
  errText, emptyNote, debounce,
} from '../ui.js';
import { updateChatBadge } from '../app.js';

export async function render(container) {
  const prefs = state.loadPrefs();
  let convs = [];
  let current = prefs.chat_conv || '';
  let messages = [];
  let searchQ = '';

  const grid = el('div', { class: 'chat-grid' });
  const convList = el('div', { class: 'conv-list' });
  const chatMain = el('div', { class: 'chat-main' });
  grid.appendChild(convList);
  grid.appendChild(chatMain);
  container.appendChild(grid);

  const convHead = el('div', { class: 'spread', style: { padding: '10px 14px', borderBottom: '1px solid var(--line-soft)' } });
  const msgs = el('div', { class: 'chat-msgs' });
  const composer = el('div', { class: 'chat-input' });
  chatMain.appendChild(convHead);
  chatMain.appendChild(msgs);
  chatMain.appendChild(composer);

  if ('Notification' in window && Notification.permission === 'default') {
    const note = el('div', { class: 'note' },
      'Desktop notifications are optional. ',
      el('button', { class: 'btn small cyan', onclick: () => Notification.requestPermission() }, 'Enable notifications'));
    container.insertBefore(note, grid);
  }

  /* ---------- conversations ---------- */
  async function refreshConvs() {
    try { convs = (await api.chatConversations()).conversations || []; }
    catch { convs = []; }
    drawConvs();
  }

  function drawConvs() {
    convList.innerHTML = '';
    const head = el('div', { style: { padding: '10px 12px', borderBottom: '1px solid var(--line-soft)' } },
      el('div', { class: 'spread' },
        el('span', { style: { fontWeight: '700', letterSpacing: '.1em', fontSize: '12px', color: 'var(--violet-hi)' } }, 'CONVERSATIONS'),
        el('div', { class: 'row', style: { gap: '4px' } },
          el('button', { class: 'btn small', title: 'New direct message', onclick: newDirect }, '+'),
          el('button', { class: 'btn small', title: 'New group', onclick: newGroup }, 'Group'),
          el('button', { class: 'btn small', title: 'Broadcast to every online device', onclick: () => { current = 'broadcast:all'; drawConvs(); loadHistory(); } }, '📻'))));
    convList.appendChild(head);

    const onlineDevices = state.devices().filter(d => !d.self);
    if (!convs.length && !onlineDevices.length) {
      convList.appendChild(emptyNote('No conversations yet. Add a device in the Radar first.'));
    }
    // broadcast entry always available
    convList.appendChild(convItem({
      conv: 'broadcast:all', kind: 'broadcast', title: 'Broadcast (all devices)',
      last_ts: 0, unread: 0, last_body: 'announcement to every online device',
    }));
    for (const c of convs) convList.appendChild(convItem(c));
    for (const d of onlineDevices) {
      const key = 'direct:' + d.device_id;
      if (!convs.find(c => c.conv === key)) {
        convList.appendChild(convItem({ conv: key, kind: 'direct', title: d.display_name,
          online: true, last_ts: 0, unread: 0, last_body: 'start chatting' }));
      }
    }
  }

  function convItem(c) {
    const unread = state.chatUnread[c.conv] || 0;
    return el('div', {
      class: 'conv-item' + (current === c.conv ? ' active' : ''),
      onclick: () => { current = c.conv; state.chatUnread[c.conv] = 0; updateChatBadge(); state.savePrefs(); prefs.chat_conv = current; drawConvs(); loadHistory(); },
    },
      el('div', { class: 'conv-title' },
        el('span', {}, c.title || c.conv),
        unread ? el('span', { class: 'unread-dot' }, String(unread)) : null),
      el('div', { class: 'conv-last' },
        c.kind === 'direct' && c.online ? '● ' : '',
        c.last_body || '', ' ', c.last_ts ? el('span', { class: 'faint' }, fmtTime(c.last_ts)) : ''));
  }

  async function newDirect() {
    const devices = state.devices().filter(d => !d.self);
    if (!devices.length) { toast('No online devices — discover companions first', 'warn'); return; }
    const res = await promptDialog('New direct message', [{
      name: 'to', label: 'Device: ' + devices.map(d => d.display_name).join(', '), placeholder: 'type device name…',
    }], 'Open');
    if (!res?.to) return;
    const d = devices.find(x => x.display_name.toLowerCase().includes(res.to.toLowerCase()));
    if (!d) { toast('No device matches', 'warn'); return; }
    current = 'direct:' + d.device_id;
    drawConvs(); loadHistory();
  }

  async function newGroup() {
    const devices = state.devices().filter(d => !d.self);
    if (!devices.length) { toast('No online devices yet', 'warn'); return; }
    const res = await promptDialog('New group', [
      { name: 'name', label: 'Group name', placeholder: 'ops-team' },
      { name: 'members', label: 'Members (comma-separated device names)', placeholder: devices.map(d => d.display_name).join(', ') },
    ], 'Create');
    if (!res?.name) return;
    const wanted = res.members.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    const members = devices.filter(d => wanted.some(w => d.display_name.toLowerCase().includes(w)))
      .map(d => d.device_id);
    try {
      await api.createGroup(res.name, members);
      toast(`Group "${res.name}" created with ${members.length} member(s)`, 'ok');
      refreshConvs();
    } catch (e) { toast(errText(e), 'err'); }
  }

  /* ---------- history ---------- */
  async function loadHistory() {
    msgs.innerHTML = '';
    msgs.appendChild(emptyNote('Loading…'));
    try {
      const params = new URLSearchParams({ conv: current, q: searchQ, limit: '300' });
      messages = (await api.chatHistory('?' + params.toString())).messages || [];
    } catch (e) {
      messages = [];
      msgs.innerHTML = '';
      msgs.appendChild(emptyNote('Cannot load history: ' + errText(e)));
      return;
    }
    paintMessages();
    // mark read
    const lastTs = messages.length ? messages[messages.length - 1].ts : 0;
    if (lastTs && current.startsWith('direct:')) {
      api.chatRead(current, lastTs).catch(() => {});
    }
  }

  function paintMessages() {
    msgs.innerHTML = '';
    if (!messages.length) {
      msgs.appendChild(emptyNote(current.startsWith('broadcast')
        ? 'Broadcast channel — every online device receives these.'
        : 'No messages yet. Say hello.'));
    }
    for (const m of messages) msgs.appendChild(msgEl(m));
    msgs.scrollTop = msgs.scrollHeight;
  }

  function msgEl(m) {
    const mine = !!m.outgoing;
    if (m.system) {
      return el('div', { class: 'msg sysmsg' }, m.body);
    }
    const status = mine ? el('span', {
      class: `m-status ${m.status}`,
      title: { sent: 'sent (waiting for delivery)', delivered: 'delivered', read: 'read', queued: 'queued — peer offline, will retry', failed: 'failed' }[m.status] || m.status,
    }, { sent: '✓', delivered: '✓✓', read: '✓✓ read', queued: '⏳', failed: '✗' }[m.status] || '') : null;
    const node = el('div', { class: `msg ${mine ? 'out' : 'in'}` },
      !mine && current !== 'broadcast:all' && m.to_kind === 'broadcast'
        ? el('div', { class: 'faint small' }, m.from?.name) : null,
      el('div', {}, m.body || ''),
      m.attachment ? el('div', {
        class: 'attach',
        onclick: () => openAttachment(m.attachment),
      }, '📎 ', m.attachment.name, el('span', { class: 'faint' }, ` ${fmtSize(m.attachment.size || 0)}`)) : null,
      el('div', { class: 'm-meta' },
        el('span', {}, fmtTime(m.ts)),
        status));
    node.addEventListener('dblclick', () => {
      navigator.clipboard?.writeText(m.body || '');
      toast('Message copied', 'ok', 1500);
    });
    return node;
  }

  function openAttachment(att) {
    modal('Attachment', (body) => {
      body.appendChild(el('div', { class: 'kv' },
        el('div', { class: 'k' }, 'File'), el('div', { class: 'v' }, att.name || '?'),
        el('div', { class: 'k' }, 'Size'), el('div', { class: 'v' }, fmtSize(att.size || 0)),
        el('div', { class: 'k' }, 'Source path'), el('div', { class: 'v' }, att.path || '?')));
      body.appendChild(el('div', { class: 'dim small mt' },
        'Attachments are transferred with the standard engine (SHA-256 verified). ' +
        'Check the Transfer Center for progress.'));
      body.appendChild(el('div', { class: 'modal-actions' },
        el('button', {
          class: 'btn primary',
          onclick: async (close) => {
            try {
              // receive the file on THIS companion: pull job from sender's base
              const spec = {
                kind: 'pull', src_base: att.src_base, src_path: att.path,
                dest_path: '~/' + (att.name || 'attachment.bin'), conflict: 'rename',
                src_device: att.from_name || 'peer', dest_device: state.self?.name,
              };
              await api.createJob('', spec);
              toast('Download job queued (goes to your home share)', 'ok');
            } catch (e) { toast(errText(e), 'err'); }
          },
        }, '⇩ Receive to my home folder'),
        el('button', { class: 'btn', onclick: (c) => c() }, 'Close')));
    });
  }

  /* ---------- composer ---------- */
  function buildComposer() {
    composer.innerHTML = '';
    const input = el('textarea', { placeholder: current.startsWith('broadcast')
      ? 'Broadcast announcement…' : 'Message — Enter to send, Shift+Enter for newline' });
    const send = async () => {
      const body = input.value.trim();
      if (!body) return;
      const kind = current.startsWith('broadcast') ? 'broadcast'
        : current.startsWith('group:') ? 'group' : 'direct';
      const toId = current.split(':').slice(1).join(':');
      input.value = '';
      try {
        const res = await api.chatSend({ to_kind: kind, to_id: toId, body });
        if (res.message?.status === 'queued') toast('Peer offline — message queued for delivery', 'warn');
      } catch (e) { toast(errText(e), 'err'); input.value = body; }
      setTimeout(loadHistory, 250);
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    const attach = el('button', { class: 'btn', title: 'Attach file (uses transfer engine)', onclick: attachFile }, '📎');
    const search = el('input', { placeholder: 'search history…', style: { maxWidth: '150px' } });
    search.addEventListener('input', debounce(() => { searchQ = search.value.trim(); loadHistory(); }, 400));
    composer.appendChild(input);
    composer.appendChild(attach);
    composer.appendChild(search);
    composer.appendChild(el('button', { class: 'btn primary', onclick: send }, 'Send ▸'));

    async function attachFile() {
      const devices = state.devices();
      const targetDev = current.startsWith('direct:') ? current.split(':')[1] : null;
      if (!targetDev) { toast('Attach works in direct conversations', 'warn'); return; }
      const input = el('input', { type: 'file', style: { display: 'none' } });
      document.body.appendChild(input);
      input.addEventListener('change', async () => {
        input.remove();
        const file = input.files[0];
        if (!file) return;
        // The peer pulls the file from US: create job on peer, then send message.
        try {
          const peer = state.peerById(targetDev);
          if (!peer || peer.status !== 'online') { toast('Peer offline', 'err'); return; }
          const spec = {
            kind: 'pull', src_base: '',            // unused in this flow
            src_path: '', dest_path: file.name, conflict: 'rename',
          };
          // upload the file to the peer companion first (browser → peer)
          toast(`Uploading ${file.name} to ${peer.display_name}…`);
          const fin = await api.uploadFile(targetDev, '~', file,
            (done, total) => { /* progress via toast updates */ });
          const msg = await api.chatSend({
            to_kind: 'direct', to_id: targetDev, body: `[file shared] ${file.name}`,
            attachment: { name: file.name, size: file.size, path: fin.target,
                          src_base: `http://${peer.addresses[0]}:${peer.api_port}`,
                          from_name: state.self?.name },
          });
          toast('Attachment sent', 'ok');
          setTimeout(loadHistory, 250);
        } catch (e) { toast('Attach failed: ' + errText(e), 'err', 6000); }
      });
      input.click();
    }
  }

  /* ---------- header ---------- */
  function paintHead() {
    convHead.innerHTML = '';
    let title = 'Select a conversation';
    let sub = '';
    if (current === 'broadcast:all') { title = '📻 Broadcast'; sub = 'announcement to every online participating device'; }
    else if (current.startsWith('direct:')) {
      const id = current.split(':')[1];
      const peer = state.peerById(id);
      title = '◌ ' + state.displayName(id);
      sub = peer ? `${peer.status === 'online' ? '● online' : '○ offline'} · ${peer.addresses?.[0] || ''} · ${peer.os || ''}` : 'unknown device';
    } else if (current.startsWith('group:')) {
      title = '👥 ' + (current.split(':')[1] || 'group');
    }
    convHead.appendChild(el('div', {},
      el('div', { style: { fontWeight: '700' } }, title),
      el('div', { class: 'dim small' }, sub)));
  }

  const onChat = () => { refreshConvs(); if (current) loadHistory(); };
  window.addEventListener('pn-chat-any', onChat);
  window.addEventListener('pn-chat', onChat);

  buildComposer();
  paintHead();
  await refreshConvs();
  if (!current) current = 'broadcast:all';
  drawConvs();
  await loadHistory();

  return () => {
    window.removeEventListener('pn-chat-any', onChat);
    window.removeEventListener('pn-chat', onChat);
  };
}
