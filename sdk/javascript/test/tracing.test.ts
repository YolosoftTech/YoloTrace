import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";

import { Span, configure, currentSpan, finishedSpans, runningSpans, startSpan, trace } from "../src/index.js";

/** Spans recorded for one trace, so tests do not see spans from other tests. */
function spansOf(traceId: string): Span[] {
  return finishedSpans().filter((span) => span.traceId === traceId);
}

afterEach(() => {
  configure();
});

describe("trace", () => {
  it("gives root spans fresh 32 and 16 character hex IDs", () => {
    let first: Span | undefined;
    let second: Span | undefined;
    trace("first", () => {
      first = currentSpan();
    });
    trace("second", () => {
      second = currentSpan();
    });

    assert.match(first?.traceId ?? "", /^[0-9a-f]{32}$/);
    assert.match(first?.spanId ?? "", /^[0-9a-f]{16}$/);
    assert.notEqual(first?.traceId, second?.traceId);
  });

  it("returns the value of the function", () => {
    assert.equal(trace("sum", () => 2 + 3), 5);
  });

  it("links nested spans to their parent and shares the trace ID", () => {
    let runSpan: Span | undefined;
    let llmSpan: Span | undefined;
    trace("run", () => {
      runSpan = currentSpan();
      trace("llm", () => {
        llmSpan = currentSpan();
      }, { kind: "llm" });
    }, { kind: "run" });

    assert.equal(llmSpan?.parentId, runSpan?.spanId);
    assert.equal(llmSpan?.traceId, runSpan?.traceId);
    assert.equal(llmSpan?.kind, "llm");
    assert.equal(runSpan?.parentId, undefined);
  });

  it("records status, duration and attributes once the work ends", () => {
    const span = trace("work", () => currentSpan(), { attributes: { model: "gpt" } });

    assert.ok(span);
    assert.equal(span.status, "ok");
    assert.equal(span.error, undefined);
    assert.equal(span.attributes.get("model"), "gpt");
    assert.equal(typeof span.duration, "number");
    assert.ok((span.duration ?? -1) >= 0);
    assert.equal(span.kind, "step");
  });

  it("records a thrown error and rethrows the same value", () => {
    const failure = new TypeError("bad input");
    let recorded: Span | undefined;
    assert.throws(
      () =>
        trace("fail", () => {
          recorded = currentSpan();
          throw failure;
        }),
      (error: unknown) => error === failure,
    );

    assert.equal(recorded?.status, "error");
    assert.equal(recorded?.error, "TypeError: bad input");
  });

  it("records a thrown non-Error value as text", () => {
    let recorded: Span | undefined;
    assert.throws(() =>
      trace("fail", () => {
        recorded = currentSpan();
        throw "plain string";
      }),
    );

    assert.equal(recorded?.status, "error");
    assert.equal(recorded?.error, "plain string");
  });

  it("records undefined thrown as a failure", () => {
    let recorded: Span | undefined;
    assert.throws(() =>
      trace("fail", () => {
        recorded = currentSpan();
        throw undefined;
      }),
    );

    assert.equal(recorded?.status, "error");
    assert.equal(recorded?.error, "undefined");
  });

  it("keeps the span current across awaits and records the resolved value", async () => {
    let inner: Span | undefined;
    let outer: Span | undefined;
    const value = await trace("async-run", async () => {
      outer = currentSpan();
      await new Promise((resolve) => setTimeout(resolve, 1));
      inner = currentSpan();
      return "done";
    });

    assert.equal(value, "done");
    assert.equal(inner, outer);
    assert.equal(outer?.status, "ok");
    assert.equal(outer?.endTime !== undefined, true);
  });

  it("records a rejected promise and rethrows the same error", async () => {
    const failure = new RangeError("too large");
    let recorded: Span | undefined;
    await assert.rejects(
      trace("async-fail", async () => {
        recorded = currentSpan();
        await Promise.resolve();
        throw failure;
      }),
      (error: unknown) => error === failure,
    );

    assert.equal(recorded?.status, "error");
    assert.equal(recorded?.error, "RangeError: too large");
  });

  it("gives concurrent async work its own parent span", async () => {
    const parents = await trace("batch", async () => {
      const batch = currentSpan();
      const children = await Promise.all(
        ["a", "b"].map((name) =>
          trace(name, async () => {
            await new Promise((resolve) => setTimeout(resolve, 1));
            return currentSpan()?.parentId;
          }),
        ),
      );
      return { batch, children };
    });

    assert.deepEqual(parents.children, [parents.batch?.spanId, parents.batch?.spanId]);
  });

  it("restores the outer span after a nested span ends", () => {
    let outer: Span | undefined;
    trace("outer", () => {
      outer = currentSpan();
      trace("inner", () => undefined);
      assert.equal(currentSpan(), outer);
    });
    assert.equal(currentSpan(), undefined);
    assert.ok(outer);
  });

  it("rejects an unknown span kind", () => {
    assert.throws(() => startSpan("bad", { kind: "other" as "run" }), RangeError);
  });
});

describe("startSpan", () => {
  it("is parented to the current span but does not become current", () => {
    let root: Span | undefined;
    let child: Span | undefined;
    trace("root", () => {
      root = currentSpan();
      child = startSpan("upload", { kind: "tool" });
      assert.equal(currentSpan(), root);
    });
    child?.end();

    assert.equal(child?.parentId, root?.spanId);
    assert.equal(child?.traceId, root?.traceId);
    assert.equal(child?.kind, "tool");
  });

  it("stays running until end is called, and is listed by runningSpans", () => {
    const span = startSpan("job");
    assert.equal(span.duration, undefined);
    assert.ok(runningSpans().includes(span));

    span.end();

    assert.equal(runningSpans().includes(span), false);
    assert.ok(finishedSpans().includes(span));
    assert.ok((span.duration ?? -1) >= 0);
  });

  it("records a failure passed to end", () => {
    const span = startSpan("upload");
    span.end(new Error("disk full"));

    assert.equal(span.status, "error");
    assert.equal(span.error, "Error: disk full");
  });

  it("throws when a span is ended twice", () => {
    const span = startSpan("once");
    span.end();

    assert.throws(() => span.end(), /already ended/);
  });

  it("records a span that ends later in another callback", async () => {
    let pending: Span | undefined;
    trace("request", () => {
      pending = startSpan("callback-work");
    });
    const span = pending;
    assert.ok(span);
    assert.ok(runningSpans().includes(span));

    await new Promise((resolve) => setTimeout(resolve, 1));
    span.end();

    assert.ok(finishedSpans().includes(span));
  });
});

describe("Span attributes", () => {
  it("replaces an existing key without counting it again", () => {
    const span = startSpan("attrs", { attributes: { a: 1 } });
    span.setAttribute("a", 2);
    span.setAttribute("b", true);
    span.setAttribute("c", null);

    assert.deepEqual([...span.attributes.entries()], [
      ["a", 2],
      ["b", true],
      ["c", null],
    ]);
    span.end();
  });
});
