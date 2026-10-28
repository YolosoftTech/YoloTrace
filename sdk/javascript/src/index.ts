/**
 * YoloTrace: open-source, self-hosted observability and debugging for AI agents.
 */

export { BATCH_SIZE, ExportError, SCHEMA_VERSION, exportSpans, spanToJson } from "./export.js";
export {
  Run,
  Span,
  configure,
  continueTrace,
  currentSpan,
  extract,
  finishedSpans,
  inject,
  runningSpans,
  runs,
  startSpan,
  trace,
} from "./tracing.js";
export type { ExportOptions, SpanJson } from "./export.js";
export type {
  AttributeValue,
  ConfigureOptions,
  SpanContext,
  SpanEvent,
  SpanKind,
  SpanOptions,
} from "./tracing.js";

/** Version of the JavaScript/TypeScript SDK. */
export const version = "0.1.0";
