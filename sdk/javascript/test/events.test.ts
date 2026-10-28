import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";

import { configure, startSpan } from "../src/index.js";

afterEach(() => {
  configure();
});

describe("span events", () => {
  it("records name, time and attributes in the order added", () => {
    const job = startSpan("job");
    job.addEvent("first");
    job.addEvent("retry", { attempt: 2, reason: "rate_limited" });
    job.end();

    const [first, retry] = job.events;
    assert.equal(first?.name, "first");
    assert.equal(retry?.name, "retry");
    assert.deepEqual(retry?.attributes, { attempt: 2, reason: "rate_limited" });
    assert.ok((first?.time ?? 0) <= (retry?.time ?? 0));
  });

  it("starts with no events, and an event without attributes has none", () => {
    const job = startSpan("job");
    assert.equal(job.events.length, 0);

    job.addEvent("checkpoint");
    job.end();

    assert.deepEqual(job.events[0]?.attributes, {});
  });

  it("applies max attributes and max attribute length to events", () => {
    configure({ maxAttributes: 1, maxAttributeLength: 3 });
    const job = startSpan("job");
    job.addEvent("many", { a: "abcdef", b: 2 });
    job.end();

    assert.deepEqual(job.events[0]?.attributes, { a: "abc" });
  });
});
