/* PHANTOM NETWORK — commander.js
   Dual-pane file manager across participating devices.
   Each pane can target a different device; transfers run companion-to-
   companion (destination pulls from source) without routing through UI. */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, modal, confirmDialog, promptDialog, contextMenu, spinner,
  fmtSize, fmtTime, fileIcon, pathJoin, parentPath, baseName, emptyNote,
  debounce, errText,
} from '../ui.js';

export async function render(container) {
  const prefs = state.loadPrefs();
  const left = Pane('left', prefs.fc_left || 'self', prefs.fc_left_dir || '~');
  const right = Pane('right', prefs.fc_right || (state.devices()[1]?.device_id || 'self'),
                     prefs.fc_right_dir || '~');
  left.other = right;
  right.other = left;

  const grid = el('div', { class: 'fc-grid' });
  grid.appendChild(left.root);
  grid.appendChild(right.root);
  container.appendChild(grid);

  const help = el('div', { class: 'note' },
    'Tip: select files then use the toolbar or right-click menu to copy/move between panes. ',
    'Drag & drop works within a pane (between panes on desktop browsers). Transfers stream ',
    'directly between the two companions and are SHA-256 verified in the Transfer Center.');
  container.appendChild(help);

  await Promise.all([left.refresh(), right.refresh()]);

  const onPeers = () => { left.refreshDevices(); right.refreshDevices(); };
  const offPeers = state.on('peers', onPeers);

  return () => {
    left.destroy();
    right.destroy();
    offPeers();
  };
}

/* ==================================================================
   One file-pane
   ================================================================== */
