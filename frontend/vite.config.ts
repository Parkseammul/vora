import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/workflow-executions": "http://localhost:8000" } },
  test: { environment: "jsdom", setupFiles: "./src/testSetup.ts" },
});
