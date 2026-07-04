// Minimal service worker - exists mainly so Chrome/Android treats this as
// an installable PWA. No offline caching of live data (the dashboard needs
// a live connection to Supabase anyway), just enough to satisfy the
// installability requirement and cache the static shell.
const CACHE_NAME = "price-tracker-shell-v1";
const SHELL_FILES = ["./dashboard.html", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // Network-first for everything - this app is meaningless without live
  // data, so we never want to silently serve a stale cached page.
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
