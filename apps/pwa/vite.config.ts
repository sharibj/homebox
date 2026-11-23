import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

export default defineConfig({
  plugins: [
    vue(),
    {
      name: "static-shell-only",
      writeBundle(_options, bundle) {
        const files = ["/", "/index.html", "/icon.svg", "/icon-192.png", "/icon-512.png", "/manifest.webmanifest",
          ...Object.keys(bundle).filter(file => file.startsWith("assets/")).map(file => `/${file}`)];
        const version = createHash("sha256").update(JSON.stringify(files)).digest("hex").slice(0, 16);
        const worker = readFileSync("public/sw.js", "utf8")
          .replace("__SHELL_FILES__", JSON.stringify(files))
          .replace("__SHELL_VERSION__", `homebox-companion-${version}`);
        writeFileSync("dist/sw.js", worker);
      },
    },
  ],
  server: { host: "0.0.0.0" },
});
