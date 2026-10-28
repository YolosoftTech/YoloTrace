// Package yolotrace records what AI agents do. It mirrors the tracing API of the
// Python and JavaScript/TypeScript SDKs, with contexts instead of implicit state.
package yolotrace

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"maps"
	"math/rand/v2"
	"slices"
	"sync"
	"time"
	"unicode/utf8"
)

// SpanKind is the kind of work a span describes.
type SpanKind string

const (
	// KindRun marks a whole agent run.
	KindRun SpanKind = "run"
	// KindStep marks a step inside a run. It is the default kind.
	KindStep SpanKind = "step"
	// KindLLM marks a model call.
	KindLLM SpanKind = "llm"
	// KindTool marks a tool or function call.
	KindTool SpanKind = "tool"
)

// valid reports whether kind is one of the known span kinds.
func (kind SpanKind) valid() bool {
	switch kind {
	case KindRun, KindStep, KindLLM, KindTool:
		return true
	default:
		return false
	}
}

// AttributeKind says which field of an AttributeValue holds the value.
type AttributeKind int

const (
	// AttributeString means the value is in the String field.
	AttributeString AttributeKind = iota
	// AttributeNumber means the value is in the Number field.
	AttributeNumber
	// AttributeBool means the value is in the Bool field.
	AttributeBool
	// AttributeNull means the value is null, so no field holds it.
	AttributeNull
)

// AttributeValue is a primitive value, so it can be stored and exported as-is.
// Build values with StringValue, NumberValue, BoolValue and NullValue.
type AttributeValue struct {
	// Kind says which of the fields below holds the value.
	Kind AttributeKind
	// String holds the value when Kind is AttributeString.
	String string
	// Number holds the value when Kind is AttributeNumber.
	Number float64
	// Bool holds the value when Kind is AttributeBool.
	Bool bool
}

// StringValue returns an attribute holding text.
func StringValue(text string) AttributeValue {
	return AttributeValue{Kind: AttributeString, String: text}
}

// NumberValue returns an attribute holding a number, such as a token count.
func NumberValue(number float64) AttributeValue {
	return AttributeValue{Kind: AttributeNumber, Number: number}
}

// BoolValue returns an attribute holding a flag.
func BoolValue(flag bool) AttributeValue {
	return AttributeValue{Kind: AttributeBool, Bool: flag}
}

// NullValue returns an attribute holding null.
func NullValue() AttributeValue {
	return AttributeValue{Kind: AttributeNull}
}

// SpanEvent is a point in time within a span, such as a retry or a cache miss.
type SpanEvent struct {
	// Name is what happened, such as "retry".
	Name string
	// Time is when the event was added.
	Time time.Time
	// Attributes holds the details of the event.
	Attributes map[string]AttributeValue
}

// SpanOptions sets the kind and the attributes of a span when it starts.
type SpanOptions struct {
	// Kind is the kind of work the span describes. The zero value means KindStep.
	Kind SpanKind
	// Attributes holds metadata known before the work starts.
	Attributes map[string]AttributeValue
}

// settings holds how much the SDK records.
type settings struct {
	sampleRate         float64
	maxAttributes      int
	maxAttributeLength int
}

// defaultSettings returns the settings used before Configure is called.
func defaultSettings() settings {
	return settings{sampleRate: 1, maxAttributes: 128, maxAttributeLength: 4096}
}

// recorder is the process-wide store of spans. Its mutex guards every field.
type recorder struct {
	mu       sync.Mutex
	settings settings
	finished []*Span
	running  []*Span
}

// store is the process-wide recorder used by every span in this process.
var store = &recorder{settings: defaultSettings()}

// currentSettings returns a copy of the settings in effect.
func currentSettings() settings {
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.settings
}

// ConfigureOption changes one recording setting passed to Configure.
type ConfigureOption func(*settings)

// WithSampleRate sets the fraction of new traces to record, from 0 to 1.
// The default is 1, which records everything.
func WithSampleRate(rate float64) ConfigureOption {
	return func(s *settings) { s.sampleRate = rate }
}

