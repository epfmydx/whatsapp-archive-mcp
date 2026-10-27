package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
)

// These tests exercise the production scheduler, not just its synchronous drain
// helper. Channel barriers stand in for slow provider requests.
func startRetryLoop(t *testing.T, b *bridge) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); b.retry(ctx) }()
	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("retry loop did not join its workers")
		}
	}
	t.Cleanup(stop)
	return stop
}

func awaitSignal[T any](t *testing.T, ch <-chan T) T {
	t.Helper()
	select {
	case value := <-ch:
		return value
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for independent retry progress")
		var zero T
		return zero
	}
}

func awaitSpool(t *testing.T, check func() bool) {
	t.Helper()
	deadline := time.NewTimer(3 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	for !check() {
		select {
		case <-tick.C:
		case <-deadline.C:
			t.Fatal("spool state did not settle")
		}
	}
}

func captureText(t *testing.T, b *bridge, id string, outgoing bool) string {
	t.Helper()
	e := sampleEvent()
	e.Info.ID, e.Info.IsFromMe = id, outgoing
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	r, _ := makeRecord(b.cfg.Account, "", e)
	return recordID(r.Message) + ".json"
}

func TestRetryLoopTextProgressesDuringBlockedMedia(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.diagnosticWriter = io.Discard
	blocked := make(chan struct{})
	release := make(chan struct{})
	var downloads atomic.Int32
	b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		if downloads.Add(1) == 1 {
			close(blocked)
		}
		select {
		case <-release:
			return whatsmeow.ErrNoURLPresent
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	posts := make(chan message, 8)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		var m message
		if json.NewDecoder(r.Body).Decode(&m) != nil {
			t.Error("invalid message")
		}
		posts <- m
		w.WriteHeader(200)
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	mediaName := captureMedia(t, b, "blocked-media")
	stop := startRetryLoop(t, b)
	awaitSignal(t, blocked)
	incoming := captureText(t, b, "incoming-after-block", false)
	captureText(t, b, "incoming-after-block", false) // pending duplicate
	outgoing := captureText(t, b, "outgoing-after-block", true)
	seen := map[string]bool{}
	for range 2 {
		m := awaitSignal(t, posts)
		if m.Media != nil || m.Account != "personal" || seen[m.ID] {
			t.Fatal("duplicate, foreign, or premature media ingestion")
		}
		seen[m.ID] = m.FromMe
	}
	if len(seen) != 2 || seen["incoming-after-block"] || !seen["outgoing-after-block"] {
		t.Fatal("incoming and outgoing text did not both progress")
	}
	awaitSpool(t, func() bool {
		_, a := os.Stat(filepath.Join(b.spool.dir, incoming))
		_, z := os.Stat(filepath.Join(b.spool.dir, outgoing))
		return os.IsNotExist(a) && os.IsNotExist(z)
	})
	if r, err := b.spool.read(mediaName); err != nil || !r.MediaPending || r.Retry.Attempts != 1 || downloads.Load() != 1 {
		t.Fatal("blocked media was lost or attempted concurrently")
	}
	select {
	case <-posts:
		t.Fatal("extra ingestion while media blocked")
	default:
	}
	close(release)
	awaitSpool(t, func() bool {
		r, err := b.spool.read(mediaName)
		return err == nil && r.Retry.Reason == "missing_url" && b.queueError.Load()
	})
	stop()
	if !b.queueError.Load() || b.statusPayload()["error"] != "archive_or_ingestion_pending" {
		t.Fatal("healthy text cleared failed media health")
	}
}

func TestRetryLoopArchivedMediaCannotSwitchLanes(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.diagnosticWriter = io.Discard
	var now atomic.Int64
	now.Store(time.Now().UnixNano())
	b.now = func() time.Time { return time.Unix(0, now.Load()) }
	b.jitter = func(time.Duration) time.Duration { return 0 }
	blocked, release := make(chan struct{}), make(chan struct{})
	var downloads atomic.Int32
	b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		downloads.Add(1)
		close(blocked)
		select {
		case <-release:
			_, err := f.Write([]byte("data"))
			return err
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s3 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		w.Header().Set("ETag", "\"fixture\"")
		w.WriteHeader(200)
	}))
	defer s3.Close()
	b.cfg.S3Endpoint, b.cfg.S3Bucket, b.cfg.S3Region = s3.URL, "private", "us-east-1"
	b.cfg.S3Access, b.cfg.S3Secret = "synthetic", "synthetic"
	b.s3, _ = newS3(b.cfg)
	mediaPost, finishMedia := make(chan struct{}, 8), make(chan struct{})
	textPost := make(chan string, 8)
	var mediaCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		var m message
		json.NewDecoder(r.Body).Decode(&m)
		if m.Media != nil {
			mediaCalls.Add(1)
			mediaPost <- struct{}{}
			select {
			case <-finishMedia:
			case <-r.Context().Done():
			}
		} else {
			textPost <- m.ID
		}
		w.WriteHeader(200)
	}))
	defer srv.Close()
	defer close(finishMedia)
	b.cfg.AppURL = srv.URL
	name := captureMedia(t, b, "archived-media")
	stop := startRetryLoop(t, b)
	awaitSignal(t, blocked)
	close(release)
	awaitSignal(t, mediaPost)
	r, err := b.spool.read(name)
	if err != nil || r.MediaPending || r.Message.Media == nil {
		t.Fatal("archive descriptor not durable before ingestion")
	}
	// Make the reservation due while its ingestion is still in flight. Fresh
	// text sweeps must not take ownership of the newly archived media record.
	now.Add(int64(2 * time.Minute))
	for _, id := range []string{"wake-one", "wake-two", "wake-three"} {
		captureText(t, b, id, false)
		if awaitSignal(t, textPost) != id {
			t.Fatal("unexpected text")
		}
	}
	stop() // cancellation joins both lanes, including the blocked API request
	if downloads.Load() != 1 || mediaCalls.Load() != 1 {
		t.Fatal("archived media switched lanes or duplicated")
	}
	r, err = b.spool.read(name)
	if err != nil || r.Message.Media == nil || r.Retry.Attempts != 1 {
		t.Fatal("shutdown lost archived pending record")
	}
}

func TestRetryLoopTextFailureBackoffSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	b.diagnosticWriter = io.Discard
	var now atomic.Int64
	now.Store(time.Now().UnixNano())
	clock := func() time.Time { return time.Unix(0, now.Load()) }
	b.now = clock
	b.jitter = func(time.Duration) time.Duration { return 0 }
	posts := make(chan string, 16)
	var attempts atomic.Int32
	var reject atomic.Bool
	reject.Store(true)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		var m message
		json.NewDecoder(r.Body).Decode(&m)
		if m.ID == "failed-text" {
			attempts.Add(1)
			if reject.Load() {
				w.WriteHeader(503)
			}
		}
		posts <- m.ID
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	name := captureText(t, b, "failed-text", false)
	stop := startRetryLoop(t, b)
	awaitSignal(t, posts)
	awaitSpool(t, func() bool {
		r, err := b.spool.read(name)
		return err == nil && r.Retry != nil && r.Retry.Reason == "http_status"
	})
	before, _ := b.spool.read(name)
	captureText(t, b, "healthy-text", true)
	awaitSignal(t, posts)
	awaitSpool(t, func() bool { n, _ := b.spool.pending(); return len(n) == 1 })
	if !b.queueError.Load() || b.statusPayload()["error"] != "archive_or_ingestion_pending" {
		t.Fatal("healthy lane erased the text failure")
	}
	stop()
	b.spool.close()

	b = newTestBridge(t, dir)
	b.cfg.AppURL, b.now, b.diagnosticWriter = srv.URL, clock, io.Discard
	b.jitter = func(time.Duration) time.Duration { return 0 }
	stop = startRetryLoop(t, b)
	captureText(t, b, "failed-text", false) // duplicate cannot reset cooldown
	captureText(t, b, "restart-barrier", false)
	if awaitSignal(t, posts) != "restart-barrier" || attempts.Load() != 1 {
		t.Fatal("restart bypassed persisted backoff")
	}
	after, _ := b.spool.read(name)
	if *before.Retry != *after.Retry || !before.CapturedAt.Equal(after.CapturedAt) {
		t.Fatal("restart or duplicate capture changed failure state")
	}
	reject.Store(false)
	now.Store(before.Retry.NextAttemptAt.UnixNano())
	captureText(t, b, "due-barrier", false)
	got := map[string]bool{}
	for range 2 {
		got[awaitSignal(t, posts)] = true
	}
	if !got["failed-text"] || !got["due-barrier"] || attempts.Load() != 2 {
		t.Fatal("due text did not retry exactly once")
	}
	awaitSpool(t, func() bool { n, _ := b.spool.pending(); return len(n) == 0 })
	stop()
}

