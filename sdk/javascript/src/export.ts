/**
 * Send finished spans to an ingestion endpoint over HTTP, in the JSON format of docs/ingestion-api.md.
 */

import { type AttributeValue, Span, finishedSpans } from "./tracing.js";

/** Version of the JSON trace format. The ingestion API rejects any other value. */
export const SCHEMA_VERSION = 1;

/** Most spans sent in one request. A larger export is split into several requests. */
export const BATCH_SIZE = 500;

/** Raised when the ingestion endpoint cannot be reached or does not accept spans. */
export class ExportError extends Error {
  /**
   * Create the error with a message that says what failed.
   *
   * @param message What failed.
   */
  constructor(message: string) {
    super(message);
    this.name = "ExportError";
  }
}

/** Options for `exportSpans`. */
export interface ExportOptions {
  /** How long to wait for the endpoint, in milliseconds. Defaults to 10000. */
  readonly timeoutMs?: number;
}

/** The JSON form of a span, using the data model's field names and Unix seconds for times. */
export interface SpanJson {
  readonly name: string;
  readonly trace_id: string;
  readonly span_id: string;
  readonly parent_id: string | null;
  readonly kind: string;
  readonly start_time: number;
  readonly end_time: number | null;
  readonly duration: number | null;
  readonly status: string;
  readonly error: string | null;
  readonly attributes: Readonly<Record<string, AttributeValue>>;
  readonly events: readonly {
    readonly name: string;
    readonly time: number;
    readonly attributes: Readonly<Record<string, AttributeValue>>;
  }[];
  readonly sampled: boolean;
}

/**
 * Return `span` in the JSON form the ingestion API accepts.
 *
 * @param span The span to convert.
 * @returns The span as a plain object that `JSON.stringify` can write.
 */
export function spanToJson(span: Span): SpanJson {
  return {
    name: span.name,
    trace_id: span.traceId,
    span_id: span.spanId,
    parent_id: span.parentId ?? null,
    kind: span.kind,
    start_time: secondsFromMilliseconds(span.startTime),
    end_time: span.endTime === undefined ? null : secondsFromMilliseconds(span.endTime),
    duration: span.duration === undefined ? null : span.duration / 1000,
    status: span.status,
    error: span.error ?? null,
    attributes: Object.fromEntries(span.attributes),
    events: span.events.map((event) => ({
      name: event.name,
      time: secondsFromMilliseconds(event.time),
      attributes: { ...event.attributes },
    })),
    sampled: span.sampled,
  };
}

/**
 * Send `spans` to the ingestion `endpoint` and return how many were sent.
 *
 * `spans` defaults to every finished span in this process. `endpoint` is the full URL of the
 * ingestion route, such as `http://127.0.0.1:4318/v1/spans`. Spans go in batches of
 * `BATCH_SIZE`. A batch that is not accepted throws `ExportError`. Batches already accepted
 * stay stored, so exporting the same spans again stores them again.
 *
 * @param endpoint Full URL of the ingestion route. Must be http or https.
 * @param spans The spans to send. Defaults to the finished spans of this process.
 * @param options Timeout for each request.
 * @returns How many spans were sent.
 * @throws RangeError when `endpoint` is not an http or https URL.
 * @throws ExportError when a batch cannot be delivered.
 */
export async function exportSpans(
  endpoint: string,
  spans: readonly Span[] = finishedSpans(),
  options: ExportOptions = {},
): Promise<number> {
  const protocol = parseProtocol(endpoint);
  if (protocol !== "http:" && protocol !== "https:") {
    throw new RangeError(`endpoint must be an http or https URL: ${endpoint}`);
  }
  const timeoutMs = options.timeoutMs ?? 10_000;
  for (let start = 0; start < spans.length; start += BATCH_SIZE) {
    await postBatch(endpoint, spans.slice(start, start + BATCH_SIZE), timeoutMs);
  }
  return spans.length;
}

/**
 * Return the protocol of `endpoint`, or an empty string when it is not a URL.
 *
 * @param endpoint The text to parse.
 * @returns The protocol, such as `"https:"`.
 */
function parseProtocol(endpoint: string): string {
  try {
    return new URL(endpoint).protocol;
  } catch {
    return "";
  }
}

/**
 * POST one batch of spans to `endpoint` and throw `ExportError` unless it is accepted.
 *
 * @param endpoint Full URL of the ingestion route.
 * @param batch The spans to send in this request.
 * @param timeoutMs How long to wait for the response.
 */
async function postBatch(
  endpoint: string,
  batch: readonly Span[],
  timeoutMs: number,
): Promise<void> {
  const body = JSON.stringify({
    schema_version: SCHEMA_VERSION,
    spans: batch.map(spanToJson),
  });
  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error: unknown) {
    throw new ExportError(`could not reach ${endpoint}: ${String(error)}`);
  }
  await response.text();
  if (!response.ok) {
    throw new ExportError(
      `ingestion endpoint refused ${batch.length} spans with HTTP ${response.status}`,
    );
  }
}

/**
 * Convert epoch milliseconds to Unix seconds, the unit the JSON format uses.
 *
 * @param milliseconds Epoch milliseconds.
 * @returns The same instant in Unix seconds.
 */
function secondsFromMilliseconds(milliseconds: number): number {
  return milliseconds / 1000;
}
