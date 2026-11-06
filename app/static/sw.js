const CACHE = "hb-v4";
const PRE = ["/", "/static/fonts/Raleway.ttf", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/apple-touch-icon.png", "/static/icons/favicon.svg"];
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c => Promise.all(PRE.map(u => c.add(u).catch(() => {}))))) });
self.addEventListener("activate", e => e.waitUntil((async () => { for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k); await self.clients.claim() })()));
self.addEventListener("message", e => { if (e.data === "clear") e.waitUntil(caches.delete(CACHE)) });
// Offline: the app shell and the last /api/state + /api/logs answers come from the cache when the network fails.
// Check-offs made offline are queued by the page itself (see index.html) and sent when the connection is back.
self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  const api = u.pathname === "/api/state" || u.pathname === "/api/logs";
  if (u.pathname.startsWith("/api/") && !api) return;
  if (r.mode === "navigate") { e.respondWith(shell(r, e)); return }
  if (u.pathname === "/" || api) { e.respondWith(networkFirst(r, null, api)); return }
  if (u.pathname.startsWith("/static/") || u.pathname === "/manifest.webmanifest") e.respondWith(staleWhileRevalidate(r));
});
// A dead connection (VPN dropped, no signal) often does not fail, it just hangs: give up after a few seconds and use the cache.
function fetchT(r, ms) {
  const ac = new AbortController(), t = setTimeout(() => ac.abort(), ms);
  return fetch(r, { signal: ac.signal }).finally(() => clearTimeout(t));
}
// The app opens instantly from the cache and updates itself in the background; when a new version arrived the page reloads the next time it comes to the foreground.
async function shell(r, e) {
  const c = await caches.open(CACHE), m = await c.match("/");
  const net = fetchT(r, 6000).then(async res => {
    if (res.ok) {
      const old = m && m.headers.get("etag");
      await c.put("/", res.clone());
      if (m && old !== res.headers.get("etag")) for (const cl of await self.clients.matchAll()) cl.postMessage("updated");
    }
    return res;
  }).catch(() => m || new Response("offline", { status: 503 }));
  if (m) { e.waitUntil(net); return m }
  return net;
}
async function networkFirst(r, key, api) {
  const c = await caches.open(CACHE), k = key || r;
  try {
    const res = await fetchT(r, 3000);
    if (res.ok) { c.put(k, res.clone()); return res }
    if (res.status < 500) return res;
    throw new Error("server error");        // 502/503/504 from the proxy = server down: use the cache like offline
  } catch (x) {
    const m = await c.match(k);
    if (!m) return new Response("offline", { status: 503 });
    if (!api) return m;
    const h = new Headers(m.headers); h.set("X-Offline", "1");
    return new Response(await m.blob(), { status: 200, headers: h });
  }
}
async function staleWhileRevalidate(r) {
  const c = await caches.open(CACHE), m = await c.match(r);
  const net = fetch(r).then(res => { if (res.ok) c.put(r, res.clone()); return res }).catch(() => m);
  return m || net;
}
self.addEventListener("push", e => {
  let d = {}; try { d = e.data.json() } catch (x) {}
  e.waitUntil((async () => {
    if (self.navigator && self.navigator.setAppBadge) { try { d.badge > 0 ? await self.navigator.setAppBadge(d.badge) : await self.navigator.clearAppBadge() } catch (x) {} }
    await self.registration.showNotification(d.title || "Habits", { body: d.body || "", icon: "/static/icons/icon-192.png", tag: d.tag || undefined, data: { url: "/" } });
  })());
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of cs) { if ("focus" in c) return c.focus() }
    return self.clients.openWindow("/");
  })());
});
