import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `vite dev` outside the stack, proxy /api to the BFF on its host port.
// In production the static bundle is served by APISIX which routes /api -> bff.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8005",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
