// Service worker: makes the dashboard installable as an app, and receives
// push messages so a new alert can show a notification and set the red
// badge on the app icon even when the app is closed.
const CACHE_NAME = "price-tracker-shell-v3";
const SHELL_FILES = ["./dashboard.html", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  // Network-first: the app is meaningless without live data.
  // "no-cache" makes the phone check GitHub for a newer version every time,
  // instead of reusing a copy up to 10 minutes old.
  const req = event.request;
  const sameOrigin = new URL(req.url).origin === self.location.origin;
  event.respondWith(
    fetch(sameOrigin && req.method === "GET" ? new Request(req, { cache: "no-cache" }) : req)
      .catch(() => caches.match(req))
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  const title = data.title || "Price Tracker";
  const count = Number(data.badge) || 0;
  const tasks = [
    self.registration.showNotification(title, {
      body: data.body || "New alert",
      tag: "pt-alert",
      renotify: true,
      data: { url: "./dashboard.html#alerts" },
    }),
  ];
  if (self.navigator && "setAppBadge" in self.navigator) {
    tasks.push(count > 0 ? self.navigator.setAppBadge(count) : self.navigator.clearAppBadge());
  }
  event.waitUntil(Promise.all(tasks).catch(() => {}));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "./dashboard.html";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) { if ("focus" in c) { c.navigate(url); return c.focus(); } }
      return self.clients.openWindow(url);
    })
  );
});
