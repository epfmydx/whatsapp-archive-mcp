package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"go.mau.fi/whatsmeow"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func fixedRetryClock(b *bridge) *time.Time {
	now := time.Date(2026, 10, 27, 1, 0, 0, 0, time.UTC)
	b.now = func() time.Time { return now }
	b.jitter = func(time.Duration) time.Duration { return 0 }
	b.diagnosticWriter = io.Discard
	return &now
}
func captureMedia(t *testing.T, b *bridge, id string) string {
	t.Helper()
	e := sampleEvent()
	e.Info.ID = id
	e.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileLength: proto.Uint64(4), FileName: proto.String("secret-file")}}
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	r, _ := makeRecord("personal", "", e)
	return recordID(r.Message) + ".json"
}
func TestBacklogRapidFailuresRemainCoolingAcrossPolls(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	calls := 0
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		calls++
		return whatsmeow.ErrNoURLPresent
	}
	const backlog = 120
	for n := 0; n < backlog; n++ {
		captureMedia(t, b, fmt.Sprint(n))
	}
	b.drain(context.Background())
	for n := 0; n < 25; n++ {
		*now = now.Add(time.Second)
		b.drain(context.Background())
	}
	if calls != backlog {
		t.Fatalf("rapid polls caused %d attempts", calls)
	}
	h := b.spool.retryHealth(*now)
	if h.CoolingDown != backlog || h.Failed != backlog || h.ErrorsByCode["missing_url"] != backlog {
		t.Fatalf("bad aggregate %#v", h)
	}
	if b.statusPayload()["error"] != "archive_or_ingestion_pending" {
		t.Fatal("cooldowns appear healthy")
	}
	*now = now.Add(35 * time.Second)
	b.drain(context.Background())
	if calls != 2*backlog {
		t.Fatal("due items did not retry")
	}
	names, _ := b.spool.pending()
	for _, name := range names {
		r, e := b.spool.read(name)
		if e != nil || r.Retry.Attempts != 2 || !r.Retry.NextAttemptAt.Equal(now.Add(2*time.Minute)) || r.ArchiveError != "missing_url" {
			t.Fatal("retry metadata missing")
		}
	}
	files, _ := os.ReadDir(b.spool.dir)
	for _, f := range files {
		if strings.HasPrefix(f.Name(), ".download-") {
			t.Fatal("partial file leaked")
		}
	}
}
func TestRetrySurvivesRestartAndDuplicateCapture(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	now := fixedRetryClock(b)
	name := captureMedia(t, b, "retry")
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return whatsmeow.ErrNoURLPresent
	}
	b.drain(context.Background())
	before, _ := b.spool.read(name)
	b.spool.close()
	b = newTestBridge(t, dir)
	b.now = func() time.Time { return *now }
	b.diagnosticWriter = io.Discard
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		t.Fatal("cooldown restarted early")
		return nil
	}
	captureMedia(t, b, "retry")
	b.drain(context.Background())
	after, _ := b.spool.read(name)
	if !after.CapturedAt.Equal(before.CapturedAt) || *after.Retry != *before.Retry {
		t.Fatal("duplicate reset durable scheduling")
	}
	if b.statusPayload()["error"] != "archive_or_ingestion_pending" {
		t.Fatal("restart cleared failure health")
	}
}
func TestFailedItemDoesNotStarveHealthyLaterItems(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	captureMedia(t, b, "bad")
	good := sampleEvent()
	good.Info.ID = "good"
	b.capture(good, "")
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return whatsmeow.ErrNoURLPresent
	}
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		calls++
		w.WriteHeader(200)
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	b.drain(context.Background())
	if calls != 1 {
		t.Fatal("healthy message starved")
	}
	names, _ := b.spool.pending()
	if len(names) != 1 {
		t.Fatal("wrong retained records")
	}
}
func TestRetryPersistenceFailureStopsBeforeExternalIO(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	name := captureMedia(t, b, "bad")
	before, _ := os.ReadFile(filepath.Join(b.spool.dir, name))
	b.spool.writeRecord = func(string, string, []byte) error { return errors.New("private-full-disk") }
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		t.Fatal("download without persisted schedule")
		return nil
	}
	b.drain(context.Background())
	select {
	case <-b.fatal:
	default:
		t.Fatal("receiver not stopped")
	}
	after, _ := os.ReadFile(filepath.Join(b.spool.dir, name))
	if !bytes.Equal(before, after) {
		t.Fatal("failed write changed record")
	}
	b.spool.writeRecord = nil
	b.drain(context.Background()) // fatal barrier remains closed
}
func TestFinalFailurePersistenceStopsFurtherItems(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	captureMedia(t, b, "first")
	captureMedia(t, b, "second")
	writes, calls := 0, 0
	b.spool.writeRecord = func(d, n string, p []byte) error {
		writes++
		if writes == 2 {
			return errors.New("full")
		}
		return atomicWrite(d, n, p)
	}
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		calls++
		return whatsmeow.ErrNoURLPresent
	}
	b.drain(context.Background())
	if calls != 1 {
		t.Fatal("continued after final persistence failure")
	}
	select {
	case <-b.fatal:
	default:
		t.Fatal("missing fatal barrier")
	}
}
func TestRetryBackoffSaturates(t *testing.T) {
	b := &bridge{jitter: func(time.Duration) time.Duration { return 0 }}
	if b.retryDelay(1) != time.Minute || b.retryDelay(2) != 2*time.Minute || b.retryDelay(math.MaxUint32) != time.Hour {
		t.Fatal("unbounded delay")
	}
	b = newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := captureMedia(t, b, "saturated")
	r, _ := b.spool.read(name)
	r.Retry = &retryState{Attempts: math.MaxUint32}
	r.MediaPending = false
	b.spool.save(r, false)
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return whatsmeow.ErrNoURLPresent
	}
	b.drain(context.Background())
	r, _ = b.spool.read(name)
	if r.Retry.Attempts != math.MaxUint32 || !r.Retry.NextAttemptAt.Equal(now.Add(time.Hour)) {
		t.Fatal("attempt overflow")
	}
}
func TestTypedDiagnosticsNeverExposePrivateErrors(t *testing.T) {
	private := "https://host/private?token=SECRET phone=123 filename=secret-file"
	cases := []struct {
		err    error
		reason string
		status int
	}{
		{fmt.Errorf(private+": %w", whatsmeow.ErrMediaDownloadFailedWith404), "http_status", 404},
		{whatsmeow.ErrMediaDownloadFailedWith403, "http_status", 403},
		{fmt.Errorf(private+": %w", whatsmeow.ErrNoURLPresent), "missing_url", 0},
		{whatsmeow.ErrInvalidMediaHMAC, "integrity_hmac", 0}, {whatsmeow.ErrInvalidMediaSHA256, "integrity_hash", 0},
		{&net.OpError{Op: private, Err: errors.New(private)}, "network", 0},
		{&tls.CertificateVerificationError{Err: errors.New(private)}, "tls", 0},
		{&os.PathError{Path: private, Err: errors.New(private)}, "filesystem", 0},
		{minio.ErrorResponse{Code: private, Message: private, StatusCode: 503}, "http_status", 503},
		{minio.ErrorResponse{Code: "AccessDenied", Message: private, StatusCode: 403}, "s3_access_denied", 403},
		{context.DeadlineExceeded, "timeout", 0}, {errors.New(private), "unknown", 0},
	}
	for _, tc := range cases {
		stage, reason, status := classifyStage("media_download", tc.err)
		if reason != tc.reason || status != tc.status {
			t.Fatalf("got %s %d want %s %d", reason, status, tc.reason, tc.status)
		}
		var out bytes.Buffer
		b := &bridge{diagnosticWriter: &out}
		b.emitDiagnostic(diagnostic{Event: "media_download", Stage: stage, Reason: reason, HTTPStatus: status, MediaType: private})
		if strings.Contains(out.String(), "SECRET") || strings.Contains(out.String(), "private") || strings.Contains(out.String(), "phone") {
			t.Fatal("private error logged")
		}
		var value map[string]any
		if json.Unmarshal(out.Bytes(), &value) != nil || len(value) != 10 {
			t.Fatal("unexpected diagnostic schema")
		}
	}
}
func TestDownloadDiagnosticsRecordActualBytesAndElapsed(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	var out bytes.Buffer
	b.diagnosticWriter = &out
	captureMedia(t, b, "bytes")
	b.download = func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		f.Write([]byte("xy"))
		time.Sleep(3 * time.Millisecond)
		return fmt.Errorf("secret: %w", whatsmeow.ErrTooShortFile)
	}
	b.drain(context.Background())
	var first diagnostic
	if json.Unmarshal(bytes.Split(out.Bytes(), []byte{'\n'})[0], &first) != nil {
		t.Fatal("invalid JSON")
	}
	if first.PartialBytes != 2 || first.ExpectedBytes != 4 || first.ElapsedMS < 2 || first.MediaType != "document" || first.Attempt != 1 {
		t.Fatalf("missing diagnostic %#v", first)
	}
}
func TestLegacyAndSnapshotConcurrency(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	e := sampleEvent()
	b.capture(e, "")
	names, _ := b.spool.pending()
	r, _ := b.spool.read(names[0])
	if r.Retry != nil || b.spool.retryHealth(*now).Due != 1 {
		t.Fatal("legacy record not immediately due")
	}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for n := 0; n < 100; n++ {
			b.statusPayload()
		}
	}()
	go func() {
		defer wg.Done()
		for n := 0; n < 20; n++ {
			r.Message.ID = fmt.Sprint(n)
			b.spool.save(r, true)
		}
	}()
	wg.Wait()
}
func TestCancellationRetainsScheduleAndPreviousFailure(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	name := captureMedia(t, b, "cancel")
	ctx, cancel := context.WithCancel(context.Background())
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		cancel()
		return context.Canceled
	}
	b.drain(ctx)
	r, _ := b.spool.read(name)
	if r.Retry == nil || r.Retry.Attempts != 1 || !r.Retry.LastFailureAt.IsZero() || r.ArchiveError != "" {
		t.Fatal("shutdown smeared failure state")
	}
	if len(r.Proto) == 0 {
		t.Fatal("cancel erased metadata")
	}
}
func TestVerifiedCacheSurvivesIngestionRetries(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	original := []byte("data")
	hash := sha256.Sum256(original)
	e := sampleEvent()
	e.Message = &waE2E.Message{AudioMessage: &waE2E.AudioMessage{FileLength: proto.Uint64(4), FileSHA256: hash[:], Mimetype: proto.String("audio/ogg")}}
	b.capture(e, "")
	names, _ := b.spool.pending()
	name := names[0]
	downloads := 0
	b.download = func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		downloads++
		_, err := f.Write(original)
		return err
	}
	s3 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		w.Header().Set("ETag", "\"fixture\"")
		w.WriteHeader(200)
	}))
	defer s3.Close()
	b.cfg.S3Endpoint = s3.URL
	b.cfg.S3Bucket = "private"
	b.cfg.S3Region = "us-east-1"
	b.cfg.S3Access = "synthetic"
	b.cfg.S3Secret = "synthetic"
	b.s3, _ = newS3(b.cfg)
	reject := true
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if reject {
			w.WriteHeader(503)
		} else {
			w.WriteHeader(200)
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	b.drain(context.Background())
	r, _ := b.spool.read(name)
	if r.MediaPending || r.Message.Media == nil {
		t.Fatal("verified upload not saved")
	}
	local := filepath.Join(b.spool.dir, recordID(r.Message)+".media")
	if _, err := os.Stat(local); err != nil {
		t.Fatal("cache deleted on API failure")
	}
	*now = now.Add(time.Minute)
	reject = false
	b.drain(context.Background())
	if downloads != 1 {
		t.Fatal("API retry redownloaded media")
	}
	names, _ = b.spool.pending()
	if len(names) != 0 {
		t.Fatal("successful retry not cleaned")
	}
}

