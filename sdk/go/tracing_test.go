package yolotrace_test

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

var (
	traceIDPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	spanIDPattern  = regexp.MustCompile(`^[0-9a-f]{16}$`)
)

// resetAfter restores the default settings when the test ends.
func resetAfter(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		_ = yolotrace.Configure()
	})
}

// capturePanic runs fn and returns the error it panicked with, or nil when it did not panic with an error.
func capturePanic(fn func()) (recovered error) {
	defer func() {
		if value := recover(); value != nil {
			recovered, _ = value.(error)
		}
	}()
	fn()
	return nil
}

func TestTraceGivesRootSpansFreshIDs(t *testing.T) {
	resetAfter(t)
	var first, second *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "first", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		first = yolotrace.CurrentSpan(ctx)
		return nil
	})
	_ = yolotrace.Trace(context.Background(), "second", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		second = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if !traceIDPattern.MatchString(first.TraceID) {
		t.Errorf("trace ID %q is not 32 lowercase hex characters", first.TraceID)
	}
	if !spanIDPattern.MatchString(first.SpanID) {
		t.Errorf("span ID %q is not 16 lowercase hex characters", first.SpanID)
	}
	if first.TraceID == second.TraceID {
		t.Error("two root spans share a trace ID")
	}
}

func TestTraceRunsFunctionAndReturnsItsResult(t *testing.T) {
	resetAfter(t)
	ran := false
	err := yolotrace.Trace(context.Background(), "sum", yolotrace.SpanOptions{}, func(context.Context) error {
		ran = true
		return nil
	})

	if err != nil || !ran {
		t.Errorf("Trace() = %v, ran = %v; want nil error and fn to run", err, ran)
	}
}

func TestTraceLinksNestedSpansToTheirParent(t *testing.T) {
	resetAfter(t)
	var runSpan, llmSpan *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "run", yolotrace.SpanOptions{Kind: yolotrace.KindRun}, func(ctx context.Context) error {
		runSpan = yolotrace.CurrentSpan(ctx)
		return yolotrace.Trace(ctx, "llm", yolotrace.SpanOptions{Kind: yolotrace.KindLLM}, func(inner context.Context) error {
			llmSpan = yolotrace.CurrentSpan(inner)
			return nil
		})
	})

	if llmSpan.ParentID != runSpan.SpanID {
		t.Errorf("llm parent = %q, want %q", llmSpan.ParentID, runSpan.SpanID)
	}
	if llmSpan.TraceID != runSpan.TraceID {
		t.Error("nested span does not share its parent's trace ID")
	}
	if llmSpan.Kind != yolotrace.KindLLM {
		t.Errorf("llm kind = %q, want %q", llmSpan.Kind, yolotrace.KindLLM)
	}
	if runSpan.ParentID != "" {
		t.Errorf("root span parent = %q, want empty", runSpan.ParentID)
	}
}

func TestTraceRecordsStatusDurationAndAttributes(t *testing.T) {
	resetAfter(t)
	var span *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "work", yolotrace.SpanOptions{
		Attributes: map[string]yolotrace.AttributeValue{"model": yolotrace.StringValue("gpt")},
	}, func(ctx context.Context) error {
		span = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if span.Failed() || span.Failure() != "" {
		t.Errorf("successful span reports failure %q", span.Failure())
	}
	if got := span.Attributes()["model"]; got != yolotrace.StringValue("gpt") {
		t.Errorf("model attribute = %+v, want gpt", got)
	}
	if span.Duration() < 0 {
		t.Errorf("duration = %v, want non-negative", span.Duration())
	}
	if span.Kind != yolotrace.KindStep {
		t.Errorf("default kind = %q, want %q", span.Kind, yolotrace.KindStep)
	}
}

func TestTraceRecordsErrorAndReturnsItUnchanged(t *testing.T) {
	resetAfter(t)
	failure := errors.New("bad input")
	var recorded *yolotrace.Span
	err := yolotrace.Trace(context.Background(), "fail", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		recorded = yolotrace.CurrentSpan(ctx)
		return failure
	})

	if !errors.Is(err, failure) {
		t.Errorf("Trace() = %v, want the same error", err)
	}
	if !recorded.Failed() {
		t.Error("span is not marked as failed")
	}
	if !strings.HasSuffix(recorded.Failure(), ": bad input") {
		t.Errorf("failure = %q, want it to end with the message", recorded.Failure())
	}
}

func TestTraceRecordsPanicAndPropagatesIt(t *testing.T) {
	resetAfter(t)
	failure := errors.New("too large")
	var recorded *yolotrace.Span
	panicked := capturePanic(func() {
		_ = yolotrace.Trace(context.Background(), "panicking", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			recorded = yolotrace.CurrentSpan(ctx)
			panic(failure)
		})
	})

	if !errors.Is(panicked, failure) {
		t.Errorf("panic = %v, want the same error", panicked)
	}
	if !recorded.Failed() {
		t.Error("panicking span is not marked as failed")
	}
	if !recorded.Ended() {
		t.Error("panicking span was not ended")
	}
}

func TestTraceRecordsNonErrorPanicAsText(t *testing.T) {
	resetAfter(t)
	var recorded *yolotrace.Span
	func() {
		defer func() { _ = recover() }()
		_ = yolotrace.Trace(context.Background(), "fail", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			recorded = yolotrace.CurrentSpan(ctx)
			panic("plain string")
		})
	}()

	if recorded.Failure() != "plain string" {
		t.Errorf("failure = %q, want %q", recorded.Failure(), "plain string")
	}
}

