//hülle cachen, schnittstelle und bilder nicht
const SHELL_CACHE = "handy_shell_v3";
const SHELL_PATHS = [
  "/",
  "/phone.html",
  "/manifest.webmanifest",
  "/icon_192.png",
  "/icon_512.png",
  "/icon_maskable_192.png",
  "/icon_maskable_512.png"
];

function is_shell(url) {
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.indexOf("/api/") === 0) return false;
  return SHELL_PATHS.indexOf(url.pathname) !== -1;
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return Promise.all(SHELL_PATHS.map(function (path) {
        return fetch(path, {credentials: "same-origin"}).then(function (response) {
          if (response && response.ok) return cache.put(path, response);
          return null;
        }).catch(function () { return null; });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key !== SHELL_CACHE) return caches.delete(key);
        return null;
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const url = new URL(event.request.url);
  if (!is_shell(url) || event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).then(function (response) {
      if (response && response.ok) {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then(function (cache) {
          cache.put(event.request, copy);
        });
      }
      return response;
    }).catch(function () {
      return caches.match(url.pathname);
    })
  );
});
