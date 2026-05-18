#!/usr/bin/env node
// Smoke test for examples/openai-realtime/main.js protocol shape.
//
// The reference relay handles three production-grade behaviours: ephemeral
// session tokens, sample-rate matching (16 → 24 kHz on the way in),
// barge-in via `response.cancel` (not mute), and function calling. This
// test verifies the wire-protocol surface without running anything.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const src = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');

const checks = [
  // SAA surface.
  ['imports AttentionClient', /import\s*\{\s*AttentionClient\s*\}/.test(src)],
  ['subscribes to speechReady', /saa(Client)?\.on\("speechReady"|"speechReady"/.test(src)],

  // OpenAI Realtime surface.
  ['references gpt-realtime model', /model=gpt-realtime|"gpt-realtime"/.test(src)],
  ['uses input_audio_buffer.append', /input_audio_buffer\.append/.test(src)],
  ['uses input_audio_buffer.commit', /input_audio_buffer\.commit/.test(src)],
  ['uses response.create', /response\.create/.test(src)],

  // Ephemeral-token + browser-direct dual mode.
  ['supports ephemeral client_secret', /client_secret/.test(src)],
  ['supports browser-direct subprotocol', /openai-insecure-api-key/.test(src)],

  // Sample-rate matching (16 kHz SAA ↔ 24 kHz OpenAI).
  ['declares SAA_INPUT_RATE 16 kHz', /SAA_INPUT_RATE\s*=\s*16000/.test(src)],
  ['declares OPENAI_RATE 24 kHz', /OPENAI_RATE\s*=\s*24000/.test(src)],
  ['playback at OPENAI_RATE to match model output',
    /sampleRate:\s*OPENAI_RATE|sampleRate:\s*24000/.test(src)],

  // Barge-in design (no mute(), we cancel + drop queued playback instead).
  ['markResponding(true) when agent starts', /markResponding\??\.?\(true\)/.test(src)],
  ['markResponding(false) when agent stops', /markResponding\??\.?\(false\)/.test(src)],
  ['barge-in via response.cancel', /response\.cancel/.test(src)],
  ['drops queued playback on barge-in', /stopPlayback\(\)/.test(src)],

  // Function calling.
  ['accumulates function_call_arguments.delta',
    /response\.function_call_arguments\.delta/.test(src)],
  ['handles function_call_arguments.done',
    /response\.function_call_arguments\.done/.test(src)],
  ['emits function_call_output via conversation.item.create',
    /conversation\.item\.create[\s\S]*function_call_output/.test(src)],
  ['registers get_weather tool', /"get_weather"/.test(src)],
  ['registers set_timer tool', /"set_timer"/.test(src)],

  // Transcripts + UX.
  ['shows user transcripts',
    /conversation\.item\.input_audio_transcription\.completed/.test(src)],
  ['handles response.audio.delta', /response\.audio\.delta/.test(src)],
  ['handles response.done', /response\.done/.test(src)],
];

let fail = 0;
for (const [name, ok] of checks) {
  if (ok) console.log(`✓ ${name}`);
  else { console.log(`✗ ${name}`); fail++; }
}
console.log(`openai-realtime shape: ${checks.length - fail}/${checks.length} pass`);
if (fail > 0) process.exit(1);
