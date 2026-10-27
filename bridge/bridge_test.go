package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func newTestBridge(t *testing.T, dir string) *bridge {
	t.Helper()
	sp, e := newSpool(dir, "personal")
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(sp.close)
	return &bridge{cfg: config{Account: "personal", Token: strings.Repeat("t", 32), MaxMedia: 1024}, spool: sp, http: &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, status: "awaiting_pairing", wake: make(chan struct{}, 1), fatal: make(chan struct{})}
}
func sampleEvent() *events.Message {
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.NewJID("test", types.DefaultUserServer), Sender: types.NewJID("sender", types.DefaultUserServer)}, ID: "message-id", Timestamp: time.Unix(123, 0), PushName: "Sender"}, Message: &waE2E.Message{Conversation: proto.String("private text")}}
}
func TestPrivateRoutesAndNoMutationRoutes(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.qr = "private-qr"
	b.qrUntil = time.Now().Add(time.Minute)
	for _, tc := range []struct {
		path, method, token string
		status              int
	}{{"/healthz", "GET", "", 200}, {"/qr", "GET", "", 401}, {"/qr", "GET", "Bearer wrong", 401}, {"/status", "GET", "", 401}, {"/qr", "GET", "Bearer " + b.cfg.Token, 200}, {"/status", "GET", "Bearer " + b.cfg.Token, 200}, {"/qr", "POST", "Bearer " + b.cfg.Token, 405}, {"/send", "POST", "Bearer " + b.cfg.Token, 404}, {"/accounts/other/qr", "GET", "Bearer " + b.cfg.Token, 404}, {"/qr/", "GET", "Bearer " + b.cfg.Token, 404}} {
		t.Run(tc.path+tc.method+tc.token, func(t *testing.T) {
			r := httptest.NewRequest(tc.method, tc.path, nil)
			r.Header.Set("Authorization", tc.token)
			w := httptest.NewRecorder()
			b.ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatalf("got %d want %d", w.Code, tc.status)
			}
			if tc.status != 200 && strings.Contains(w.Body.String(), b.qr) {
				t.Fatal("QR leaked")
			}
			if w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("missing private cache control")
			}
		})
	}
	b.qrUntil = time.Now().Add(-time.Second)
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/qr", nil)
	r.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	b.ServeHTTP(w, r)
	var v map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &v)
	if v["code"] != nil {
		t.Fatal("expired QR exposed")
	}
	b.setStatus("connected")
	if b.qr != "" {
		t.Fatal("QR not cleared on connection")
	}
}
func TestSpoolRestartDedupAndAccountIsolation(t *testing.T) {
	dir := t.TempDir()
	sp, e := newSpool(dir, "personal")
	if e != nil {
		t.Fatal(e)
	}
	r, e := makeRecord("personal", "Chat", sampleEvent())
	if e != nil {
		t.Fatal(e)
	}
	if e = sp.save(r, true); e != nil {
		t.Fatal(e)
	}
	r.Message.Text = "duplicate"
	if e = sp.save(r, true); e != nil {
		t.Fatal(e)
	}
	if _, e = newSpool(dir, "personal"); e == nil {
		t.Fatal("concurrent spool allowed")
	}
	sp.close()
	sp, e = newSpool(dir, "personal")
	if e != nil {
		t.Fatal(e)
	}
	names, e := sp.pending()
	if e != nil || len(names) != 1 {
		t.Fatalf("missing durable record %v", e)
	}
	stored, e := sp.read(names[0])
	if e != nil || stored.Message.Text != "private text" || len(stored.Proto) == 0 {
		t.Fatal("dedup or restart lost metadata")
	}
	foreign := stored
	foreign.Message.Account = "other"
	if sp.save(foreign, true) == nil {
		t.Fatal("foreign account accepted")
	}
	if _, e = sp.read("../secret"); e == nil {
		t.Fatal("path traversal accepted")
	}
	info, _ := os.Stat(filepath.Join(dir, names[0]))
	if info.Mode().Perm() != 0600 {
		t.Fatal("record not private")
	}
	sp.close()
	if _, e = newSpool(dir, "other"); e == nil {
		t.Fatal("spool account reassignment accepted")
	}
}
func TestRejectedIngestionRetriedAfterRestart(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	b.capture(sampleEvent(), "Chat")
	reject := true
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		calls++
		if r.URL.Path != "/internal/messages" || r.Header.Get("Authorization") != "Bearer "+b.cfg.Token {
			t.Error("wrong callback contract")
		}
		if reject {
			w.WriteHeader(503)
			return
		}
		var m message
		if json.NewDecoder(r.Body).Decode(&m) != nil || m.Account != "personal" || m.Text != "private text" {
			t.Error("wrong message payload")
		}
		w.WriteHeader(200)
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	b.drain(context.Background())
	pending, _ := b.spool.pending()
	if len(pending) != 1 || !b.queueError.Load() {
		t.Fatal("rejected message not retained")
	}
	b.spool.close()
	b = newTestBridge(t, dir)
	b.cfg.AppURL = srv.URL
	b.now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	reject = false
	b.drain(context.Background())
	pending, _ = b.spool.pending()
	if len(pending) != 0 || calls != 2 {
		t.Fatal("restart retry failed")
	}
}
func TestBoundedFile(t *testing.T) {
	f, e := os.CreateTemp(t.TempDir(), "bounded")
	if e != nil {
		t.Fatal(e)
	}
	defer f.Close()
	bf := boundedFile{f, 8}
	if _, e = bf.Write([]byte("12345678")); e != nil {
		t.Fatal(e)
	}
	if _, e = bf.Write([]byte("9")); e == nil {
		t.Fatal("write exceeded bound")
	}
	if _, e = bf.WriteAt([]byte("12"), 7); e == nil {
		t.Fatal("writeat exceeded bound")
	}
	if e = bf.Truncate(9); e == nil {
		t.Fatal("truncate exceeded bound")
	}
	if _, e = bf.Seek(9, io.SeekStart); e == nil {
		t.Fatal("seek exceeded bound")
	}
	if _, e = bf.Seek(-9, io.SeekEnd); e == nil {
		t.Fatal("negative seek accepted")
	}
}
func TestHistorySyncCapturedWithChatMetadata(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.client = &whatsmeow.Client{}
	b.event(&events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String("chat@s.whatsapp.net"), Name: proto.String("History Chat"), Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("history-1"), FromMe: proto.Bool(false)}, MessageTimestamp: proto.Uint64(123), Message: &waE2E.Message{Conversation: proto.String("history text")}}}}}}}})
	names, _ := b.spool.pending()
	if len(names) != 1 {
		t.Fatal("history not captured")
	}
	r, e := b.spool.read(names[0])
	if e != nil || r.Message.ChatName != "History Chat" || r.Message.Text != "history text" {
		t.Fatalf("history conversion failed %v", e)
	}
}
func TestMediaOriginalArchiveRetryAndSafeKey(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	original := []byte("original media bytes")
	digest := sha256.Sum256(original)
	event := sampleEvent()
	event.Message = &waE2E.Message{AudioMessage: &waE2E.AudioMessage{Mimetype: proto.String("audio/ogg"), FileLength: proto.Uint64(uint64(len(original))), FileSHA256: digest[:]}}
	b.capture(event, "")
	names, _ := b.spool.pending()
	r, e := b.spool.read(names[0])
	if e != nil {
		t.Fatal(e)
	}
	downloads := 0
	b.download = func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		downloads++
		_, e := f.Write(original)
		return e
	}
	reject := true
	uploads := 0
	s3 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		uploads++
		if !strings.HasPrefix(r.URL.Path, "/private/accounts/personal/media/") || strings.Contains(r.URL.Path, event.Info.ID) {
			t.Error("unsafe S3 object key")
		}
		if reject {
			w.Header().Set("Content-Type", "application/xml")
			w.WriteHeader(403)
			_, _ = io.WriteString(w, "<Error><Code>AccessDenied</Code></Error>")
			return
		}
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), string(original)) {
			t.Error("original missing from upload")
		}
		w.Header().Set("ETag", "\"fixture\"")
		w.WriteHeader(200)
	}))
	defer s3.Close()
	b.cfg.S3Endpoint = s3.URL
	b.cfg.S3Bucket = "private"
	b.cfg.S3Region = "us-east-1"
	b.cfg.S3Access = "synthetic"
	b.cfg.S3Secret = "synthetic"
	b.s3, e = newS3(b.cfg)
	if e != nil {
		t.Fatal(e)
	}
	if b.archive(context.Background(), &r) == nil {
		t.Fatal("S3 rejection ignored")
	}
	if !r.MediaPending {
		t.Fatal("archive prematurely completed")
	}
	r, e = b.spool.read(names[0])
	if e != nil || len(r.Proto) == 0 {
		t.Fatal("media metadata lost")
	}
	reject = false
	if e = b.archive(context.Background(), &r); e != nil {
		t.Fatal(e)
	}
	if downloads != 1 || uploads != 2 || r.Message.Media == nil || r.Message.Media.Size != int64(len(original)) {
		t.Fatal("durable original retry failed")
	}
	persisted, e := b.spool.read(names[0])
	if e != nil || persisted.MediaPending || persisted.Message.Media.SHA256 == "" {
		t.Fatal("archive completion not durable")
	}
}
func TestMediaDownloadFailureRetainsMetadata(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := sampleEvent()
	e.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileLength: proto.Uint64(2), FileName: proto.String("../../unsafe")}}
	b.capture(e, "")
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return errors.New("synthetic failure")
	}
	b.drain(context.Background())
	names, _ := b.spool.pending()
	if len(names) != 1 {
		t.Fatal("lost failed media")
	}
	r, err := b.spool.read(names[0])
	if err != nil || !r.MediaPending || len(r.Proto) == 0 {
		t.Fatal("original metadata not retained")
	}
}
func TestConfigRejectsUnknownAccount(t *testing.T) {
	for k, v := range map[string]string{"ACCOUNT_ID": "unknown", "INTERNAL_TOKEN": strings.Repeat("t", 32), "SESSION_DATABASE_URL": "postgres://user:pass@localhost/session", "S3_ENDPOINT_URL": "http://localhost:9000", "S3_BUCKET": "private", "S3_ACCESS_KEY_ID": "test", "S3_SECRET_ACCESS_KEY": "test", "APP_INTERNAL_URL": "http://localhost:8000", "ACCOUNTS_JSON": `[{"id":"personal"}]`} {
		t.Setenv(k, v)
	}
	if _, e := loadConfig(); e == nil {
		t.Fatal("unknown account allowed")
	}
	t.Setenv("ACCOUNT_ID", "personal")
	if _, e := loadConfig(); e != nil {
		t.Fatal(e)
	}
	t.Setenv("ACCOUNT_ID", "../personal")
	if _, e := loadConfig(); e == nil {
		t.Fatal("unsafe account allowed")
	}
}
func TestStatusCallbackAndRedirectRejection(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	calls := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		t.Error("followed callback redirect")
	}))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		calls++
		if r.URL.Path != "/internal/accounts/status" || r.Method != "POST" || r.Header.Get("Authorization") != "Bearer "+b.cfg.Token {
			t.Error("wrong status callback")
		}
		var value map[string]any
		if json.NewDecoder(r.Body).Decode(&value) != nil || value["account_id"] != "personal" || value["status"] != "awaiting_pairing" || value["error"] != nil {
			t.Error("wrong status payload")
		}
		http.Redirect(w, r, target.URL, 307)
	}))
	defer server.Close()
	b.cfg.AppURL = server.URL
	if b.post(context.Background(), "/internal/accounts/status", b.statusPayload()) == nil || calls != 1 {
		t.Fatal("redirect response accepted")
	}
}

