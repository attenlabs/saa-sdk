/**
 * Zero-dependency Node token broker for SAA.
 *
 * This server keeps sk_live_* out of browser bundles. The browser calls
 * POST /v1/saa/session, then uses the short-lived SAA session token with
 * @attenlabs/saa-js.
 *
 * Required env:
 *   SAA_API_KEY=sk_live_...
 *   SAA_PROJECT_ID=prj_...
 *
 * Optional env:
 *   SAA_CONTROL_BASE_URL=https://api.attentionlabs.ai
 *   SAA_ALLOWED_ORIGINS=https://app.example.com,https://staging.example.com
 *   PORT=8787
 */

import http from "node:http";
import crypto from "node:crypto";

const DEFAULT_CONTROL_BASE_URL = "https://api.attentionlabs.ai";
const DEFAULT_TTL_SECONDS = 60;
const MAX_TTL_SECONDS = 300;

export function createTokenBrokerServer(options = {}) {
  const config = normalizeConfig(options);
  const limiter = createFixedWindowLimiter({
    windowMs: config.rateLimitWindowMs,
    max: config.rateLimitMax,
  });
  const metrics = {
    startedAt: new Date().toISOString(),
    sessionRequests: 0,
    sessionIssued: 0,
    sessionRejected: 0,
    controlPlaneErrors: 0,
  };

  return http.createServer(async (req, res) => {
    const requestId = crypto.randomUUID();
    res.setHeader("X-Request-Id", requestId);
    res.setHeader("Content-Type", "application/json; charset=utf-8");

    try {
      if (req.method === "GET" && req.url === "/healthz") {
        return sendJson(res, 200, { ok: true, requestId });
      }
      if (req.method === "GET" && req.url === "/metrics") {
        return sendPrometheus(res, metrics);
      }
      if (req.method !== "POST" || req.url !== "/v1/saa/session") {
        return sendJson(res, 404, { error: "not_found", requestId });
      }

      metrics.sessionRequests++;
      const ip = req.socket.remoteAddress || "unknown";
      const rate = limiter.check(ip);
      if (!rate.allowed) {
        metrics.sessionRejected++;
        res.setHeader("Retry-After", String(Math.ceil(rate.retryAfterMs / 1000)));
        return sendJson(res, 429, { error: "rate_limited", requestId });
      }

      const origin = req.headers.origin || "";
      if (!isAllowedOrigin(origin, config.allowedOrigins)) {
        metrics.sessionRejected++;
        return sendJson(res, 403, { error: "origin_not_allowed", requestId });
      }

      const body = await readJson(req, { maxBytes: 4096 });
      const ttlSeconds = clampInt(body.ttl_seconds ?? DEFAULT_TTL_SECONDS, 30, MAX_TTL_SECONDS);
      const scope = body.scope === "server" ? "server" : "browser";

      const session = await mintSaaSessionToken({
        apiKey: config.apiKey,
        baseUrl: config.controlBaseUrl,
        projectId: config.projectId,
        scope,
        ttlSeconds,
        allowedOrigins: origin ? [new URL(origin).host] : [],
      });

      metrics.sessionIssued++;
      return sendJson(res, 201, {
        requestId,
        token: session.token,
        expires_at: session.expires_at,
        id: session.id,
        ws_url: config.wsUrl,
      });
    } catch (error) {
      const status = error.statusCode || 500;
      if (status >= 500) metrics.controlPlaneErrors++;
      return sendJson(res, status, {
        error: status >= 500 ? "internal_error" : "bad_request",
        message: status >= 500 ? "internal server error" : error.message,
        requestId,
      });
    }
  });
}