// WithMaxAttributes sets the maximum number of attributes a span keeps.
// The default is 128.
func WithMaxAttributes(count int) ConfigureOption {
	return func(s *settings) { s.maxAttributes = count }
}

// WithMaxAttributeLength sets the maximum length of string attributes and failure
// messages. The default is 4096.
func WithMaxAttributeLength(length int) ConfigureOption {
	return func(s *settings) { s.maxAttributeLength = length }
}

// Configure sets how much the SDK records. Each call replaces every setting; omitted
// settings return to their defaults.
//
// The sample rate is the fraction of new traces to record. The choice is made once, where
// a trace starts, and every span of that trace follows it, including spans in other
// processes that continued the trace through Extract. Unsampled spans still run, so their
// code keeps working, but nothing is stored for them.
//
// Configure returns an error when the sample rate is outside 0 to 1, or a limit is negative.
func Configure(options ...ConfigureOption) error {
	next := defaultSettings()
	for _, option := range options {
		option(&next)
	}
	if !(next.sampleRate >= 0 && next.sampleRate <= 1) {
		return fmt.Errorf("sampleRate must be between 0 and 1: %v", next.sampleRate)
	}
	if next.maxAttributes < 0 || next.maxAttributeLength < 0 {
		return errors.New("maxAttributes and maxAttributeLength cannot be negative")
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	store.settings = next
	return nil
}

// Span is one recorded unit of work, linked to its parent through ParentID.
//
// Spans are created by Trace and StartSpan, not directly. The identity fields are set when
// the span starts and never change. The result fields are set when End is called.
type Span struct {
	// Name is the name of the work the span describes.
	Name string
	// TraceID is shared by every span of one trace, including spans recorded in other processes.
	TraceID string
	// SpanID identifies this span within its trace.
	SpanID string
	// ParentID is the span ID of the parent span. It is empty for a root span.
	ParentID string
	// Kind is the kind of work the span describes.
	Kind SpanKind
	// Sampled reports whether the span is recorded. Unsampled spans still run but are never stored.
	Sampled bool
	// StartTime is when the span started.
	StartTime time.Time

	mu         sync.Mutex
	ended      bool
	endTime    time.Time
	failed     bool
	failure    string
	attributes map[string]AttributeValue
	events     []SpanEvent
}

// SetAttribute stores value under key, replacing any earlier value for that key.
//
// A new key is dropped once the span holds maxAttributes keys, and a string longer than
// maxAttributeLength is cut short, so no error is returned.
func (s *Span) SetAttribute(key string, value AttributeValue) {
	s.mu.Lock()
	defer s.mu.Unlock()
	current := currentSettings()
	if _, exists := s.attributes[key]; !exists && len(s.attributes) >= current.maxAttributes {
		return
	}
	s.attributes[key] = limitAttribute(value, current.maxAttributeLength)
}

// Attributes returns a copy of the attributes recorded for the span.
func (s *Span) Attributes() map[string]AttributeValue {
	s.mu.Lock()
	defer s.mu.Unlock()
	return maps.Clone(s.attributes)
}

// AddEvent records an event named name at the current time, with optional attributes.
//
// The attributes obey the same limits as SetAttribute, so an event never fails because of
// its size. Events keep the order in which they were added.
func (s *Span) AddEvent(name string, attributes map[string]AttributeValue) {
	current := currentSettings()
	kept := make(map[string]AttributeValue, len(attributes))
	for _, key := range slices.Sorted(maps.Keys(attributes)) {
		if _, exists := kept[key]; !exists && len(kept) >= current.maxAttributes {
			continue
		}
		kept[key] = limitAttribute(attributes[key], current.maxAttributeLength)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, SpanEvent{Name: name, Time: time.Now(), Attributes: kept})
}

// Events returns a copy of the events recorded for the span, in the order they were added.
func (s *Span) Events() []SpanEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	events := make([]SpanEvent, len(s.events))
	for i, event := range s.events {
		event.Attributes = maps.Clone(event.Attributes)
		events[i] = event
	}
	return events
}

// End finishes a span started with StartSpan. Pass nil when the work succeeded, or the
// error that ended it so the span is marked as failed.
//
// End panics when the span has already ended.
func (s *Span) End(err error) {
	if err == nil {
		s.finish("", false)
		return
	}
	s.finish(describeError(err), true)
}

