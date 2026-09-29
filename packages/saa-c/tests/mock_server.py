#!/usr/bin/env python3
"""mock_server.py - stand-in for the SAA broker (/allocate) and a backend (/ws), used by the
saa-c tests. No model: it scripts a plausible session so the client can be checked without a
key. It logs the request headers the client is expected to send (User-Agent, Content-Type,
Sec-WebSocket-Protocol presence) and echoes the offered subprotocol back, as the real backend
does with the API key. With --summary, it appends one JSON line per WebSocket session,
including every action the session received.

Scenarios are selected by the token string. Allocations and WebSocket sessions are counted
per token, each from 1, so a scenario can fail once and then recover.

  <anything else>      the happy path, below
  auth                 /allocate answers 401
  ratelimit            /allocate answers 429 with Retry-After: 2
  close1008            closes 1008 right after the upgrade
  drop                 resets the TCP connection after 30 audio frames, on every session: no
                       close frame, so the client sees 1006
  silent               never answers pings (the client should stall out at 15 s)
  noecho               the upgrade succeeds but no Sec-WebSocket-Protocol is echoed
  acceptclose          closes 1011 right after the upgrade, before started, on every session
  upgrade401           the WebSocket upgrade answers HTTP 401
  upgrade503           the WebSocket upgrade answers HTTP 503 with Retry-After: 2
  auth_flap            session 1 closes 1011 after 5 audio frames; allocations 2 and 3
                       answer 401
  ratelimit_reconnect  session 1 closes 1011 after 5 audio frames; allocation 2 answers 429
                       with Retry-After: 2
  garbage              the happy path, after non-JSON text, unknown types, binary frames, a
                       pong without client_ts, and a config without a value
  bigturn, tls         the turn carries 6,000,000 bytes of PCM
  longturn             the turn carries 25 s of PCM
  latency              the happy path; the summary lists when each audio frame arrived,
                       in seconds of CLOCK_MONOTONIC, the clock a C client on the same
                       machine reads
  toobig               one 20 MiB text message right after started
  utterance            utterance_config after started and utterance_ended at audio frame
                       50, when the session was allocated with utterance_handling: true
  harness              an utterance_assistant_turn with the text "drop" closes the session
                       with 1011

The happy path: started; warmup_complete after 20 audio frames; a prediction and a vad every
4 frames from then; state listening at frame 40; at frame 60 state sending, a turn_ready that
echoes the last 2 s of received PCM with two JPEG frames, then state idle; an interrupt at
frame 70. Pings get pongs, and set_threshold gets a config with the value.

More listeners:
  --tls-cert/--tls-key  HTTPS and WSS ports; their /allocate hands out wss://localhost URLs
  --slowlink-port       a TCP proxy to the WebSocket port that reads from the client at 64 KB/s
  --blackhole-port      a TCP proxy to the WebSocket port whose first connection goes dark 2 s
                        in: it stops reading and forwarding both ways, as a half-open link
                        does, and is dropped after 20 s. Later connections pass through.
Both proxies read through a 16 KB receive buffer and clamp the MSS to 1460. Loopback's 64 KB
MSS would otherwise let Linux start the client's send buffer at megabytes, which no real path
does. Linux honours both; macOS refuses the clamp and grows the receive buffer past 500 KB
anyway, so there the queue builds in the proxy, as it would in a router with a deep buffer.

Requires: python3 -m pip install websockets>=13
"""
import argparse, asyncio, base64, contextlib, hashlib, json, select, socket, ssl, struct, threading, time
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import serve

HTTP_PORT = 8765
WS_PORT = 8766
SUMMARY = None                       # path from --summary

LOCK = threading.Lock()              # the HTTP servers run on threads, the WebSocket on asyncio
COUNTS = defaultdict(lambda: {"alloc": 0, "session": 0, "alloc_body": None})

TURN_JPEGS = [b"\xff\xd8\xff\xe0" + bytes(96) + b"\xff\xd9",   # 102 bytes
              b"\xff\xd8\xff\xdb" + bytes(200) + b"\xff\xd9"]  # 206 bytes
