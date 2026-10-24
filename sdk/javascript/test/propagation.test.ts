import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  configure,
  continueTrace,
  currentSpan,
  extract,
  inject,
  startSpan,
  trace,
  type Span,
  type SpanContext,
} from "../src/index.js";

const SAMPLED_CONTEXT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

afterEach(() => {
  configure();
});

describe("inject", () => {
  it("writes the current span as a traceparent entry", () => {
    const carrier: Record<string, string> = {};
    let caller: Span | undefined;
    trace("caller", () => {
      caller = currentSpan();
      inject(carrier);
    });

    assert.deepEqual(carrier, { traceparent: `00-${caller?.traceId}-${caller?.spanId}-01` });
  });

  it("writes nothing without an active context", () => {
    const carrier: Record<string, string> = {};
    inject(carrier);

    assert.deepEqual(carrier, {});
  });

  it("marks unsampled traces with flag 00", () => {
    configure({ sampleRate: 0 });
    const carrier: Record<string, string> = {};
    trace("caller", () => inject(carrier));

    assert.ok(carrier.traceparent?.endsWith("-00"));
  });
});

describe("extract", () => {
  it("reads a valid traceparent entry", () => {
    const context = extract({ traceparent: SAMPLED_CONTEXT });

    assert.deepEqual(context, {
      traceId: "4bf92f3577b34da6a3ce929d0e0e4736",
      spanId: "00f067aa0ba902b7",
      sampled: true,
    });
  });

  it("reads the sampled flag as off when it is 00", () => {
    const context = extract({ traceparent: SAMPLED_CONTEXT.slice(0, -2) + "00" });

    assert.equal(context?.sampled, false);
  });

  it("returns undefined when the entry is missing", () => {
    assert.equal(extract({}), undefined);
  });

  it("returns undefined for malformed or all-zero values", () => {
    const malformed = [
      "",
      "garbage",
      "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
      "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
      "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
      "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
      "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
    ];
    for (const value of malformed) {
      assert.equal(extract({ traceparent: value }), undefined, value);
    }
  });
});

describe("continueTrace", () => {
  it("makes root spans children of a remote context", () => {
    const remote = extract({ traceparent: SAMPLED_CONTEXT });
    let job: Span | undefined;
    continueTrace(remote, () => {
      trace("job", () => {
        job = currentSpan();
      });
    });

    assert.equal(job?.traceId, remote?.traceId);
    assert.equal(job?.parentId, remote?.spanId);
  });

  it("returns the value of the function", () => {
    assert.equal(
      continueTrace(undefined, () => "result"),
      "result",
    );
  });

  it("starts a new trace when the context is undefined", () => {
    let job: Span | undefined;
    continueTrace(undefined, () => {
      job = startSpan("job");
    });
    job?.end();

    assert.equal(job?.parentId, undefined);
  });

  it("gives a local span precedence over the remote context", () => {
    const remote = extract({ traceparent: SAMPLED_CONTEXT });
    let local: Span | undefined;
    let nested: Span | undefined;
    trace("local", () => {
      local = currentSpan();
      continueTrace(remote, () => {
        nested = startSpan("nested");
      });
    });
    nested?.end();

    assert.equal(nested?.parentId, local?.spanId);
    assert.equal(nested?.traceId, local?.traceId);
  });

  it("keeps the remote sampling decision for every span under it", () => {
    configure({ sampleRate: 1 });
    const unsampled: SpanContext = {
      traceId: "a".repeat(32),
      spanId: "b".repeat(16),
      sampled: false,
    };
    let job: Span | undefined;
    continueTrace(unsampled, () => {
      job = startSpan("job");
    });
    job?.end();

    assert.equal(job?.sampled, false);
  });
});

describe("cross-process round trip", () => {
  it("continues a trace written by inject in another process", () => {
    const headers: Record<string, string> = {};
    let request: Span | undefined;
    trace("request", () => {
      request = currentSpan();
      inject(headers);
    });

    // The worker receives the headers under a normalized, lower-case key.
    const received: Record<string, string> = { traceparent: headers.traceparent ?? "" };
    let job: Span | undefined;
    continueTrace(extract(received), () => {
      trace("job", () => {
        job = currentSpan();
      });
    });

    assert.equal(job?.traceId, request?.traceId);
    assert.equal(job?.parentId, request?.spanId);
  });
});
