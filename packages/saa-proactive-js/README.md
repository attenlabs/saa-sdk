<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# `@attenlabs/saa-proactive`

Lifecycle helper for **proactive voice agents** on top of [`@attenlabs/saa-js`](https://www.npmjs.com/package/@attenlabs/saa-js). The agent speaks first; SAA gates the reply.

Apache-2.0. Zero runtime deps. Peer-dep on `@attenlabs/saa-js`. Works in Node 18+, the browser, Deno, Bun, and edge runtimes.

## Install

```bash
npm install @attenlabs/saa-proactive @attenlabs/saa-js
```

## `ProactiveLifecycle`

Wraps a `speak` callback with `markResponding(true) → speak → tail → markResponding(false)`. Catches errors and releases the gate either way.

```js
import { AttentionClient } from "@attenlabs/saa-js";
import { ProactiveLifecycle } from "@attenlabs/saa-proactive";

const saa = new AttentionClient({ token: process.env.ATTENLABS_TOKEN });
await saa.start();

const lifecycle = new ProactiveLifecycle({ client: saa, tailMs: 200 });

await lifecycle.run(async () => {
  // Your framework's "speak first" call. Examples:
  //   ws.send(JSON.stringify({ type: "response.create", response: { instructions: "…" } }));
  //   convo.sendUserMessage("…");
});
```

The lifecycle is single-use per instance; create a fresh one per turn or `await` the previous one.

```js
import { runProactiveTurn } from "@attenlabs/saa-proactive";
await runProactiveTurn({ client: saa }, () => agent.speakOpening());
```

## `TriggerHub`

In-process pub/sub for proactive-turn events. Used by framework overlays to relay `POST /trigger` HTTP webhooks to connected browsers via Server-Sent Events.

`sseStream()` returns a Web-Streams `ReadableStream<Uint8Array>`. The example below is Fetch-style (Hono, Bun, Cloudflare Workers); for Node `http` / Express, adapt the stream with `Readable.fromWeb(...)` (shown after).

```js
import { TriggerHub } from "@attenlabs/saa-proactive";

const hub = new TriggerHub();

app.post("/trigger", async (req, res) => {
  const { instructions } = await req.json();
  res.json({ ok: true, subscribers: hub.publish({ instructions }) });
});

app.get("/trigger-events", (req, res) => {
  res.setHeader("content-type", "text/event-stream");
  hub.sseStream().pipeTo(res);
});
```

For Express / Node `http` (`res` is a Node `Writable`, not a Web `WritableStream`):

```js
import { Readable } from "node:stream";

app.get("/trigger-events", (req, res) => {
  res.setHeader("content-type", "text/event-stream");
  Readable.fromWeb(hub.sseStream()).pipe(res);
});
```

The hub does not bind to a specific HTTP framework. Exposed API:

- `publish(event)`: validates `event.instructions` is a non-empty string; returns subscriber count.
- `subscribe()`: returns `{ events(), close() }`. `events()` is an async generator.
- `sseStream(subscriber?)`: returns a `ReadableStream<Uint8Array>` of SSE frames.

## What this package does not do

- **It does not decide *when* to speak.** Proactivity policy lives in your LLM / scheduler / orchestrator.
- **It is not a learned model.** No accuracy claims. The cloud classifier holds those.
- **It does not extend the SAA wire.** No new message types, no new SDK events.

## Tests

```bash
node --test packages/saa-proactive-js/tests/*.test.mjs
```

Includes both pure unit tests and integration tests that instantiate the real `AttentionClient`, intercept `sendControl`, and assert the `responding_start → responding_stop` flow.

## See also

- [`examples/proactive-agent/`](../../examples/proactive-agent/): five working overlays using this helper.
- [`@attenlabs/saa-js`](../saa-js/): the peer-dep cloud SDK.
- [`@attenlabs/saa-gate`](../saa-gate/): the production routing policy state machine.
- [`attenlabs-saa-proactive`](../saa-proactive-py/): the Python twin of this helper.

## License

Apache-2.0. See [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