export async function mintSaaSessionToken({
  apiKey,
  baseUrl = DEFAULT_CONTROL_BASE_URL,
  projectId,
  scope = "browser",
  ttlSeconds = DEFAULT_TTL_SECONDS,
  allowedOrigins = [],
}) {
  if (!apiKey || !apiKey.startsWith("sk_")) throw Object.assign(new Error("SAA_API_KEY is missing or invalid"), { statusCode: 500 });
  if (!projectId) throw Object.assign(new Error("SAA_PROJECT_ID is required"), { statusCode: 500 });

  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/tokens`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "User-Agent": "attenlabs-production-gate-example/0.1",
    },
    body: JSON.stringify({
      project_id: projectId,
      ttl_seconds: ttlSeconds,
      scope,
      allowed_origins: allowedOrigins,
    }),
  });

  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    const error = new Error(data.message || data.error || `control plane returned ${response.status}`);
    error.statusCode = response.status === 401 || response.status === 403 ? 500 : response.status;
    throw error;
  }
  if (!data.token || !data.expires_at) {
    throw Object.assign(new Error("control plane response missing token or expires_at"), { statusCode: 500 });
  }
  return data;
}

export function normalizeConfig(options = {}) {
  return {
    apiKey: options.apiKey ?? process.env.SAA_API_KEY,
    projectId: options.projectId ?? process.env.SAA_PROJECT_ID,
    controlBaseUrl: options.controlBaseUrl ?? process.env.SAA_CONTROL_BASE_URL ?? DEFAULT_CONTROL_BASE_URL,
    wsUrl: options.wsUrl ?? process.env.SAA_WS_URL ?? "wss://server.attentionlabs.ai/ws",
    allowedOrigins: options.allowedOrigins ?? parseAllowedOrigins(process.env.SAA_ALLOWED_ORIGINS ?? ""),
    rateLimitWindowMs: Number(options.rateLimitWindowMs ?? process.env.RATE_LIMIT_WINDOW_MS ?? 60_000),
    rateLimitMax: Number(options.rateLimitMax ?? process.env.RATE_LIMIT_MAX ?? 30),
  };
}

export function parseAllowedOrigins(value) {
  return String(value)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((origin) => {
      const url = new URL(origin);
      return url.origin;
    });
}

export function isAllowedOrigin(origin, allowedOrigins) {
  if (!allowedOrigins || allowedOrigins.length === 0) return false;
  try {
    const normalized = new URL(origin).origin;
    return allowedOrigins.includes(normalized);
  } catch {
    return false;
  }
}

export function createFixedWindowLimiter({ windowMs, max }) {
  const buckets = new Map();
  return {
    check(key) {
      const now = Date.now();
      const current = buckets.get(key);
      if (!current || now >= current.resetAt) {
        buckets.set(key, { count: 1, resetAt: now + windowMs });
        return { allowed: true, remaining: max - 1, retryAfterMs: 0 };
      }
      current.count++;
      if (current.count > max) {
        return { allowed: false, remaining: 0, retryAfterMs: current.resetAt - now };
      }
      return { allowed: true, remaining: max - current.count, retryAfterMs: 0 };
    },
  };
}

async function readJson(req, { maxBytes }) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.byteLength;
    if (size > maxBytes) throw Object.assign(new Error("request too large"), { statusCode: 413 });
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw Object.assign(new Error("invalid JSON"), { statusCode: 400 });
  }
}

function sendJson(res, status, body) {
  res.writeHead(status);
  res.end(JSON.stringify(body) + "\n");
}

function sendPrometheus(res, metrics) {
  res.setHeader("Content-Type", "text/plain; version=0.0.4; charset=utf-8");
  const lines = [
    "# HELP saa_session_requests_total Token broker session requests.",
    "# TYPE saa_session_requests_total counter",
    `saa_session_requests_total ${metrics.sessionRequests}`,
    "# HELP saa_session_issued_total Token broker sessions issued.",
    "# TYPE saa_session_issued_total counter",
    `saa_session_issued_total ${metrics.sessionIssued}`,
    "# HELP saa_session_rejected_total Token broker sessions rejected.",
    "# TYPE saa_session_rejected_total counter",
    `saa_session_rejected_total ${metrics.sessionRejected}`,
    "# HELP saa_control_plane_errors_total Token broker control-plane errors.",
    "# TYPE saa_control_plane_errors_total counter",
    `saa_control_plane_errors_total ${metrics.controlPlaneErrors}`,
    "",
  ];
  res.writeHead(200);
  res.end(lines.join("\n"));
}

function clampInt(value, min, max) {
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n)) return min;
  return Math.max(min, Math.min(max, n));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const port = Number(process.env.PORT || 8787);
  const server = createTokenBrokerServer();
  server.listen(port, () => {
    console.log(`SAA token broker listening on http://127.0.0.1:${port}`);
  });
}
