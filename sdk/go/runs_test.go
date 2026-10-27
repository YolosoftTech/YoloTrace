package yolotrace_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

// runFor returns the run of the trace with traceID, failing the test when it is missing.
func runFor(t *testing.T, traceID string) yolotrace.Run {
	t.Helper()
	for _, run := range yolotrace.Runs() {
		if run.TraceID == traceID {
			return run
		}
	}
	t.Fatalf("no run for trace %q", traceID)
	return yolotrace.Run{}
}

func TestRunLinksNestedSpansToTheirParent(t *testing.T) {
	resetAfter(t)
	var root, lookup, fetch, formatted *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "answer", yolotrace.SpanOptions{Kind: yolotrace.KindRun}, func(ctx context.Context) error {
		root = yolotrace.CurrentSpan(ctx)
		_ = yolotrace.Trace(ctx, "lookup", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			lookup = yolotrace.CurrentSpan(ctx)
			return yolotrace.Trace(ctx, "fetch", yolotrace.SpanOptions{}, func(ctx context.Context) error {
				fetch = yolotrace.CurrentSpan(ctx)
				return nil
			})
		})
		return yolotrace.Trace(ctx, "format", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			formatted = yolotrace.CurrentSpan(ctx)
			return nil
		})
	})

	run := runFor(t, root.TraceID)
	if roots := run.Roots(); len(roots) != 1 || roots[0] != root {
		t.Fatalf("roots = %v, want only the answer span", roots)
	}
	if children := run.Children(root); len(children) != 2 || children[0] != lookup || children[1] != formatted {
		t.Errorf("children of root = %v, want lookup then format", children)
	}
	if children := run.Children(lookup); len(children) != 1 || children[0] != fetch {
		t.Errorf("children of lookup = %v, want fetch", children)
	}
	if children := run.Children(fetch); len(children) != 0 {
		t.Errorf("children of fetch = %v, want none", children)
	}
}

func TestRunsKeepSeparateTracesApart(t *testing.T) {
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

	if run := runFor(t, first.TraceID); len(run.Spans) != 1 || run.Spans[0] != first {
		t.Errorf("first run spans = %v, want only the first span", run.Spans)
	}
	if run := runFor(t, second.TraceID); len(run.Spans) != 1 || run.Spans[0] != second {
		t.Errorf("second run spans = %v, want only the second span", run.Spans)
	}
}

func TestRunListsSpansInStartOrder(t *testing.T) {
	resetAfter(t)
	var root, late *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "root", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		root = yolotrace.CurrentSpan(ctx)
		time.Sleep(5 * time.Millisecond)
		_ = yolotrace.Trace(ctx, "early", yolotrace.SpanOptions{}, func(context.Context) error { return nil })
		time.Sleep(5 * time.Millisecond)
		return yolotrace.Trace(ctx, "late", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			late = yolotrace.CurrentSpan(ctx)
			return nil
		})
	})

	spans := runFor(t, root.TraceID).Spans
	if spans[0] != root || spans[len(spans)-1] != late {
		t.Errorf("spans not in start order: first %q, last %q", spans[0].Name, spans[len(spans)-1].Name)
	}
	for i := 1; i < len(spans); i++ {
		if spans[i].StartTime.Before(spans[i-1].StartTime) {
			t.Errorf("span %q starts before %q", spans[i].Name, spans[i-1].Name)
		}
	}
}

func TestRunTreatsSpanWithRecordedElsewhereParentAsRoot(t *testing.T) {
	resetAfter(t)
	traceID := strings.Repeat("a", 32)
	parentID := strings.Repeat("b", 16)
	remote := yolotrace.Extract(map[string]string{"traceparent": "00-" + traceID + "-" + parentID + "-01"})

	var job *yolotrace.Span
	_ = yolotrace.Trace(yolotrace.ContinueTrace(context.Background(), remote), "job", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		job = yolotrace.CurrentSpan(ctx)
		return nil
	})

	run := runFor(t, traceID)
	if job.ParentID != parentID {
		t.Errorf("job parent = %q, want %q", job.ParentID, parentID)
	}
	if roots := run.Roots(); len(roots) != 1 || roots[0] != job {
		t.Errorf("roots = %v, want only the job span", roots)
	}
}

func TestRunChildOfRunningParentIsRootUntilParentEnds(t *testing.T) {
	resetAfter(t)
	parent, err := yolotrace.StartSpan(context.Background(), "parent", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	remote := yolotrace.Extract(map[string]string{"traceparent": "00-" + parent.TraceID + "-" + parent.SpanID + "-01"})
	var child *yolotrace.Span
	_ = yolotrace.Trace(yolotrace.ContinueTrace(context.Background(), remote), "child", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		child = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if roots := runFor(t, parent.TraceID).Roots(); len(roots) != 1 || roots[0] != child {
		t.Errorf("roots before parent ends = %v, want only the child", roots)
	}

	parent.End(nil)

	run := runFor(t, parent.TraceID)
	if roots := run.Roots(); len(roots) != 1 || roots[0] != parent {
		t.Errorf("roots after parent ends = %v, want only the parent", roots)
	}
	if children := run.Children(parent); len(children) != 1 || children[0] != child {
		t.Errorf("children of parent = %v, want the child", children)
	}
}

func TestRunLeavesRunningSpansOut(t *testing.T) {
	resetAfter(t)
	var request, pending *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "request", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		request = yolotrace.CurrentSpan(ctx)
		var err error
		pending, err = yolotrace.StartSpan(ctx, "upload", yolotrace.SpanOptions{})
		return err
	})

	if spans := runFor(t, request.TraceID).Spans; len(spans) != 1 || spans[0] != request {
		t.Fatalf("spans while upload runs = %v, want only the request", spans)
	}

	pending.End(nil)
	if children := runFor(t, request.TraceID).Children(request); len(children) != 1 || children[0] != pending {
		t.Errorf("children of request = %v, want the upload span", children)
	}
}

func TestRunDoesNotGroupUnsampledTraces(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0)); err != nil {
		t.Fatal(err)
	}
	_ = yolotrace.Trace(context.Background(), "hidden", yolotrace.SpanOptions{}, func(context.Context) error { return nil })

	for _, run := range yolotrace.Runs() {
		for _, span := range run.Spans {
			if span.Name == "hidden" {
				t.Errorf("unsampled span appears in run %q", run.TraceID)
			}
		}
	}
}

func TestRunKeepsFailedSpanWithItsParentLink(t *testing.T) {
	resetAfter(t)
	var root, failed *yolotrace.Span
	boom := errors.New("boom")
	err := yolotrace.Trace(context.Background(), "run", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		root = yolotrace.CurrentSpan(ctx)
		return yolotrace.Trace(ctx, "fail", yolotrace.SpanOptions{}, func(ctx context.Context) error {
			failed = yolotrace.CurrentSpan(ctx)
			return boom
		})
	})
	if !errors.Is(err, boom) {
		t.Fatalf("Trace returned %v, want the original error", err)
	}

	run := runFor(t, root.TraceID)
	if !failed.Failed() {
		t.Error("failed span is not marked as failed")
	}
	if children := run.Children(root); len(children) != 1 || children[0] != failed {
		t.Errorf("children of root = %v, want the failed span", children)
	}
}

func TestRunsAreReturnedWithStableIdentity(t *testing.T) {
	resetAfter(t)
	var span *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "block", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		span = yolotrace.CurrentSpan(ctx)
		return nil
	})

	run := runFor(t, span.TraceID)
	if !containsSpan(run.Spans, span) {
		t.Error("run does not contain the span that started it")
	}
}
