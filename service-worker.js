// Service worker: makes the dashboard installable as an app, and receives
// push messages so a new alert can show a notification and set the red
// badge on the app icon even when the app is closed.
const CACHE_NAME = "price-tracker-shell-v2";
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
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
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
