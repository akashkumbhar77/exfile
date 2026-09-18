// Vite dev server config for the dashboard (SPEC-PATCH-003).
// /api is proxied to the FastAPI backend (`uv run python cli.py api`, :8000), so the browser
// talks to one origin in development; the backend also allows http://localhost:5173 via CORS.
// Styling: CSS Modules (built into Vite; see docs/DECISIONS.md "S5 styling").
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const API_TARGET = process.env.API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: false },
    },
  },
  css: {
    modules: { localsConvention: "camelCaseOnly" },
  },
  test: {
    environment: "jsdom",
    css: { modules: { classNameStrategy: "non-scoped" } },
  },
});
