// Service-Worker der 3D-Karte (PWA). Absichtlich klein und sparsam:
// - Hält nur die festen Dateien bereit (three.js, Code, Styles, Symbole), damit die App sofort startet.
// - Live-Daten (/karte/status, /karte/ha, …) gehen immer direkt ans Netz und werden nie gespeichert.
// - Keine Push-Nachrichten, kein Hintergrund-Abgleich: Ist die App zu, fragt sie nichts ab.
// - Neue Version (nach „cloudy update“) -> neuer Cache, alter wird gelöscht.
const VERSION = '__V__';
const CACHE = `karte-${VERSION}`;
const SHELL = [
  '/static/karte/vendor/three.min.js',
  '/static/karte/vendor/OrbitControls.js',
  `/static/karte/karte.js?v=${VERSION}`,
  `/static/karte/cloudy.js?v=${VERSION}`,
  `/static/karte/karte.css?v=${VERSION}`,
  `/static/karte/cloudy.css?v=${VERSION}`,
  `/static/karte/m3.css?v=${VERSION}`,
  '/static/karte/icon-192.png',
  '/static/karte/apple-touch-icon.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith('karte-') && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // z. B. Cloudflare-Anmeldung: nie anfassen

  // Feste Dateien: aus dem Speicher, sonst einmal laden und merken
  if (url.pathname.startsWith('/static/karte/')) {
    event.respondWith(
      caches.open(CACHE).then(cache => cache.match(req).then(hit => hit || fetch(req).then(res => {
        if (res.ok && res.type === 'basic') cache.put(req, res.clone());
        return res;
      })))
    );
    return;
  }

  // Seite selbst: immer frisch vom Netz (so kommen Updates und die Anmeldung unterwegs sofort an);
  // nur ohne Netz die zuletzt geladene Seite zeigen.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then(res => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put('__seite__', copy));
        }
        return res;
      }).catch(() => caches.open(CACHE).then(c => c.match('__seite__')).then(hit => hit || Response.error()))
    );
  }
  // Alles andere (Live-Daten): nicht anfassen -> direkt ans Netz
});
