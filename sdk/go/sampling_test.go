package yolotrace_test

import (
	"context"
	"errors"
	"math"
	"testing"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

func TestConfigureRejectsSampleRateOutsideZeroToOne(t *testing.T) {
	resetAfter(t)
	for _, rate := range []float64{1.5, -0.1, math.NaN()} {
		if err := yolotrace.Configure(yolotrace.WithSampleRate(rate)); err == nil {
			t.Errorf("Configure accepted sample rate %v", rate)
		}
	}
}

func TestConfigureRejectsNegativeLimits(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithMaxAttributes(-1)); err == nil {
		t.Error("Configure accepted a negative maxAttributes")
	}
	if err := yolotrace.Configure(yolotrace.WithMaxAttributeLength(-1)); err == nil {
		t.Error("Configure accepted a negative maxAttributeLength")
	}
}

func TestUnsampledTraceIsDroppedFromFinishedAndRunningSpans(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}

	var span *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "hidden", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		span = yolotrace.CurrentSpan(ctx)
		return nil
	})

	if span.Sampled {
		t.Error("span is sampled at rate 0")
	}
	if containsSpan(yolotrace.FinishedSpans(), span) {
		t.Error("unsampled span appears in FinishedSpans")
	}
	if containsSpan(yolotrace.RunningSpans(), span) {
		t.Error("unsampled span appears in RunningSpans")
	}
}

func TestUnsampledWorkStillRuns(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}

	result := 0
	err := yolotrace.Trace(context.Background(), "compute", yolotrace.SpanOptions{}, func(context.Context) error {
		result = 6 * 7
		return nil
	})

	if err != nil || result != 42 {
		t.Errorf("result = %d, err = %v; want 42 and nil", result, err)
	}
}

func TestDefaultRateRecordsEverySpan(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "default", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.End(nil)

	if !span.Sampled {
		t.Error("span is not sampled at the default rate")
	}
	if !containsSpan(yolotrace.FinishedSpans(), span) {
		t.Error("span is missing from FinishedSpans")
	}
}

func TestChildSpansFollowTheSamplingDecisionOfTheirRoot(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	var root *yolotrace.Span
	var child *yolotrace.Span
	_ = yolotrace.Trace(context.Background(), "root", yolotrace.SpanOptions{}, func(ctx context.Context) error {
		root = yolotrace.CurrentSpan(ctx)
		span, err := yolotrace.StartSpan(ctx, "child", yolotrace.SpanOptions{})
		if err != nil {
			t.Fatalf("StartSpan() error = %v", err)
		}
		child = span
		return nil
	})
	child.End(nil)

	if root.Sampled || child.Sampled {
		t.Error("child span was sampled although its root was not")
	}
	if containsSpan(yolotrace.FinishedSpans(), child) {
		t.Error("unsampled child appears in FinishedSpans")
	}
}

func TestConfigureWithoutOptionsResetsEverySetting(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithSampleRate(0), yolotrace.WithMaxAttributes(1)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	if err := yolotrace.Configure(); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}

	span, err := yolotrace.StartSpan(context.Background(), "reset", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.SetAttribute("a", yolotrace.NumberValue(1))
	span.SetAttribute("b", yolotrace.NumberValue(2))
	span.End(nil)

	if !span.Sampled {
		t.Error("span is not sampled after Configure() reset the rate")
	}
	if len(span.Attributes()) != 2 {
		t.Errorf("attribute count = %d, want 2 after the limit was reset", len(span.Attributes()))
	}
}

func TestMaxAttributesDropsExtraKeysSilently(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithMaxAttributes(2)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	span, err := yolotrace.StartSpan(context.Background(), "limited", yolotrace.SpanOptions{
		Attributes: map[string]yolotrace.AttributeValue{"a": yolotrace.NumberValue(1), "b": yolotrace.NumberValue(2)},
	})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.SetAttribute("c", yolotrace.NumberValue(3))
	span.SetAttribute("a", yolotrace.NumberValue(10))
	span.End(nil)

	attributes := span.Attributes()
	if len(attributes) != 2 {
		t.Fatalf("attribute count = %d, want 2", len(attributes))
	}
	if attributes["a"] != yolotrace.NumberValue(10) || attributes["b"] != yolotrace.NumberValue(2) {
		t.Errorf("attributes = %+v, want a=10 and b=2", attributes)
	}
	if _, kept := attributes["c"]; kept {
		t.Error("attribute beyond the limit was kept")
	}
}

func TestMaxAttributeLengthCutsStringAttributes(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithMaxAttributeLength(4)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	span, err := yolotrace.StartSpan(context.Background(), "long", yolotrace.SpanOptions{
		Attributes: map[string]yolotrace.AttributeValue{
			"prompt": yolotrace.StringValue("abcdefgh"),
			"n":      yolotrace.NumberValue(123456),
		},
	})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.End(nil)

	if got := span.Attributes()["prompt"]; got != yolotrace.StringValue("abcd") {
		t.Errorf("prompt = %+v, want abcd", got)
	}
	if got := span.Attributes()["n"]; got != yolotrace.NumberValue(123456) {
		t.Errorf("n = %+v, want 123456", got)
	}
}

func TestMaxAttributeLengthCutsFailureMessages(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithMaxAttributeLength(10)); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	span, err := yolotrace.StartSpan(context.Background(), "failing", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatalf("StartSpan() error = %v", err)
	}
	span.End(errors.New("a very long failure message"))

	// The full failure is "*errors.errorString: a very long failure message", cut to 10 characters.
	if span.Failure() != "*errors.er" {
		t.Errorf("failure = %q, want %q", span.Failure(), "*errors.er")
	}
}
