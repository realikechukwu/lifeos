{% load static %}
const CACHE_PREFIX = "family-assistant-public-";
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const OFFLINE_URL = "{% static 'offline.html' %}";
const STATIC_PATH = new URL("{% get_static_prefix %}", self.location.origin).pathname;
const PUBLIC_ASSETS = [
  OFFLINE_URL,
  "{% static 'css/app.css' %}",
  "{% static 'icons/app-icon.svg' %}",
  "{% static 'icons/app-icon-192.png' %}",
  "{% static 'icons/app-icon-512.png' %}",
  "{% static 'icons/app-icon-maskable-192.png' %}",
  "{% static 'icons/app-icon-maskable-512.png' %}",
  "{% static 'icons/apple-touch-icon.png' %}",
  "{% static 'icons/favicon-32x32.png' %}"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PUBLIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

async function cachePublicAsset(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  if (response.ok || response.type === "opaque") {
    await cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const {request} = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Authenticated documents and JSON always come from the network and are
  // never written to Cache Storage. Only the public offline page is returned
  // when a navigation cannot connect.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  const isLocalStaticAsset =
    url.origin === self.location.origin && url.pathname.startsWith(STATIC_PATH);
  const isJsDelivrAsset =
    url.origin === "https://cdn.jsdelivr.net" && ["font", "script", "style"].includes(request.destination);

  if (isLocalStaticAsset || isJsDelivrAsset) {
    event.respondWith(cachePublicAsset(request));
  }
});