// Only the durable failure completion owns the next retry timestamp. A long SDK
// call and newly drawn jitter both make the pre-attempt reservation inaccurate.
func TestDownloadLogsOnlyPersistedFailureCooldown(t *testing.T) {
	for _, elapsed := range []time.Duration{0, 2 * time.Minute} {
		t.Run(elapsed.String(), func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			now := fixedRetryClock(b)
			var out bytes.Buffer
			b.diagnosticWriter = &out
			draws := 0
			b.jitter = func(time.Duration) time.Duration {
				draws++
				if draws == 1 {
					return time.Second
				}
				return 7 * time.Second
			}
			name := captureMedia(t, b, "scheduled")
			b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
				*now = now.Add(elapsed)
				return whatsmeow.ErrNoURLPresent
			}
			b.drain(context.Background())
			saved, err := b.spool.read(name)
			if err != nil {
				t.Fatal(err)
			}
			lines := bytes.Split(bytes.TrimSpace(out.Bytes()), []byte{'\n'})
			if len(lines) != 2 {
				t.Fatalf("got %d diagnostic lines", len(lines))
			}
			timestamps := 0
			for _, line := range lines {
				var entry map[string]json.RawMessage
				if json.Unmarshal(line, &entry) != nil {
					t.Fatal("invalid diagnostic")
				}
				raw, ok := entry["next_retry_at"]
				if !ok {
					continue
				}
				timestamps++
				var event string
				json.Unmarshal(entry["event"], &event)
				if event != "spool_attempt" {
					t.Fatal("SDK event includes provisional retry schedule")
				}
				var next time.Time
				if json.Unmarshal(raw, &next) != nil || !next.Equal(saved.Retry.NextAttemptAt) {
					t.Fatal("diagnostic differs from durable retry schedule")
				}
			}
			if timestamps != 1 || draws != 2 || !saved.Retry.NextAttemptAt.Equal(now.Add(time.Minute+7*time.Second)) {
				t.Fatal("missing authoritative completion cooldown")
			}
		})
	}
}

