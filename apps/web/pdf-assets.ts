import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import type { Plugin } from "vite";

/** Same-origin resources and their upstream licenses, in development and built output. */
export function pdfAssets(): Plugin {
    const root = dirname(createRequire(import.meta.url).resolve("pdfjs-dist/package.json"));
    const assets = new Map<string, Buffer>();
    for (const folder of ["cmaps", "standard_fonts"])
        for (const name of readdirSync(join(root, folder)))
            assets.set(`assets/pdfjs/${folder}/${name}`, readFileSync(join(root, folder, name)));
    assets.set("assets/pdfjs/LICENSE", readFileSync(join(root, "LICENSE")));
    return {
        name: "coinpup-pdf-resources",
        generateBundle() {
            for (const [fileName, source] of assets)
                this.emitFile({ type: "asset", fileName, source });
        },
        configureServer(server) {
            server.middlewares.use((request, response, next) => {
                const data = assets.get((request.url ?? "").replace(/^\//u, ""));
                if (!data) return next();
                response.setHeader("Content-Type", "application/octet-stream");
                response.end(data);
            });
        },
    };
}
