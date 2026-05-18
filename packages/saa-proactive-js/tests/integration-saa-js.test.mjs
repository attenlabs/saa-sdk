// SPDX-License-Identifier: Apache-2.0
//
// End-to-end contract test: real @attenlabs/saa-js AttentionClient +
// real @attenlabs/saa-proactive ProactiveLifecycle. Verifies the
// markResponding(true) -> speak -> markResponding(false) sequence
// flows through the AttentionClient's actual public method, with
// the SDK's internal send captured.

import assert from "node:assert/strict";
import test from "node:test";

import { AttentionClient } from "@attenlabs/saa-js";
import { ProactiveLifecycle } from "@attenlabs/saa-proactive";

function captureSends(client) {
  const sends = [];
  // Intercept the SDK's internal sendControl so we observe the wire
  // bytes markResponding would emit, without opening a real socket.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  client["sendControl"] = (msg) => {
    sends.push(msg);
  };
  return sends;
}

test("ProactiveLifecycle.run wraps speak with markResponding on a real AttentionClient", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const sends = captureSends(client);

  const lifecycle = new ProactiveLifecycle({ client, tailMs: 10 });

  let spoke = false;
  await lifecycle.run(async () => {
    spoke = true;
  });

  assert.equal(spoke, true, "the speak callback was invoked");
  assert.deepEqual(
    sends.map((s) => s.action),
    ["responding_start", "responding_stop"],
    "markResponding(true) precedes speak; markResponding(false) follows it",
  );
});

test("ProactiveLifecycle releases the gate even when speak throws", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const sends = captureSends(client);
  const lifecycle = new ProactiveLifecycle({ client, tailMs: 10 });

  await assert.rejects(
    lifecycle.run(async () => {
      throw new Error("tts failed");
    }),
    /tts failed/,
  );

  assert.deepEqual(
    sends.map((s) => s.action),
    ["responding_start", "responding_stop"],
    "responding_stop must still fire when speak throws",
  );
});

test("ProactiveLifecycle is single-use per instance", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  captureSends(client);
  const lifecycle = new ProactiveLifecycle({ client, tailMs: 10 });

  // Start a run but don't await it; second call must reject.
  const p1 = lifecycle.run(async () => {
    await new Promise((r) => setTimeout(r, 50));
  });
  await assert.rejects(
    lifecycle.run(async () => {}),
    /lifecycle already active/,
  );
  await p1;
});
