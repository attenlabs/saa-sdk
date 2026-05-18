// @ts-check
// SPDX-License-Identifier: Apache-2.0
/**
 * TriggerHub is an in-process pub/sub for proactive-turn events. It
 * exists so framework overlays can stop hand-rolling the
 * back-end-webhook → SSE-fan-out → browser path: every proactive
 * trigger ends with a back-end HTTP POST, a fan-out to one-or-more
 * connected browsers (or other subscribers), and a JSON payload that
 * carries instructions for the agent's opening turn. The shape is the
 * same across OpenAI Realtime and ElevenLabs CAI browser overlays; the
 * hub is the shared piece.
 *
 * The hub deliberately does NOT bind to a specific HTTP framework
 * (Express / Fastify / Hono / FastAPI). It exposes:
 *
 *   - publish(event)        — call from your HTTP POST handler.
 *   - subscribe()           — returns an async iterator of events.
 *   - sseStream(subscriber) — convenience: returns a ReadableStream
 *                             ready to hand to a fetch-Response or
 *                             Node ServerResponse for SSE.
 *
 * @see ../README.md for an integration recipe.
 */

/**
 * @typedef {{
 *   push(event: TriggerEvent): void,
 *   events(): AsyncGenerator<TriggerEvent, void, void>,
 *   close(): void,
 * }} Subscriber
 */

/**
 * @typedef {{ instructions: string, [key: string]: unknown }} TriggerEvent
 */

export class TriggerHub {
  constructor() {
    /** @type {Set<Subscriber>} */
    this._subscribers = new Set();
  }

  /** @returns {number} */
  get subscriberCount() {
    return this._subscribers.size;
  }

  /**
   * @param {TriggerEvent} event
   * @returns {number} the number of subscribers that received the event
   */
  publish(event) {
    if (!event || typeof event.instructions !== "string" || event.instructions.length === 0) {
      throw new TypeError(
        "[saa-proactive] TriggerHub.publish(event): event must have a non-empty 'instructions' string"
      );
    }
    let fanout = 0;
    for (const sub of this._subscribers) {
      try {
        sub.push(event);
        fanout++;
      } catch {
        // a subscriber whose queue is full / aborted is dropped silently;
        // it will be GCed when its stream ends.
      }
    }
    return fanout;
  }

  /** @returns {Subscriber} */
  subscribe() {
    /** @type {TriggerEvent[]} */
    const queue = [];
    /** @type {((value: TriggerEvent | null) => void) | null} */
    let resolveNext = null;
    let closed = false;
    // Matches the Python twin (asyncio.Queue(maxsize=64)). A slow
    // subscriber drops events on overflow rather than growing memory
    // without bound.
    const QUEUE_LIMIT = 64;

    const subscriber = /** @type {Subscriber} */ ({
      push(event) {
        if (closed) return;
        if (resolveNext) {
          const r = resolveNext;
          resolveNext = null;
          r(event);
        } else if (queue.length < QUEUE_LIMIT) {
          queue.push(event);
        }
        // else: queue saturated; drop on overflow.
      },
      events: async function* () {
        try {
          while (!closed) {
            let event;
            if (queue.length > 0) {
              event = queue.shift();
            } else {
              event = await new Promise((resolve) => { resolveNext = resolve; });
            }
            // Check closed AFTER the await: close() wakes the awaiting
            // promise (with null) to unblock the generator; we must NOT
            // yield that wake-up signal to the consumer.
            if (closed) return;
            yield event;
          }
        } finally {
          subscriber.close();
        }
      },
      close: () => {
        if (closed) return;
        closed = true;
        this._subscribers.delete(subscriber);
        if (resolveNext) {
          // Wake the awaiting next() so the generator can exit. The
          // events() loop checks `closed` and returns without yielding,
          // so this null never reaches consumers.
          const r = resolveNext;
          resolveNext = null;
          try { r(null); } catch {}
        }
      },
    });

    this._subscribers.add(subscriber);
    return subscriber;
  }

  /**
   * Return a ReadableStream that emits `event: trigger` SSE frames for
   * a subscriber. Handy for `new Response(stream, {headers: {"content-type": "text/event-stream"}})`
   * in a Fetch-style HTTP server (Hono / Bun / Cloudflare Workers) or
   * for `stream.pipe(res)` in Node 18+ http servers.
   *
   * @param {Subscriber} [subscriber] if omitted, a fresh subscriber is created
   * @returns {ReadableStream<Uint8Array>}
   */
  sseStream(subscriber) {
    const sub = subscriber ?? this.subscribe();
    const encoder = new TextEncoder();
    return new ReadableStream({
      async start(controller) {
        // Comment line on connect so reverse proxies don't time out.
        controller.enqueue(encoder.encode(": connected\n\n"));
        try {
          for await (const event of sub.events()) {
            controller.enqueue(
              encoder.encode(`event: trigger\ndata: ${JSON.stringify(event)}\n\n`)
            );
          }
        } catch (err) {
          // close the stream cleanly on any iteration error
        } finally {
          try { controller.close(); } catch {}
        }
      },
      cancel() {
        sub.close();
      },
    });
  }
}