func TestTraceRejectsUnknownKindWithoutRunningFunction(t *testing.T) {
	resetAfter(t)
	ran := false
	err := yolotrace.Trace(context.Background(), "bad", yolotrace.SpanOptions{Kind: "other"}, func(context.Context) error {
		ran = true
		return nil
	})

	if err == nil {
		t.Fatal("Trace() accepted an unknown span kind")
	}
	if ran {
		t.Error("fn ran although the span kind was rejected")
	}
}

func TestStartSpanIsChildButNotCurrent(t *testing.T) {
	resetAfter(t)
	var root *yolotrace.Span
	var child *yolotrace.Span
	var nested *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "root", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		root = yolotrace.CurrentSpan(ctx)
		span, err := yolotrace.StartSpan(ctx, "upload", yolotrace.SpanOptions{Kind: yolotrace.KindTool})
		if err != nil {
			t.Fatalf("StartSpan() error = %v", err)
		}
		child = span
		return yolotrace.Trace(ctx, "after", yolotrace.SpanOptions{}, func(inner context.Context) error {
			nested = yolotrace.CurrentSpan(inner)
			return nil
		})
	})
	child.End(nil)

	if child.ParentID != root.SpanID {
		t.Errorf("child parent = %q, want %q", child.ParentID, root.SpanID)
	}
	if child.TraceID != root.TraceID {
		t.Error("child does not share its parent's trace ID")
	}
	if child.Kind != yolotrace.KindTool {
		t.Errorf("child kind = %q, want %q", child.Kind, yolotrace.KindTool)
	}
	if nested.ParentID != root.SpanID {
		t.Errorf("nested span parent = %q, want the root, because the started span is not current", nested.ParentID)
	}
}

func TestStartSpanStaysRunningUntilEnded(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	if span.Duration() != 0 || span.Ended() {
		t.Error("span reports an end before End was called")
	}
	if !containsSpan(yolotrace.RunningSpans(), span) {
		t.Error("RunningSpans does not list the started span")
	}

	span.End(nil)

	if containsSpan(yolotrace.RunningSpans(), span) {
		t.Error("RunningSpans still lists the ended span")
	}
	if !containsSpan(yolotrace.FinishedSpans(), span) {
		t.Error("FinishedSpans does not list the ended span")
	}
	if span.Duration() < 0 {
		t.Errorf("duration = %v, want non-negative", span.Duration())
	}
}

func TestStartSpanRecordsFailurePassedToEnd(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "upload", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.End(errors.New("disk full"))

	if !span.Failed() {
		t.Error("span is not marked as failed")
	}
	if !strings.HasSuffix(span.Failure(), ": disk full") {
		t.Errorf("failure = %q, want it to end with the message", span.Failure())
	}
}

func TestEndPanicsWhenCalledTwice(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "once", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.End(nil)

	panicked := false
	func() {
		defer func() { panicked = recover() != nil }()
		span.End(nil)
	}()
	if !panicked {
		t.Error("second End did not panic")
	}
}

func TestStartSpanRejectsUnknownKind(t *testing.T) {
	resetAfter(t)
	if _, err := yolotrace.StartSpan(context.Background(), "bad", yolotrace.SpanOptions{Kind: "other"}); err == nil {
		t.Error("StartSpan() accepted an unknown span kind")
	}
}

func TestConcurrentTracesKeepTheirOwnParent(t *testing.T) {
	resetAfter(t)
	var batch *yolotrace.Span
	children := make([]*yolotrace.Span, 8)
	_ = yolotrace.Trace(context.Background(), "batch", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		batch = yolotrace.CurrentSpan(ctx)
		var wg sync.WaitGroup
		for i := range children {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_ = yolotrace.Trace(ctx, "child", yolotrace.SpanOptions{}, func(inner context.Context) error {
					children[i] = yolotrace.CurrentSpan(inner)
					return nil
				})
			}()
		}
		wg.Wait()
		return nil
	})

	for i, child := range children {
		if child.ParentID != batch.SpanID {
			t.Errorf("child %d parent = %q, want %q", i, child.ParentID, batch.SpanID)
		}
	}
}

func TestSetAttributeReplacesExistingKey(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "attrs", yolotrace.SpanOptions{
		Attributes: map[string]yolotrace.AttributeValue{"a": yolotrace.NumberValue(1)},
	})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.SetAttribute("a", yolotrace.NumberValue(2))
	span.SetAttribute("b", yolotrace.BoolValue(true))
	span.SetAttribute("c", yolotrace.NullValue())
	span.End(nil)

	attributes := span.Attributes()
	if len(attributes) != 3 {
		t.Fatalf("attribute count = %d, want 3", len(attributes))
	}
	if attributes["a"] != yolotrace.NumberValue(2) {
		t.Errorf("a = %+v, want 2", attributes["a"])
	}
	if attributes["b"] != yolotrace.BoolValue(true) {
		t.Errorf("b = %+v, want true", attributes["b"])
	}
	if attributes["c"].Kind != yolotrace.AttributeNull {
		t.Errorf("c kind = %v, want null", attributes["c"].Kind)
	}
}

// containsSpan reports whether spans holds span.
func containsSpan(spans []*yolotrace.Span, span *yolotrace.Span) bool {
	for _, candidate := range spans {
		if candidate == span {
			return true
		}
	}
	return false
}
