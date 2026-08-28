import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// iter-56: SPA 构建产物由服务端 /spa 路由托管 (lan_mesh/web/static/spa/)
// base 必须为 /spa/ 使静态资源引用与服务端挂载点一致
export default defineConfig({
  plugins: [react()],
  base: "/spa/",
  build: {
    outDir: "../lan_mesh/web/static/spa",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1024,
  },
  server: {
    // 开发模式代理到本机 Station (默认 45500)
    proxy: {
      "/api": "http://127.0.0.1:45500",
      "/ws": { target: "ws://127.0.0.1:45500", ws: true },
    },
  },
});
