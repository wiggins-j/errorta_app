import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

// Errorta frontend (Vite + React).
// Tauri spawns this dev server on `tauri dev` and bundles the built output
// for production via `tauri build`.
export default defineConfig({
  plugins: [react()],

  // Tauri expects a fixed dev-server port so its webview can connect.
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: "127.0.0.1",
    watch: {
      // Don't watch the Rust shell or Python sidecar from the JS-side watcher.
      ignored: ["**/src-tauri/**", "**/python/**"],
    },
  },

  // Tauri injects env vars prefixed with TAURI_; ignore those during build.
  envPrefix: ["VITE_", "TAURI_ENV_"],

  build: {
    target: "es2022",
    minify: "oxc",
    sourcemap: !!process.env.TAURI_DEBUG,
  },
});
