import react from "@vitejs/plugin-react";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig } from "vite";

// Dev server proxies API calls to FastAPI, so the browser sees ONE origin:
// no CORS preflights and the same URLs as in production (where nginx proxies).
// 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first (+~200 ms).
const API = process.env.VITE_API_PROXY ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  // Dependency pre-bundle cache outside the project folder: cloud-sync tools
  // (OneDrive, Dropbox) lock files they are uploading, which made Vite fail
  // with "EPERM: operation not permitted, rmdir node_modules/.vite/deps".
  cacheDir: join(tmpdir(), "documind-vite-cache"),
  server: {
    // Bind IPv4 explicitly: "localhost" resolves to IPv6 ::1 only on some
    // Windows machines, so http://127.0.0.1:5173 would be refused.
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: false },
      "/health": { target: API, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
