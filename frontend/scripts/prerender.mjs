import { createServer } from "vite";
import { readFile, writeFile, mkdir } from "node:fs/promises";
const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
try {
  const { render } = await server.ssrLoadModule("/src/prerender.tsx");
  const pages = JSON.parse(await readFile("public/seo.json", "utf8"));
  const template = await readFile("dist/index.html", "utf8");
  // Use Vite's production asset map so pre-rendered video URLs are deployable.
  const manifest = JSON.parse(await readFile("dist/.vite/manifest.json", "utf8"));
  await mkdir("dist/prerender", { recursive: true });
  for (const path of Object.keys(pages)) {
    let markup = render(path);
    for (const [source, asset] of Object.entries(manifest)) {
      if (source.startsWith("src/assets/")) markup = markup.replaceAll(`/${source}`, `/${asset.file}`);
    }
    const poster = path === "/" ? markup.match(/poster="([^"]+)"/)?.[1] : null;
    const document = poster ? template.replace("</head>", `<link rel="preload" as="image" href="${poster}"></head>`) : template;
    await writeFile(`dist/prerender/${path === "/" ? "home" : path.slice(1)}.html`, document.replace('<div id="root"></div>', `<div id="root">${markup}</div>`));
  }
} finally { await server.close(); }
