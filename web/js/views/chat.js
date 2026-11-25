/* PHANTOM NETWORK — chat.js (GhostChat 2.0)
   Conversations with computers AND mobile clients, replies, reactions,
   edits, deletion tombstones, honest delivery ticks, attachments incl.
   voice notes, searchable history. */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, promptDialog, modal, fmtTime, fmtSize,
  errText, emptyNote, debounce,
} from '../ui.js';
import { updateChatBadge } from '../app.js';

export async function render(container) {
  const prefs = state.loadPrefs();
  let convs = [];
  let current = prefs.chat_conv || '';
  let messages = [];
  let searchQ = '';
  let replyTo = null;

  const grid = el('div', { class: 'chat-grid' });
  const convList = el('div', { class: 'conv-list' });
  const chatMain = el('div', { class: 'chat-main' });
  grid.appendChild(convList);
  grid.appendChild(chatMain);
  container.appendChild(grid);

  const convHead = el('div', { class: 'spread', style: { padding: '10px 14px', borderBottom: '1px solid var(--line-soft)' } });
  const replyBar = el('div', { class: 'note hidden', style: { padding: '6px 14px' } });
  const msgs = el('div', { class: 'chat-msgs' });
  const composer = el('div', { class: 'chat-input' });
  chatMain.appendChild(convHead);
  chatMain.appendChild(replyBar);
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

  /* every addressable device: computers, phones hosted here and by peers */
  function addressable() {
    const out = [...state.devices().filter(d => !d.self)];
    for (const p of state.peers) {
      for (const mc of (p.mobile_clients || [])) {
        out.push({
          device_id: mc.client_id, display_name: mc.name || mc.client_id,
          kind: 'mobile', online: mc.online, host_device_id: p.device_id,
        });
      }
    }
    return out;
  }

  function drawConvs() {
    convList.innerHTML = '';
    const head = el('div', { style: { padding: '10px 12px', borderBottom: '1px solid var(--line-soft)' } },
      el('div', { class: 'spread' },
        el('span', { style: { fontWeight: '700', letterSpacing: '.1em', fontSize: '12px', color: 'var(--violet-hi)' } }, 'CONVERSATIONS'),
        el('div', { class: 'row', style: { gap: '4px' } },
          el('button', { class: 'btn small', title: 'New direct message', onclick: newDirect }, '+'),
          el('button', { class: 'btn small', title: 'New group (computers + phones)', onclick: newGroup }, 'Group'),
          el('button', { class: 'btn small', title: 'Broadcast to every online device', onclick: () => { current = 'broadcast:all'; drawConvs(); loadHistory(); paintHead(); } }, '📻'))));
    convList.appendChild(head);

    if (!convs.length) {
      convList.appendChild(emptyNote('No conversations yet. Add a device in the Radar, or register a phone in Settings.'));
    }
    // broadcast entry always available
    convList.appendChild(convItem({
      conv: 'broadcast:all', kind: 'broadcast', title: 'Broadcast (all devices)',
      last_ts: 0, unread: 0, last_body: 'announcement to every online device',
    }));
    for (const c of convs) convList.appendChild(convItem(c));
    // conversation stubs for addressable devices without history
    for (const d of addressable()) {
      const key = (d.kind === 'mobile' ? 'mobile:' : 'direct:') + d.device_id;
      if (!convs.find(c => c.conv === key)) {
        convList.appendChild(convItem({ conv: key, kind: d.kind === 'mobile' ? 'mobile' : 'direct',
          title: d.display_name + (d.kind === 'mobile' ? ' 📱' : ''),
          online: d.online, last_ts: 0, unread: 0, last_body: 'start chatting' }));
      }
    }
  }

  function convItem(c) {
    const unread = state.chatUnread[c.conv] || 0;
    const icon = c.conv === 'broadcast:all' ? '📻'
      : c.conv.startsWith('mobile:') ? '📱'
      : c.conv.startsWith('group:') ? '👥'
      : c.conv.startsWith('system:') ? '🔔' : '◌';
    return el('div', {
      class: 'conv-item' + (current === c.conv ? ' active' : ''),
      onclick: () => { current = c.conv; state.chatUnread[c.conv] = 0; updateChatBadge(); state.savePrefs(); prefs.chat_conv = current; replyTo = null; drawConvs(); loadHistory(); paintHead(); },
    },
      el('div', { class: 'conv-title' },
        el('span', {}, `${icon} ${c.title || c.conv}`),
        unread ? el('span', { class: 'unread-dot' }, String(unread)) : null),
      el('div', { class: 'conv-last' },
        (c.kind === 'direct' || c.kind === 'mobile') && c.online ? '● ' : '',
        c.last_body || '', ' ', c.last_ts ? el('span', { class: 'faint' }, fmtTime(c.last_ts)) : ''));
  }

  async function newDirect() {
    const devices = addressable();
    if (!devices.length) { toast('No online devices — discover companions or register a phone first', 'warn'); return; }
    const res = await promptDialog('New direct message', [{
      name: 'to', label: 'Device: ' + devices.slice(0, 8).map(d => d.display_name).join(', '), placeholder: 'type device name…',
    }], 'Open');
    if (!res?.to) return;
    const d = devices.find(x => x.display_name.toLowerCase().includes(res.to.toLowerCase()));
    if (!d) { toast('No device matches', 'warn'); return; }
    current = (d.kind === 'mobile' ? 'mobile:' : 'direct:') + d.device_id;
    paintHead();
    drawConvs(); loadHistory();
  }

  async function newGroup() {
    const devices = addressable();
    if (!devices.length) { toast('No online devices yet', 'warn'); return; }
    const res = await promptDialog('New group (computers AND phones welcome)', [
      { name: 'name', label: 'Group name', placeholder: 'family' },
      { name: 'members', label: 'Members (comma-separated device names)', placeholder: devices.map(d => d.display_name).join(', ') },
    ], 'Create');
    if (!res?.name) return;
    const wanted = res.members.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    const members = devices.filter(d => wanted.some(w => d.display_name.toLowerCase().includes(w)))
      .map(d => d.device_id);
    try {
      await api.createGroup(res.name, members);
      toast(`Group "${res.name}" created with ${members.length} member(s) — phones receive via their hosts`, 'ok');
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
    // mark read + notify phone conversations of the read receipt
    const lastTs = messages.length ? messages[messages.length - 1].ts : 0;
    if (lastTs && (current.startsWith('direct:') || current.startsWith('mobile:'))) {
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

  function attachmentUrl(m) {
    const att = m.attachment || {};
    if (att.src_base) return null;          // legacy transfer-engine attachment
    if (att.file_id) {
      const host = m.from?.host_device_id || (m.from?.device_id?.startsWith('mob_')
        ? m.from?.host_device_id : null);
      if (!host || (state.self && host === state.self.device_id)) {
        return `/api/chat/file/${att.file_id}`;
      }
      const peer = state.peerById(host);
      if (peer && peer.addresses?.length) {
        return `http://${peer.addresses[0]}:${peer.api_port}/api/chat/file/${att.file_id}`;
      }
    }
    return null;
  }

  function msgEl(m) {
    const mine = !!m.outgoing;
    if (m.deleted) {
      return el('div', { class: `msg ${mine ? 'out' : 'in'} tombstone` },
        el('div', { class: 'faint', style: { fontStyle: 'italic' } }, 'message deleted'));
    }
    const status = mine ? el('span', {
      class: `m-status ${m.status}`,
      title: { sent: 'sent — waiting for the recipient device', delivered: 'delivered to the recipient device', read: 'read', queued: 'queued — peer offline, will retry', failed: 'failed' }[m.status] || m.status,
    }, { sent: '✓', delivered: '✓✓', read: '✓✓ read', queued: '⏳', failed: '✗' }[m.status] || '') : null;

    const reacts = m.reactions && Object.keys(m.reactions).length
      ? el('div', { class: 'm-reacts' },
          ...Object.entries(m.reactions).filter(([, who]) => who.length)
            .map(([emo, who]) => el('span', { class: 'pill', title: who.join(', ') }, `${emo} ${who.length}`)))
      : null;

    const url = attachmentUrl(m);
    let attachNode = null;
    if (m.attachment) {
      const a = m.attachment;
      if (a.kind === 'voice') {
        attachNode = el('div', { class: 'attach voice' },
          el('div', { class: 'faint small' }, `🎤 voice message (${a.duration_s || '?'}s)`),
          url ? el('audio', { controls: true, src: url, preload: 'metadata' })
              : el('div', { class: 'faint small' }, 'voice file on its host companion'));
      } else if (a.kind === 'image' && url) {
        attachNode = el('div', { class: 'attach' },
          el('img', { src: url, alt: a.name, loading: 'lazy',
                      style: { maxWidth: '260px', maxHeight: '260px', borderRadius: '10px', display: 'block' } }),
          el('div', { class: 'faint small' }, a.name));
      } else if (url) {
        attachNode = el('div', { class: 'attach' },
          el('a', { href: url, download: a.name, style: { color: 'var(--cyan)' } }, '📎 ', a.name),
          el('span', { class: 'faint' }, ` ${fmtSize(a.size || 0)}`));
      } else {
        attachNode = el('div', { class: 'attach', onclick: () => openAttachment(m) },
          '📎 ', a.name, el('span', { class: 'faint' }, ` ${fmtSize(a.size || 0)}`));
      }
    }

    const replyRef = m.reply_to
      ? el('div', { class: 'm-replyto faint small' },
          '↩ ', (messages.find(x => x.id === m.reply_to)?.body || 'reply'))
      : null;

    const node = el('div', { class: `msg ${mine ? 'out' : 'in'}` },
      !mine && current !== 'broadcast:all' && m.to_kind === 'broadcast'
        ? el('div', { class: 'faint small' }, m.from?.name) : null,
      replyRef,
      el('div', {}, m.body || ''),
      attachNode,
      reacts,
      el('div', { class: 'm-meta' },
        el('span', {}, fmtTime(m.ts), m.edited ? ' · edited' : ''),
        status));

    node.addEventListener('dblclick', () => {
      navigator.clipboard?.writeText(m.body || '');
      toast('Message copied', 'ok', 1500);
    });
    node.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      messageMenu(m);
    });
    node.addEventListener('click', (e) => {
      if (e.detail === 2) return;           // dblclick already handled
    });
    return node;
  }

  function messageMenu(m) {
    const items = [];
    items.push({ icon: '↩', label: 'Reply', handler: () => { replyTo = m.id; paintReplyBar(); } });
    items.push({ icon: '👍', label: 'React 👍', handler: () => api.chatReact(m.id, '👍').then(refreshCurrent).catch(e => toast(errText(e), 'err')) });
    items.push({ icon: '❤️', label: 'React ❤️', handler: () => api.chatReact(m.id, '❤️').then(refreshCurrent).catch(e => toast(errText(e), 'err')) });
    items.push({ icon: '📋', label: 'Copy', handler: () => { navigator.clipboard?.writeText(m.body || ''); toast('Copied', 'ok', 1200); } });
    if (m.outgoing) {
      items.push({ icon: '✏️', label: 'Edit', handler: () => editMessage(m) });
      items.push({ icon: '🗑', label: 'Delete (tombstone)', danger: true, handler: () => {
        if (confirm('Delete this message for everyone?')) {
          api.chatDelete(m.id).then(refreshCurrent).catch(e => toast(errText(e), 'err'));
        }
      } });
    }
    items.push({ icon: 'ℹ️', label: 'Status: ' + m.status, handler: () => {
      modal('Message status', (body) => body.appendChild(el('div', { class: 'dim small' },
        statusExplanation(m))));
    } });
    // lightweight inline menu
    const menu = el('div', { class: 'ctx-layer', style: { left: '50%', top: '120px', display: 'flex', flexDirection: 'column', zIndex: 200 } });
    for (const item of items) {
      menu.appendChild(el('div', { class: `ctx-item ${item.danger ? 'danger' : ''}`, onclick: () => { menu.remove(); item.handler(); } },
        item.icon, ' ', item.label));
    }
    document.body.appendChild(menu);
    setTimeout(() => document.addEventListener('mousedown', function dismiss(e) {
      if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', dismiss); }
    }), 0);
  }

  function statusExplanation(m) {
    const map = {
      sent: 'Sent to the recipient device. For phones, "delivered" only appears after the phone itself acknowledged receipt — not merely when its host companion received it.',
      queued: 'The recipient is offline. The message is stored and will be delivered automatically when it returns.',
      delivered: 'The recipient device (computer UI or phone) confirmed receipt.',
      read: 'The recipient opened the conversation.',
      failed: 'No route to any recipient. Check the Network Radar.',
    };
    return map[m.status] || m.status;
  }

  function editMessage(m) {
    modal('Edit message', (body) => {
      const ta = el('textarea', { style: { width: '100%', minHeight: '80px' } });
      ta.value = m.body || '';
      body.appendChild(ta);
      body._ta = ta;
    }, [
      { label: 'Cancel', handler: (close) => close() },
      { label: 'Save', kind: 'primary', handler: async (close, body) => {
        const text = body._ta.value.trim();
        if (!text) return;
        await api.chatEdit(m.id, text);
        close(); refreshCurrent();
      } },
    ]);
  }

  function openAttachment(m) {
    const att = m.attachment || {};
    const url = attachmentUrl(m);
    modal('Attachment', (body) => {
      body.appendChild(el('div', { class: 'kv' },
        el('div', { class: 'k' }, 'File'), el('div', { class: 'v' }, att.name || '?'),
        el('div', { class: 'k' }, 'Size'), el('div', { class: 'v' }, fmtSize(att.size || 0)),
        el('div', { class: 'k' }, 'Storage'), el('div', { class: 'v' },
          att.file_id ? 'chat attachment store (host companion)' : (att.path || 'transfer engine job'))));
      if (att.file_id && url) {
        body.appendChild(el('div', { class: 'modal-actions' },
          el('a', { class: 'btn primary', href: url, download: att.name || 'attachment' }, '⇩ Download'),
          el('button', { class: 'btn', onclick: (c) => c() }, 'Close')));
      } else if (att.path) {
        body.appendChild(el('div', { class: 'dim small mt' },
          'This attachment travelled with the standard transfer engine (SHA-256 verified). ' +
          'Receive it into an authorized directory of yours:'));
        body.appendChild(el('div', { class: 'modal-actions' },
          el('button', { class: 'btn primary', onclick: async (close) => {
            try {
              const spec = {
                kind: 'pull', src_base: att.src_base, src_path: att.path,
                dest_path: '~/' + (att.name || 'attachment.bin'), conflict: 'rename',
                src_device: att.from_name || 'peer', dest_device: state.self?.name,
              };
              await api.createJob('', spec);
              toast('Download job queued (goes to your home share)', 'ok');
            } catch (e) { toast(errText(e), 'err'); }
          } }, '⇩ Receive to my home folder'),
          el('button', { class: 'btn', onclick: (c) => c() }, 'Close')));
      }
    });
  }

  /* ---------- composer ---------- */
  function paintReplyBar() {
    replyBar.innerHTML = '';
    if (!replyTo) { replyBar.classList.add('hidden'); return; }
    const src = messages.find(x => x.id === replyTo);
    replyBar.classList.remove('hidden');
    replyBar.appendChild(el('div', { class: 'spread' },
      el('span', { class: 'dim small' }, `↩ replying to: ${(src?.body || '[attachment]').slice(0, 60)}`),
      el('button', { class: 'btn small', onclick: () => { replyTo = null; paintReplyBar(); } }, '×')));
  }

  function buildComposer() {
    composer.innerHTML = '';
    const input = el('textarea', { placeholder: current.startsWith('broadcast')
      ? 'Broadcast announcement…' : 'Message — Enter to send, Shift+Enter for newline' });
    const send = async () => {
      const body = input.value.trim();
      if (!body) return;
      let kind = current.startsWith('broadcast') ? 'broadcast'
        : current.startsWith('group:') ? 'group'
        : current.startsWith('mobile:') ? 'mobile'
        : current.startsWith('system:') ? 'system' : 'direct';
      const toId = current.split(':').slice(1).join(':');
      input.value = '';
      try {
        const res = await api.chatSend({ to_kind: kind, to_id: toId, body, reply_to: replyTo || '' });
        replyTo = null; paintReplyBar();
        if (res.message?.status === 'queued') toast('Recipient offline — message queued', 'warn');
        if (kind === 'mobile' && res.message?.status === 'sent') {
          toast('Hosted by the phone\'s companion — ✓ appears when the phone receives it', 'info', 4200);
        }
      } catch (e) { toast(errText(e), 'err'); input.value = body; }
      setTimeout(loadHistory, 250);
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    const attach = el('button', { class: 'btn', title: 'Attach file (chat store or transfer engine)', onclick: attachFile }, '📎');
    const voice = el('button', { class: 'btn', title: 'Voice message (MediaRecorder)', onclick: recordVoiceNote }, '🎤');
    const search = el('input', { placeholder: 'search history…', style: { maxWidth: '150px' } });
    search.addEventListener('input', debounce(() => { searchQ = search.value.trim(); loadHistory(); }, 400));
    composer.appendChild(input);
    composer.appendChild(attach);
    composer.appendChild(voice);
    composer.appendChild(search);
    composer.appendChild(el('button', { class: 'btn primary', onclick: send }, 'Send ▸'));

    async function attachFile() {
      const input = el('input', { type: 'file', style: { display: 'none' } });
      document.body.appendChild(input);
      input.addEventListener('change', async () => {
        input.remove();
        const file = input.files[0];
        if (!file) return;
        try {
          if (current.startsWith('mobile:')) {
            // phones pull from OUR chat store — upload to it first
            const fid = await uploadChatStore(file);
            const toId = current.split(':')[1];
            await api.chatSend({ to_kind: 'mobile', to_id: toId, body: '',
              reply_to: replyTo || '',
              attachment: { kind: (file.type || '').startsWith('image/') ? 'image' : 'file',
                            file_id: fid.id, name: file.name, size: file.size,
                            mime: file.type || 'application/octet-stream' } });
            toast('Attachment stored — the phone fetches it from this companion', 'ok');
          } else if (current.startsWith('direct:')) {
            const targetDev = current.split(':')[1];
            const peer = state.peerById(targetDev);
            if (!peer || !(peer.addresses || []).length) { toast('Peer has no reachable address', 'err'); return; }
            const preferLan = (addrs) => {
              const list = (addrs || []).filter(Boolean);
              const rank = (a) => /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(a) ? 0 : (a.startsWith('100.') ? 2 : 1);
              return [...list].sort((a,b)=>rank(a)-rank(b))[0] || list[0];
            };
            const addr = preferLan(peer.addresses);
            toast(`Uploading ${file.name} to ${peer.display_name}…`);
            const fin = await api.uploadFile(targetDev, '~', file, () => {});
            await api.chatSend({
              to_kind: 'direct', to_id: targetDev, body: `[file shared] ${file.name}`,
              reply_to: replyTo || '',
              attachment: { name: file.name, size: file.size, path: fin.target,
                            src_base: `http://${addr}:${peer.api_port}`,
                            from_name: state.self?.name },
            });
          } else {
            toast('Attach works in direct and phone conversations', 'warn');
            return;
          }
          replyTo = null; paintReplyBar();
          toast('Attachment sent', 'ok');
          setTimeout(loadHistory, 250);
        } catch (e) { toast('Attach failed: ' + errText(e), 'err', 6000); }
      });
      input.click();
    }

    async function uploadChatStore(file) {
      const s = await api.chatFileSession(file.name, file.size, file.type || '');
      const CHUNK = 512 * 1024;
      let offset = 0;
      while (offset < file.size) {
        const buf = await file.slice(offset, offset + CHUNK).arrayBuffer();
        await fetch(`/api/chat/file/${s.sid}`, { method: 'PUT', body: buf });
        offset += buf.byteLength;
      }
      const fin = await api.chatFileFinish(s.sid);
      return fin.file;
    }

    function recordVoiceNote() {
      if (!navigator.mediaDevices?.getUserMedia) {
        toast('Voice recording requires a secure context (localhost or HTTPS)', 'warn', 5000);
        return;
      }
      let rec = null, chunks = [], t0 = 0, timer = null;
      modal('Voice message', (body, close) => {
        const status = el('div', { class: 'dim' }, 'Idle');
        const audio = el('audio', { controls: true, style: { width: '100%', marginTop: '10px', display: 'none' } });
        body.appendChild(status);
        body.appendChild(audio);
        body._rec = null;
        body._start = async () => {
          try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            rec = new MediaRecorder(stream);
            chunks = [];
            rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
            rec.onstop = () => {
              stream.getTracks().forEach(t => t.stop());
              const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
              audio.src = URL.createObjectURL(blob);
              audio.style.display = 'block';
              body._blob = blob;
              status.textContent = `Recorded ${Math.round((Date.now() - t0) / 1000)}s — preview, then send or discard.`;
            };
            rec.start();
            t0 = Date.now();
            status.textContent = 'Recording… (60 s limit)';
            timer = setInterval(() => {
              if (Date.now() - t0 > 60000) { body._stop(); }
            }, 300);
          } catch (e) { status.textContent = 'Microphone unavailable: ' + errText(e); }
        };
        body._stop = () => {
          clearInterval(timer);
          if (rec && rec.state !== 'inactive') rec.stop();
        };
        body.appendChild(el('div', { class: 'modal-actions' },
          el('button', { class: 'btn', onclick: () => body._start() }, '● Record'),
          el('button', { class: 'btn', onclick: () => body._stop() }, '■ Stop'),
          el('button', { class: 'btn primary', onclick: async (close) => {
            if (!body._blob) { toast('Record first', 'warn'); return; }
            const dur = Math.round((Date.now() - t0) / 1000);
            const ext = ((body._blob.type || 'audio/webm').split('/')[1] || 'webm').split(';')[0];
            const file = new File([body._blob], `voice-${Date.now()}.${ext}`, { type: body._blob.type });
            const fid = await uploadChatStore(file);
            const kind = current.startsWith('mobile:') ? 'mobile'
              : current.startsWith('direct:') ? 'direct' : null;
            if (!kind) { toast('Voice works in direct and phone conversations', 'warn'); return; }
            await api.chatSend({ to_kind: kind, to_id: current.split(':')[1], body: '',
              attachment: { kind: 'voice', file_id: fid.id, name: file.name, size: file.size,
                            duration_s: dur, mime: body._blob.type } });
            close(); refreshCurrent();
            toast('Voice message sent', 'ok');
          } }, 'Send'),
          el('button', { class: 'btn', onclick: (c) => c() }, 'Close')));
      });
    }
  }

  /* ---------- header ---------- */
  function paintHead() {
    convHead.innerHTML = '';
    let title = 'Select a conversation';
    let sub = '';
    if (current === 'broadcast:all') { title = '📻 Broadcast'; sub = 'announcement to every online participating device'; }
    else if (current.startsWith('mobile:')) {
      const id = current.split(':')[1];
      let client = null;
      for (const p of state.peers) {
        const found = (p.mobile_clients || []).find(c => c.client_id === id);
        if (found) { client = found; client._host = p; break; }
      }
      title = '📱 ' + (client?.name || id);
      sub = client
        ? `${client.online ? '● online' : '○ offline'} · mobile client of ${client._host.display_name} · messages relay via the host`
        : 'mobile client';
    } else if (current.startsWith('direct:')) {
      const id = current.split(':')[1];
      const peer = state.peerById(id);
      title = '◌ ' + state.displayName(id);
      sub = peer ? `${peer.status === 'online' ? '● online' : '○ offline'} · ${peer.addresses?.[0] || ''} · ${peer.os || ''}` : 'unknown device';
    } else if (current.startsWith('group:')) {
      title = '👥 ' + (current.split(':')[1] || 'group');
    } else if (current.startsWith('system:')) {
      title = '🔔 System Alerts';
      sub = 'local monitoring alerts (no recipient devices involved)';
    }
    convHead.appendChild(el('div', {},
      el('div', { style: { fontWeight: '700' } }, title),
      el('div', { class: 'dim small' }, sub)));
  }

  function refreshCurrent() { loadHistory(); refreshConvs(); }

  const onChat = () => { refreshConvs(); if (current) loadHistory(); };
  window.addEventListener('pn-chat-any', onChat);
  window.addEventListener('pn-chat', onChat);

  buildComposer();
  paintHead();
  await refreshConvs();
  if (!current) current = 'broadcast:all';
  /* radar/palette "chat with device" deep-link */
  if (state.uiPrefs.chat_target) {
    const target = state.uiPrefs.chat_target;
    const isMobile = target.startsWith('mob_');
    const key = (isMobile ? 'mobile:' : 'direct:') + target;
    current = key;
    state.uiPrefs.chat_target = null;
    state.savePrefs();
  }
  drawConvs();
  await loadHistory();
  paintHead();      // repaint after deep-link / restore so the header never
                    // shows "Select a conversation" for an active conv

  return () => {
    window.removeEventListener('pn-chat-any', onChat);
    window.removeEventListener('pn-chat', onChat);
  };
}
