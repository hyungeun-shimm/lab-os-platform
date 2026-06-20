// Minimal service worker — exists so the app is installable (add to home screen).
// Deliberately does NOT cache: the app is server-driven with no-store responses,
// so a pass-through fetch handler keeps content always fresh while still meeting
// the PWA installability criteria.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => { /* pass-through: browser handles normally */ });
