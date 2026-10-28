import { describe, it, afterEach, before, after } from "node:test";
import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import {
  BATCH_SIZE,
  ExportError,
  SCHEMA_VERSION,
  Span,
  configure,
  exportSpans,
  finishedSpans,
  spanToJson,
  trace,
} from "../src/index.js";

interface Received {
  readonly method: string | undefined;
  readonly path: string | undefined;
  readonly contentType: string | undefined;
  readonly body: unknown;
}

let server: Server;
let baseUrl: string;
let received: Received[] = [];
let respondWith = 202;

before(async () => {
  server = createServer((request: IncomingMessage, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      received.push({
        method: request.method,
        path: request.url,
        contentType: request.headers["content-type"],
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown,
      });
      response.writeHead(respondWith, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ accepted: 0 }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  baseUrl = `http://127.0.0.1:${port}/v1/spans`;
});

after(async () => {
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
});

afterEach(() => {
  configure();
  received = [];
  respondWith = 202;
});

describe("spanToJson", () => {
  it("uses the data model field names and Unix seconds for times", () => {
    const span = new Span(
      "tool",
      "0".repeat(31) + "1",
      "0".repeat(15) + "2",
      "0".repeat(15) + "3",
      "tool",
      true,
    );
    span.setAttribute("bytes", 1024);
    span.addEvent("retry", { attempt: 2 });
    span.end(new Error("bad"));

    const json = spanToJson(span);

    assert.equal(json.name, "tool");
    assert.equal(json.trace_id, "0".repeat(31) + "1");
    assert.equal(json.parent_id, "0".repeat(15) + "3");
    assert.equal(json.kind, "tool");
    assert.equal(json.start_time, span.startTime / 1000);
    assert.equal(json.end_time, (span.endTime ?? 0) / 1000);
    assert.equal(json.duration, (span.duration ?? 0) / 1000);
    assert.equal(json.status, "error");
    assert.equal(json.error, "Error: bad");
    assert.deepEqual(json.attributes, { bytes: 1024 });
    assert.equal(json.events[0]?.name, "retry");
    assert.equal(json.events[0]?.time, (span.events[0]?.time ?? 0) / 1000);
    assert.deepEqual(json.events[0]?.attributes, { attempt: 2 });
    assert.equal(json.sampled, true);
  });

  it("writes a running span with null end, duration and parent", () => {
    const span = new Span("job", "a".repeat(32), "b".repeat(16), undefined, "step", true);

    const json = spanToJson(span);

    assert.equal(json.end_time, null);
    assert.equal(json.duration, null);
    assert.equal(json.parent_id, null);
  });
});

describe("exportSpans", () => {
  it("sends finished spans by default in the documented envelope", async () => {
    trace("answer", () => undefined, { kind: "run" });
    const expected = finishedSpans().length;

    const sent = await exportSpans(baseUrl);

    assert.equal(sent, expected);
    assert.equal(received.length, 1);
    const request = received[0];
    assert.equal(request?.method, "POST");
    assert.equal(request?.path, "/v1/spans");
    assert.equal(request?.contentType, "application/json");
    const body = request?.body as { schema_version: number; spans: { name: string }[] };
    assert.equal(body.schema_version, SCHEMA_VERSION);
    assert.equal(body.spans.length, expected);
    assert.equal(body.spans.at(-1)?.name, "answer");
  });

  it("sends only the spans it is given", async () => {
    trace("kept", () => undefined);
    trace("skipped", () => undefined);
    const kept = finishedSpans().filter((span) => span.name === "kept");

    const sent = await exportSpans(baseUrl, kept);

    assert.equal(sent, 1);
    const body = received[0]?.body as { spans: { name: string }[] };
    assert.deepEqual(
      body.spans.map((span) => span.name),
      ["kept"],
    );
  });

  it("sends nothing when there are no spans", async () => {
    assert.equal(await exportSpans(baseUrl, []), 0);
    assert.equal(received.length, 0);
  });

  it("splits a large export into batches", async () => {
    const spans = Array.from(
      { length: BATCH_SIZE + 3 },
      (_, index) =>
        new Span(`span-${index}`, "a".repeat(32), "b".repeat(16), undefined, "step", true),
    );

    const sent = await exportSpans(baseUrl, spans);

    assert.equal(sent, BATCH_SIZE + 3);
    assert.deepEqual(
      received.map((request) => (request.body as { spans: unknown[] }).spans.length),
      [BATCH_SIZE, 3],
    );
  });

  it("rejects a URL that is not http or https", async () => {
    await assert.rejects(exportSpans("file:///tmp/spans.json"), RangeError);
    await assert.rejects(exportSpans("not a url"), RangeError);
  });

  it("raises ExportError when the endpoint refuses the batch", async () => {
    respondWith = 500;
    const span = new Span("lost", "a".repeat(32), "b".repeat(16), undefined, "step", true);

    await assert.rejects(exportSpans(baseUrl, [span]), (error: unknown) => {
      assert.ok(error instanceof ExportError);
      assert.match(error.message, /refused 1 spans with HTTP 500/);
      return true;
    });
  });

  it("raises ExportError when the endpoint cannot be reached", async () => {
    const span = new Span("lost", "a".repeat(32), "b".repeat(16), undefined, "step", true);

    await assert.rejects(
      exportSpans("http://127.0.0.1:1/v1/spans", [span], { timeoutMs: 2000 }),
      ExportError,
    );
  });
});
