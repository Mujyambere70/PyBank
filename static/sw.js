// PyBank service worker: makes the site installable as a phone app and shows push notifications.
// It does not cache bank pages (account data must never be stored on the phone).

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = { title: 'PyBank', body: '', url: '/netbanking/alerts' };
  try { data = Object.assign(data, event.data ? event.data.json() : {}); } catch (e) { data.body = event.data ? event.data.text() : ''; }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/static/images/app-icon-192.png',
    badge: '/static/images/favicon.png',
    data: { url: data.url },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/netbanking/alerts';
  event.waitUntil(self.clients.matchAll({ type: 'window' }).then((wins) => {
    for (const w of wins) { if ('focus' in w) { w.navigate(url); return w.focus(); } }
    return self.clients.openWindow(url);
  }));
});