BIG_TURN_BYTES = 6_000_000
LONG_TURN_S = 25
TOOBIG_BYTES = 20 << 20
DARK_AFTER_S = 2                     # blackhole: before the client's first ping, at 5 s
DARK_FOR_S = 20                      # past the client's stall (15 s) and its 1 s kill
_cache = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mask(tok: str) -> str:
    return f"<{len(tok)} chars, sha1 {hashlib.sha1(tok.encode()).hexdigest()[:8]}>" if tok else "<empty>"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def big_turn_json() -> str:
    if "big" not in _cache:
        pcm = (bytes(range(256)) * (BIG_TURN_BYTES // 256 + 1))[:BIG_TURN_BYTES]
        _cache["big"] = json.dumps({"type": "turn_ready", "duration": BIG_TURN_BYTES / 32000.0,
                                    "audio_base64": b64(pcm), "frames": [],
                                    "server_turn_ready_ts_ms": int(time.time() * 1000)})
    return _cache["big"]


def long_turn_json() -> str:
    if "long" not in _cache:
        n = LONG_TURN_S * 32000
        pcm = (bytes(range(256)) * (n // 256 + 1))[:n]
        _cache["long"] = json.dumps({"type": "turn_ready", "duration": float(LONG_TURN_S),
                                     "audio_base64": b64(pcm), "frames": [],
                                     "server_turn_ready_ts_ms": int(time.time() * 1000)})
    return _cache["long"]


def toobig_json() -> str:
    if "toobig" not in _cache:
        head = '{"type":"turn_ready","duration":1,"audio_base64":"'
        _cache["toobig"] = head + "A" * (TOOBIG_BYTES - len(head) - 2) + '"}'
    return _cache["toobig"]


# Nothing here may make the client report an error or drop the session.
GARBAGE_TEXT = [
    "not json at all", "{", "", "[1,2,3]", '"a string"', "null", '{"no_type":true}', '{"type":42}',
    '{"type":"no_such_type","x":1}', '{"type":"pong"}', '{"type":"pong","client_ts":"soon"}',
    '{"type":"config"}', '{"type":"config","model_class2_threshold":"0.1"}',
    '{"type":"state","state":"thinking"}',
]
GARBAGE_BINARY = [b"\x00\x01\x02\x03", b"", b"\x01" + bytes(3200)]


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
        with LOCK:
            COUNTS[tok]["alloc"] += 1
            n = COUNTS[tok]["alloc"]
        if tok == "auth" or (tok == "auth_flap" and n in (2, 3)):
            return self._json(401, {"detail": "invalid api key"})
        if tok == "ratelimit" or (tok == "ratelimit_reconnect" and n == 2):
            return self._json(429, {"detail": "demo per-IP concurrency limit reached"}, {"Retry-After": "2"})
        try:
            b = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json(400, {"detail": "bad json"})
        with LOCK:
            COUNTS[tok]["alloc_body"] = b
        params = []
        if b.get("server_profile"):
            params.append("server_profile=" + b["server_profile"])
        if b.get("utterance_handling") is True:
            params.append("utterance_handling=1")
        url = self.server.ws_base + "/ws" + ("?" + "&".join(params) if params else "")
        self._json(200, {"url": url, "backend": "mock", "expires_in_s": 30})


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def finish_request(self, request, client_address):
        if isinstance(request, ssl.SSLSocket):
            request.do_handshake()                   # on the request thread, not the accept loop
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        log(f"[alloc] connection from {client_address[0]} failed")   # e.g. a rejected certificate


def select_subprotocol(connection, subprotocols):
    # The real backend echoes the API key it was offered. `noecho` simulates a server that
    # accepts the upgrade without selecting a subprotocol.
    if not subprotocols or subprotocols[0] == "noecho":
        return None
    return subprotocols[0]


def process_request(connection, request):
    key = request.headers.get("Sec-WebSocket-Protocol", "")
    if key == "upgrade401":
        log("[ws] upgrade refused: 401")
        return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid api key\n")
    if key == "upgrade503":
        log("[ws] upgrade refused: 503")
        response = connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "no capacity\n")
        response.headers["Retry-After"] = "2"
        return response
    return None


async def handler(ws):
    req = ws.request
    offered = req.headers.get("Sec-WebSocket-Protocol", "")
    is_tls = ws.transport.get_extra_info("ssl_object") is not None
    log(f"[ws] upgrade {req.path}{' (tls)' if is_tls else ''}")
    for k, v in req.headers.items():
        if k.lower() == "sec-websocket-protocol":
            v = mask(v)
        log(f"[ws]   {k}: {v}")
    log(f"[ws] subprotocol offered={'yes' if offered else 'NO'} selected={'echoed' if ws.subprotocol else 'none'}")
    scenario = offered
    with LOCK:
        COUNTS[scenario]["session"] += 1
        session = COUNTS[scenario]["session"]
        alloc_body = COUNTS[scenario]["alloc_body"]
    utterance = "utterance_handling=1" in req.path

    n_audio = n_video = bad = n_ctl = pings = 0
    actions, per_s, arrivals = [], [], []
    pcm = bytearray()
    t0 = time.monotonic()
    turn_sent = False

    def j(obj):
        return ws.send(json.dumps(obj))

    try:
        if scenario == "close1008":
            await ws.close(1008, "auth rejected")
            return
        if scenario == "acceptclose":
            await ws.close(1011, "mock accept-close")
            return

        await j({"type": "started", "session_id": f"mock-{session}"})
        if scenario == "toobig":
            await ws.send(toobig_json())
        if scenario == "garbage":
            for m in GARBAGE_TEXT:
                await ws.send(m)
            for m in GARBAGE_BINARY:
                await ws.send(m)
        if utterance:
            await j({"type": "utterance_config", "enabled": True, "class1_threshold": 0.9, "preview": True})

        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                tag = msg[0] if msg else -1
                if tag == 0x01:
                    if scenario == "latency":
                        arrivals.append(time.clock_gettime(time.CLOCK_MONOTONIC))
                    n_audio += 1
                    sec = int(time.monotonic() - t0)
                    per_s.extend([0] * (sec + 1 - len(per_s)))
                    per_s[sec] += 1
                    if len(msg) != 3201:
                        bad += 1
                        log(f"[ws] BAD audio frame length {len(msg)}")
                    pcm += msg[1:]
                    if n_audio == 20:
                        await j({"type": "warmup_complete", "session_id": f"mock-{session}"})
                    if n_audio >= 20 and n_audio % 4 == 0:
                        cls = 2 if 40 <= n_audio < 60 else 0
                        await j({"type": "prediction", "class": cls, "display_class": cls, "confidence": 0.9,
                                 "source": "model", "num_faces": 0, "responding": False})
                        await j({"type": "vad", "is_speech": cls == 2, "probability": 0.97 if cls == 2 else 0.02})
                    if n_audio == 40:
                        await j({"type": "state", "state": "listening"})
                    if n_audio == 50 and utterance:
                        await j({"type": "utterance_ended", "seq": 1, "text": "two burgers please",
                                 "prediction": 2, "confidence": 0.88, "decision": "respond",
                                 "reason": "addressed_to_device", "start_s": 1.25, "end_s": 3.5,
                                 "truncated": False, "assistant_turns": 0, "preview": True,
                                 "latency_ms": 140, "audio_base64": b64(bytes(pcm[-32000:]))})
                    if n_audio == 60 and not turn_sent:
                        turn_sent = True
                        await j({"type": "state", "state": "sending"})
                        if scenario in ("bigturn", "tls"):
                            await ws.send(big_turn_json())
                        elif scenario == "longturn":
                            await ws.send(long_turn_json())
                        else:
                            turn = bytes(pcm[-20 * 3200:])
                            await j({"type": "turn_ready", "duration": len(turn) / 32000.0,
                                     "audio_base64": b64(turn),
                                     "frames": [{"ts_offset_s": -0.5, "image_base64": b64(TURN_JPEGS[0])},
                                                {"ts_offset_s": 0.25, "image_base64": b64(TURN_JPEGS[1])}],
                                     "server_turn_ready_ts_ms": int(time.time() * 1000)})
                        await j({"type": "state", "state": "idle"})
                    if n_audio == 70:
                        await j({"type": "interrupt", "fade_ms": 300, "confidence": 0.93})
                    if scenario == "drop" and n_audio == 30:
                        sock = ws.transport.get_extra_info("socket")
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        ws.transport.abort()               # RST, not FIN
                        return
                    if scenario in ("auth_flap", "ratelimit_reconnect") and session == 1 and n_audio == 5:
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
                    pings += 1
                    if scenario != "silent":
                        await j({"type": "pong", "client_ts": obj.get("ts"), "server_ts": int(time.time() * 1000)})
                    continue
                actions.append(obj)
                log(f"[ws] action {obj}")
                if act == "set_threshold":
                    await j({"type": "config", "model_class2_threshold": obj.get("value")})
                elif scenario == "harness" and act == "utterance_assistant_turn" and obj.get("text") == "drop":
                    await ws.close(1011, "mock drop")
                    return
    except Exception as e:  # ConnectionClosedError etc.
        log(f"[ws] closed: {type(e).__name__}: {e}")
    finally:
        dt = time.monotonic() - t0
        rate = n_audio / dt if dt > 0 else 0.0
        log(f"[ws] summary {scenario!r} session {session}: audio={n_audio} ({rate:.2f}/s over {dt:.1f}s) "
            f"video={n_video} ctl={n_ctl} bad={bad} close_code={ws.close_code} close_reason={ws.close_reason!r}")
        if SUMMARY:
            extra = {"audio_arrivals": arrivals} if scenario == "latency" else {}
            with open(SUMMARY, "a") as f:
                f.write(json.dumps({"scenario": scenario, "session": session, "tls": is_tls, "path": req.path,
                                    "audio_frames": n_audio, "bad_frames": bad, "video_frames": n_video,
                                    "control_messages": n_ctl, "pings": pings, "actions": actions,
                                    "audio_per_s": per_s, "seconds": round(dt, 3), "audio_rate": round(rate, 2),
                                    "close_code": ws.close_code, "close_reason": ws.close_reason,
                                    "user_agent": req.headers.get("User-Agent", ""),
                                    "allocate_body": alloc_body, **extra}) + "\n")


def proxy(name, port, upstream, rate=None, dark_after=None):
    """TCP proxy to 127.0.0.1:upstream. rate: client-to-server bytes per second (None:
    unthrottled). dark_after: seconds after which the first connection stops moving data."""
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)   # inherited by accepted sockets
    try:
        ls.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1460)  # announced in the SYN-ACK
    except (AttributeError, OSError) as e:
        log(f"[{name}] no MSS clamp on this OS: {e}")
    ls.bind(("127.0.0.1", port))
    ls.listen(16)
    connections = [0]

    def close_both(a, b):
        for s in (a, b):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            s.close()

    def pump(src, dst, limit, dark_at):
        budget, last = 0.0, time.monotonic()
        try:
            while True:
                now = time.monotonic()
                if dark_at and now >= dark_at:             # a half-open link: nothing moves
                    time.sleep(max(0.0, dark_at + DARK_FOR_S - now))
                    break
                n = 65536
                if limit:                                  # token bucket, bursts of 50 ms
                    budget = min(budget + (now - last) * limit, limit * 0.05)
                    last = now
                    if budget < 1024:
                        time.sleep(0.01)
                        continue
                    n = int(budget)
                if dark_at and not select.select([src], [], [], max(0.0, dark_at - now))[0]:
                    continue
                data = src.recv(n)
                if not data:
                    break
                dst.sendall(data)
                budget -= len(data)
        except OSError:
            pass
        close_both(src, dst)

    def accept_loop():
        while True:
            client, _ = ls.accept()
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)   # macOS does not inherit it
            connections[0] += 1
            try:
                server = socket.create_connection(("127.0.0.1", upstream))
            except OSError:
                client.close()
                continue
            dark_at = time.monotonic() + dark_after if dark_after and connections[0] == 1 else None
            threading.Thread(target=pump, args=(client, server, rate, dark_at), daemon=True).start()
            threading.Thread(target=pump, args=(server, client, None, dark_at), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    how = f"at {rate // 1024} KB/s" if rate else f"dark {dark_after} s into its first connection"
    log(f"{name} proxy ws://127.0.0.1:{port}/ws -> {upstream}, {how}")


def http_server(port, ws_base, tls_ctx=None):
    httpd = QuietHTTPServer(("127.0.0.1", port), Alloc)
    httpd.ws_base = ws_base
    if tls_ctx:
        httpd.socket = tls_ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"mock broker  {'https://localhost' if tls_ctx else 'http://127.0.0.1'}:{port}/allocate")


