/**
 * Functions and spans that record agent work, mirroring the Python SDK's tracing module.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";

/** The kinds of work a span can describe. */
export type SpanKind = "run" | "step" | "llm" | "tool";

const SPAN_KINDS: readonly SpanKind[] = ["run", "step", "llm", "tool"];

/** A span attribute is a primitive value, so it can be stored and exported as-is. */
export type AttributeValue = string | number | boolean | null;

/** Carrier key under which `inject` writes and `extract` reads the trace context. */
export const TRACEPARENT_KEY = "traceparent";

const TRACEPARENT_PATTERN = /^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;
const SAMPLED_FLAG = 0x01;
const ZERO_TRACE_ID = "0".repeat(32);
const ZERO_SPAN_ID = "0".repeat(16);

/** The trace and span IDs that identify a point in a trace, possibly in another process. */
export interface SpanContext {
  /** Shared by every span of one trace. */
  readonly traceId: string;
  /** Identifies the span that spans started under this context link to. */
  readonly spanId: string;
  /** Whether the trace is recorded. Spans under an unsampled context are not recorded either. */
  readonly sampled: boolean;
}

/** Options for `configure`. Omitted settings return to their defaults. */
export interface ConfigureOptions {
  /** Fraction of new traces to record, from 0 to 1. Defaults to 1, which records everything. */
  readonly sampleRate?: number;
  /** Maximum number of attributes a span keeps. Defaults to 128. */
  readonly maxAttributes?: number;
  /** Maximum length of string attributes and error messages. Defaults to 4096. */
  readonly maxAttributeLength?: number;
}

/** Options for `startSpan` and `trace`. */
export interface SpanOptions {
  /** The kind of work the span describes. Defaults to `"step"`. */
  readonly kind?: SpanKind;
  /** Metadata known before the work starts. */
  readonly attributes?: Readonly<Record<string, AttributeValue>>;
}

interface Settings {
  readonly sampleRate: number;
  readonly maxAttributes: number;
  readonly maxAttributeLength: number;
}

let settings: Settings = {
  sampleRate: 1,
  maxAttributes: 128,
  maxAttributeLength: 4096,
};

const currentStorage = new AsyncLocalStorage<Span>();
const remoteStorage = new AsyncLocalStorage<SpanContext | undefined>();
const finished: Span[] = [];
const running = new Map<string, Span>();

/**
 * One recorded unit of work, linked to its parent through `parentId`.
 *
 * Spans are created by `trace` and `startSpan`, not directly. Times are epoch milliseconds.
 */
export class Span {
  /** Name of the work the span describes. */
  readonly name: string;
  /** Shared by every span of one trace, including spans recorded in other processes. */
  readonly traceId: string;
  /** Identifies this span within its trace. */
  readonly spanId: string;
  /** The span ID of the parent span, or `undefined` for a root span. */
  readonly parentId: string | undefined;
  /** The kind of work the span describes. */
  readonly kind: SpanKind;
  /** Whether the span is recorded. Unsampled spans still run but are never stored. */
  readonly sampled: boolean;
  /** Epoch milliseconds when the span started. */
  readonly startTime: number;
  /** Epoch milliseconds when the span ended, or `undefined` while it is still running. */
  endTime: number | undefined = undefined;
  /** `"ok"`, or `"error"` when the span ended with a failure. */
  status: "ok" | "error" = "ok";
  /** `"Type: message"` of the failure when `status` is `"error"`. */
  error: string | undefined = undefined;
  /** Metadata recorded for the span. */
  readonly attributes = new Map<string, AttributeValue>();

  /**
   * Create a span. Use `trace` or `startSpan` instead, which also register the span.
   *
   * @param name Name of the work the span describes.
   * @param traceId Trace the span belongs to.
   * @param spanId Identifier of this span.
   * @param parentId Identifier of the parent span, if any.
   * @param kind Kind of work the span describes.
   * @param sampled Whether the span is recorded.
   */
  constructor(
    name: string,
    traceId: string,
    spanId: string,
    parentId: string | undefined,
    kind: SpanKind,
    sampled: boolean,
  ) {
    this.name = name;
    this.traceId = traceId;
    this.spanId = spanId;
    this.parentId = parentId;
    this.kind = kind;
    this.sampled = sampled;
    this.startTime = Date.now();
  }

