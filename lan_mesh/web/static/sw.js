/*
 * LAN Mesh 仪表盘 Service Worker (iter-62 F5.4 移动端 PWA)
 *
 * 能力:
 *   - 预缓存应用壳 (/ 仪表盘 + /static/manifest.json)
 *   - navigate 请求 network-first, 离线时回退缓存壳 (断网可打开)
 *   - API 请求 (/api/) 一律 network-only 不缓存 (数据实时性优先)
 *   - 静态资源 stale-while-revalidate (后台更新缓存)
 *
 * 注意: SW 经根路径 /sw.js 挂载 (scope 默认 /), 认证白名单放行
 * (SW 注册请求由浏览器发起, 不携带 Authorization 头)。
 */
const CACHE_NAME = 'lan-mesh-shell-v1';
const SHELL_URLS = ['/', '/static/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 非 GET 或非同源: 直接透传
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }

  // API 请求: network-only (数据实时性, 失败透传错误给前端处理)
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // 页面导航: network-first, 离线回退缓存壳
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match('/').then((resp) => resp || Response.error())
      )
    );
    return;
  }

  // 静态资源: stale-while-revalidate
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetched = fetch(event.request)
        .then((resp) => {
          if (resp && resp.status === 200) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return resp;
        })
        .catch(() => cached || Response.error());
      return cached || fetched;
    })
  );
});
