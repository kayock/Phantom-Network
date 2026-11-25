/* ============================================================
   PHANTOM NETWORK — sw.js
   Service worker: offline app shell + cache version management.

   Rules (deliberately conservative — see MOBILE_CLIENT_GUIDE.md):
   * The static shell (HTML/CSS/JS/icons/sounds) is precached and
     served cache-first with a versioned cache name; a new release
     bumps PN_SW_VERSION, the old caches are deleted and clients get
     a "update available" signal (skipWaiting + controllerchange).
   * /api/** is NEVER cached — fleet data, chat, telemetry and files
     must always be live; a cached "online" fleet would be a lie.
   * Navigation requests fall back to the offline shell page, which
     clearly labels itself as offline and shows no fabricated state.
   * Server-push features (SSE) do not exist offline — the UI treats
     a dead stream as "reconnecting" and shows honest last-known data.
   ============================================================ */

const PN_SW_VERSION = 'v1.3.1-p2';
const SHELL_CACHE = `pn-shell-${PN_SW_VERSION}`;
const ASSET_CACHE = `pn-assets-${PN_SW_VERSION}`;

/* core shell entries — everything else is cached opportunistically */
const PRECACHE = [
  '/',
  '/m',
  '/offline.html',
  '/css/theme.css',
  '/css/mobile.css',
  '/assets/logo.svg',
  '/assets/icon-192.png',
  '/assets/icon-512.png',
  '/manifest.webmanifest',
  '/js/app.js',
  '/js/api.js',
  '/js/state.js',
  '/js/ui.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // fill one-by-one: a single 404 must not abort the whole install
    await Promise.all(PRECACHE.map(async (url) => {
      try {
        const resp = await fetch(url, { cache: 'no-store' });
        if (resp && resp.ok) await cache.put(url, resp.clone());
      } catch (e) { /* offline during install — retry on next activation */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter(n => n.startsWith('pn-') && n !== SHELL_CACHE && n !== ASSET_CACHE)
      .map(n => caches.delete(n)));
    await self.clients.claim();
    // update detection: tell open windows a new build took over
    const clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach(c => c.postMessage({ type: 'sw-updated', version: PN_SW_VERSION }));
  })());
});

self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  try {
    const resp = await fetch(request);
    if (resp && resp.ok && resp.type === 'basic') {
      cache.put(request, resp.clone());
    }
    return resp;
  } catch (e) {
    return new Response('', { status: 504, statusText: 'offline' });
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;      // never cache POST/PUT

  // API traffic: ALWAYS live, never cached (fleet truth, chat, files).
  if (url.pathname.startsWith('/api/')) return;

  // page navigations: network first, offline shell as fallback
  if (event.request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const resp = await fetch(event.request);
        return resp;
      } catch (e) {
        const offline = await caches.match('/offline.html');
        return offline || new Response(
          '<h1>Phantom Network — offline</h1><p>This companion is unreachable.</p>',
          { headers: { 'Content-Type': 'text/html' } });
      }
    })());
    return;
  }

  // static assets of this origin: cache-first (sounds, icons, media)
  if (url.origin === self.location.origin) {
    const isStatic = /\.(css|js|mjs|svg|png|ico|wav|woff2|webmanifest|json|txt)$/
      .test(url.pathname) || url.pathname === '/';
    if (isStatic) {
      event.respondWith(cacheFirst(event.request,
        url.pathname.endsWith('.wav') || url.pathname.startsWith('/assets/')
          ? ASSET_CACHE : SHELL_CACHE));
    }
  }
});