func TestProtocolDownloadFailureDiagnosticsArePrivateAndNotHTTP(t *testing.T) {
	private := "PRIVATE_SECRET https://host/path?token=secret phone=123 filename=private"
	node := &waBinary.Node{Tag: private, Attrs: waBinary.Attrs{"token": private}, Content: []byte(private)}
	cases := []struct {
		name   string
		err    error
		reason string
	}{
		{"401", &whatsmeow.IQError{Code: 401, Text: private, RawNode: node, ErrorNode: node}, "protocol_denied"},
		{"403", &whatsmeow.IQError{Code: 403, Text: private, RawNode: node, ErrorNode: node}, "protocol_denied"},
		{"419", &whatsmeow.IQError{Code: 419, Text: private, RawNode: node, ErrorNode: node}, "protocol_rate_limited"},
		{"429", &whatsmeow.IQError{Code: 429, Text: private, RawNode: node, ErrorNode: node}, "protocol_rate_limited"},
		{"500", &whatsmeow.IQError{Code: 500, Text: private, RawNode: node, ErrorNode: node}, "protocol_unavailable"},
		{"599", &whatsmeow.IQError{Code: 599, Text: private, RawNode: node, ErrorNode: node}, "protocol_unavailable"},
		{"404", &whatsmeow.IQError{Code: 404, Text: private, RawNode: node, ErrorNode: node}, "protocol_error"},
		{"zero", &whatsmeow.IQError{Text: private, RawNode: node, ErrorNode: node}, "protocol_error"},
		{"disconnected", &whatsmeow.DisconnectedError{Action: private, Node: node}, "disconnected"},
		{"not_connected", whatsmeow.ErrNotConnected, "disconnected"},
		{"unknown", errors.New(private), "unknown"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			fixedRetryClock(b)
			var out bytes.Buffer
			b.diagnosticWriter = &out
			name := captureMedia(t, b, "protocol")
			b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
				return fmt.Errorf("%s: %w", private, tc.err)
			}
			b.drain(context.Background())
			saved, err := b.spool.read(name)
			if err != nil || saved.Retry.Reason != tc.reason || saved.Retry.HTTPStatus != 0 {
				t.Fatal("protocol failure metadata wrong")
			}
			if strings.Contains(out.String(), "PRIVATE_SECRET") || strings.Contains(out.String(), "https:") || strings.Contains(out.String(), "phone") || strings.Contains(out.String(), "filename") {
				t.Fatal("private protocol detail leaked")
			}
			lines := bytes.Split(bytes.TrimSpace(out.Bytes()), []byte{'\n'})
			if len(lines) != 2 {
				t.Fatal("missing diagnostics")
			}
			for _, line := range lines {
				var event diagnostic
				if json.Unmarshal(line, &event) != nil || event.Reason != tc.reason || event.HTTPStatus != 0 || event.PartialBytes != 0 {
					t.Fatal("protocol mislabeled as HTTP or unsafe pre-write diagnostic")
				}
			}
		})
	}
}
