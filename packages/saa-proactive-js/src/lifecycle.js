// @ts-check
// SPDX-License-Identifier: Apache-2.0
/**
 * ProactiveLifecycle wraps the markResponding(true) -> speak ->
 * markResponding(false) sequence that every proactive voice agent
 * needs. The class is intentionally tiny: it exists to centralise the
 * gate semantics (assert BEFORE the speak action, release AFTER a
 * configurable tail-ms to absorb trailing TTS chunks, release even
 * when the speak action throws) so every framework overlay only
 * carries the framework-specific "how do I tell the agent to speak"
 * surface and not the gate-lifecycle scaffolding.
 *
 * @see ../README.md for the before/after diff that motivated this.
 */

/**
 * Minimal subset of the @attenlabs/saa-js AttentionClient surface
 * used by this lifecycle. Anything that exposes a markResponding
 * (sync or async) is compatible.
 *
 * @typedef {{
 *   markResponding(active: boolean): void | Promise<void>,
 * }} MarkRespondable
 */

export class ProactiveLifecycle {
  /**
   * @param {{
   *   client: MarkRespondable,
   *   tailMs?: number,
   * }} opts
   */
  constructor({ client, tailMs = 200 }) {
    if (!client || typeof client.markResponding !== "function") {
      throw new TypeError(
        "[saa-proactive] ProactiveLifecycle requires a client with " +
        "a markResponding(boolean) method (typically @attenlabs/saa-js's AttentionClient)"
      );
    }
    if (typeof tailMs !== "number" || tailMs < 0) {
      throw new RangeError(
        "[saa-proactive] tailMs must be a non-negative number"
      );
    }
    this._client = client;
    this._tailMs = tailMs;
    this._active = false;
  }

  /** @returns {boolean} true while a run() is in flight */
  get active() {
    return this._active;
  }

  /**
   * Run a proactive turn. Asserts markResponding(true) before invoking
   * `speak`, awaits it to resolve, then waits `tailMs` (default 200)
   * and asserts markResponding(false). If `speak` throws, the gate is
   * still released and the error propagates.
   *
   * Concurrent run() calls on the same instance are rejected: create
   * a new instance per turn or await the previous one.
   *
   * @param {() => void | Promise<void>} speak
   * @returns {Promise<void>}
   */
  async run(speak) {
    if (typeof speak !== "function") {
      throw new TypeError(
        "[saa-proactive] ProactiveLifecycle.run(speak): speak must be a function"
      );
    }
    if (this._active) {
      throw new Error(
        "[saa-proactive] lifecycle already active. Create a new " +
        "ProactiveLifecycle per turn or await the previous run() to complete."
      );
    }
    this._active = true;
    try {
      await this._client.markResponding(true);
      try {
        await speak();
      } finally {
        if (this._tailMs > 0) {
          await new Promise((resolve) => setTimeout(resolve, this._tailMs));
        }
        await this._client.markResponding(false);
      }
    } finally {
      this._active = false;
    }
  }
}

/**
 * Convenience wrapper: run a single proactive turn without
 * instantiating ProactiveLifecycle. Equivalent to
 * `new ProactiveLifecycle({client, tailMs}).run(speak)`.
 *
 * @param {{
 *   client: MarkRespondable,
 *   tailMs?: number,
 * }} opts
 * @param {() => void | Promise<void>} speak
 * @returns {Promise<void>}
 */
export function runProactiveTurn(opts, speak) {
  return new ProactiveLifecycle(opts).run(speak);
}
