import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  test: {
    globals: true,
    include: ["test/**/*.test.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      // axios is a transitive dep of @centaur/api-client; resolve it for tests
      // that import bot.ts (which uses AxiosError)
      "axios": path.resolve(__dirname, "node_modules/@centaur/api-client/node_modules/axios"),
    },
  },
});
