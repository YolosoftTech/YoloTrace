package yolotrace

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/url"
	"time"
)

// schemaVersion is the version of the JSON trace format. The ingestion API rejects any other value.
const schemaVersion = 1

// exportBatchSize is the most spans sent in one request. A larger export is split into several requests.
const exportBatchSize = 500

// ErrExport is wrapped by every error ExportSpans returns for a batch that could not be delivered.
var ErrExport = errors.New("export failed")

// ExportSpans sends spans to the ingestion endpoint and returns how many were sent.
//
// endpoint is the full URL of the ingestion route, such as http://127.0.0.1:4318/v1/spans, and
// must be http or https. Pass nil for spans to send every finished span of this process. Spans
// go in batches of 500. When a batch is not delivered, ExportSpans returns how many spans were
// sent before it, and an error that wraps ErrExport. Batches already accepted stay stored, so
// exporting the same spans again stores them again.
func ExportSpans(ctx context.Context, endpoint string, spans []*Span) (int, error) {
	parsed, err := url.Parse(endpoint)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return 0, fmt.Errorf("endpoint must be an http or https URL: %q", endpoint)
	}
	if spans == nil {
		spans = FinishedSpans()
	}
	for start := 0; start < len(spans); start += exportBatchSize {
		end := min(start+exportBatchSize, len(spans))
		if err := postBatch(ctx, endpoint, spans[start:end]); err != nil {
			return start, err
		}
	}
	return len(spans), nil
}

// postBatch sends one batch of spans to endpoint and returns an error wrapping ErrExport unless it is accepted.
func postBatch(ctx context.Context, endpoint string, batch []*Span) error {
	records := make([]spanJSON, len(batch))
	for i, span := range batch {
		records[i] = spanToJSON(span)
	}
	body, err := json.Marshal(batchJSON{SchemaVersion: schemaVersion, Spans: records})
	if err != nil {
		return fmt.Errorf("%w: cannot encode spans: %w", ErrExport, err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("%w: %w", ErrExport, err)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return fmt.Errorf("%w: could not reach %s: %w", ErrExport, endpoint, err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	if response.StatusCode/100 != 2 {
		return fmt.Errorf("%w: ingestion endpoint refused %d spans with HTTP %d", ErrExport, len(batch), response.StatusCode)
	}
	return nil
}

// batchJSON is the request body the ingestion API accepts.
type batchJSON struct {
	SchemaVersion int        `json:"schema_version"`
	Spans         []spanJSON `json:"spans"`
}

// spanJSON is the JSON form of a span, using the data model's field names. Times are Unix seconds.
type spanJSON struct {
	Name       string                    `json:"name"`
	TraceID    string                    `json:"trace_id"`
	SpanID     string                    `json:"span_id"`
	ParentID   *string                   `json:"parent_id"`
	Kind       string                    `json:"kind"`
	StartTime  float64                   `json:"start_time"`
	EndTime    *float64                  `json:"end_time"`
	Duration   *float64                  `json:"duration"`
	Status     string                    `json:"status"`
	Error      *string                   `json:"error"`
	Attributes map[string]AttributeValue `json:"attributes"`
	Events     []eventJSON               `json:"events"`
	Sampled    bool                      `json:"sampled"`
}

// eventJSON is the JSON form of a span event.
type eventJSON struct {
	Name       string                    `json:"name"`
	Time       float64                   `json:"time"`
	Attributes map[string]AttributeValue `json:"attributes"`
}

// MarshalJSON writes the value as a JSON string, number, boolean or null, matching its kind.
func (v AttributeValue) MarshalJSON() ([]byte, error) {
	switch v.Kind {
	case AttributeString:
		return json.Marshal(v.String)
	case AttributeNumber:
		return json.Marshal(v.Number)
	case AttributeBool:
		return json.Marshal(v.Bool)
	default:
		return []byte("null"), nil
	}
}

// spanToJSON returns span in the JSON form the ingestion API accepts. It holds the span's lock
// while it reads the span, so a span that is still being updated is copied consistently.
func spanToJSON(span *Span) spanJSON {
	span.mu.Lock()
	defer span.mu.Unlock()
	record := spanJSON{
		Name:       span.Name,
		TraceID:    span.TraceID,
		SpanID:     span.SpanID,
		Kind:       string(span.Kind),
		StartTime:  unixSeconds(span.StartTime),
		Status:     "ok",
		Attributes: maps.Clone(span.attributes),
		Events:     make([]eventJSON, 0, len(span.events)),
		Sampled:    span.Sampled,
	}
	if span.ParentID != "" {
		parentID := span.ParentID
		record.ParentID = &parentID
	}
	if span.ended {
		endTime := unixSeconds(span.endTime)
		duration := span.endTime.Sub(span.StartTime).Seconds()
		record.EndTime = &endTime
		record.Duration = &duration
	}
	if span.failed {
		failure := span.failure
		record.Status = "error"
		record.Error = &failure
	}
	for _, event := range span.events {
		record.Events = append(record.Events, eventJSON{
			Name:       event.Name,
			Time:       unixSeconds(event.Time),
			Attributes: maps.Clone(event.Attributes),
		})
	}
	return record
}

// unixSeconds returns t as Unix seconds with microsecond precision.
func unixSeconds(t time.Time) float64 {
	return float64(t.UnixMicro()) / 1e6
}
