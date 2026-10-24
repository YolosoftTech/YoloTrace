package yolotrace_test

import (
	"context"
	"fmt"
	"testing"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

const sampledContext = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

func TestInjectWritesTheCurrentSpanAsTraceparent(t *testing.T) {
	resetAfter(t)
	carrier := map[string]string{}
	var caller *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "caller", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		caller = yolotrace.CurrentSpan(ctx)
		yolotrace.Inject(ctx, carrier)
		return nil
	})

	want := fmt.Sprintf("00-%s-%s-01", caller.TraceID, caller.SpanID)
	if carrier[yolotrace.TraceparentKey] != want {
		t.Errorf("traceparent = %q, want %q", carrier[yolotrace.TraceparentKey], want)
	}
}

func TestInjectWritesNothingWithoutAnActiveContext(t *testing.T) {
	carrier := map[string]string{}
	yolotrace.Inject(context.Background(), carrier)

	if len(carrier) != 0 {
		t.Errorf("carrier = %v, want empty", carrier)
	}
}

func TestInjectMarksUnsampledTracesWithFlagZero(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	carrier := map[string]string{}
	_ = yolotrace.Trace(context.Background(), "caller", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		yolotrace.Inject(ctx, carrier)
		return nil
	})

	if got := carrier[yolotrace.TraceparentKey]; got == "" || got[len(got)-2:] != "00" {
		t.Errorf("traceparent = %q, want flag 00", got)
	}
}

func TestExtractReadsAValidTraceparent(t *testing.T) {
	remote := yolotrace.Extract(map[string]string{yolotrace.TraceparentKey: sampledContext})

	if remote == nil {
		t.Fatal("Extract() returned nil for a valid traceparent")
	}
	if remote.TraceID != "4bf92f3577b34da6a3ce929d0e0e4736" || remote.SpanID != "00f067aa0ba902b7" || !remote.Sampled {
		t.Errorf("Extract() = %+v", *remote)
	}
}

func TestExtractReadsTheSampledFlagAsOffWhenZero(t *testing.T) {
	carrier := map[string]string{yolotrace.TraceparentKey: sampledContext[:len(sampledContext)-2] + "00"}
	remote := yolotrace.Extract(carrier)

	if remote == nil || remote.Sampled {
		t.Errorf("Extract() = %+v, want an unsampled context", remote)
	}
}

func TestExtractReturnsNilWhenTheEntryIsMissing(t *testing.T) {
	if yolotrace.Extract(map[string]string{}) != nil {
		t.Error("Extract() returned a context without a traceparent entry")
	}
}

func TestExtractReturnsNilForMalformedOrAllZeroValues(t *testing.T) {
	malformed := []string{
		"",
		"garbage",
		"01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
		"00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
		"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
		"00-00000000000000000000000000000000-00f067aa0ba902b7-01",
		"00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
	}
	for _, value := range malformed {
		if yolotrace.Extract(map[string]string{yolotrace.TraceparentKey: value}) != nil {
			t.Errorf("Extract(%q) returned a context", value)
		}
	}
}

func TestContinueTraceMakesRootSpansChildrenOfTheRemoteContext(t *testing.T) {
	resetAfter(t)
	remote := yolotrace.Extract(map[string]string{yolotrace.TraceparentKey: sampledContext})
	var job *yolotrace.Span
	_ = yolotrace.Trace(yolotrace.ContinueTrace(context.Background(), remote), "job", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		job = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if job.TraceID != remote.TraceID || job.ParentID != remote.SpanID {
		t.Errorf("job trace = %q parent = %q, want the remote trace and span", job.TraceID, job.ParentID)
	}
}

func TestContinueTraceWithNilStartsNewTraces(t *testing.T) {
	resetAfter(t)
	job, err := yolotrace.StartSpan(yolotrace.ContinueTrace(context.Background(), nil), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	job.End(nil)

	if job.ParentID != "" {
		t.Errorf("job parent = %q, want a new trace without a parent", job.ParentID)
	}
}

func TestLocalSpanTakesPrecedenceOverRemoteContext(t *testing.T) {
	resetAfter(t)
	remote := yolotrace.Extract(map[string]string{yolotrace.TraceparentKey: sampledContext})
	var local, nested *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "local", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		local = yolotrace.CurrentSpan(ctx)
		continued := yolotrace.ContinueTrace(ctx, remote)
		span, err := yolotrace.StartSpan(continued, "nested", yolotrace.SpanOptions{})
		if err != nil {
			t.Fatalf("StartSpan() error = %v", err)
		}
		nested = span
		return nil
	})
	nested.End(nil)

	if nested.ParentID != local.SpanID || nested.TraceID != local.TraceID {
		t.Errorf("nested parent = %q trace = %q, want the local span", nested.ParentID, nested.TraceID)
	}
}

func TestRemoteSamplingDecisionAppliesToEverySpanUnderIt(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(1)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	unsampled := &yolotrace.SpanContext{TraceID: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", SpanID: "bbbbbbbbbbbbbbbb", Sampled: false}
	job, err := yolotrace.StartSpan(yolotrace.ContinueTrace(context.Background(), unsampled), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	job.End(nil)

	if job.Sampled {
		t.Error("span under an unsampled remote context was sampled")
	}
}

func TestCrossProcessRoundTripContinuesTheTrace(t *testing.T) {
	resetAfter(t)
	headers := map[string]string{}
	var request *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "request", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		request = yolotrace.CurrentSpan(ctx)
		yolotrace.Inject(ctx, headers)
		return nil
	})

	// The worker receives the headers under a normalized, lowercase key.
	received := map[string]string{yolotrace.TraceparentKey: headers[yolotrace.TraceparentKey]}
	var job *yolotrace.Span
	_ = yolotrace.Trace(yolotrace.ContinueTrace(context.Background(), yolotrace.Extract(received)), "job", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		job = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if job.TraceID != request.TraceID || job.ParentID != request.SpanID {
		t.Errorf("job trace = %q parent = %q, want the request trace and span", job.TraceID, job.ParentID)
	}
}
