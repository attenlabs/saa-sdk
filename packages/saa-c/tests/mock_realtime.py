#!/usr/bin/env python3
"""mock_realtime.py - stand-in for the OpenAI Realtime API, used by the voice agent's test
(run_voice_agent.py). It speaks the API's current event names over plain ws:// on /v1/realtime,
checks what a client has to send, and answers every response.create with a tone, so the example
runs with no key and no network. With --summary, it appends one JSON line per connection: the
session.update it got, every item, response, cancel, and truncate, and each problem it found.
Times in the summary are seconds of CLOCK_MONOTONIC, the clock a C client on the same machine
reads.

- The upgrade needs "Authorization: Bearer <--key>"; anything else gets HTTP 401. An OpenAI-Beta
  header gets HTTP 400: it selects the old interface, which a client of this API must not ask for.
- session.update must declare PCM at 24 kHz both ways and no turn detection. It gets
  session.updated either way; what was wrong is a problem in the summary.
- input_audio_buffer.append, .commit, and .clear: a commit of 100 ms or more becomes a user item,
  with input_audio_buffer.committed, conversation.item.added and .done, and its transcription
  0.3 s later; a shorter one gets error input_audio_buffer_commit_empty.
- conversation.item.create is recorded too, an input_image part among the problems. Its audio is
  not transcribed, as the real API does not transcribe audio that arrives that way.
- response.create: response.created, then --tone-s seconds of tone in 100 ms deltas, sent as fast
  as the socket takes them, its transcript, and response.done. Reply n is a sine at 300 + 100 n Hz
  and -12 dBFS, so a recording shows which replies were played. --hold N:S holds reply N back S
  seconds after response.created.
- response.cancel ends the reply in flight with response.done, status "cancelled". With none in
  flight it answers error response_cancel_not_active, as the real API does.
- conversation.item.truncate must name an assistant item, content index 0, and no more audio than
  the item has; it gets conversation.item.truncated, or an error.

Requires: python3 -m pip install websockets>=13
"""
import argparse, asyncio, base64, json, math, struct, time
from http import HTTPStatus

from websockets.asyncio.server import serve

RATE = 24000
DELTA_SAMPLES = 2400                 # 100 ms a delta
AMPLITUDE = 0.25                     # -12 dBFS
TRANSCRIBE_S = 0.3

ARGS = None
CONNECTIONS = [0]


def now():
    return round(time.clock_gettime(time.CLOCK_MONOTONIC), 4)


def log(*a):
    print(time.strftime("%H:%M:%S"), "[realtime]", *a, flush=True)


def tone(n_reply, seconds):
    freq = 300 + 100 * n_reply
    n = int(round(seconds * RATE))
    amp = AMPLITUDE * 32767
    return freq, struct.pack(f"<{n}h", *(int(round(amp * math.sin(2 * math.pi * freq * i / RATE)))
                                         for i in range(n)))


def process_request(connection, request):
    if not request.path.startswith("/v1/realtime"):
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
    if request.headers.get("Authorization", "") != "Bearer " + ARGS.key:
        log("upgrade refused: 401")
        return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid api key\n")
    if "OpenAI-Beta" in request.headers:
        log("upgrade refused: OpenAI-Beta header")
        return connection.respond(HTTPStatus.BAD_REQUEST, "OpenAI-Beta selects the old interface\n")
    return None


def check_session(s, problems):
    audio = s.get("audio") or {}
    inp, out = audio.get("input") or {}, audio.get("output") or {}
    want = {"type": "audio/pcm", "rate": RATE}
    if s.get("type") != "realtime":
        problems.append(f"session.type is {s.get('type')!r}, not 'realtime'")
    if inp.get("format") != want:
        problems.append(f"input format {inp.get('format')!r}, not {want}")
    if out.get("format") != want:
        problems.append(f"output format {out.get('format')!r}, not {want}")
    if "turn_detection" not in inp or inp["turn_detection"] is not None:
        problems.append(f"turn_detection is {inp.get('turn_detection', 'missing')!r}, not null")
    if s.get("output_modalities") != ["audio"]:
        problems.append(f"output_modalities {s.get('output_modalities')!r}, not ['audio']")


