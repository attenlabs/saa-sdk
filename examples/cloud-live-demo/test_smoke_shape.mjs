#!/usr/bin/env node
// Smoke test for examples/cloud-live-demo. Shape-only: no browser, no network.
// Verifies the demo still wires the SDK surface it depends on and renders the
// load-bearing affordances (60-second countdown, demo-token preflight, RTT
// badge, waveform overlay, Coming-Soon fallback).
//
// Run via `node test_smoke_shape.mjs`.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const js   = fs.readFileSync(path.join(__dirname, "main.js"),    "utf8");
const readme = fs.readFileSync(path.join(__dirname, "README.md"), "utf8");

const checks = [
  // SDK wiring (version-agnostic so it survives the 0.2.0 → 1.0.0 launch bump)
  ["loads saa-js from a CDN",                      /esm\.sh\/@attenlabs\/saa-js@[\w.-]+/.test(js)],
  ["dynamic-imports AttentionClient",              /AttentionClient\s*=\s*mod\.AttentionClient|import\(\s*CONFIG\.sdkUrl/.test(js)],
  ["surfaces SDK-load failure to the user",        /showComingSoon\(/.test(js)],
  ["on('connected')",                              /\.on\(\s*["']connected["']/.test(js)],
  ["on('started')",                                /\.on\(\s*["']started["']/.test(js)],
  ["on('warmupComplete')",                         /\.on\(\s*["']warmupComplete["']/.test(js)],
  ["on('prediction')",                             /\.on\(\s*["']prediction["']/.test(js)],
  ["on('vad')",                                    /\.on\(\s*["']vad["']/.test(js)],
  ["on('state')",                                  /\.on\(\s*["']state["']/.test(js)],
  ["on('config')",                                 /\.on\(\s*["']config["']/.test(js)],
  ["on('stats')",                                  /\.on\(\s*["']stats["']/.test(js)],
  ["on('error')",                                  /\.on\(\s*["']error["']/.test(js)],
  ["on('disconnected')",                           /\.on\(\s*["']disconnected["']/.test(js)],
  ["calls client.start({ videoElement })",         /client\.start\(\s*\{\s*videoElement/.test(js)],

  // demo-token preflight: this is the differentiator from browser-demo
  ["POSTs /api/demo-token",                        /\/api\/demo-token/.test(js) && /method:\s*["']POST["']/.test(js)],
  ["clamps server-provided expires_in_sec",        /expires_in_sec/.test(js) && /clamp\(/.test(js)],
  ["reads ws_url override from server",            /ws_url/.test(js)],
  ["handles 429 rate-limit",                       /res\.status === 429/.test(js)],
  ["handles 503 / ready:false capacity",           /res\.status === 503|ready === false/.test(js)],
  ["handles 404 endpoint-missing",                 /res\.status === 404/.test(js)],
  ["#api= URL-fragment override",                  /\bapi\b/.test(js) && /location\.hash/.test(js)],
  ["reads meta[name='saa-api-base']",              /saa-api-base/.test(js)],

  // 60-second session lifecycle
  ["hard session cap (60s default)",               /sessionMaxSec:\s*60/.test(js)],
  ["countdown timer per second",                   /setInterval\(tick/.test(js)],
  ["clears countdown + session timer on stop",     /clearInterval\(countdownTimer\)|clearTimeout\(sessionTimer\)/.test(js)],
  ["stops session when countdown hits 0",          /remaining <=\s*0/.test(js)],

  // Live waveform sibling AnalyserNode
  ["creates AnalyserNode for waveform",            /createAnalyser\(\)/.test(js)],
  ["reads time-domain data",                       /getFloatTimeDomainData/.test(js)],
  ["renders waveform via requestAnimationFrame",   /requestAnimationFrame/.test(js)],

  // RTT / latency badge
  ["RTT badge classes by threshold",               /under-target|under_target/.test(js) && /150|300/.test(js)],

  // num_faces from server (NOT MediaPipe)
  ["face badge driven by server numFaces",         /numFaces/.test(js) && /faceBadge/.test(js)],
  ["does NOT bundle MediaPipe",                    !/mediapipe|MediaPipe|@mediapipe/.test(js)],

  // HTML structural
  ["index.html has video id=video",                /<video[^>]+id="video"/.test(html)],
  ["index.html has waveform canvas",               /id="waveform"/.test(html)],
  ["index.html has countdown overlay",             /id="countdown"/.test(html)],
  ["index.html has comingsoon banner",             /id="comingsoon"/.test(html)],
  ["index.html has start gate + button",           /id="gate"/.test(html) && /id="startBtn"/.test(html)],
  ["index.html has prediction pill",               /id="predPill"/.test(html)],
  ["index.html has latency badge",                 /id="latencyBadge"/.test(html)],
  ["index.html has face badge",                    /id="faceBadge"/.test(html)],
  ["index.html declares lang=\"en\"",              /<html\s+lang="en"/.test(html)],
  ["index.html declares an SVG favicon",           /rel="icon"[^>]*data:image\/svg/.test(html)],

  // Install/docs row points at the real published artifacts
  ["links real npm package",                       /npmjs\.com\/package\/@attenlabs\/saa-js/.test(html)],
  ["links real PyPI package",                      /pypi\.org\/project\/attenlabs-saa/.test(html)],
  ["links JS docs reference",                      /attentionlabs\.ai\/docs\/js\/reference/.test(html)],
  ["links Python docs reference",                  /attentionlabs\.ai\/docs\/python\/reference/.test(html)],

  // README documents the server contract for the prod-server team
  ["README documents POST /api/demo-token",        /POST\s+\/api\/demo-token/.test(readme)],
  ["README spells out per-IP rate limit",          /Per-IP rate limit/i.test(readme)],
  ["README spells out per-token concurrency cap",  /Per-token concurrency cap/i.test(readme)],
  ["README spells out server-side kill",           /Server-side kill/i.test(readme)],
  ["README rules out on-device client variant claim", /no client-side learned variant/i.test(readme)],
  ["README rules out browser-side face tracker",     /No browser-side face tracker/i.test(readme)],
];

let fail = 0;
for (const [name, ok] of checks) {
  if (ok) console.log(`✓ ${name}`);
  else    { console.log(`✗ ${name}`); fail++; }
}
console.log(`cloud-live-demo shape: ${checks.length - fail}/${checks.length} pass`);
assert.equal(fail, 0, `${fail} smoke checks failed`);