async def main():
    global WS_PORT, SUMMARY
    ap = argparse.ArgumentParser()
    ap.add_argument("--http-port", type=int, default=HTTP_PORT)
    ap.add_argument("--ws-port", type=int, default=WS_PORT)
    ap.add_argument("--summary", help="append one JSON line per WebSocket session to this file")
    ap.add_argument("--tls-cert", help="PEM certificate chain for the HTTPS and WSS ports")
    ap.add_argument("--tls-key", help="PEM private key for --tls-cert")
    ap.add_argument("--https-port", type=int)
    ap.add_argument("--wss-port", type=int)
    ap.add_argument("--slowlink-port", type=int, help="throttling proxy in front of the WebSocket port")
    ap.add_argument("--blackhole-port", type=int, help="proxy whose first connection goes dark")
    args = ap.parse_args()
    WS_PORT = args.ws_port
    SUMMARY = args.summary

    ws_opts = dict(select_subprotocol=select_subprotocol, process_request=process_request,
                   max_size=8 * 1024 * 1024, ping_interval=None)
    http_server(args.http_port, f"ws://127.0.0.1:{args.ws_port}")
    tls_ctx = None
    if args.tls_cert:
        if not (args.tls_key and args.https_port and args.wss_port):
            ap.error("--tls-cert needs --tls-key, --https-port, and --wss-port")
        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(args.tls_cert, args.tls_key)
        http_server(args.https_port, f"wss://localhost:{args.wss_port}", tls_ctx)
    if args.slowlink_port:
        proxy("slowlink", args.slowlink_port, args.ws_port, rate=64 * 1024)
    if args.blackhole_port:
        proxy("blackhole", args.blackhole_port, args.ws_port, dark_after=DARK_AFTER_S)
    log(f"mock backend ws://127.0.0.1:{args.ws_port}/ws")
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(serve(handler, "127.0.0.1", args.ws_port, **ws_opts))
        if tls_ctx:
            await stack.enter_async_context(serve(handler, "127.0.0.1", args.wss_port, ssl=tls_ctx, **ws_opts))
            log(f"mock backend wss://localhost:{args.wss_port}/ws")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