async def handler(ws):
    CONNECTIONS[0] += 1
    conn = CONNECTIONS[0]
    req = ws.request
    rec = {"connection": conn, "path": req.path, "t_open": now(),
           "user_agent": req.headers.get("User-Agent", ""), "session_update": None,
           "items": [], "responses": [], "cancels": [], "truncates": [], "errors_sent": [],
           "problems": []}
    items = {}                          # id -> {"role", "audio_ms"}
    buf = bytearray()                   # the input audio buffer
    rec["appends"] = 0
    active = {"task": None, "resp": None}
    counter = {"event": 0, "item": 0}
    log(f"connection {conn}: {req.path}")

    async def send(obj):
        counter["event"] += 1
        obj.setdefault("event_id", f"event_{conn}_{counter['event']}")
        await ws.send(json.dumps(obj))

    async def error(code, message, client_event=None):
        rec["errors_sent"].append({"t": now(), "code": code})
        await send({"type": "error", "error": {"type": "invalid_request_error", "code": code,
                                               "message": message, "param": None,
                                               "event_id": client_event}})

    def new_id(prefix):
        counter["item"] += 1
        return f"{prefix}_{conn}_{counter['item']:03d}"

    async def transcribe(item_id, n):
        await asyncio.sleep(TRANSCRIBE_S)
        try:
            await send({"type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item_id, "content_index": 0, "transcript": f"turn {n}"})
        except Exception:                            # the connection closed meanwhile
            pass

    async def respond(resp):
        rid, n = resp["id"], resp["n"]
        item_id = new_id("item")
        try:
            await send({"type": "response.created",
                        "response": {"object": "realtime.response", "id": rid, "status": "in_progress",
                                     "metadata": resp["metadata"], "output": []}})
            hold = dict(ARGS.hold).get(n)
            if hold:
                resp["held_s"] = hold
                await asyncio.sleep(hold)
            freq, pcm = tone(n, ARGS.tone_s)
            resp["freq_hz"] = freq
            item = {"id": item_id, "object": "realtime.item", "type": "message", "role": "assistant",
                    "status": "in_progress", "content": []}
            await send({"type": "response.output_item.added", "response_id": rid, "output_index": 0,
                        "item": item})
            await send({"type": "conversation.item.added", "previous_item_id": None, "item": item})
            await send({"type": "response.content_part.added", "response_id": rid, "item_id": item_id,
                        "output_index": 0, "content_index": 0,
                        "part": {"type": "audio", "transcript": ""}})
            items[item_id] = {"role": "assistant", "audio_ms": 0}
            resp["item_id"] = item_id
            step = DELTA_SAMPLES * 2
            for off in range(0, len(pcm), step):
                chunk = pcm[off:off + step]
                await send({"type": "response.output_audio.delta", "response_id": rid, "item_id": item_id,
                            "output_index": 0, "content_index": 0,
                            "delta": base64.b64encode(chunk).decode()})
                resp["audio_samples"] += len(chunk) // 2
                items[item_id]["audio_ms"] = resp["audio_samples"] * 1000 // RATE
                await asyncio.sleep(0)
            text = f"reply {n}"
            await send({"type": "response.output_audio_transcript.delta", "response_id": rid,
                        "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
            await send({"type": "response.output_audio.done", "response_id": rid, "item_id": item_id,
                        "output_index": 0, "content_index": 0})
            await send({"type": "response.output_audio_transcript.done", "response_id": rid,
                        "item_id": item_id, "output_index": 0, "content_index": 0, "transcript": text})
            await send({"type": "response.content_part.done", "response_id": rid, "item_id": item_id,
                        "output_index": 0, "content_index": 0,
                        "part": {"type": "audio", "transcript": text}})
            item["status"] = "completed"
            await send({"type": "response.output_item.done", "response_id": rid, "output_index": 0,
                        "item": item})
            resp["status"] = "completed"
            resp["t_done"] = now()
            await send({"type": "response.done",
                        "response": {"object": "realtime.response", "id": rid, "status": "completed",
                                     "status_details": None, "metadata": resp["metadata"], "output": [item],
                                     "usage": {"total_tokens": 100, "input_tokens": 60, "output_tokens": 40,
                                               "input_token_details": {"audio_tokens": 50, "text_tokens": 10,
                                                                       "cached_tokens": 0},
                                               "output_token_details": {"audio_tokens": 35,
                                                                        "text_tokens": 5}}}})
        finally:
            if active["resp"] is resp:
                active["task"] = active["resp"] = None

    try:
        await send({"type": "session.created",
                    "session": {"type": "realtime", "object": "realtime.session", "id": f"sess_{conn}"}})
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                rec["problems"].append("binary frame")
                continue
            try:
                ev = json.loads(msg)
            except json.JSONDecodeError:
                rec["problems"].append("non-JSON text")
                continue
            t = ev.get("type")
            cid = ev.get("event_id")
            if t == "session.update":
                s = ev.get("session") or {}
                rec["session_update"] = s
                check_session(s, rec["problems"])
                await send({"type": "session.updated", "session": s})
            elif t == "conversation.item.create":
                item = ev.get("item") or {}
                item_id = item.get("id") or new_id("item")
                role = item.get("role")
                types, samples = [], 0
                for part in item.get("content") or []:
                    types.append(part.get("type"))
                    if part.get("type") == "input_audio":
                        try:
                            raw = base64.b64decode(part.get("audio") or "", validate=True)
                        except ValueError:
                            rec["problems"].append(f"{item_id}: input_audio is not base64")
                            raw = b""
                        if len(raw) % 2:
                            rec["problems"].append(f"{item_id}: odd input_audio length {len(raw)}")
                        samples += len(raw) // 2
                if "input_image" in types:
                    rec["problems"].append(f"{item_id}: input_image sent")
                rec["items"].append({"t": now(), "id": item_id, "role": role, "content": types,
                                     "input_audio_samples": samples, "via": "conversation.item.create"})
                items[item_id] = {"role": role, "audio_ms": 0}
                full = dict(item, id=item_id, status="completed")
                await send({"type": "conversation.item.added", "previous_item_id": None, "item": full})
                await send({"type": "conversation.item.done", "previous_item_id": None, "item": full})
            elif t == "input_audio_buffer.append":
                try:
                    buf += base64.b64decode(ev.get("audio") or "", validate=True)
                    rec["appends"] += 1
                except ValueError:
                    rec["problems"].append("input_audio_buffer.append: audio is not base64")
            elif t == "input_audio_buffer.clear":
                buf.clear()
                await send({"type": "input_audio_buffer.cleared"})
            elif t == "input_audio_buffer.commit":
                if len(buf) < RATE // 10 * 2:
                    await error("input_audio_buffer_commit_empty",
                                f"buffer too small: expected at least 100ms of audio, "
                                f"but buffer only has {len(buf) / 48:.2f}ms of audio", cid)
                    continue
                if len(buf) % 2:
                    rec["problems"].append(f"odd input audio length {len(buf)}")
                item_id = new_id("item")
                rec["items"].append({"t": now(), "id": item_id, "role": "user", "content": ["input_audio"],
                                     "input_audio_samples": len(buf) // 2, "via": "input_audio_buffer.commit"})
                items[item_id] = {"role": "user", "audio_ms": 0}
                buf.clear()
                full = {"id": item_id, "object": "realtime.item", "type": "message", "role": "user",
                        "status": "completed", "content": [{"type": "input_audio", "transcript": None}]}
                await send({"type": "input_audio_buffer.committed", "previous_item_id": None, "item_id": item_id})
                await send({"type": "conversation.item.added", "previous_item_id": None, "item": full})
                await send({"type": "conversation.item.done", "previous_item_id": None, "item": full})
                asyncio.create_task(transcribe(item_id, sum(1 for i in rec["items"] if i["role"] == "user")))
            elif t == "response.create":
                if active["task"]:
                    rec["problems"].append("response.create while a reply was in flight")
                    await error("conversation_already_has_active_response",
                                "Conversation already has an active response in progress", cid)
                    continue
                n = len(rec["responses"]) + 1
                params = ev.get("response") or {}
                resp = {"n": n, "id": f"resp_{conn}_{n}", "t_create": now(), "status": "in_progress",
                        "audio_samples": 0, "instructions": params.get("instructions"),
                        "metadata": params.get("metadata")}
                rec["responses"].append(resp)
                active["resp"] = resp
                active["task"] = asyncio.create_task(respond(resp))
            elif t == "response.cancel":
                resp, task = active["resp"], active["task"]
                rec["cancels"].append({"t": now(), "response": resp["id"] if resp else None,
                                       "audio_samples_sent": resp["audio_samples"] if resp else None})
                if not task:
                    await error("response_cancel_not_active", "Cancellation failed: no active response found",
                                cid)
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                resp["status"] = "cancelled"
                resp["t_done"] = now()
                await send({"type": "response.done",
                            "response": {"object": "realtime.response", "id": resp["id"], "status": "cancelled",
                                         "metadata": resp["metadata"],
                                         "status_details": {"type": "cancelled", "reason": "client_cancelled"},
                                         "output": [], "usage": None}})
            elif t == "conversation.item.truncate":
                item_id, ci, end_ms = ev.get("item_id"), ev.get("content_index"), ev.get("audio_end_ms")
                info = items.get(item_id)
                ok = (info is not None and info["role"] == "assistant" and ci == 0
                      and isinstance(end_ms, int) and 0 <= end_ms <= info["audio_ms"])
                rec["truncates"].append({"t": now(), "item_id": item_id, "content_index": ci,
                                         "audio_end_ms": end_ms, "item_audio_ms": info and info["audio_ms"],
                                         "ok": ok})
                if ok:
                    await send({"type": "conversation.item.truncated", "item_id": item_id,
                                "content_index": ci, "audio_end_ms": end_ms})
                else:
                    rec["problems"].append(f"bad truncate {ev}")
                    await error("invalid_value", "Audio content of this item is shorter than audio_end_ms", cid)
            else:
                rec["problems"].append(f"unexpected client event {t!r}")
    except Exception as e:  # ConnectionClosedError etc.
        log(f"connection {conn} closed: {type(e).__name__}: {e}")
    finally:
        if active["task"]:
            active["task"].cancel()
        rec["t_close"] = now()
        rec["close_code"] = ws.close_code
        log(f"connection {conn}: {len(rec['items'])} items, {len(rec['responses'])} responses, "
            f"{len(rec['cancels'])} cancels, {len(rec['truncates'])} truncates, problems {rec['problems']}")
        if ARGS.summary:
            with open(ARGS.summary, "a") as f:
                f.write(json.dumps(rec) + "\n")


def hold_arg(v):
    n, s = v.split(":")
    return int(n), float(s)


async def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--key", default="test-key")
    ap.add_argument("--summary", help="append one JSON line per connection to this file")
    ap.add_argument("--tone-s", type=float, default=2.5, help="seconds of tone in each reply")
    ap.add_argument("--hold", type=hold_arg, action="append", default=[], metavar="N:S",
                    help="hold reply N back S seconds after response.created")
    ARGS = ap.parse_args()
    async with serve(handler, "127.0.0.1", ARGS.port, process_request=process_request,
                     max_size=32 * 1024 * 1024, ping_interval=None):
        log(f"mock realtime ws://127.0.0.1:{ARGS.port}/v1/realtime")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