  /** Milliseconds between start and end, or `undefined` while the span is still running. */
  get duration(): number | undefined {
    if (this.endTime === undefined) {
      return undefined;
    }
    return this.endTime - this.startTime;
  }

  /**
   * Store `value` under `key`, replacing any earlier value for that key.
   *
   * A new key is dropped once the span holds `maxAttributes` keys, and a string longer
   * than `maxAttributeLength` is cut short, so no error is thrown.
   *
   * @param key Name of the attribute.
   * @param value Primitive value to store.
   */
  setAttribute(key: string, value: AttributeValue): void {
    if (!this.attributes.has(key) && this.attributes.size >= settings.maxAttributes) {
      return;
    }
    this.attributes.set(key, limit(value));
  }

  /**
   * Finish a span started with `startSpan`, optionally marking it as failed.
   *
   * @param failure Pass the thrown value to mark the span as failed. Pass nothing on success.
   * @throws Error when the span has already ended.
   */
  end(...failure: [] | [error: unknown]): void {
    if (this.endTime !== undefined) {
      throw new Error(`span ${this.name} has already ended`);
    }
    this.endTime = Date.now();
    if (failure.length === 1) {
      this.status = "error";
      this.error = truncate(describeError(failure[0]));
    }
    running.delete(this.spanId);
    if (this.sampled) {
      finished.push(this);
    }
  }
}

/**
 * Set how much the SDK records. Each call replaces every setting; omitted ones reset to their defaults.
 *
 * `sampleRate` is the fraction of new traces to record, from 0 to 1. The choice is made once,
 * where a trace starts, and every span of that trace follows it, including spans in other
 * processes that continued the trace through `extract`. Unsampled spans still run, so their
 * code keeps working, but nothing is stored for them.
 *
 * @param options The recording settings to apply.
 * @throws RangeError when `sampleRate` is outside 0 to 1, or a limit is negative.
 */
export function configure(options: ConfigureOptions = {}): void {
  const sampleRate = options.sampleRate ?? 1;
  const maxAttributes = options.maxAttributes ?? 128;
  const maxAttributeLength = options.maxAttributeLength ?? 4096;
  if (!(sampleRate >= 0 && sampleRate <= 1)) {
    throw new RangeError(`sampleRate must be between 0 and 1: ${sampleRate}`);
  }
  if (maxAttributes < 0 || maxAttributeLength < 0) {
    throw new RangeError("maxAttributes and maxAttributeLength cannot be negative");
  }
  settings = { sampleRate, maxAttributes, maxAttributeLength };
}

/**
 * Run `fn` as a span that ends when it returns, or when its promise settles.
 *
 * The span is the current span while `fn` runs, including after its `await`s, so spans
 * started inside `fn` become its children. A failure is recorded on the span and rethrown
 * unchanged.
 *
 * @param name Name of the work the span describes.
 * @param fn The work to record.
 * @param options Kind and attributes of the span.
 * @returns Whatever `fn` returns.
 */
export function trace<T>(name: string, fn: () => Promise<T>, options?: SpanOptions): Promise<T>;
export function trace<T>(name: string, fn: () => T, options?: SpanOptions): T;
export function trace(name: string, fn: () => unknown, options: SpanOptions = {}): unknown {
  const span = newSpan(name, options.kind ?? "step", options.attributes);
  return currentStorage.run(span, () => {
    let result: unknown;
    try {
      result = fn();
    } catch (error: unknown) {
      span.end(error);
      throw error;
    }
    if (isPromiseLike(result)) {
      return result.then(
        (value: unknown) => {
          span.end();
          return value;
        },
        (error: unknown) => {
          span.end(error);
          throw error;
        },
      );
    }
    span.end();
    return result;
  });
}

/**
 * Start a span that is ended explicitly with `span.end()`.
 *
 * Use it when the work does not fit one `trace` call, for example when a span starts in one
 * callback and ends in another. The span is parented to the current span but does not become
 * the current span, so nested `trace` calls do not link to it.
 *
 * @param name Name of the work the span describes.
 * @param options Kind and attributes of the span.
 * @returns The running span.
 * @throws RangeError when `kind` is not a known span kind.
 */
export function startSpan(name: string, options: SpanOptions = {}): Span {
  return newSpan(name, options.kind ?? "step", options.attributes);
}

/**
 * Return the span whose `trace` call is running in this async context, if any.
 *
 * @returns The current span, or `undefined` outside any span.
 */
export function currentSpan(): Span | undefined {
  return currentStorage.getStore();
}

