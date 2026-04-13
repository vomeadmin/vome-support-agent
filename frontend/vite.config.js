import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ops": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
