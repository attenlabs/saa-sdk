#!/usr/bin/env python3
"""mock_server.py - stand-in for the SAA broker (/allocate) and a backend (/ws), used by the
saa-c tests. No model: it scripts a plausible session so the client can be checked without a
key. It logs the request headers the client is expected to send (User-Agent, Content-Type,
Sec-WebSocket-Protocol presence) and echoes the offered subprotocol back, as the real backend
does with the API key. With --summary, it appends one JSON line per WebSocket session.

Scenarios are selected by the token string:
  <anything else>  happy path: started, warmup after 20 audio frames, predictions every 4
                   frames, listening at 40, sending + turn_ready at 60 (echoes the last
                   2 s of received PCM back as audio_base64)
  auth             /allocate answers 401
  ratelimit        /allocate answers 429 with Retry-After: 2
  close1008        WS closes 1008 right after the upgrade
  drop             WS closes 1011 after 30 audio frames
  silent           WS never answers pings (client should stall out at 15 s)
  noecho           WS upgrade succeeds but no Sec-WebSocket-Protocol is echoed
  acceptclose      WS closes 1011 right after the upgrade, before started, on every session

Requires: python3 -m pip install websockets>=13
"""
import argparse, asyncio, base64, hashlib, json, sys, threading, time

SUMMARY = None      # path from --summary
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import serve

HTTP_PORT = 8765
WS_PORT = 8766


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mask(tok: str) -> str:
    return f"<{len(tok)} chars, sha1 {hashlib.sha1(tok.encode()).hexdigest()[:8]}>" if tok else "<empty>"


class Alloc(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj, extra=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        log(f"[alloc] POST {self.path} body={body!r}")
        for k, v in self.headers.items():
            if k.lower() == "authorization":
                v = "Bearer " + mask(tok)
            log(f"[alloc]   {k}: {v}")
        if self.path != "/allocate":
            return self._json(404, {"detail": "not found"})
        if not tok:
            return self._json(401, {"detail": "missing or malformed bearer token"})
        if tok == "auth":
            return self._json(401, {"detail": "invalid api key"})
        if tok == "ratelimit":
            return self._json(429, {"detail": "demo per-IP concurrency limit reached"}, {"Retry-After": "2"})
        try:
            b = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json(400, {"detail": "bad json"})
        params = []
        if b.get("server_profile"):
            params.append("server_profile=" + b["server_profile"])
        if b.get("utterance_handling"):
            params.append("utterance_handling=1")
        url = f"ws://127.0.0.1:{WS_PORT}/ws" + ("?" + "&".join(params) if params else "")
        self._json(200, {"url": url, "backend": "mock", "expires_in_s": 30})


def select_subprotocol(connection, subprotocols):
    # The real backend echoes the API key it was offered. `noecho` simulates a server that
    # accepts the upgrade without selecting a subprotocol.
    if not subprotocols or subprotocols[0] == "noecho":
        return None
    return subprotocols[0]


async def handler(ws):
    req = ws.request
    offered = req.headers.get("Sec-WebSocket-Protocol", "")
    log(f"[ws] upgrade {req.path}")
    for k, v in req.headers.items():
        if k.lower() == "sec-websocket-protocol":
            v = mask(v)
        log(f"[ws]   {k}: {v}")
    log(f"[ws] subprotocol offered={'yes' if offered else 'NO'} selected={'echoed' if ws.subprotocol else 'none'}")
    scenario = offered
    if scenario == "close1008":
        await ws.close(1008, "auth rejected")
        return
    if scenario == "acceptclose":
        await ws.close(1011, "mock accept-close")
        return

    n_audio = n_video = bad = n_ctl = 0
    pcm = bytearray()
    t0 = time.monotonic()
    turn_sent = False

    def j(obj):
        return ws.send(json.dumps(obj))

    await j({"type": "started", "session_id": "mock-1"})
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                tag = msg[0]
                if tag == 0x01:
                    n_audio += 1
                    if len(msg) != 3201:
                        bad += 1
                        log(f"[ws] BAD audio frame length {len(msg)}")
                    pcm += msg[1:]
                    if n_audio == 20:
                        await j({"type": "warmup_complete", "session_id": "mock-1"})
                    if n_audio >= 20 and n_audio % 4 == 0:
                        cls = 2 if 40 <= n_audio < 60 else 0
                        await j({"type": "prediction", "class": cls, "display_class": cls, "confidence": 0.9,
                                 "source": "model", "num_faces": 0, "responding": False})
                        await j({"type": "vad", "is_speech": cls == 2, "probability": 0.97 if cls == 2 else 0.02})
                    if n_audio == 40:
                        await j({"type": "state", "state": "listening"})
                    if n_audio == 60 and not turn_sent:
                        turn_sent = True
                        await j({"type": "state", "state": "sending"})
                        turn = bytes(pcm[-20 * 3200:])
                        await j({"type": "turn_ready", "duration": len(turn) / 32000.0,
                                 "audio_base64": base64.b64encode(turn).decode(), "frames": [],
                                 "server_turn_ready_ts_ms": int(time.time() * 1000)})
                    if scenario == "drop" and n_audio == 30:
                        await ws.close(1011, "mock drop")
                        return
                elif tag == 0x02:
                    n_video += 1
                else:
                    bad += 1
                    log(f"[ws] BAD tag {tag}")
            else:
                try:
                    obj = json.loads(msg)
                except json.JSONDecodeError:
                    log(f"[ws] non-JSON text {msg[:60]!r}")
                    continue
                n_ctl += 1
                act = obj.get("action")
                if act == "ping":
                    if scenario != "silent":
                        await j({"type": "pong", "client_ts": obj.get("ts"), "server_ts": int(time.time() * 1000)})
                elif act == "set_threshold":
                    await j({"type": "config", "model_class2_threshold": obj.get("value")})
                else:
                    log(f"[ws] action {obj}")
    except Exception as e:  # ConnectionClosedError etc.
        log(f"[ws] closed: {type(e).__name__}: {e}")
    finally:
        dt = time.monotonic() - t0
        rate = n_audio / dt if dt > 0 else 0.0
        log(f"[ws] summary audio={n_audio} ({rate:.2f}/s over {dt:.1f}s) video={n_video} ctl={n_ctl} bad={bad} "
            f"close_code={ws.close_code} close_reason={ws.close_reason!r}")
        if SUMMARY:
            with open(SUMMARY, "a") as f:
                f.write(json.dumps({"scenario": scenario, "path": req.path, "audio_frames": n_audio,
                                    "bad_frames": bad, "video_frames": n_video, "control_messages": n_ctl,
                                    "seconds": round(dt, 3), "audio_rate": round(rate, 2),
                                    "close_code": ws.close_code, "close_reason": ws.close_reason,
                                    "user_agent": req.headers.get("User-Agent", "")}) + "\n")


async def main():
    global WS_PORT, SUMMARY
    ap = argparse.ArgumentParser()
    ap.add_argument("--http-port", type=int, default=HTTP_PORT)
    ap.add_argument("--ws-port", type=int, default=WS_PORT)
    ap.add_argument("--summary", help="append one JSON line per WebSocket session to this file")
    args = ap.parse_args()
    WS_PORT = args.ws_port
    SUMMARY = args.summary
    httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), Alloc)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"mock broker  http://127.0.0.1:{args.http_port}/allocate")
    log(f"mock backend ws://127.0.0.1:{args.ws_port}/ws")
    async with serve(handler, "127.0.0.1", args.ws_port, select_subprotocol=select_subprotocol,
                     max_size=8 * 1024 * 1024, ping_interval=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
