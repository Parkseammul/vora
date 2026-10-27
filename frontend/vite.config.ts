import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import localDesignReference from "./vite.reference.mjs";

export default defineConfig({
  plugins: [react(), localDesignReference()],
  server: {
    proxy: {
      "/workflow-executions": "http://localhost:8000",
      "/social": "http://localhost:8000", // YouTube/Instagram OAuth 요청 전달
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/testSetup.ts",
  },
});
