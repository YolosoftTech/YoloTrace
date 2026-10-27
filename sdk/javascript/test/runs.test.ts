import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  Run,
  Span,
  configure,
  continueTrace,
  currentSpan,
  extract,
  runs,
  startSpan,
  trace,
} from "../src/index.js";

afterEach(() => {
  configure();
});

/** Return the run of the trace with `traceId`, failing the test when it is missing. */
function runFor(traceId: string | undefined): Run {
  const run = runs().find((candidate) => candidate.traceId === traceId);
  assert.ok(run, `no run for trace ${traceId}`);
  return run;
}

describe("runs", () => {
  it("links nested spans to their parent and groups them into one run", () => {
    let root: Span | undefined;
    let lookup: Span | undefined;
    let fetch: Span | undefined;
    let formatted: Span | undefined;
    trace(
      "answer",
      () => {
        root = currentSpan();
        trace("lookup", () => {
          lookup = currentSpan();
          trace("fetch", () => {
            fetch = currentSpan();
          });
        });
        trace("format", () => {
          formatted = currentSpan();
        });
      },
      { kind: "run" },
    );

    const run = runFor(root?.traceId);
    assert.deepEqual(run.roots, [root]);
    assert.deepEqual(run.children(root as Span), [lookup, formatted]);
    assert.deepEqual(run.children(lookup as Span), [fetch]);
    assert.deepEqual(run.children(fetch as Span), []);
  });

  it("keeps separate traces in separate runs", () => {
    let first: Span | undefined;
    let second: Span | undefined;
    trace("first", () => {
      first = currentSpan();
    });
    trace("second", () => {
      second = currentSpan();
    });

    assert.deepEqual(runFor(first?.traceId).spans, [first]);
    assert.deepEqual(runFor(second?.traceId).spans, [second]);
  });

  it("lists the spans of a run in start order", async () => {
    let root: Span | undefined;
    let late: Span | undefined;
    await trace("root", async () => {
      root = currentSpan();
      await new Promise((resolve) => setTimeout(resolve, 5));
      trace("early", () => {});
      await new Promise((resolve) => setTimeout(resolve, 5));
      trace("late", () => {
        late = currentSpan();
      });
    });

    const spans = runFor(root?.traceId).spans;
    const starts = spans.map((span) => span.startTime);
    assert.deepEqual(
      starts,
      [...starts].sort((a, b) => a - b),
    );
    assert.equal(spans[0], root);
    assert.equal(spans[spans.length - 1], late);
  });

  it("treats a span whose parent was recorded elsewhere as a root", () => {
    const traceId = "a".repeat(32);
    const parentId = "b".repeat(16);
    let job: Span | undefined;
    continueTrace(extract({ traceparent: `00-${traceId}-${parentId}-01` }), () => {
      trace("job", () => {
        job = currentSpan();
      });
    });

    const run = runFor(traceId);
    assert.equal(job?.parentId, parentId);
    assert.deepEqual(run.roots, [job]);
  });

  it("keeps multiple roots of one continued trace in the same run", () => {
    const remote = extract({ traceparent: `00-${"c".repeat(32)}-${"d".repeat(16)}-01` });
    let first: Span | undefined;
    let second: Span | undefined;
    continueTrace(remote, () => {
      trace("first", () => {
        first = currentSpan();
      });
      trace("second", () => {
        second = currentSpan();
      });
    });

    const run = runFor("c".repeat(32));
    assert.deepEqual(run.roots, [first, second]);
  });

  it("treats a child of a running parent as a root until the parent ends", () => {
    const parent = startSpan("parent");
    let child: Span | undefined;
    continueTrace(extract({ traceparent: `00-${parent.traceId}-${parent.spanId}-01` }), () => {
      trace("child", () => {
        child = currentSpan();
      });
    });

    assert.deepEqual(runFor(parent.traceId).roots, [child]);

    parent.end();

    const run = runFor(parent.traceId);
    assert.deepEqual(run.roots, [parent]);
    assert.deepEqual(run.children(parent), [child]);
  });

  it("leaves running spans out of every run", () => {
    let request: Span | undefined;
    let pending: Span | undefined;
    trace("request", () => {
      request = currentSpan();
      pending = startSpan("upload");
    });

    assert.deepEqual(runFor(request?.traceId).spans, [request]);

    pending?.end();
    assert.deepEqual(runFor(request?.traceId).children(request as Span), [pending]);
  });

  it("does not group unsampled traces", () => {
    configure({ sampleRate: 0 });
    trace("hidden", () => {});

    assert.equal(
      runs().some((run) => run.spans.some((span) => span.name === "hidden")),
      false,
    );
  });

  it("keeps a failed span in its run with its parent link", () => {
    let root: Span | undefined;
    let failed: Span | undefined;
    assert.throws(() =>
      trace("run", () => {
        root = currentSpan();
        trace("fail", () => {
          failed = currentSpan();
          throw new Error("boom");
        });
      }),
    );

    assert.equal(failed?.status, "error");
    assert.deepEqual(runFor(root?.traceId).children(root as Span), [failed]);
  });

  it("is immutable", () => {
    let span: Span | undefined;
    trace("block", () => {
      span = currentSpan();
    });

    const run = runFor(span?.traceId);
    assert.ok(Object.isFrozen(run));
  });
});
