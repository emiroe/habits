self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
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
