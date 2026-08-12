import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createRequire } from "node:module";

const { version } = createRequire(import.meta.url)("./package.json");

export default defineConfig({
  plugins: [react()],
  define: {
    __CADASTRE_VERSION__: JSON.stringify(version),
  },
  build: { outDir: "dist", sourcemap: false },
});
