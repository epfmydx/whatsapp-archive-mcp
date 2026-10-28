package main

import (
	"bytes"
	"context"
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
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"go.mau.fi/whatsmeow/util/gcmutil"
	"go.mau.fi/whatsmeow/util/hkdfutil"
	"google.golang.org/protobuf/proto"
)

func TestRequeueRejectsOversizedWhitespaceAfterValidJSON(t *testing.T) {
	b := &bridge{cfg: config{Token: strings.Repeat("t", 32)}}
	body := `{"item_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","request_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}` + strings.Repeat(" ", 2048)
	req := httptest.NewRequest(http.MethodPost, "/media/requeue", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	w := httptest.NewRecorder()
	b.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("oversized body accepted: %d", w.Code)
	}
}

func TestRecoveryCallbackNeverWaitsForWorkerPersistenceLock(t *testing.T) {
	b := &bridge{}
	b.recoveryMu.Lock()
	defer b.recoveryMu.Unlock()
	done := make(chan struct{})
	go func() { b.queueMediaRetry(&events.MediaRetry{}); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("SDK callback blocked behind worker persistence")
	}
}

func recoveryFixture(t *testing.T, b *bridge, id string) string {
	t.Helper()
	b.cfg.MediaRecoveryEnabled = true
	e := sampleEvent()
	e.Info.ID = id
	e.Message = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{MediaKey: bytes.Repeat([]byte{7}, 32), FileLength: proto.Uint64(4), DirectPath: proto.String("/v/old?ext=1&signed=KEEP%2BME")}}
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	r, _ := makeRecord(b.cfg.Account, "", e)
	name := recordID(r.Message) + ".json"
	r, err := b.spool.read(name)
	if err != nil {
		t.Fatal(err)
	}
	// Reproduce legacy exhausted state without inventing capture provenance.
	r.CapturedAt = time.Time{}
	r.Source = ""
	r.Retry = &retryState{Attempts: 14, Stage: "media_download", Reason: "http_status", HTTPStatus: 403, LastFailureAt: b.nowUTC(), NextAttemptAt: b.nowUTC().Add(time.Hour)}
	if err := b.spool.save(r, false); err != nil {
		t.Fatal(err)
	}
	return name
}
func encryptedRecovery(t *testing.T, r record, at time.Time, path string) *events.MediaRetry {
	t.Helper()
	return encryptedRecoveryStanza(t, r, at, path, r.Message.ID)
}
func encryptedRecoveryStanza(t *testing.T, r record, at time.Time, path, stanza string) *events.MediaRetry {
	t.Helper()
	n := &waMmsRetry.MediaRetryNotification{StanzaID: proto.String(stanza), Result: waMmsRetry.MediaRetryNotification_SUCCESS.Enum(), DirectPath: proto.String(path)}
	return encryptedRecoveryNotification(t, r, at, n)
}
func encryptedRecoveryNotification(t *testing.T, r record, at time.Time, n *waMmsRetry.MediaRetryNotification) *events.MediaRetry {
	t.Helper()
	plaintext, err := proto.Marshal(n)
	if err != nil {
		t.Fatal(err)
	}
	iv := bytes.Repeat([]byte{3}, 12)
	key := hkdfutil.SHA256(r.Recovery.MediaKey, nil, []byte("WhatsApp Media Retry Notification"), 32)
	ciphertext, err := gcmutil.Encrypt(key, iv, plaintext, []byte(r.Message.ID))
	if err != nil {
		t.Fatal(err)
	}
	info, err := mediaInfo(r.Message)
	if err != nil {
		t.Fatal(err)
	}
	e := &events.MediaRetry{MessageID: r.Message.ID, ChatID: info.Chat, FromMe: r.Message.FromMe, Timestamp: at, IV: iv, Ciphertext: ciphertext}
	if info.IsGroup {
		e.SenderID = info.Sender
	}
	return e
}
func readRecovery(t *testing.T, b *bridge, name string) record {
	t.Helper()
	r, err := b.spool.read(name)
	if err != nil {
		t.Fatal(err)
	}
	return r
}
func TestRecoveryLegacyReservationPersistsAndTimesOutWithoutReplay(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "legacy")
	sends := 0
	b.sendMediaRetry = func(_ context.Context, info *types.MessageInfo, key []byte) error {
		sends++
		r := readRecovery(t, b, name)
		if r.Recovery == nil || r.Recovery.State != "awaiting" || !opaquePattern.MatchString(r.ItemID) || len(key) != 32 || info.ID != "legacy" {
			t.Fatal("receipt before durable reservation")
		}
		return errors.New("secret error")
	}
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	if sends != 1 || r.Recovery.Attempts != 1 || r.MediaAttempts != 14 || !r.CapturedAt.IsZero() || r.Source != "" {
		t.Fatal("legacy recovery lost budget or provenance")
	}
	h := b.spool.retryHealth(*now)
	if h.AwaitingMediaRetry != 1 || h.Due != 0 || h.CoolingDown != 0 || h.Failed != 1 {
		t.Fatalf("bad waiting health %#v", h)
	}
	for i := 0; i < 4; i++ {
		b.drain(context.Background())
	}
	if sends != 1 {
		t.Fatal("receipt replayed")
	}
	b.spool.close()
	restarted := newTestBridge(t, dir)
	restarted.cfg.MediaRecoveryEnabled = true
	restarted.now = b.now
	restarted.diagnosticWriter = io.Discard
	restarted.sendMediaRetry = b.sendMediaRetry
	restarted.drain(context.Background())
	if sends != 1 {
		t.Fatal("restart replayed receipt")
	}
	*now = now.Add(121 * time.Second)
	restarted.drain(context.Background())
	r = readRecovery(t, restarted, name)
	if r.QuarantineReason != "recovery_timeout" || r.Recovery.State != "failed" {
		t.Fatal("timeout did not quarantine")
	}
	h = restarted.spool.retryHealth(*now)
	if h.Quarantined != 1 || h.Due != 0 || h.CoolingDown != 0 || h.AwaitingMediaRetry != 0 {
		t.Fatal("quarantine still eligible for network")
	}
}
func TestRecoveryResponsePersistsPathAndOriginalIntegrity(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "success")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	oldRaw := append([]byte(nil), r.RawProto...)
	oldProto := append([]byte(nil), r.Proto...)
	path := "/v/t62.7118-24/new?ext=42&signed=KEEP%2BME&oh=a%2Fb"
	e := encryptedRecovery(t, r, *now, path)
	b.queueMediaRetry(e)
	b.applyRecoveryResponses()
	got := readRecovery(t, b, name)
	p, a, err := recoveryAttachment(got)
	if err != nil || a.GetDirectPath() != path || !bytes.Equal(got.RawProto, oldRaw) || bytes.Equal(got.Proto, oldProto) || got.Recovery.State != "refreshed" || got.MediaAttempts != 0 {
		t.Fatal("refreshed metadata not durably preserved")
	}
	before := proto.Clone(p)
	setRecoveryPath(before.(*waE2E.Message), "/v/old?ext=1&signed=KEEP%2BME")
	restored, _ := proto.Marshal(before)
	if !bytes.Equal(restored, oldProto) {
		t.Fatal("refresh changed key/hash metadata")
	}
	b.queueMediaRetry(e)
	b.applyRecoveryResponses()
	if readRecovery(t, b, name).Recovery.State != "refreshed" {
		t.Fatal("duplicate changed success")
	}
	downloads := 0
	b.download = func(_ context.Context, m whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		downloads++
		if m.GetDirectPath() != path {
			t.Fatal("downloader saw old path")
		}
		return whatsmeow.ErrMediaDownloadFailedWith404
	}
	b.drain(context.Background())
	if downloads != 1 {
		t.Fatal("did not perform bounded refreshed download")
	}
	b.drain(context.Background())
	if readRecovery(t, b, name).QuarantineReason != "recovery_exhausted" {
		t.Fatal("repeated recovery exceeded budget")
	}
}
func TestRecoveryCallbackBindingAndDeadline(t *testing.T) {
	for _, scenario := range []string{"chat", "message", "from_me", "sender", "timestamp", "late", "wrong_key", "changed_key", "stanza", "url"} {
		t.Run(scenario, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			now := fixedRetryClock(b)
			name := recoveryFixture(t, b, "binding")
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
			b.drain(context.Background())
			r := readRecovery(t, b, name)
			path := "/v/new?ext=1"
			if scenario == "url" {
				path = "https://attacker.example/private"
			}
			e := encryptedRecovery(t, r, *now, path)
			if scenario == "stanza" {
				e = encryptedRecoveryStanza(t, r, *now, path, "other")
			}
			switch scenario {
			case "chat":
				e.ChatID = types.NewJID("other", types.DefaultUserServer)
			case "message":
				e.MessageID = "other"
			case "from_me":
				e.FromMe = !e.FromMe
			case "sender":
				e.SenderID = types.NewJID("other", types.DefaultUserServer)
			case "timestamp":
				e.Timestamp = now.Add(-time.Minute)
			case "late":
				*now = now.Add(121 * time.Second)
			case "wrong_key":
				e.Ciphertext[0] ^= 1
			case "changed_key":
				p, _, _ := recoveryAttachment(r)
				p.ImageMessage.MediaKey = bytes.Repeat([]byte{9}, 32)
				r.Proto, _ = proto.Marshal(p)
				b.spool.save(r, false)
			}
			b.queueMediaRetry(e)
			b.applyRecoveryResponses()
			got := readRecovery(t, b, name)
			if got.Recovery.State == "refreshed" {
				t.Fatal("unbound callback accepted")
			}
			if scenario == "url" && got.QuarantineReason != "recovery_invalid_path" {
				t.Fatal("unsafe path not reviewable")
			}
		})
	}
}
func TestRecoveryResponseReceivedOnTimeSurvivesBusyWorker(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "busy")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	*now = now.Add(119 * time.Second)
	b.queueMediaRetry(encryptedRecovery(t, r, *now, "/v/new?ext=7"))
	*now = now.Add(time.Hour)
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return context.DeadlineExceeded
	}
	b.drain(context.Background())
	if readRecovery(t, b, name).Recovery.State == "failed" {
		t.Fatal("on-time queued response discarded after worker delay")
	}
}
func TestRecoveryGlobalThrottleDurableAndFailureStopsIO(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	recoveryFixture(t, b, "one")
	recoveryFixture(t, b, "two")
	sends := 0
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { sends++; return nil }
	b.drain(context.Background())
	if sends != 1 {
		t.Fatal("backlog receipt storm")
	}
	b.drain(context.Background())
	if sends != 1 {
		t.Fatal("global interval ignored")
	}
	*now = now.Add(30 * time.Second)
	b.drain(context.Background())
	if sends != 2 {
		t.Fatal("global throttle blocked next item")
	}
	b2 := newTestBridge(t, t.TempDir())
	fixedRetryClock(b2)
	name := recoveryFixture(t, b2, "fsync")
	b2.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error {
		t.Fatal("receipt after failed reservation")
		return nil
	}
	b2.spool.writeRecord = func(string, string, []byte) error { return errors.New("fsync failure") }
	b2.drain(context.Background())
	if !b2.stopped(context.Background()) {
		t.Fatal("persistence failure did not stop receiver")
	}
	if readRecovery(t, b2, name).Recovery != nil {
		t.Fatal("failed reservation changed durable state")
	}
}
func TestRecoveryNoKeyUnavailableAndMissingPath(t *testing.T) {
	for _, scenario := range []string{"no_key", "unavailable", "missing_path", "404", "410"} {
		t.Run(scenario, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			now := fixedRetryClock(b)
			name := recoveryFixture(t, b, scenario)
			r := readRecovery(t, b, name)
			if scenario == "no_key" {
				p, _, _ := recoveryAttachment(r)
				p.ImageMessage.MediaKey = nil
				r.Proto, _ = proto.Marshal(p)
				b.spool.save(r, false)
			}
			if scenario == "missing_path" {
				r.Retry.Reason = "missing_url"
				r.Retry.HTTPStatus = 0
				b.spool.save(r, false)
			}
			if scenario == "404" {
				r.Retry.HTTPStatus = 404
				b.spool.save(r, false)
			}
			if scenario == "410" {
				r.Retry.HTTPStatus = 410
				b.spool.save(r, false)
			}
			sends := 0
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { sends++; return nil }
			b.drain(context.Background())
			r = readRecovery(t, b, name)
			if scenario == "no_key" {
				if sends != 0 || r.QuarantineReason != "recovery_no_key" {
					t.Fatal("missing key not bounded")
				}
				return
			}
			if sends != 1 || r.Recovery.State != "awaiting" {
				t.Fatal("recoverable failure did not reserve")
			}
			if scenario == "unavailable" {
				info, _ := mediaInfo(r.Message)
				b.queueMediaRetry(&events.MediaRetry{MessageID: r.Message.ID, ChatID: info.Chat, FromMe: r.Message.FromMe, Timestamp: *now, Error: &events.MediaRetryError{Code: 2}})
				b.applyRecoveryResponses()
				if readRecovery(t, b, name).QuarantineReason != "recovery_unavailable" {
					t.Fatal("phone unavailable not reviewable")
				}
			}
		})
	}
}
func TestRequeueIsAuthenticatedBoundedIdempotentAndPreservesData(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	name := recoveryFixture(t, b, "manual")
	r := readRecovery(t, b, name)
	b.quarantine(&r, "download_exhausted")
	r = readRecovery(t, b, name)
	old := append([]byte(nil), r.Proto...)
	requestID := opaqueID()
	body, _ := json.Marshal(map[string]string{"item_id": r.ItemID, "request_id": requestID})
	request := func(data []byte, authorized bool) int {
		req := httptest.NewRequest("POST", "/media/requeue", bytes.NewReader(data))
		if authorized {
			req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
		}
		w := httptest.NewRecorder()
		b.ServeHTTP(w, req)
		return w.Code
	}
	if request(body, false) != 401 {
		t.Fatal("unauthenticated requeue")
	}
	for _, bad := range []string{string(body) + " {}", `{"item_id":"../../secret","request_id":"` + requestID + `"}`, `{"item_id":"` + r.ItemID + `","request_id":"` + requestID + `","path":"private"}`, strings.Repeat(" ", 1025) + string(body)} {
		if request([]byte(bad), true) != 400 {
			t.Fatal("unbounded or ambiguous input accepted")
		}
	}
	if request(body, true) != 204 || request(body, true) != 204 {
		t.Fatal("manual requeue failed")
	}
	got := readRecovery(t, b, name)
	if got.QuarantineReason != "" || got.MediaAttempts != 0 || got.Recovery != nil || got.Requeue.Count != 1 || len(got.Requeue.RequestIDs) != 1 || !bytes.Equal(got.Proto, old) || !got.CapturedAt.IsZero() {
		t.Fatal("requeue changed content/provenance or repeated audit")
	}
	if request([]byte(`{"item_id":"`+got.ItemID+`","request_id":"`+opaqueID()+`"}`), true) != 409 {
		t.Fatal("nonquarantined item requeued")
	}
}
func TestRecoveryPathAndExpiryDiagnosticsPreserveSignedQuery(t *testing.T) {
	path := "/v/t62/file?ext=17&oh=SECRET%2Bsigned%2Fquery"
	a := &waE2E.ImageMessage{DirectPath: proto.String(path)}
	if mediaURLExpiry(a, time.Unix(1, 0)) != "present" || a.GetDirectPath() != path || !validRecoveryPath(path) {
		t.Fatal("signed query changed")
	}
	for _, path := range []string{"https://example/private", "//example/private", "/v/../private", "/v/%2e%2e/private", "/v/%0a/private", "/v/a#secret", "/mms/a\\secret"} {
		if validRecoveryPath(path) {
			t.Fatal("unsafe path accepted")
		}
	}
	var out bytes.Buffer
	b := &bridge{diagnosticWriter: &out}
	b.emitDiagnostic(diagnostic{Event: "media_download", ItemID: "privatejid", AttemptID: "secretfilename", URLExpiry: path})
	if strings.Contains(out.String(), "SECRET") || strings.Contains(out.String(), "privatejid") || strings.Contains(out.String(), "secretfilename") {
		t.Fatal("unsafe correlation logged")
	}
}
func TestMediaBudgetQuarantinesButCacheUploadFailuresContinue(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	b.cfg.MediaDownloadMaxAttempts = 2
	name := captureMedia(t, b, "budget")
	b.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		return whatsmeow.ErrInvalidMediaHMAC
	}
	b.drain(context.Background())
	*now = now.Add(2 * time.Minute)
	b.drain(context.Background())
	*now = now.Add(3 * time.Minute)
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	if r.QuarantineReason != "download_exhausted" || r.MediaAttempts != 2 {
		t.Fatal("download budget not bounded")
	}
	// A verified cache routes around download budgets. S3 is synthetic and denied.
	b2 := newTestBridge(t, t.TempDir())
	fixedRetryClock(b2)
	name2 := captureMedia(t, b2, "cached")
	r = readRecovery(t, b2, name2)
	r.MediaAttempts = 100
	r.Retry = &retryState{Attempts: 100}
	b2.spool.save(r, false)
	if err := os.WriteFile(filepath.Join(b2.spool.dir, recordID(r.Message)+".media"), []byte("data"), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) { w.WriteHeader(403) }))
	defer server.Close()
	b2.cfg.S3Endpoint = server.URL
	b2.cfg.S3Access = "test"
	b2.cfg.S3Secret = "testsecret"
	b2.cfg.S3Region = "us-east-1"
	b2.cfg.S3Bucket = "test"
	b2.s3, _ = newS3(b2.cfg)
	b2.download = func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error {
		t.Fatal("cached item downloaded")
		return nil
	}
	b2.drain(context.Background())
	r = readRecovery(t, b2, name2)
	if r.QuarantineReason != "" || r.Retry.Stage != "s3_upload" {
		t.Fatal("S3 failure consumed download budget")
	}
}
func TestRecoveryGroupSenderAndOtherAccountCannotClaimResponse(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "group")
	r := readRecovery(t, b, name)
	// Rebind the synthetic fixture through a new capture rather than rewriting a
	// hashed filename: the real queue always keeps account/chat identity immutable.
	event := sampleEvent()
	event.Info.ID = "group-exact"
	event.Info.Chat = types.NewJID("group", types.GroupServer)
	event.Info.IsGroup = true
	var p waE2E.Message
	if proto.Unmarshal(r.Proto, &p) != nil {
		t.Fatal("bad fixture")
	}
	event.Message = &p
	b.capture(event, "")
	group, _ := makeRecord(b.cfg.Account, "", event)
	groupName := recordID(group.Message) + ".json"
	group = readRecovery(t, b, groupName)
	group.Retry = r.Retry
	b.spool.save(group, false)
	// Only the group fixture needs a reservation for this assertion.
	b.quarantine(&r, "download_exhausted")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	group = readRecovery(t, b, groupName)
	wrong := encryptedRecovery(t, group, *now, "/v/new")
	wrong.SenderID = types.NewJID("wrong", types.DefaultUserServer)
	b.queueMediaRetry(wrong)
	b.applyRecoveryResponses()
	if readRecovery(t, b, groupName).Recovery.State != "awaiting" {
		t.Fatal("wrong group participant accepted")
	}
	other := newTestBridge(t, t.TempDir())
	fixedRetryClock(other)
	other.queueMediaRetry(encryptedRecovery(t, group, *now, "/v/new"))
	other.applyRecoveryResponses()
	if h := other.spool.retryHealth(*now); h.AwaitingMediaRetry != 0 {
		t.Fatal("cross-account callback created a request")
	}
	b.queueMediaRetry(encryptedRecovery(t, group, *now, "/v/new"))
	b.applyRecoveryResponses()
	if readRecovery(t, b, groupName).Recovery.State != "refreshed" {
		t.Fatal("exact group participant rejected")
	}
}
func TestURLExpiryHexIsDiagnosticOnly(t *testing.T) {
	now := time.Unix(0x100, 0)
	for _, tc := range []struct{ path, want string }{{"/v/a?oe=FF", "past"}, {"/v/a?oe=0100", "past"}, {"/v/a?oe=101", "future"}, {"/v/a?oe=FFFF&ext=1", "future"}, {"/v/a?oe=invalid", "unknown"}, {"/v/a?oe=1&oe=2", "unknown"}, {"/v/a?oe=", "unknown"}, {"/v/a?signed=secret", "absent"}, {"/v/a?expires=secret", "present"}} {
		a := &waE2E.ImageMessage{DirectPath: proto.String(tc.path)}
		if got := mediaURLExpiry(a, now); got != tc.want || a.GetDirectPath() != tc.path {
			t.Fatalf("expiry state %s want %s", got, tc.want)
		}
	}
	for _, path := range []string{"/v/a?token=%0aX", "/v/%2f%2fhost/a", "/v/a?token=%7f"} {
		if validRecoveryPath(path) {
			t.Fatal("encoded unsafe path/query accepted")
		}
	}
}
func TestRequeueBusyWorkerReturnsImmediately(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.drainMu[mediaLane].Lock()
	defer b.drainMu[mediaLane].Unlock()
	req := httptest.NewRequest("POST", "/media/requeue", strings.NewReader(`{"item_id":"`+opaqueID()+`","request_id":"`+opaqueID()+`"}`))
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	w := httptest.NewRecorder()
	b.ServeHTTP(w, req)
	if w.Code != 503 || w.Header().Get("Retry-After") == "" {
		t.Fatal("busy worker did not return bounded retry response")
	}
}
func TestRecoveryAcceptsMissingOptionalStanzaAndIgnoresOldProtocolFlag(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "optionalstanza")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	b.queueMediaRetry(encryptedRecoveryStanza(t, r, *now, "/v/new", ""))
	b.applyRecoveryResponses()
	if readRecovery(t, b, name).Recovery.State != "refreshed" {
		t.Fatal("optional stanza rejected")
	}
	text := sampleEvent()
	text.Info.ID = "oldflag"
	b.capture(text, "")
	record, _ := makeRecord(b.cfg.Account, "", text)
	textName := recordID(record.Message) + ".json"
	record = readRecovery(t, b, textName)
	record.ProtocolOnly = true
	b.spool.save(record, false)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls++
		w.Header().Set("X-Archive-Format", "2")
		w.WriteHeader(200)
	}))
	defer server.Close()
	b.cfg.AppURL = server.URL
	b.drainLane(context.Background(), textLane)
	if calls != 1 {
		t.Fatal("persisted flag removed real content without reclassification")
	}
}
func TestRecoveryClockMissingTimestampFailsClosed(t *testing.T) {
	for _, raw := range []string{`{}`, `{"next_at":"0001-01-01T00:00:00Z"}`, `invalid`} {
		t.Run(raw, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			fixedRetryClock(b)
			recoveryFixture(t, b, "clock")
			if err := os.WriteFile(filepath.Join(b.spool.dir, "media-recovery-clock.json"), []byte(raw), 0600); err != nil {
				t.Fatal(err)
			}
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error {
				t.Fatal("receipt after corrupt global state")
				return nil
			}
			b.drain(context.Background())
			if !b.stopped(context.Background()) {
				t.Fatal("invalid throttle state did not stop unsafe I/O")
			}
		})
	}
}
func TestProtocolOnlyCaptureAndLegacyDrainRetainWithoutMessageEvents(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls++
		if req.URL.Path != "/internal/messages" {
			t.Fatal("unexpected ingestion route")
		}
		w.Header().Set("X-Archive-Format", "2")
		w.WriteHeader(200)
	}))
	defer server.Close()
	b.cfg.AppURL = server.URL
	control := validProtocolOnlyMessage(t)
	e := sampleEvent()
	e.Info.ID = "wrapped-control"
	e.Message = &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: control}}
	e.RawMessage = e.Message
	if !b.capture(e, "") {
		t.Fatal("control capture failed")
	}
	r, _ := makeRecord(b.cfg.Account, "", e)
	name := recordID(r.Message) + ".json"
	queued := readRecovery(t, b, name)
	if !queued.ProtocolOnly || len(queued.RawProto) == 0 {
		t.Fatal("raw control not durably queued")
	}
	// The legacy record lacks the persisted flag. Drain must classify its payload.
	legacy := sampleEvent()
	legacy.Info.ID = "legacy-control"
	legacy.Message = proto.Clone(control).(*waE2E.Message)
	legacyRecord, _ := makeRecord(b.cfg.Account, "", legacy)
	if err := b.spool.save(legacyRecord, true); err != nil {
		t.Fatal(err)
	}
	mixed := sampleEvent()
	mixed.Info.ID = "mixed-control"
	mixed.Message = proto.Clone(control).(*waE2E.Message)
	mixed.Message.Conversation = proto.String("synthetic human content")
	if !b.capture(mixed, "") {
		t.Fatal("mixed capture failed")
	}
	b.drain(context.Background())
	if calls != 1 {
		t.Fatalf("controls generated normal message events: %d calls", calls)
	}
	names, _ := b.spool.pending()
	if len(names) != 0 {
		t.Fatal("classified controls still in network queue")
	}
	retained, err := os.ReadDir(filepath.Join(b.spool.dir, "retained"))
	if err != nil || len(retained) < 2 {
		t.Fatal("control raw protobuf not privately retained")
	}
}
func TestRecoveryResponseReceivedDuringSweepWinsBeforeTimeout(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "waiting")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	waiting := readRecovery(t, b, name)
	// Ensure a synthetic slow item sorts before the waiting record, so its
	// downloader can queue the phone response after sweep-start consumption.
	var slowName string
	for n := 0; n < 1000; n++ {
		e := sampleEvent()
		e.Info.ID = string(rune('a' + n))
		e.Message = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{FileLength: proto.Uint64(4)}}
		r, _ := makeRecord(b.cfg.Account, "", e)
		candidate := recordID(r.Message) + ".json"
		if candidate < name {
			if !b.capture(e, "") {
				t.Fatal("capture failed")
			}
			slowName = candidate
			break
		}
	}
	if slowName == "" {
		t.Fatal("cannot order synthetic fixture")
	}
	downloads := 0
	b.download = func(_ context.Context, m whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		downloads++
		if m.GetDirectPath() == "" {
			*now = waiting.Recovery.Deadline.Add(-time.Second)
			b.queueMediaRetry(encryptedRecovery(t, waiting, *now, "/v/refreshed"))
			*now = waiting.Recovery.Deadline.Add(time.Second)
		}
		return whatsmeow.ErrInvalidMediaHMAC
	}
	b.drainLane(context.Background(), mediaLane)
	r := readRecovery(t, b, name)
	if r.Recovery.State != "used" || r.QuarantineReason != "" || downloads != 2 {
		t.Fatal("during-sweep on-time response lost to timeout or stale record")
	}
}
func TestGlobalRecoveryThrottleAvoidsRedundantPersistence(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	recoveryFixture(t, b, "first")
	recoveryFixture(t, b, "second")
	sends := 0
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { sends++; return nil }
	b.drain(context.Background())
	writes := 0
	b.spool.writeRecord = func(dir, name string, data []byte) error { writes++; return atomicWrite(dir, name, data) }
	for n := 0; n < 3; n++ {
		b.drain(context.Background())
	}
	if sends != 1 || writes != 0 {
		t.Fatalf("throttled pending items rewrote unchanged deadlines: %d writes", writes)
	}
}
func TestRecoveryRejectsProviderTimestampBeyondReservationDeadline(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "future-response")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	// Local receipt is on time, but a far-future provider timestamp cannot bind
	// this response to the reservation. Provider timestamps use whole seconds.
	invalid := encryptedRecovery(t, r, r.Recovery.Deadline.Truncate(time.Second).Add(time.Second), "/v/future")
	b.queueMediaRetry(invalid)
	b.applyRecoveryResponses()
	if readRecovery(t, b, name).Recovery.State != "awaiting" {
		t.Fatal("future provider timestamp accepted")
	}
	valid := encryptedRecovery(t, r, r.Recovery.Deadline.Truncate(time.Second), "/v/deadline-second")
	if !retryResponseMatches(r, recoveryResponse{Event: valid, ReceivedAt: *now}) {
		t.Fatal("provider timestamp in final deadline second rejected")
	}
	b.queueMediaRetry(valid)
	b.applyRecoveryResponses()
	if readRecovery(t, b, name).Recovery.State != "refreshed" {
		t.Fatal("valid provider timestamp rejected")
	}
}
func TestRecoveryCallbackOverflowPreservesAwaitingUntilReviewTimeout(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	name := recoveryFixture(t, b, "overflow")
	sends := 0
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { sends++; return nil }
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	invalid := encryptedRecovery(t, r, *now, "/v/not-authenticated")
	invalid.Ciphertext[0] ^= 1
	for n := 0; n < 64; n++ {
		b.queueMediaRetry(invalid)
	}
	b.queueMediaRetry(encryptedRecovery(t, r, *now, "/v/overflow-dropped"))
	if len(b.recoveryEvents) != 64 || cap(b.recoveryEvents) != 64 {
		t.Fatal("callback inbox exceeded its fixed bound")
	}
	before := readRecovery(t, b, name)
	if before.Recovery.State != "awaiting" || before.QuarantineReason != "" || !bytes.Equal(before.Proto, r.Proto) {
		t.Fatal("overflow changed durable reservation")
	}
	b.applyRecoveryResponses()
	after := readRecovery(t, b, name)
	if len(b.recoveryEvents) != 0 || after.Recovery.State != "awaiting" || after.QuarantineReason != "" {
		t.Fatal("unverified queued callbacks fabricated recovery success")
	}
	*now = r.Recovery.Deadline.Add(time.Second)
	b.drain(context.Background())
	after = readRecovery(t, b, name)
	if sends != 1 || after.QuarantineReason != "recovery_timeout" || !bytes.Equal(after.Proto, r.Proto) || !bytes.Equal(after.RawProto, r.RawProto) {
		t.Fatal("overflow did not preserve payloads for timeout review")
	}
}
func TestRequeueCycleCapAndIdempotenceSurviveSpoolRestart(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	fixedRetryClock(b)
	name := recoveryFixture(t, b, "manual-cap")
	r := readRecovery(t, b, name)
	r.Requeue = &requeueAudit{Count: 63, LastAt: b.nowUTC()}
	for n := 0; n < 63; n++ {
		r.Requeue.RequestIDs = append(r.Requeue.RequestIDs, opaqueID())
	}
	if !b.quarantine(&r, "download_exhausted") {
		t.Fatal("quarantine failed")
	}
	request := func(current *bridge, id string) int {
		body, _ := json.Marshal(map[string]string{"item_id": r.ItemID, "request_id": id})
		req := httptest.NewRequest("POST", "/media/requeue", bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+current.cfg.Token)
		w := httptest.NewRecorder()
		current.ServeHTTP(w, req)
		return w.Code
	}
	finalID := opaqueID()
	if request(b, finalID) != 204 {
		t.Fatal("final allowed manual cycle rejected")
	}
	r = readRecovery(t, b, name)
	if r.Requeue.Count != 64 || len(r.Requeue.RequestIDs) != 64 {
		t.Fatal("manual cycle audit not persisted")
	}
	if !b.quarantine(&r, "recovery_timeout") {
		t.Fatal("second quarantine failed")
	}
	before := readRecovery(t, b, name)
	b.spool.close()
	restarted := newTestBridge(t, dir)
	fixedRetryClock(restarted)
	for _, id := range []string{finalID, r.Requeue.RequestIDs[0]} {
		if request(restarted, id) != 204 {
			t.Fatal("persisted idempotency key rejected after restart")
		}
	}
	if request(restarted, opaqueID()) != 409 {
		t.Fatal("manual cycle cap exceeded after restart")
	}
	after := readRecovery(t, restarted, name)
	if after.Requeue.Count != 64 || len(after.Requeue.RequestIDs) != 64 || after.QuarantineReason != "recovery_timeout" || after.AttemptID != before.AttemptID || !after.Requeue.LastAt.Equal(before.Requeue.LastAt) || !bytes.Equal(after.Proto, before.Proto) || !bytes.Equal(after.RawProto, before.RawProto) {
		t.Fatal("duplicate or capped request changed durable state")
	}
}
