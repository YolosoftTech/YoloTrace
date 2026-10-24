import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  configure,
  currentSpan,
  finishedSpans,
  runningSpans,
  startSpan,
  trace,
  type Span,
} from "../src/index.js";

afterEach(() => {
  configure();
});

describe("configure sampling", () => {
  it("rejects a sample rate outside 0 to 1", () => {
    assert.throws(() => configure({ sampleRate: 1.5 }), RangeError);
    assert.throws(() => configure({ sampleRate: -0.1 }), RangeError);
    assert.throws(() => configure({ sampleRate: Number.NaN }), RangeError);
  });

  it("rejects negative limits", () => {
    assert.throws(() => configure({ maxAttributes: -1 }), RangeError);
    assert.throws(() => configure({ maxAttributeLength: -1 }), RangeError);
  });

  it("drops unsampled traces from finished and running spans", () => {
    configure({ sampleRate: 0 });

    let span: Span | undefined;
    trace("hidden", () => {
      span = currentSpan();
    });

    assert.equal(span?.sampled, false);
    assert.equal(finishedSpans().includes(span), false);
    assert.equal(runningSpans().includes(span), false);
  });

  it("keeps unsampled work running so the code still works", () => {
    configure({ sampleRate: 0 });

    assert.equal(
      trace("compute", () => 6 * 7),
      42,
    );
  });

  it("records every trace at the default rate of 1", () => {
    const span = startSpan("default");
    span.end();

    assert.equal(span.sampled, true);
    assert.ok(finishedSpans().includes(span));
  });

  it("makes child spans follow the sampling decision of their root", () => {
    configure({ sampleRate: 0 });
    let child: Span | undefined;
    trace("root", () => {
      child = startSpan("child");
    });
    child?.end();

    assert.equal(child?.sampled, false);
    assert.equal(finishedSpans().includes(child), false);
  });

  it("resets every setting when configure is called without options", () => {
    configure({ sampleRate: 0, maxAttributes: 1 });
    configure();

    const span = startSpan("reset");
    span.end();
    assert.equal(span.sampled, true);
  });
});

describe("configure capture limits", () => {
  it("caps the number of attributes and drops extra keys silently", () => {
    configure({ maxAttributes: 2 });
    const span = startSpan("limited", { attributes: { a: 1, b: 2 } });
    span.setAttribute("c", 3);
    span.setAttribute("a", 10);

    assert.deepEqual(
      [...span.attributes.entries()],
      [
        ["a", 10],
        ["b", 2],
      ],
    );
    span.end();
  });

  it("cuts string attributes to maxAttributeLength", () => {
    configure({ maxAttributeLength: 4 });
    const span = startSpan("long", { attributes: { prompt: "abcdefgh", n: 123456 } });

    assert.equal(span.attributes.get("prompt"), "abcd");
    assert.equal(span.attributes.get("n"), 123456);
    span.end();
  });

  it("cuts error messages to maxAttributeLength", () => {
    configure({ maxAttributeLength: 10 });
    const span = startSpan("failing");
    span.end(new Error("a very long failure message"));

    assert.equal(span.error, "Error: a v");
  });
});
