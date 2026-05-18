// Offline tests for ProactiveLifecycle + TriggerHub. No
// @attenlabs/saa-js dep needed: we stub markResponding with a
// trivial mock so the test only exercises the lifecycle semantics.

import test from "node:test";
import assert from "node:assert/strict";

import {
  ProactiveLifecycle,
  runProactiveTurn,
  TriggerHub,
  VERSION,
} from "../src/index.js";


function mockClient() {
  /** @type {boolean[]} */
  const calls = [];
  return {
    calls,
    markResponding(active) {
      calls.push(active);
    },
  };
}


test("VERSION is a string", () => {
  assert.equal(typeof VERSION, "string");
  assert.match(VERSION, /^\d+\.\d+\.\d+/);
});


test("ProactiveLifecycle: happy path asserts true then false around speak", async () => {
  const client = mockClient();
  let speakCalled = false;
  await new ProactiveLifecycle({ client, tailMs: 0 }).run(async () => {
    assert.deepEqual(client.calls, [true]);
    speakCalled = true;
  });
  assert.ok(speakCalled);
  assert.deepEqual(client.calls, [true, false]);
});


test("ProactiveLifecycle: speak throwing still releases the gate", async () => {
  const client = mockClient();
  await assert.rejects(
    () => new ProactiveLifecycle({ client, tailMs: 0 }).run(() => {
      throw new Error("speak failed");
    }),
    /speak failed/,
  );
  assert.deepEqual(client.calls, [true, false]);
});


test("ProactiveLifecycle: rejects nested concurrent runs", async () => {
  const client = mockClient();
  const life = new ProactiveLifecycle({ client, tailMs: 0 });
  let inner = null;
  await life.run(async () => {
    inner = await life.run(() => {}).then(
      () => "resolved",
      (e) => e.message,
    );
  });
  assert.match(inner, /lifecycle already active/);
});


test("ProactiveLifecycle: tailMs delays the false assertion", async () => {
  // Use a generous tailMs and a small tolerance so the assertion is
  // robust against slow CI runners. The 5 ms tolerance covers normal
  // timer-skew while still proving the wait happened.
  const TAIL_MS = 200;
  const TOLERANCE_MS = 5;
  const client = mockClient();
  const start = Date.now();
  await new ProactiveLifecycle({ client, tailMs: TAIL_MS }).run(() => {});
  const elapsed = Date.now() - start;
  assert.ok(
    elapsed >= TAIL_MS - TOLERANCE_MS,
    `tailMs should have waited >= ${TAIL_MS - TOLERANCE_MS}ms (got ${elapsed}ms)`,
  );
});


test("ProactiveLifecycle: rejects bad client", () => {
  assert.throws(() => new ProactiveLifecycle({ client: null }), /markResponding/);
  assert.throws(() => new ProactiveLifecycle({ client: { markResponding: 42 } }), /markResponding/);
});


test("ProactiveLifecycle: rejects negative tailMs", () => {
  assert.throws(() => new ProactiveLifecycle({ client: mockClient(), tailMs: -1 }), /tailMs/);
});


test("runProactiveTurn: same semantics as new+run", async () => {
  const client = mockClient();
  await runProactiveTurn({ client, tailMs: 0 }, () => {});
  assert.deepEqual(client.calls, [true, false]);
});


test("TriggerHub: publish fans out to all subscribers", async () => {
  const hub = new TriggerHub();
  const a = hub.subscribe();
  const b = hub.subscribe();
  assert.equal(hub.subscriberCount, 2);

  const fanout = hub.publish({ instructions: "hello" });
  assert.equal(fanout, 2);

  const aGen = a.events();
  const bGen = b.events();
  const ev1 = await aGen.next();
  const ev2 = await bGen.next();
  assert.equal(ev1.value.instructions, "hello");
  assert.equal(ev2.value.instructions, "hello");

  a.close();
  b.close();
  assert.equal(hub.subscriberCount, 0);
});


test("TriggerHub: publish rejects malformed event", () => {
  const hub = new TriggerHub();
  assert.throws(() => hub.publish({}), /instructions/);
  assert.throws(() => hub.publish({ instructions: "" }), /instructions/);
  assert.throws(() => hub.publish({ instructions: 42 }), /instructions/);
});


test("TriggerHub: queued events arrive in order even before subscribe", async () => {
  const hub = new TriggerHub();
  const sub = hub.subscribe();
  hub.publish({ instructions: "first" });
  hub.publish({ instructions: "second" });

  const gen = sub.events();
  const a = await gen.next();
  const b = await gen.next();
  assert.equal(a.value.instructions, "first");
  assert.equal(b.value.instructions, "second");
  sub.close();
});


test("TriggerHub: close() during pending await does not leak a sentinel to the consumer", async () => {
  const hub = new TriggerHub();
  const sub = hub.subscribe();
  const consumed = [];
  const reader = (async () => {
    for await (const ev of sub.events()) {
      consumed.push(ev);
    }
  })();
  // Close while the consumer is awaiting the next event.
  setTimeout(() => sub.close(), 20);
  await reader;
  assert.equal(consumed.length, 0, "close should not emit a phantom trigger");
});

test("TriggerHub: subscriber queue is bounded (drops on overflow)", () => {
  const hub = new TriggerHub();
  const sub = hub.subscribe();
  // Queue limit is 64 in lockstep with the Python twin. Push 100 events;
  // the first 64 enqueue, the rest drop silently.
  for (let i = 0; i < 100; i++) {
    hub.publish({ instructions: `payload-${i}` });
  }
  // Drain synchronously by walking the queue via repeated nudges.
  // Async generators don't expose length, so consume up to 100 with a
  // tight timeout to confirm the count is bounded at 64.
  return (async () => {
    const drained = [];
    const it = sub.events();
    while (drained.length < 70) {
      const r = await Promise.race([
        it.next(),
        new Promise((resolve) => setTimeout(() => resolve({ done: true }), 30)),
      ]);
      if (r.done) break;
      drained.push(r.value);
    }
    sub.close();
    assert.equal(drained.length, 64, `expected 64 events but got ${drained.length}`);
  })();
});

test("TriggerHub: sseStream emits a connect comment + trigger frames", async () => {
  const hub = new TriggerHub();
  const stream = hub.sseStream();
  const reader = stream.getReader();
  const decoder = new TextDecoder();

  const connect = await reader.read();
  assert.equal(decoder.decode(connect.value), ": connected\n\n");

  hub.publish({ instructions: "go" });
  const frame = await reader.read();
  const text = decoder.decode(frame.value);
  assert.ok(text.startsWith("event: trigger\n"));
  assert.ok(text.includes('"instructions":"go"'));

  await reader.cancel();
});
