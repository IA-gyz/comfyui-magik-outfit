// Shared helpers for the JS tests.
//
// web/*.js are plain ES modules (no package.json "type"), which Node would treat as CommonJS.
// This copies them as .mjs into a temp dir laid out like ComfyUI serves them:
//   <tmp>/extensions/Magik_Outfit/*.mjs      (the extension files)
//   <tmp>/scripts/*.mjs                      (fake app/api for the wiring test, optional)
// and rewrites the import specifiers (.js -> .mjs) to match.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const webDir = path.join(here, "..", "web");

export function loadWebModules(fakeScripts = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "magik_outfit_js_"));
  const extDir = path.join(tmp, "extensions", "Magik_Outfit");
  const scriptsDir = path.join(tmp, "scripts");
  fs.mkdirSync(extDir, { recursive: true });
  fs.mkdirSync(scriptsDir, { recursive: true });

  const rewrite = (src) => src.replace(/(from\s+["'])((?:\.\.?\/)[^"']+)\.js(["'])/g, "$1$2.mjs$3");
  for (const f of fs.readdirSync(webDir)) {
    if (f.endsWith(".js")) {
      fs.writeFileSync(path.join(extDir, f.replace(/\.js$/, ".mjs")), rewrite(fs.readFileSync(path.join(webDir, f), "utf8")));
    } else if (f.endsWith(".css")) {
      fs.copyFileSync(path.join(webDir, f), path.join(extDir, f));
    }
  }
  for (const [name, source] of Object.entries(fakeScripts)) {
    fs.writeFileSync(path.join(scriptsDir, `${name}.mjs`), source);
  }
  return {
    dir: tmp,
    import: (name) => import(pathToFileURL(path.join(extDir, name.replace(/\.js$/, ".mjs"))).href),
  };
}

// jsdom is a dev dependency (npm i -D jsdom). JSDOM_DIR can point at an existing node_modules parent.
export async function loadJSDOM() {
  try {
    return (await import("jsdom")).JSDOM;
  } catch { /* fall through */ }
  if (process.env.JSDOM_DIR) {
    try {
      return createRequire(path.join(process.env.JSDOM_DIR, "noop.js"))("jsdom").JSDOM;
    } catch { /* fall through */ }
  }
  return null;
}

export function loadFixtures() {
  return JSON.parse(fs.readFileSync(path.join(here, "parity_fixtures.json"), "utf8"));
}