func TestMessageAckSuccessRequiresDurableCapture(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.client = &whatsmeow.Client{}
	b.registerEvents()
	if !b.client.SynchronousAck || !b.client.EnableDecryptedEventBuffer {
		t.Fatal("durable receiver acknowledgment configuration missing")
	}
	if !b.event(sampleEvent()) {
		t.Fatal("persisted event should permit acknowledgment")
	}
	names, err := b.spool.pending()
	if err != nil || len(names) != 1 {
		t.Fatal("ack permitted without durable record")
	}
	r, err := b.spool.read(names[0])
	if err != nil || r.Message.Text != "private text" || len(r.Proto) == 0 {
		t.Fatal("ack permitted before complete durable capture")
	}
	if !b.event(sampleEvent()) {
		t.Fatal("durably captured duplicate should permit acknowledgment")
	}
	// A nonexistent spool path deterministically simulates storage unavailability,
	// including the same atomic-write failure path as exhausted PVC capacity.
	b.spool.dir = filepath.Join(t.TempDir(), "missing-spool")
	failed := sampleEvent()
	failed.Info.ID = "persist-failure"
	if b.event(failed) {
		t.Fatal("failed persistence permitted acknowledgment")
	}
	select {
	case <-b.fatal:
	default:
		t.Fatal("failed capture did not stop receiver")
	}
	if !b.event(&events.Message{Message: &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_EPHEMERAL_SETTING.Enum()}}}) {
		t.Fatal("explicitly ignored control event rejected")
	}
}
func TestHistoryHandlerRejectsFailedDurableCapture(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.client = &whatsmeow.Client{}
	b.spool.dir = filepath.Join(t.TempDir(), "missing-spool")
	history := &events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String("chat@s.whatsapp.net"), Name: proto.String("History Chat"), Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("history-failure"), FromMe: proto.Bool(false)}, MessageTimestamp: proto.Uint64(123), Message: &waE2E.Message{Conversation: proto.String("history text")}}}}}}}}
	if b.event(history) {
		t.Fatal("history capture failure reported success")
	}
}
