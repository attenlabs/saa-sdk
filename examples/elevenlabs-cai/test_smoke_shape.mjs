#!/usr/bin/env node
// No-network shape check for the SAA × ElevenLabs CAI browser bundle.
//
// Verifies that main.js uses the official ElevenLabs JS SDK
// (@elevenlabs/client) and the SAA JS SDK (@attenlabs/saa-js@^1) in the
// production-grade pattern: server-side token mint, rich SAA signals,
// EL setMicMuted gate, markResponding on agent speaking,
// sendContextualUpdate from speechReady + face-count, three clientTools,
// dynamicVariables on startSession, sendUserMessage + sendFeedback.
//
// Runs as a Node --test suite so it slots into examples-smoke-shape CI.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
const page = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

const expectations = {
  "imports AttentionClient from @attenlabs/saa-js (pinned ^1)":
    /import\s*\{\s*AttentionClient\s*\}\s*from\s*["'][^"']*@attenlabs\/saa-js@[\w.-]+/.test(main),
  "imports Conversation from @elevenlabs/client (pinned ^1)":
    /import\s*\{\s*Conversation\s*\}\s*from\s*["'][^"']*@elevenlabs\/client@\^1/.test(main),
  "fetches /api/conversation-config (no inline secrets)":
    /fetch\(\s*["']\/api\/conversation-config["']/.test(main),
  "mints WebRTC token via /api/conversation-token":
    /fetch\(\s*["']\/api\/conversation-token["']/.test(main),
  "mints WebSocket signed URL via /api/signed-url":
    /fetch\(\s*["']\/api\/signed-url["']/.test(main),
  "calls Conversation.startSession":
    /Conversation\.startSession\(/.test(main),
  "passes connectionType from server config":
    /connectionType:\s*config\.connectionType/.test(main),
  "wires SAA prediction": /attention\.on\(["']prediction["']/.test(main),
  "wires SAA vad": /attention\.on\(["']vad["']/.test(main),
  "wires SAA state": /attention\.on\(["']state["']/.test(main),
  "wires SAA speechReady": /attention\.on\(["']speechReady["']/.test(main),
  "wires SAA stats": /attention\.on\(["']stats["']/.test(main),
  "wires SAA reconnecting": /attention\.on\(["']reconnecting["']/.test(main),
  "wires SAA reconnected": /attention\.on\(["']reconnected["']/.test(main),
  "wires SAA error": /attention\.on\(["']error["']/.test(main),
  "wires SAA disconnected": /attention\.on\(["']disconnected["']/.test(main),
  "drives EL setMicMuted from SAA decisions":
    /convo\.setMicMuted\(/.test(main),
  "calls markResponding(true) on EL speaking":
    /markResponding\(true\)/.test(main),
  "calls markResponding(false) when EL stops speaking":
    /markResponding\(false\)/.test(main),
  "reacts to EL onModeChange":
    /onModeChange:\s*\(\s*\{?\s*mode\s*\}?\s*\)/.test(main),
  "reacts to EL onStatusChange": /onStatusChange:/.test(main),
  "reacts to EL onMessage": /onMessage:/.test(main),
  "reacts to EL onError": /onError:/.test(main),
  "reacts to EL onConnect": /onConnect:/.test(main),
  "reacts to EL onDisconnect": /onDisconnect:/.test(main),
  "reacts to EL onUnhandledClientToolCall": /onUnhandledClientToolCall:/.test(main),
  "sends sendContextualUpdate from SAA events":
    /sendContextualUpdate\(/.test(main),
  "registers clientTools.get_user_attention":
    /get_user_attention\s*:/.test(main),
  "registers clientTools.get_face_count":
    /get_face_count\s*:/.test(main),
  "registers clientTools.get_last_directed_utterance":
    /get_last_directed_utterance\s*:/.test(main),
  "passes dynamicVariables to startSession":
    /dynamicVariables\s*:\s*\{/.test(main),
  "exposes sendUserMessage for typed input":
    /sendUserMessage\(/.test(main),
  "exposes sendFeedback for thumbs up/down":
    /sendFeedback\?\.\(\s*true\s*\)|sendFeedback\(\s*true\s*\)/.test(main),
  "reacts to onCanSendFeedbackChange":
    /onCanSendFeedbackChange:/.test(main),
  "updates SAA threshold live via setThreshold":
    /attention\?\.setThreshold\(/.test(main),
  "handles EL speaking mode without privacy-muting SAA (preserves barge-in)":
    !/markResponding\(true\)[\s\S]{0,80}attention\.mute\(\)/.test(main),
  "page loads main.js as a module":
    /<script[^>]*type="module"[^>]*main\.js/.test(page),
};

for (const [name, ok] of Object.entries(expectations)) {
  test(`elevenlabs-cai browser bundle: ${name}`, () => {
    assert.ok(ok, `expectation failed: ${name}`);
  });
}
