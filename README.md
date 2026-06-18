# saa-js-demo

End-to-end browser demo for attention labs SAA (Selective Auditory Attention) SDK — streams microphone and webcam to the SAA Server, then forwards detected speech segments to OpenAI Realtime.

Everything runs in the browser. The server tells you *when* someone is speaking and what they said; this demo routes that speech to the LLM of your choice (OpenAI Realtime is shown).

## What you'll need

- A SAA auth token (sign up on the dashboard [here](https://attentionlabs.ai/dashboard/))
- An OpenAI API key with Realtime access *(optional — omit to run and just see live predictions)*
- Node 18+ and a modern browser (Chrome, Edge, Safari, Firefox)

## Setup

Follow these steps in order:

### 1. Clone and install

```bash
git clone https://github.com/attenlabs/saa-js-demo
cd saa-js-demo
npm install
```

### 2. Serve the repo

```bash
npx serve
```

This prints a local URL — typically `http://localhost:3000`. Open it in your browser.

### 3. Connect

In the **Setup** panel (top-left of the page), paste your SAA token (and optionally your OpenAI key) and click **Connect**. The browser will prompt for microphone and camera access — allow both.

After a short warmup, the orb starts reacting to your voice.

## URL parameters

All optional. The token / key fields stay editable in the UI. URL params just auto-populate them for future runs.

| param         | notes |
| ------------- | ----- |
| `token`       | Pre-fills the SAA auth token field. |
| `openai_key`  | Pre-fills the OpenAI key field.  Omit to just watch predictions and VAD.  |

Example: `/?token=al_live_…&openai_key=sk-…`

## How it works

1. [`app.js`](app.js) constructs an `AttentionClient` from [`saa-js`](https://www.npmjs.com/package/saa-js), which acquires the mic + webcam and opens a WebSocket to the SAA server.
2. The SDK emits typed events — `prediction`, `vad`, `state`, `turnReady`, `warmupComplete`.  `app.js` renders into the UI.
3. On `turnReady`, `app.js` hands the PCM16 audio (and any attached JPEG frames) to [`llm.js`](llm.js), a small OpenAI Realtime bridge that wraps them into `input_audio` / `input_image` content parts and plays the response back through WebAudio.
4. While the LLM is speaking, `app.js` calls `client.mute()` + `client.markResponding(true)` so the server stops emitting predictions until playback ends.
6. A small guided flow (top-of-screen pill) walks first-time users through *talk to the computer → talk to each other → free play*.

The LLM bridge is deliberately part of this demo, not the SDK — swap in whichever provider you like.

## Security note

This demo accepts the OpenAI API key in the browser (typed into the UI or passed via URL) for simplicity. **Never do that in production**, always proxy the Realtime connection through a server you control so the key never reaches the client.

## License

MIT