function Pane(side, deviceId, startDir) {
  let device = deviceId || 'self';
  let cwd = startDir || '~';
  let entries = [];
  let selected = new Set();      // paths
  let lastClicked = null;
  let showHidden = false;
  let sortKey = 'name';
  let sortDir = 1;
  let roots = null;
  let destroyed = false;
  let other = null;              // the opposite pane (set by render)

  /* ---------- DOM skeleton ---------- */
  const deviceSel = el('select', { style: { maxWidth: '220px' }, onchange: async () => {
    device = deviceSel.value;
    cwd = '~';
    savePrefs();
    await refresh();
  } });

  const crumbs = el('div', { class: 'crumbs' });
  const searchBox = el('input', { placeholder: 'Search here…', style: { maxWidth: '150px' } });
  const searchOut = el('div', {});
  const listWrap = el('div', { class: 'fc-list' });
  const foot = el('div', { class: 'fc-foot' }, '—');

  const hiddenBtn = el('button', { class: 'btn small', title: 'Toggle hidden files',
    onclick: () => { showHidden = !showHidden; refresh(); } }, '👁');
  const favBtn = el('button', { class: 'btn small', title: 'Bookmark this location',
    onclick: () => {
      state.toggleFavorite(device, cwd, baseName(cwd) || cwd);
      toast(state.isFavorite(device, cwd) ? 'Bookmarked' : 'Bookmark removed', 'ok');
    } }, '★');
  const favSel = el('select', { style: { maxWidth: '140px' },
    onchange: () => {
      const f = state.favorites[Number(favSel.value)];
      if (f) { device = f.device; cwd = f.path; savePrefs(); refresh(); }
    } });
  const rootSel = el('select', { style: { maxWidth: '190px' },
    onchange: () => {
      if (!rootSel.value) return;
      cwd = rootSel.value;
      savePrefs();
      refresh();
    } });
  const upBtn = el('button', { class: 'btn small', title: 'Up one level', onclick: () => goUp() }, '↑');
  const homeBtn = el('button', { class: 'btn small', title: 'Home', onclick: () => { cwd = '~'; refresh(); } }, '⌂');
  const newBtn = el('button', { class: 'btn small', title: 'New folder', onclick: () => newFolder() }, '+ Dir');
  const uplBtn = el('button', { class: 'btn small', title: 'Upload file(s) from this computer', onclick: () => uploadFiles() }, '⇧ Upload');
  const delBtn = el('button', { class: 'btn small danger', title: 'Delete selected (asks confirmation)', onclick: () => deleteSel() }, '✕ Delete');
  const copyBtn = el('button', { class: 'btn small cyan', title: 'Copy selected → other pane', onclick: () => transferSel('copy') }, '⧉ Copy →');
  const moveBtn = el('button', { class: 'btn small cyan', title: 'Move selected → other pane (verified, then source removed)', onclick: () => transferSel('move') }, '⇨ Move →');
  const selAllBtn = el('button', { class: 'btn small', title: 'Select all', onclick: () => {
    entries.forEach(e => selected.add(e.path)); paint();
  } }, '☑');

  const head = el('div', { class: 'fc-head' },
    el('div', { class: 'row' }, deviceSel, el('span', { class: 'grow' }),
       favBtn, hiddenBtn, upBtn, homeBtn),
    el('div', { class: 'row' }, crumbs, el('span', { class: 'grow' }), searchBox),
    el('div', { class: 'fc-toolbar' },
      rootSel, favSel, newBtn, uplBtn, selAllBtn, copyBtn, moveBtn, delBtn));

  const root = el('div', { class: 'fc-pane' }, head, listWrap, foot, searchOut);

  // active-pane highlight
  root.addEventListener('mousedown', () => {
    document.querySelectorAll('.fc-pane').forEach(p => p.classList.remove('active'));
    root.classList.add('active');
  });

  /* ---------- device & roots ---------- */
  function refreshDevices() {
    deviceSel.innerHTML = '';
    for (const d of state.devices()) {
      let label = (d.device_id === device ? '▶ ' : '') + d.display_name;
      if (!d.self && d.shares_count === 0) label += ' — no shared dirs';
      else if (!d.self && d.shares_count) label += ` — ${d.shares_count} share(s)`;
      deviceSel.appendChild(el('option', { value: d.device_id }, label));
      if (d.device_id === device) deviceSel.value = d.device_id;
    }
    refreshFavSel();
  }

  function refreshFavSel() {
    favSel.innerHTML = '';
    favSel.appendChild(el('option', { value: '' }, '★ favorites'));
    state.favorites.forEach((f, i) => {
      if (f.device !== device) return;
      favSel.appendChild(el('option', { value: String(i) }, f.label || baseName(f.path)));
    });
  }

  async function loadRoots() {
    try {
      roots = await api.fsRoots(device);
      if (!roots.shares?.length && roots.drives?.length) {
        // nothing authorized yet: show a clear hint in the list area
      }
    } catch (e) {
      roots = { shares: [], drives: [], platform: '?' };
    }
    // roots/drives jump selector (shares first, then drives/mounts)
    rootSel.innerHTML = '';
    rootSel.appendChild(el('option', { value: '' }, '▸ go to share / drive'));
    for (const s of roots?.shares || []) {
      rootSel.appendChild(el('option', { value: s.path }, `share: ${s.label} (${s.mode})`));
      if (s.path === cwd) rootSel.value = s.path;
    }
    for (const d of roots?.drives || []) {
      rootSel.appendChild(el('option', { value: d.path },
        `${d.kind}: ${d.name}${d.free != null ? ` · ${fmtSize(d.free)} free` : ''}`));
      if (d.path === cwd) rootSel.value = d.path;
    }
  }

  /* ---------- navigation ---------- */
  async function refresh() {
    if (destroyed) return;
    refreshDevices();
    await loadRoots();
    selected.clear();
    paint();
    try {
      const data = await api.fsList(device, cwd, showHidden);
      cwd = data.path;
      entries = data.entries || [];
      savePrefs();
    } catch (e) {
      entries = [];
      listWrap.innerHTML = '';
      const msg = String(e.message || e);
      const shareHint = roots?.shares?.length
        ? `Authorized roots on this device: ${roots.shares.map(s => s.path).join(' ; ')}`
        : 'No shares are configured on that device yet (Settings → Authorized directories).';
      listWrap.appendChild(el('div', { class: 'panel' },
        el('h3', {}, 'Cannot open this location'),
        el('div', { class: 'dim', style: { lineHeight: 1.7 } },
          msg, el('br'), shareHint)));
      foot.textContent = 'error';
      return;
    }
    sortEntries();
    paint();
  }

  function savePrefs() {
    state.uiPrefs['fc_' + side] = device;
    state.uiPrefs['fc_' + side + '_dir'] = cwd;
    state.savePrefs();
  }

  function goUp() {
    const parent = parentPath(cwd);
    if (parent !== null) { cwd = parent; refresh(); }
  }

  function sortEntries() {
    const mul = sortDir;
    entries.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
      if (sortKey === 'size') return ((a.size ?? -1) - (b.size ?? -1)) * mul;
      if (sortKey === 'mtime') return (a.mtime - b.mtime) * mul;
      if (sortKey === 'ext') return (a.ext || '').localeCompare(b.ext || '') * mul;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) * mul;
    });
  }

  /* ---------- painting ---------- */
  function paint() {
    crumbs.innerHTML = '';
    crumbs.appendChild(el('span', { class: 'faint' }, `${roots?.platform || ''} ▸ `));
    crumbs.appendChild(crumbLink(device === 'self' ? 'This device' : state.displayName(device), null));
    // split path into segments honoring / and \
    const sep = cwd.includes('\\') ? '\\' : '/';
    const parts = cwd.replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean);
    let acc = '';
    parts.forEach((part, i) => {
      if (/^[a-zA-Z]:$/.test(part)) acc = part + '\\';
      else if (cwd.startsWith('/')) acc = (i === 0 ? '' : acc) + '/' + part;
      else acc = acc.endsWith('\\') ? acc + part : acc + sep + part;
      const target = acc || '/';
      crumbs.appendChild(el('span', { class: 'sep' }, ' ▸ '));
      crumbs.appendChild(crumbLink(part, target));
    });
    if (cwd === '/') crumbs.appendChild(crumbLink('/', '/'));

    const table = el('table', {});
    const thead = el('thead', {}, el('tr', {},
      th('name', 'Name'), th('size', 'Size'), th('ext', 'Type'),
      th('mtime', 'Modified')));
    const tbody = el('tbody', {});
    for (const entry of entries) {
      const tr = el('tr', {
        class: selected.has(entry.path) ? 'sel' : '',
        onmousedown: (e) => {
          document.querySelectorAll('.fc-pane').forEach(p => p.classList.remove('active'));
          root.classList.add('active');
          if (e.button === 2) return;
          if (e.ctrlKey || e.metaKey) {
            if (selected.has(entry.path)) selected.delete(entry.path);
            else selected.add(entry.path);
            lastClicked = entry.path;
          } else if (e.shiftKey && lastClicked) {
            const i1 = entries.findIndex(x => x.path === lastClicked);
            const i2 = entries.findIndex(x => x.path === entry.path);
            if (i1 >= 0 && i2 >= 0) {
              selected.clear();
              for (let i = Math.min(i1, i2); i <= Math.max(i1, i2); i++) selected.add(entries[i].path);
            }
          } else {
            selected.clear();
            selected.add(entry.path);
            lastClicked = entry.path;
          }
          paint();
        },
        ondblclick: () => openEntry(entry),
        oncontextmenu: (e) => {
          e.preventDefault();
          if (!selected.has(entry.path)) { selected.clear(); selected.add(entry.path); paint(); }
          entryMenu(e.clientX, e.clientY, entry);
        },
        draggable: true,
        ondragstart: (e) => e.dataTransfer.setData('text/pn', JSON.stringify(
          { device, paths: [...selected], dir: cwd })),
      },
        el('td', { class: 'fname', onclick: () => openEntry(entry) },
          el('span', { class: 'f-ico' }, fileIcon(entry)),
          entry.name,
          entry.hidden ? el('span', { class: 'faint small' }, ' (hidden)') : null,
          entry.broken_link ? el('span', { class: 'badge err' }, 'broken link') : null),
        el('td', { class: 'right' }, entry.type === 'dir' ? '—' : fmtSize(entry.size)),
        el('td', { class: 'dim' }, entry.type === 'dir' ? 'DIR' : (entry.ext || '?').toUpperCase()),
        el('td', { class: 'dim' }, fmtTime(entry.mtime)));
      tbody.appendChild(tr);
    }
    table.appendChild(thead); table.appendChild(tbody);
    listWrap.innerHTML = '';
    if (entries.length === 0) {
      listWrap.appendChild(emptyNote('Empty directory'));
    }
    listWrap.appendChild(table);

    const files = entries.filter(e => e.type !== 'dir');
    const bytes = files.reduce((a, b) => a + (b.size || 0), 0);
    foot.textContent = `${entries.length} items · ${files.length} files · ${fmtSize(bytes)} · ` +
      `${selected.size} selected · ${state.displayName(device)}`;
  }

  function crumbLink(label, target) {
    return el('a', { onclick: () => { if (target !== null) { cwd = target || '/'; refresh(); } } }, label || '/');
  }

  function th(key, label) {
    return el('th', { onclick: () => {
      if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = 1; }
      sortEntries(); paint();
    } }, label + (sortKey === key ? (sortDir > 0 ? ' ▲' : ' ▼') : ''));
  }

  async function openEntry(entry) {
    if (entry.type === 'dir') { cwd = entry.path; refresh(); return; }
    // file → preview modal
    try {
      const pv = await api.fsPreview(device, entry.path);
      modal(baseName(entry.path), (body) => {
        if (pv.kind === 'image') {
          const url = `${api.baseUrl(device)}/api/fs/download?path=${encodeURIComponent(entry.path)}`;
          body.appendChild(el('img', { src: url, style: { maxWidth: '100%', maxHeight: '55vh', borderRadius: '8px' } }));
        } else if (pv.kind === 'text') {
          body.appendChild(el('div', { class: 'log-view', style: { maxHeight: '50vh' } }, pv.data));
        } else {
          body.appendChild(el('div', { class: 'dim' },
            `Binary file (${fmtSize(pv.size)}). No inline preview — use Download or transfer it.`));
        }
        body.appendChild(el('div', { class: 'row mt' },
          el('button', { class: 'btn small', onclick: () => api.downloadToBrowser(device, entry.path) }, '⇩ Download to this computer'),
          el('span', { class: 'badge' }, fmtSize(pv.size)),
          el('span', { class: 'badge' }, pv.kind)));
      }, [{ label: 'Close' }]);
    } catch (e) {
      toast(`Preview failed: ${errText(e)}`, 'err');
    }
  }

  /* ---------- operations ---------- */
  async function newFolder() {
    const res = await promptDialog(`New folder in ${baseName(cwd) || cwd}`, [
      { name: 'name', label: 'Folder name' }], 'Create');
    if (!res?.name) return;
    try { await api.fsMkdir(device, cwd, res.name); await refresh(); toast('Folder created', 'ok'); }
    catch (e) { toast(errText(e), 'err'); }
  }

  async function renameEntry(entry) {
    const res = await promptDialog(`Rename ${entry.name}`, [
      { name: 'name', label: 'New name', value: entry.name }], 'Rename');
    if (!res?.name || res.name === entry.name) return;
    try { await api.fsRename(device, entry.path, res.name); await refresh(); toast('Renamed', 'ok'); }
    catch (e) { toast(errText(e), 'err'); }
  }

  async function deleteSel() {
    const paths = [...selected];
    if (!paths.length) { toast('Nothing selected', 'warn'); return; }
    const list = paths.map(p => '• ' + baseName(p)).join('\n');
    const ok = await confirmDialog('Confirm deletion',
      `Permanently delete ${paths.length} item(s) from ${state.displayName(device)}?\n\n${list}\n\n` +
      'This cannot be undone.', 'Delete permanently');
    if (!ok) return;
    try {
      const res = await api.fsDelete(device, paths);
      const failed = res.results.filter(r => !r.deleted);
      if (failed.length) {
        modal('Deletion summary — some items were not deleted', (body) => {
          body.appendChild(el('div', { class: 'log-view', style: { maxHeight: '220px' } },
            res.results.map(r => `${r.deleted ? 'DELETED' : 'KEPT '}  ${baseName(r.path)}${r.error ? ' — ' + r.error : ''}`).join('\n')));
          body.appendChild(el('div', { class: 'dim small mt' },
            'Kept items are usually protected by OS permissions or are files currently in use.'));
        }, [{ label: 'Close' }]);
      } else toast(`Deleted ${paths.length} item(s)`, 'ok');
      await refresh();
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function transferSel(mode) {
    const paths = [...selected];
    if (!paths.length) { toast('Select files first', 'warn'); return; }
    const dstDevice = other.device;
    if (dstDevice === device) { toast('Target pane must show a different device (or a different folder).', 'warn'); }
    const dstDir = other.cwd;
    // confirm move explicitly (move deletes sources after verification)
    if (mode === 'move') {
      const ok = await confirmDialog('Confirm move',
        `Move ${paths.length} item(s) to ${state.displayName(dstDevice)}:${dstDir}?\n\n` +
        'Sources are deleted ONLY after the copy is SHA-256 verified on the destination.',
        'Move');
      if (!ok) return;
    }
    const launched = [], failed = [];
    for (const srcPath of paths) {
      const entry = entries.find(e => e.path === srcPath);
      const isDir = entry?.type === 'dir' || !entry;
      const spec = isDir ? {
        kind: 'pull_dir', src_base: srcBase(device), src_path: srcPath,
        dest_path: pathJoin(dstDir, baseName(srcPath)),
        conflict: 'rename', src_device: state.displayName(device),
        dest_device: state.displayName(dstDevice),
      } : {
        kind: 'pull', src_base: srcBase(device), src_path: srcPath,
        dest_path: pathJoin(dstDir, baseName(srcPath)),
        conflict: 'rename', src_device: state.displayName(device),
        dest_device: state.displayName(dstDevice),
      };
      if (mode === 'move') spec.delete_source = true;
      try {
        await api.createJob(dstDevice, spec);
        launched.push(baseName(srcPath));
      } catch (e) {
        failed.push({ name: baseName(srcPath), error: errText(e) });
      }
    }
    // detailed operation summary when some items could not be queued
    if (failed.length) {
      modal('Operation summary — some items failed', (body, close) => {
        body.appendChild(el('div', { class: 'row' },
          el('span', { class: 'badge on' }, `${launched.length} queued`),
          el('span', { class: 'badge err' }, `${failed.length} failed`)));
        body.appendChild(el('div', { class: 'log-view', style: { maxHeight: '220px' } },
          launched.map(n => `QUEUED  ${n}`).join('\n') +
          (launched.length ? '\n' : '') +
          failed.map(f => `FAILED  ${f.name} — ${f.error}`).join('\n')));
        body.appendChild(el('div', { class: 'dim small mt' },
          'Common causes: the item is outside the authorized shares on either device, ' +
          'the destination share is read-only, or the peer went offline.'));
      }, [{ label: 'Close' }]);
    } else if (launched.length) {
      toast(`${mode === 'move' ? 'Move' : 'Copy'} of ${launched.length} item(s) queued — see Transfer Center`, 'ok');
    }
    if (launched.length) {
      if (mode === 'move') {
        setTimeout(() => { if (!destroyed) refresh(); }, 1500);
      } else {
        setTimeout(() => { if (!destroyed) other.refresh(); }, 1500);
      }
    }
  }

  function srcBase(deviceId) {
    if (state.isSelf(deviceId)) return 'self';
    const p = state.peerById(deviceId);
    return `http://${p.addresses[0]}:${p.api_port}`;
  }

  async function uploadFiles() {
    const input = el('input', { type: 'file', multiple: true, style: { display: 'none' } });
    document.body.appendChild(input);
    input.addEventListener('change', async () => {
      input.remove();
      for (const file of input.files) {
        toast(`Uploading ${file.name} (${fmtSize(file.size)})…`);
        try {
          await api.uploadFile(device, cwd, file,
            (done, total) => { foot.textContent = `upload ${file.name}: ${fmtSize(done)} / ${fmtSize(total)}`; });
          toast(`${file.name} uploaded`, 'ok');
        } catch (e) { toast(`Upload failed: ${errText(e)}`, 'err', 6000); }
      }
      refresh();
    });
    input.click();
  }

  /* ---------- context menu ---------- */
  function entryMenu(x, y, entry) {
    const n = selected.size;
    contextMenu(x, y, [
      entry.type === 'dir' && { label: 'Open', icon: '▸', handler: () => { cwd = entry.path; refresh(); } },
      entry.type !== 'dir' && { label: 'Preview', icon: '👁', handler: () => openEntry(entry) },
      'sep',
      n > 0 && { label: `Copy ${n} → other pane`, icon: '⧉', handler: () => transferSel('copy') },
      n > 0 && { label: `Move ${n} → other pane`, icon: '⇨', handler: () => transferSel('move') },
      n === 1 && { label: 'Rename', icon: '✎', handler: () => renameEntry(entry) },
      n > 0 && { label: `Delete ${n}`, icon: '✕', danger: true, handler: () => deleteSel() },
      'sep',
      { label: state.isFavorite(device, entry.path) ? '★ Remove bookmark' : '★ Bookmark location',
        handler: () => { state.toggleFavorite(device, entry.path, entry.name); refreshFavSel(); } },
      entry.type !== 'dir' && { label: 'Download to this computer', icon: '⇩',
        handler: () => api.downloadToBrowser(device, entry.path) },
      n === 1 && entry.type !== 'dir' && { label: 'Copy path', icon: '⧉',
        handler: () => { navigator.clipboard?.writeText(entry.path); toast('Path copied', 'ok'); } },
    ].filter(Boolean));
  }

  /* ---------- drag & drop between panes ---------- */
  listWrap.addEventListener('dragover', (e) => { e.preventDefault(); listWrap.classList.add('drop-hint'); });
  listWrap.addEventListener('dragleave', () => listWrap.classList.remove('drop-hint'));
  listWrap.addEventListener('drop', (e) => {
    e.preventDefault();
    listWrap.classList.remove('drop-hint');
    try {
      const payload = JSON.parse(e.dataTransfer.getData('text/pn'));
      if (payload.device === device) {
        toast('Source and destination are the same device & pane', 'warn');
        return;
      }
      // drop INTO a hovered directory if the drop target row is one
      const row = e.target.closest('tr');
      let targetDir = cwd;
      if (row) {
        const nameCell = row.querySelector('.fname')?.textContent?.trim();
        const entry = entries.find(x => x.name === nameCell);
        if (entry?.type === 'dir') targetDir = entry.path;
      }
      other.dropFrom(payload, targetDir);
    } catch { /* foreign drop (OS files) handled by browser default = ignore */ }
  });

  async function dropFrom(payload, targetDir) {
    const srcDevice = payload.device;
    for (const srcPath of payload.paths) {
      const spec = {
        kind: 'pull', src_base: srcBase(srcDevice), src_path: srcPath,
        dest_path: pathJoin(targetDir, baseName(srcPath)),
        conflict: 'rename',
        src_device: state.displayName(srcDevice), dest_device: state.displayName(device),
      };
      try { await api.createJob(device, spec); } catch (e) { toast(errText(e), 'err'); }
    }
    toast('Transfer queued', 'ok');
    setTimeout(() => { if (!destroyed) refresh(); }, 1200);
  }

  /* search */
  const doSearch = debounce(async () => {
    const q = searchBox.value.trim();
    searchOut.innerHTML = '';
    if (!q) return;
    try {
      const res = await api.fsSearch(device, cwd, q);
      searchOut.appendChild(el('div', { class: 'panel' },
        el('h3', {}, `Search: "${q}" — ${res.results.length} results`),
        el('div', { class: 'log-view', style: { maxHeight: '160px' } },
          res.results.map(r => `${r.type === 'dir' ? '[D]' : '[F]'} ${r.path}`).join('\n') || 'no matches')));
    } catch (e) { toast(errText(e), 'err'); }
  }, 450);
  searchBox.addEventListener('input', doSearch);

  function destroy() { destroyed = true; }

  return {
    root, refresh, destroy,
    get device() { return device; },
    set device(v) { device = v; },
    get cwd() { return cwd; },
    set cwd(v) { cwd = v; },
    get other() { return other; },
    set other(v) { other = v; },
    refreshDevices,
    dropFrom,
  };
}
