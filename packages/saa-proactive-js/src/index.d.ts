// SPDX-License-Identifier: Apache-2.0
// Hand-written types so the package ships without a build step.

export interface MarkRespondable {
  markResponding(active: boolean): void | Promise<void>;
}

export interface ProactiveLifecycleOptions {
  client: MarkRespondable;
  /** Trailing audio tail before releasing the gate. Default 200 ms. */
  tailMs?: number;
}

export interface TriggerEvent {
  instructions: string;
  [key: string]: unknown;
}

export interface TriggerSubscriber {
  push(event: TriggerEvent): void;
  events(): AsyncGenerator<TriggerEvent, void, void>;
  close(): void;
}

export class ProactiveLifecycle {
  constructor(opts: ProactiveLifecycleOptions);
  readonly active: boolean;
  run(speak: () => void | Promise<void>): Promise<void>;
}

export function runProactiveTurn(
  opts: ProactiveLifecycleOptions,
  speak: () => void | Promise<void>,
): Promise<void>;

export class TriggerHub {
  constructor();
  readonly subscriberCount: number;
  publish(event: TriggerEvent): number;
  subscribe(): TriggerSubscriber;
  sseStream(subscriber?: TriggerSubscriber): ReadableStream<Uint8Array>;
}

export const VERSION: string;