// Ended reports whether End has been called.
func (s *Span) Ended() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ended
}

// Duration returns the time between start and end. It returns zero while the span is still running.
func (s *Span) Duration() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.ended {
		return 0
	}
	return s.endTime.Sub(s.StartTime)
}

// Failed reports whether the span ended with a failure.
func (s *Span) Failed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.failed
}

// Failure returns the failure as "Type: message", or an empty string when the span did not fail.
func (s *Span) Failure() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.failure
}

// finish marks the span as ended and stores it. It panics when the span has already ended.
func (s *Span) finish(failure string, failed bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ended {
		panic(fmt.Sprintf("span %s has already ended", s.Name))
	}
	s.ended = true
	s.endTime = time.Now()
	s.failed = failed
	if failed {
		s.failure = truncate(failure, currentSettings().maxAttributeLength)
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	store.running = slices.DeleteFunc(store.running, func(other *Span) bool { return other == s })
	if s.Sampled {
		store.finished = append(store.finished, s)
	}
}

// Trace runs fn as a span that ends when it returns. The span is the current span of the
// context fn receives, so spans started with that context become its children.
//
// The error returned by fn marks the span as failed and is returned unchanged. A panic in
// fn is recorded the same way and then propagates. Trace returns an error without running
// fn when options.Kind is not a known span kind.
func Trace(ctx context.Context, name string, options SpanOptions, fn func(context.Context) error) error {
	span, err := newSpan(ctx, name, options)
	if err != nil {
		return err
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			failure := fmt.Sprint(recovered)
			if panicErr, ok := recovered.(error); ok {
				failure = describeError(panicErr)
			}
			span.finish(failure, true)
			panic(recovered)
		}
	}()
	err = fn(withSpan(ctx, span))
	span.End(err)
	return err
}

// StartSpan starts a span that is ended explicitly with span.End.
//
// Use it when the work does not fit one Trace call, for example when a span starts in one
// callback and ends in another. The span is a child of the current span of ctx, but it is
// not the current span of any context, so nested Trace calls do not link to it.
//
// StartSpan returns an error when options.Kind is not a known span kind.
func StartSpan(ctx context.Context, name string, options SpanOptions) (*Span, error) {
	return newSpan(ctx, name, options)
}

// withSpan returns a context whose current span is span.
func withSpan(ctx context.Context, span *Span) context.Context {
	return context.WithValue(ctx, currentSpanKey, span)
}

// CurrentSpan returns the span of ctx, or nil outside any span.
func CurrentSpan(ctx context.Context) *Span {
	span, _ := ctx.Value(currentSpanKey).(*Span)
	return span
}

// FinishedSpans returns a copy of every span that has ended in this process, in end order.
// Only spans that are recorded appear.
func FinishedSpans() []*Span {
	store.mu.Lock()
	defer store.mu.Unlock()
	return slices.Clone(store.finished)
}

// RunningSpans returns a copy of every span that has started but not ended yet, in start order.
// Use it to see long-running workflows while they are still working. Only spans that are
// recorded appear.
func RunningSpans() []*Span {
	store.mu.Lock()
	defer store.mu.Unlock()
	return slices.Clone(store.running)
}

// Run is the finished spans of one trace, grouped so that parents and children can be walked.
//
// Spans holds them in start order. A span is a root when its parent is not part of the run.
// That covers top-level spans, spans whose parent was recorded in another process, and spans
// whose parent has not ended yet. Spans that started at the same time keep their end order.
type Run struct {
	// TraceID is the trace ID shared by the run's spans. It identifies the run.
	TraceID string
	// Spans holds the finished, recorded spans of the trace, in start order.
	Spans []*Span
}

// Roots returns the spans whose parent is not part of this run, in start order.
func (r Run) Roots() []*Span {
	spanIDs := make(map[string]bool, len(r.Spans))
	for _, span := range r.Spans {
		spanIDs[span.SpanID] = true
	}
	var roots []*Span
	for _, span := range r.Spans {
		if span.ParentID == "" || !spanIDs[span.ParentID] {
			roots = append(roots, span)
		}
	}
	return roots
}

