import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import federation from "@originjs/vite-plugin-federation"
import { fileURLToPath } from "node:url"
import { resolve } from "node:path"

const currentDir = fileURLToPath(new URL(".", import.meta.url))
const outputDir = resolve(currentDir, "../plugins.v3/quarkchannel/dist/assets")

export default defineConfig({
  base: "./",
  plugins: [
    vue(),
    federation({
      name: "quarkchannel",
      filename: "remoteEntry.js",
      exposes: {
        "./Config": "./src/components/Config.vue",
        "./AppPage": "./src/components/AppPage.vue",
      },
      shared: {
        vue: { requiredVersion: false, generate: false },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
        "vuetify/styles": { requiredVersion: false, generate: false, singleton: true },
      },
      format: "esm",
    }),
  ],
  build: {
    target: "esnext",
    minify: "esbuild",
    cssCodeSplit: true,
    emptyOutDir: true,
    outDir: outputDir,
    assetsDir: "",
  },
})
