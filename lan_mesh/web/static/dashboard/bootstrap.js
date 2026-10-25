// ── 初始化 ──
document.addEventListener('DOMContentLoaded',()=>{
  checkSecretaryState();
  refreshStation();
  connectWS();
  setInterval(refreshHosts,5000);
  refreshP2PHosts();
  // 安全网: 选举窗口(5s)结束后复查 Secretary 状态, 防止 WS 事件丢失
  setTimeout(checkSecretaryState, 6000);
});

// ── PWA Service Worker 注册 (iter-62 F5.4 移动端) ──
// SW 仅在 https 或 localhost 可用; 注册失败静默降级 (不影响功能)
if('serviceWorker' in navigator && (location.protocol==='https:'||location.hostname==='localhost'||location.hostname==='127.0.0.1')){
  window.addEventListener('load',()=>{
    navigator.serviceWorker.register('/sw.js').catch(()=>{/* 静默: 非安全上下文 */});
  });
}