// Children returns the spans of this run whose parent is span, in start order.
func (r Run) Children(span *Span) []*Span {
	var children []*Span
	for _, child := range r.Spans {
		if child.ParentID == span.SpanID {
			children = append(children, child)
		}
	}
	return children
}

// Runs groups the finished spans of this process into runs, one per trace.
//
// Spans are grouped by TraceID. Runs appear in the order their first span started, and each
// run lists its spans in start order. Running spans are not included.
func Runs() []Run {
	finished := FinishedSpans()
	slices.SortStableFunc(finished, func(a, b *Span) int {
		return a.StartTime.Compare(b.StartTime)
	})
	var order []string
	byTrace := make(map[string][]*Span)
	for _, span := range finished {
		if _, seen := byTrace[span.TraceID]; !seen {
			order = append(order, span.TraceID)
		}
		byTrace[span.TraceID] = append(byTrace[span.TraceID], span)
	}
	result := make([]Run, 0, len(order))
	for _, traceID := range order {
		result = append(result, Run{TraceID: traceID, Spans: byTrace[traceID]})
	}
	return result
}

// newSpan creates a span under the active context of ctx and registers it as running when
// it is recorded. It returns an error when options.Kind is not a known span kind.
func newSpan(ctx context.Context, name string, options SpanOptions) (*Span, error) {
	kind := options.Kind
	if kind == "" {
		kind = KindStep
	}
	if !kind.valid() {
		return nil, fmt.Errorf("unknown span kind: %s", kind)
	}
	parent := activeContext(ctx)
	var sampled bool
	if parent != nil {
		sampled = parent.Sampled
	} else {
		// Sampling is load shedding, not a security decision, so a fast PRNG is enough.
		sampled = rand.Float64() < currentSettings().sampleRate
	}
	span := &Span{
		Name:       name,
		TraceID:    randomHex(16),
		SpanID:     randomHex(8),
		Kind:       kind,
		Sampled:    sampled,
		StartTime:  time.Now(),
		attributes: map[string]AttributeValue{},
	}
	if parent != nil {
		span.TraceID = parent.TraceID
		span.ParentID = parent.SpanID
	}
	for _, key := range slices.Sorted(maps.Keys(options.Attributes)) {
		span.SetAttribute(key, options.Attributes[key])
	}
	if sampled {
		store.mu.Lock()
		store.running = append(store.running, span)
		store.mu.Unlock()
	}
	return span, nil
}

// activeContext returns the trace context that spans started with ctx link to: the current
// span when there is one, otherwise the remote context set by ContinueTrace. It returns nil
// when neither exists.
func activeContext(ctx context.Context) *SpanContext {
	if span := CurrentSpan(ctx); span != nil {
		return &SpanContext{TraceID: span.TraceID, SpanID: span.SpanID, Sampled: span.Sampled}
	}
	remote, _ := ctx.Value(remoteContextKey).(*SpanContext)
	return remote
}

// contextKey names the values this package stores in a context.
type contextKey int

const (
	// currentSpanKey stores the span that Trace started.
	currentSpanKey contextKey = iota
	// remoteContextKey stores the context that ContinueTrace received from another process.
	remoteContextKey
)

// limitAttribute cuts a string value to maxLength characters and leaves other values as they are.
func limitAttribute(value AttributeValue, maxLength int) AttributeValue {
	if value.Kind == AttributeString {
		value.String = truncate(value.String, maxLength)
	}
	return value
}

// truncate returns at most maxLength characters of text.
func truncate(text string, maxLength int) string {
	if utf8.RuneCountInString(text) <= maxLength {
		return text
	}
	return string([]rune(text)[:maxLength])
}

// describeError returns the error as "Type: message".
func describeError(err error) string {
	return fmt.Sprintf("%T: %s", err, err.Error())
}

// randomHex returns n random bytes encoded as 2n lowercase hexadecimal characters.
func randomHex(n int) string {
	buf := make([]byte, n)
	// Since Go 1.24, Read never returns an error: it crashes the program instead.
	_, _ = cryptorand.Read(buf)
	return hex.EncodeToString(buf)
}
