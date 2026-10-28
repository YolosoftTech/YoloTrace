package yolotrace_test

import (
	"context"
	"reflect"
	"testing"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

func TestAddEventRecordsNameTimeAndAttributesInOrder(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	span.AddEvent("first", nil)
	span.AddEvent("retry", map[string]yolotrace.AttributeValue{
		"attempt": yolotrace.NumberValue(2),
		"reason":  yolotrace.StringValue("rate_limited"),
	})
	span.End(nil)

	events := span.Events()
	if len(events) != 2 || events[0].Name != "first" || events[1].Name != "retry" {
		t.Fatalf("events = %+v, want first then retry", events)
	}
	if events[0].Time.After(events[1].Time) {
		t.Error("events are not in the order they were added")
	}
	want := map[string]yolotrace.AttributeValue{
		"attempt": yolotrace.NumberValue(2),
		"reason":  yolotrace.StringValue("rate_limited"),
	}
	if !reflect.DeepEqual(events[1].Attributes, want) {
		t.Errorf("retry attributes = %v, want %v", events[1].Attributes, want)
	}
}

func TestNewSpanHasNoEvents(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if got := span.Events(); len(got) != 0 {
		t.Errorf("new span has %d events, want 0", len(got))
	}
}

func TestAddEventAppliesAttributeLimits(t *testing.T) {
	resetAfter(t)
	if err := yolotrace.Configure(yolotrace.WithMaxAttributes(1), yolotrace.WithMaxAttributeLength(3)); err != nil {
		t.Fatal(err)
	}
	span, err := yolotrace.StartSpan(context.Background(), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	span.AddEvent("many", map[string]yolotrace.AttributeValue{
		"a": yolotrace.StringValue("abcdef"),
		"b": yolotrace.NumberValue(2),
	})
	span.End(nil)

	want := map[string]yolotrace.AttributeValue{"a": yolotrace.StringValue("abc")}
	if got := span.Events()[0].Attributes; !reflect.DeepEqual(got, want) {
		t.Errorf("event attributes = %v, want %v", got, want)
	}
}

func TestEventsCopyIsNotAliasedByTheSpan(t *testing.T) {
	resetAfter(t)
	span, err := yolotrace.StartSpan(context.Background(), "job", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	span.AddEvent("retry", map[string]yolotrace.AttributeValue{"attempt": yolotrace.NumberValue(1)})
	span.Events()[0].Attributes["attempt"] = yolotrace.NumberValue(99)
	span.End(nil)

	if got := span.Events()[0].Attributes["attempt"]; got != yolotrace.NumberValue(1) {
		t.Errorf("mutating a copy changed the span: attempt = %v", got)
	}
}
