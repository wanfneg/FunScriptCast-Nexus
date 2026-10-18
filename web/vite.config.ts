import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 生产：构建到 web/dist，由 Python 宿主在 /app/ 下托管。
// 开发：vite dev server 把 /api 代理到本机 8790。
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": { target: "http://127.0.0.1:8790", changeOrigin: false },
    },
  },
});