func TestRetryLoopBoundsEachLaneAndJoinsShutdown(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.diagnosticWriter = io.Discard
	mediaStarted, textStarted := make(chan struct{}), make(chan struct{})
	var downloads, posts atomic.Int32
	b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		if downloads.Add(1) == 1 {
			close(mediaStarted)
		}
		<-ctx.Done()
		return ctx.Err()
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		io.Copy(io.Discard, r.Body)
		if posts.Add(1) == 1 {
			close(textStarted)
		}
		<-r.Context().Done()
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	for _, id := range []string{"media-one", "media-two", "media-three"} {
		captureMedia(t, b, id)
	}
	stop := startRetryLoop(t, b)
	awaitSignal(t, mediaStarted)
	for _, id := range []string{"text-one", "text-two", "text-three"} {
		captureText(t, b, id, false)
	}
	awaitSignal(t, textStarted)
	// More wakes cannot create more workers while both lane calls are blocked.
	for range 30 {
		select {
		case b.wake <- struct{}{}:
		default:
		}
	}
	stop()
	if downloads.Load() != 1 || posts.Load() != 1 {
		t.Fatal("unbounded attempts or work continued after cancellation")
	}
	names, _ := b.spool.pending()
	if len(names) != 6 {
		t.Fatal("shutdown discarded pending records")
	}
	attempted := 0
	for _, name := range names {
		r, _ := b.spool.read(name)
		if r.Retry != nil {
			attempted++
			if r.Retry.Attempts != 1 || !r.Retry.LastFailureAt.IsZero() {
				t.Fatal("shutdown overwrote durable reservation")
			}
		}
	}
	if attempted != 2 {
		t.Fatal("each lane did not reserve exactly one record")
	}
}

func TestRetryLoopPersistenceFailureCancelsBothLanes(t *testing.T) {
	for _, final := range []bool{false, true} {
		t.Run(map[bool]string{false: "reservation", true: "failure-completion"}[final], func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			b.diagnosticWriter = io.Discard
			blocked, cancelled := make(chan struct{}), make(chan struct{})
			b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
				close(blocked)
				<-ctx.Done()
				close(cancelled)
				return ctx.Err()
			}
			var posts atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("X-Archive-Format", "2")
				posts.Add(1)
				w.WriteHeader(503)
			}))
			defer srv.Close()
			b.cfg.AppURL = srv.URL
			captureMedia(t, b, "blocked-before-fatal")
			stop := startRetryLoop(t, b)
			awaitSignal(t, blocked)
			b.spool.mu.Lock()
			b.spool.writeRecord = func(dir, name string, data []byte) error {
				var r record
				if err := json.Unmarshal(data, &r); err != nil {
					return err
				}
				if r.Message.ID == "failing-text" && r.Retry != nil && (final == !r.Retry.LastFailureAt.IsZero()) {
					return os.ErrPermission
				}
				return atomicWrite(dir, name, data)
			}
			b.spool.mu.Unlock()
			name := captureText(t, b, "failing-text", false)
			awaitSignal(t, b.fatal)
			awaitSignal(t, cancelled)
			stop()
			if posts.Load() != map[bool]int32{false: 0, true: 1}[final] || !b.queueError.Load() {
				t.Fatal("fatal persistence failure did not stop work and latch health")
			}
			r, err := b.spool.read(name)
			if err != nil || (r.Retry != nil) != final || (r.Retry != nil && !r.Retry.LastFailureAt.IsZero()) {
				t.Fatal("failed persistence altered durable state")
			}
		})
	}
}

// The two lanes share one diagnostic destination, which may itself have no
// synchronization (for example the bytes buffer used here).
func TestConcurrentLaneDiagnosticsRemainCompleteJSON(t *testing.T) {
	var out bytes.Buffer
	b := &bridge{diagnosticWriter: &out}
	var writers sync.WaitGroup
	for range 2 {
		writers.Add(1)
		go func() {
			defer writers.Done()
			for range 100 {
				b.emitDiagnostic(diagnostic{Event: "spool_attempt", Stage: "ingestion", Reason: "http_status", HTTPStatus: 503})
			}
		}()
	}
	writers.Wait()
	lines := bytes.Split(bytes.TrimSpace(out.Bytes()), []byte{'\n'})
	if len(lines) != 200 {
		t.Fatal("concurrent diagnostics lost records")
	}
	for _, line := range lines {
		var d diagnostic
		if json.Unmarshal(line, &d) != nil || d.HTTPStatus != 503 || d.Stage != "ingestion" {
			t.Fatal("concurrent diagnostics interleaved records")
		}
	}
}
