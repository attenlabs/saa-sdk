import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import {
  createFixedWindowLimiter,
  createTokenBrokerServer,
  isAllowedOrigin,
  parseAllowedOrigins,
} from "./token-server.mjs";

test("origin parsing and checks are strict", () => {
  const origins = parseAllowedOrigins("https://app.example.com, http://localhost:5173");
  assert.deepEqual(origins, ["https://app.example.com", "http://localhost:5173"]);
  assert.equal(isAllowedOrigin("https://app.example.com/path", origins), true);
  assert.equal(isAllowedOrigin("https://evil.example.com", origins), false);
  assert.equal(isAllowedOrigin("not a url", origins), false);
});

test("fixed window limiter rejects after max", () => {
  const limiter = createFixedWindowLimiter({ windowMs: 1000, max: 1 });
  assert.equal(limiter.check("ip").allowed, true);
  const second = limiter.check("ip");
  assert.equal(second.allowed, false);
  assert.ok(second.retryAfterMs > 0);
});

test("token broker rejects missing or disallowed origins", async () => {
  const server = createTokenBrokerServer({
    apiKey: "sk_live_replace_me",
    projectId: "prj_test",
    allowedOrigins: ["https://app.example.com"],
  });
  const base = await listen(server);
  try {
    const missing = await postJson(`${base}/v1/saa/session`, {}, {});
    assert.equal(missing.status, 403);
    assert.equal(missing.body.error, "origin_not_allowed");

    const bad = await postJson(`${base}/v1/saa/session`, {}, { Origin: "https://evil.example.com" });
    assert.equal(bad.status, 403);
    assert.equal(bad.body.error, "origin_not_allowed");
  } finally {
    server.close();
  }
});

test("token broker clamps ttl and sends allowed origin host", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = JSON.parse(init.body);
    return new Response(JSON.stringify({
      id: "tok_test",
      token: "session_token",
      expires_at: "2026-05-14T12:00:00.000Z",
    }), { status: 201, headers: { "Content-Type": "application/json" } });
  };
  const server = createTokenBrokerServer({
    apiKey: "sk_live_replace_me",
    projectId: "prj_test",
    allowedOrigins: ["https://app.example.com"],
  });
  const base = await listen(server);
  try {
    const response = await postJson(
      `${base}/v1/saa/session`,
      { ttl_seconds: 999, scope: "browser" },
      { Origin: "https://app.example.com" },
    );
    assert.equal(response.status, 201);
    assert.equal(captured.ttl_seconds, 300);
    assert.deepEqual(captured.allowed_origins, ["app.example.com"]);
    assert.equal(response.body.token, "session_token");
  } finally {
    globalThis.fetch = originalFetch;
    server.close();
  }
});

test("token broker does not leak control-plane secret text in 500 responses", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    message: "bad key sk_live_replace_me should not leak",
  }), { status: 401, headers: { "Content-Type": "application/json" } });
  const server = createTokenBrokerServer({
    apiKey: "sk_live_replace_me",
    projectId: "prj_test",
    allowedOrigins: ["https://app.example.com"],
  });
  const base = await listen(server);
  try {
    const response = await postJson(
      `${base}/v1/saa/session`,
      { ttl_seconds: 60 },
      { Origin: "https://app.example.com" },
    );
    assert.equal(response.status, 500);
    assert.equal(JSON.stringify(response.body).includes("sk_live"), false);
  } finally {
    globalThis.fetch = originalFetch;
    server.close();
  }
});

function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve(`http://127.0.0.1:${port}`);
    });
  });
}

function postJson(url, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const payload = JSON.stringify(body);
    const req = http.request({
      method: "POST",
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.pathname,
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(payload),
        ...headers,
      },
    }, (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { text += chunk; });
      res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(text) }));
    });
    req.on("error", reject);
    req.end(payload);
  });
}
