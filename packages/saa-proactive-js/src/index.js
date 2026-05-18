// @ts-check
// SPDX-License-Identifier: Apache-2.0
/**
 * @attenlabs/saa-proactive — tiny lifecycle helper for proactive voice
 * agents on top of @attenlabs/saa-js.
 *
 * The public surface is two helpers plus their supporting types:
 *
 *   - ProactiveLifecycle / runProactiveTurn — the
 *     markResponding(true) -> speak -> markResponding(false) loop.
 *   - TriggerHub — in-process pub/sub for back-end-webhook -> SSE
 *     fan-out to connected browsers; events conform to TriggerEvent
 *     ({ instructions: string }) and subscribers conform to
 *     TriggerSubscriber (see ./index.d.ts).
 *
 * Apache-2.0. See ../LICENSE and ../NOTICE.
 */

export { ProactiveLifecycle, runProactiveTurn } from "./lifecycle.js";
export { TriggerHub } from "./trigger-server.js";

export const VERSION = "0.1.0";
