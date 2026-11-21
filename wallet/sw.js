// Keeps the app working offline. Bump VERSION when the app's files change.
const VERSION = "wallet-v15";
const FILES = ["./", "index.html", "app.js", "manifest.webmanifest", "jsbarcode.min.js", "qrcode.js", "zxing.min.js",
  "fonts/sora-latin-500-normal.woff2", "fonts/sora-latin-600-normal.woff2", "fonts/sora-latin-700-normal.woff2",
  "fonts/inter-latin-400-normal.woff2", "fonts/inter-latin-500-normal.woff2", "fonts/inter-latin-600-normal.woff2",
  "fonts/jetbrains-mono-latin-500-normal.woff2",
  "apple-touch-icon.png", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// Network first (so updates show up), falling back to the saved copy when offline.
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  if (new URL(e.request.url).origin !== self.location.origin) return; // prices & balances always come live
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res && (res.ok || res.type === "opaque")) {
        const copy = res.clone();
        caches.open(VERSION).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || caches.match("index.html")))
  );
});