/**
 * Return a copy of every span that has ended in this process, in end order.
 *
 * @returns The finished spans that are recorded.
 */
export function finishedSpans(): Span[] {
  return [...finished];
}

/**
 * Return every span that has started but not ended yet, in start order.
 *
 * Use it to see long-running workflows while they are still working, before their spans
 * appear in `finishedSpans`.
 *
 * @returns The running spans that are recorded.
 */
export function runningSpans(): Span[] {
  return [...running.values()];
}

/**
 * Write the active trace context into `carrier` so another process can continue it.
 *
 * Call it where work leaves this process, such as before an HTTP request or when a job is
 * queued, and pass the carrier along with the work. Nothing is written when no span or
 * remote context is active.
 *
 * @param carrier Mutable map, such as HTTP headers, that receives the `traceparent` entry.
 */
export function inject(carrier: Record<string, string>): void {
  const context = activeContext();
  if (context !== undefined) {
    const flags = context.sampled ? "01" : "00";
    carrier[TRACEPARENT_KEY] = `00-${context.traceId}-${context.spanId}-${flags}`;
  }
}

/**
 * Read the trace context that `inject` wrote into `carrier`.
 *
 * Carriers often come from untrusted callers, so a missing or malformed value returns
 * `undefined` and starts a new trace instead of throwing. The key is `traceparent` in
 * lower case, so normalize HTTP header names first.
 *
 * @param carrier Map that may hold a `traceparent` entry.
 * @returns The context to pass to `continueTrace`, or `undefined` when none is valid.
 */
export function extract(
  carrier: Readonly<Record<string, string | undefined>>,
): SpanContext | undefined {
  const value = carrier[TRACEPARENT_KEY];
  const match = value === undefined ? null : TRACEPARENT_PATTERN.exec(value);
  if (match === null) {
    return undefined;
  }
  const [, traceId = "", spanId = "", flags = ""] = match;
  if (traceId === ZERO_TRACE_ID || spanId === ZERO_SPAN_ID) {
    return undefined;
  }
  const sampled = (Number.parseInt(flags, 16) & SAMPLED_FLAG) !== 0;
  return { traceId, spanId, sampled };
}

/**
 * Run `fn` so that root spans it starts become children of `context`, a span from elsewhere.
 *
 * The new spans share `context.traceId` and link to `context.spanId`, even though that span
 * is recorded in another process. Use it with the result of `extract`. A local span that is
 * already running still takes precedence over `context`.
 *
 * @param context The remote context, or `undefined` to start new traces.
 * @param fn The work to run under that context.
 * @returns Whatever `fn` returns.
 */
export function continueTrace<T>(context: SpanContext | undefined, fn: () => T): T {
  return remoteStorage.run(context, fn);
}

function activeContext(): SpanContext | undefined {
  const span = currentStorage.getStore();
  if (span !== undefined) {
    return { traceId: span.traceId, spanId: span.spanId, sampled: span.sampled };
  }
  return remoteStorage.getStore();
}

function newSpan(
  name: string,
  kind: SpanKind,
  attributes: Readonly<Record<string, AttributeValue>> = {},
): Span {
  assertKind(kind);
  const context = activeContext();
  // Sampling is load shedding, not a security decision, so a fast PRNG is enough.
  const sampled = context !== undefined ? context.sampled : Math.random() < settings.sampleRate;
  const span = new Span(
    name,
    context?.traceId ?? randomBytes(16).toString("hex"),
    randomBytes(8).toString("hex"),
    context?.spanId,
    kind,
    sampled,
  );
  for (const [key, value] of Object.entries(attributes)) {
    span.setAttribute(key, value);
  }
  if (sampled) {
    running.set(span.spanId, span);
  }
  return span;
}

function assertKind(kind: string): asserts kind is SpanKind {
  if (!(SPAN_KINDS as readonly string[]).includes(kind)) {
    throw new RangeError(`unknown span kind: ${kind}`);
  }
}

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    "then" in value &&
    typeof value.then === "function"
  );
}

function describeError(error: unknown): string {
  if (error instanceof Error) {
    return `${error.name}: ${error.message}`;
  }
  return String(error);
}

function limit(value: AttributeValue): AttributeValue {
  return typeof value === "string" ? truncate(value) : value;
}

function truncate(text: string): string {
  return text.slice(0, settings.maxAttributeLength);
}
