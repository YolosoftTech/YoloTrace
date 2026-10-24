package yolotrace

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
)

// TraceparentKey is the carrier key under which Inject writes and Extract reads the trace context.
const TraceparentKey = "traceparent"

// traceparentPattern matches a W3C Trace Context value with lowercase hexadecimal IDs.
var traceparentPattern = regexp.MustCompile(`^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$`)

const (
	// sampledFlag is the trace-flags bit that marks a trace as recorded.
	sampledFlag = 0x01
	// zeroTraceID is the all-zero trace ID, which W3C Trace Context forbids.
	zeroTraceID = "00000000000000000000000000000000"
	// zeroSpanID is the all-zero span ID, which W3C Trace Context forbids.
	zeroSpanID = "0000000000000000"
)

// SpanContext holds the trace and span IDs that identify a point in a trace, possibly in another process.
type SpanContext struct {
	// TraceID is shared by every span of one trace.
	TraceID string
	// SpanID identifies the span that spans started under this context link to.
	SpanID string
	// Sampled reports whether the trace is recorded. Spans under an unsampled context are not recorded either.
	Sampled bool
}

// Inject writes the active trace context of ctx into carrier so another process can continue
// the trace. Call it where work leaves this process, such as before an HTTP request or when a
// job is queued. It writes nothing when no span or remote context is active.
//
// Inject panics when carrier is nil.
func Inject(ctx context.Context, carrier map[string]string) {
	active := activeContext(ctx)
	if active == nil {
		return
	}
	flags := "00"
	if active.Sampled {
		flags = "01"
	}
	carrier[TraceparentKey] = fmt.Sprintf("00-%s-%s-%s", active.TraceID, active.SpanID, flags)
}

// Extract reads the trace context that Inject wrote into carrier.
//
// Carriers often come from untrusted callers, so a missing or malformed value returns nil and
// starts a new trace instead of failing. The key is "traceparent" in lowercase, so normalize
// HTTP header names first.
func Extract(carrier map[string]string) *SpanContext {
	match := traceparentPattern.FindStringSubmatch(carrier[TraceparentKey])
	if match == nil {
		return nil
	}
	traceID, spanID, flags := match[1], match[2], match[3]
	if traceID == zeroTraceID || spanID == zeroSpanID {
		return nil
	}
	flagBits, _ := strconv.ParseUint(flags, 16, 8)
	return &SpanContext{TraceID: traceID, SpanID: spanID, Sampled: flagBits&sampledFlag != 0}
}

// ContinueTrace returns a context in which root spans become children of remote, a span
// recorded in another process. Use it with the result of Extract. The new spans share
// remote.TraceID and link to remote.SpanID.
//
// A nil remote starts new traces. A span that is already current in ctx still takes
// precedence over remote.
func ContinueTrace(ctx context.Context, remote *SpanContext) context.Context {
	return context.WithValue(ctx, remoteContextKey, remote)
}
