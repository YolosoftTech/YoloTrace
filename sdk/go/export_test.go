package yolotrace_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/YolosoftTech/YoloTrace/sdk/go"
)

// fakeIngestion records every request it receives and answers with status.
type fakeIngestion struct {
	mu     sync.Mutex
	status int
	bodies []map[string]any
	paths  []string
	types  []string
}

func (f *fakeIngestion) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	raw, _ := io.ReadAll(r.Body)
	var body map[string]any
	_ = json.Unmarshal(raw, &body)
	f.mu.Lock()
	f.bodies = append(f.bodies, body)
	f.paths = append(f.paths, r.Method+" "+r.URL.Path)
	f.types = append(f.types, r.Header.Get("Content-Type"))
	f.mu.Unlock()
	w.WriteHeader(f.status)
}

// newIngestion starts a fake ingestion endpoint that answers with status.
func newIngestion(t *testing.T, status int) (*fakeIngestion, string) {
	t.Helper()
	fake := &fakeIngestion{status: status}
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	return fake, server.URL + "/v1/spans"
}

// finishedSpan starts and ends a span named name, and returns it.
func finishedSpan(t *testing.T, name string) *yolotrace.Span {
	t.Helper()
	span, err := yolotrace.StartSpan(context.Background(), name, yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	span.End(nil)
	return span
}

func TestExportSpansPostsTheDocumentedEnvelope(t *testing.T) {
	resetAfter(t)
	fake, endpoint := newIngestion(t, http.StatusAccepted)
	span := finishedSpan(t, "answer")
	span.AddEvent("retry", map[string]yolotrace.AttributeValue{"attempt": yolotrace.NumberValue(2)})

	sent, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{span})

	if err != nil || sent != 1 {
		t.Fatalf("ExportSpans = %d, %v; want 1, nil", sent, err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.paths[0] != "POST /v1/spans" || fake.types[0] != "application/json" {
		t.Errorf("request = %q with %q", fake.paths[0], fake.types[0])
	}
	body := fake.bodies[0]
	if body["schema_version"] != float64(1) {
		t.Errorf("schema_version = %v, want 1", body["schema_version"])
	}
	record := body["spans"].([]any)[0].(map[string]any)
	if record["name"] != "answer" || record["status"] != "ok" || record["sampled"] != true {
		t.Errorf("span record = %v", record)
	}
	if record["parent_id"] != nil || record["error"] != nil {
		t.Errorf("root span should have null parent_id and error: %v", record)
	}
	if len(record["span_id"].(string)) != 16 || len(record["trace_id"].(string)) != 32 {
		t.Errorf("IDs have the wrong length: %v", record)
	}
	event := record["events"].([]any)[0].(map[string]any)
	if event["name"] != "retry" || event["attributes"].(map[string]any)["attempt"] != float64(2) {
		t.Errorf("event record = %v", event)
	}
	if _, ok := record["end_time"].(float64); !ok {
		t.Errorf("end_time should be a number for a finished span: %v", record["end_time"])
	}
}

func TestExportSpansWritesFailureAndRunningSpanAsDocumented(t *testing.T) {
	resetAfter(t)
	fake, endpoint := newIngestion(t, http.StatusAccepted)
	failed, err := yolotrace.StartSpan(context.Background(), "failed", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	failed.End(errors.New("bad input"))
	running, err := yolotrace.StartSpan(context.Background(), "running", yolotrace.SpanOptions{})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{failed, running}); err != nil {
		t.Fatal(err)
	}

	fake.mu.Lock()
	defer fake.mu.Unlock()
	spans := fake.bodies[0]["spans"].([]any)
	failedRecord := spans[0].(map[string]any)
	if failedRecord["status"] != "error" || failedRecord["error"] != "*errors.errorString: bad input" {
		t.Errorf("failed span record = %v", failedRecord)
	}
	runningRecord := spans[1].(map[string]any)
	if runningRecord["end_time"] != nil || runningRecord["duration"] != nil {
		t.Errorf("running span should have null end_time and duration: %v", runningRecord)
	}
}

func TestExportSpansWithNilSendsFinishedSpans(t *testing.T) {
	resetAfter(t)
	fake, endpoint := newIngestion(t, http.StatusAccepted)
	finishedSpan(t, "first")
	finishedSpan(t, "second")
	want := len(yolotrace.FinishedSpans())

	sent, err := yolotrace.ExportSpans(context.Background(), endpoint, nil)

	if err != nil || sent != want {
		t.Fatalf("ExportSpans = %d, %v; want %d, nil", sent, err, want)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if got := len(fake.bodies[0]["spans"].([]any)); got != want {
		t.Errorf("sent %d spans, want %d", got, want)
	}
}

func TestExportSpansWithEmptySliceSendsNothing(t *testing.T) {
	resetAfter(t)
	fake, endpoint := newIngestion(t, http.StatusAccepted)

	sent, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{})

	if err != nil || sent != 0 {
		t.Fatalf("ExportSpans = %d, %v; want 0, nil", sent, err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.bodies) != 0 {
		t.Errorf("sent %d requests, want none", len(fake.bodies))
	}
}

func TestExportSpansSplitsLargeExportsIntoBatches(t *testing.T) {
	resetAfter(t)
	fake, endpoint := newIngestion(t, http.StatusAccepted)
	spans := make([]*yolotrace.Span, 503)
	for i := range spans {
		spans[i] = finishedSpan(t, "span")
	}

	sent, err := yolotrace.ExportSpans(context.Background(), endpoint, spans)

	if err != nil || sent != 503 {
		t.Fatalf("ExportSpans = %d, %v; want 503, nil", sent, err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.bodies) != 2 {
		t.Fatalf("sent %d requests, want 2", len(fake.bodies))
	}
	first := len(fake.bodies[0]["spans"].([]any))
	second := len(fake.bodies[1]["spans"].([]any))
	if first != 500 || second != 3 {
		t.Errorf("batch sizes = %d and %d, want 500 and 3", first, second)
	}
}

func TestExportSpansRejectsANonHTTPEndpoint(t *testing.T) {
	resetAfter(t)
	for _, endpoint := range []string{"file:///tmp/spans.json", "not a url", "ftp://example.com/spans"} {
		if _, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{}); err == nil {
			t.Errorf("ExportSpans(%q) returned no error", endpoint)
		}
	}
}

func TestExportSpansReportsARefusedBatchAsErrExport(t *testing.T) {
	resetAfter(t)
	_, endpoint := newIngestion(t, http.StatusBadRequest)

	sent, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{finishedSpan(t, "rejected")})

	if !errors.Is(err, yolotrace.ErrExport) {
		t.Fatalf("error = %v, want ErrExport", err)
	}
	if sent != 0 || !strings.Contains(err.Error(), "refused 1 spans with HTTP 400") {
		t.Errorf("sent = %d, error = %v", sent, err)
	}
}

func TestExportSpansReportsAnUnreachableEndpointAsErrExport(t *testing.T) {
	resetAfter(t)
	server := httptest.NewServer(http.NotFoundHandler())
	endpoint := server.URL + "/v1/spans"
	server.Close()

	_, err := yolotrace.ExportSpans(context.Background(), endpoint, []*yolotrace.Span{finishedSpan(t, "lost")})

	if !errors.Is(err, yolotrace.ErrExport) {
		t.Fatalf("error = %v, want ErrExport", err)
	}
}
