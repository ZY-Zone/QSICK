/**
 * QA 截图脚本（增强版）：整页 / 分段 / 按 section 精确截图。
 * 用法: node qa-shot.mjs <url> <outdir> <prefix> [width] [height] [mobile0/1]
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [url, outdir, prefix, wArg, hArg, mobileArg] = process.argv.slice(2);
const width = parseInt(wArg || "1440", 10);
const height = parseInt(hArg || "1000", 10);
const mobile = mobileArg === "1";
const endpoint = "http://127.0.0.1:9222";
const maxH = parseInt(process.env.QA_MAX_H || "16000", 10);

mkdirSync(outdir, { recursive: true });

const version = await (await fetch(endpoint + "/json/version")).json();
const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.onopen = res;
  ws.onerror = rej;
});

let nextId = 1;
const pending = new Map();
const listeners = new Map();

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  } else if (msg.method) {
    (listeners.get(msg.method) || []).forEach((fn) => fn(msg.params, msg.sessionId));
  }
};

function send(method, params = {}, sessionId) {
  const id = nextId++;
  const payload = { id, method, params };
  if (sessionId) payload.sessionId = sessionId;
  ws.send(JSON.stringify(payload));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
function on(method, fn) {
  if (!listeners.has(method)) listeners.set(method, []);
  listeners.get(method).push(fn);
}

const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
const P = (m, p) => send(m, p, sessionId);

await P("Page.enable");
await P("Runtime.enable");
await P("Network.enable");
await P("Network.setCacheDisabled", { cacheDisabled: true });
await P("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
await P("Page.navigate", { url });
await new Promise((res) => {
  const timer = setTimeout(res, 5000);
  on("Page.loadEventFired", () => {
    clearTimeout(timer);
    setTimeout(res, 1000);
  });
});

await P("Runtime.evaluate", {
  expression: `(() => {
    document.querySelectorAll('[data-reveal]').forEach(el => el.classList.add('in'));
    document.querySelectorAll('.terminal').forEach(el => el.classList.add('in'));
    document.documentElement.style.scrollBehavior = 'auto';
    window.scrollTo(0, 0);
    return true;
  })()`,
});
await new Promise((r) => setTimeout(r, 800));

const metrics = await P("Page.getLayoutMetrics");
const content = metrics.cssContentSize || metrics.contentSize;
const contentHeight = Math.min(Math.ceil(content.height), maxH);

async function shot(name, clip) {
  const params = { format: "png", captureBeyondViewport: true };
  if (clip) params.clip = { x: 0, y: clip.y, width, height: Math.max(1, Math.round(clip.height)), scale: 1 };
  const { data } = await P("Page.captureScreenshot", params);
  writeFileSync(join(outdir, name), Buffer.from(data, "base64"));
  console.log("saved", name, Math.round(clip ? clip.height : 0));
}

const wantFull = process.env.QA_FULL !== "0";
if (wantFull) await shot(prefix + "-full.png", { y: 0, height: contentHeight });

// 按 section 截图
const targets = [
  ["hero", "#top"],
  ["features", "#features"],
  ["architecture", "#architecture"],
  ["quickstart", "#quickstart"],
  ["stats", ".stats-band"],
  ["docs", "#docs"],
  ["ecosystem", "#ecosystem"],
  ["community", "#community"],
  ["disclaimer", "#disclaimer"],
  ["footer", ".site-footer"],
];
const positions = await P("Runtime.evaluate", {
  expression: `JSON.stringify(${JSON.stringify(targets.map((t) => t[1]))}.map(sel => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { top: Math.max(0, r.top + window.scrollY - 30), height: Math.min(r.height + 60, 3000) };
  }))`,
  returnByValue: true,
});
const arr = JSON.parse(positions.result.value);
for (let i = 0; i < targets.length; i++) {
  const pos = arr[i];
  if (!pos) continue;
  await shot(prefix + "-" + targets[i][0] + ".png", { y: pos.top, height: pos.height });
}

console.log("contentHeight", contentHeight, "width", width);
ws.close();
process.exit(0);
