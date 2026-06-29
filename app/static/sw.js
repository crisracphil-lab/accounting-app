/* BookPoint Service Worker - PWA offline support */
const CACHE = 'bookpoint-v9';
const STATIC = [
  '/static/style.css',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/login'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Only cache GET requests on our origin
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // Network-first for HTML pages (always fresh data)
  if (e.request.headers.get('accept')?.includes('text/html')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        caches.match('/login') || new Response('<h1>Offline</h1><p>Please reconnect to use BookPoint.</p>', {headers:{'Content-Type':'text/html'}})
      )
    );
    return;
  }
  // Cache-first for static assets (CSS, images)
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }))
    );
    return;
  }
  // Default: network only
  e.respondWith(fetch(e.request));
});
