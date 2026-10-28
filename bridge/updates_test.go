package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"go.mau.fi/whatsmeow/util/gcmutil"
	"go.mau.fi/whatsmeow/util/hkdfutil"
	"google.golang.org/protobuf/proto"
)

func reactionEvent(emoji string) *events.Message {
	e := sampleEvent()
	e.Info.ID = "reaction-transport"
	e.Message = &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Key: &waCommon.MessageKey{ID: proto.String("original"), RemoteJID: proto.String(e.Info.Chat.String()), FromMe: proto.Bool(true)}, Text: &emoji, SenderTimestampMS: proto.Int64(123456)}}
	return e
}
func TestUpdateCaptureDedupAndDistinctReactions(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := reactionEvent("👍")
	if !b.capture(e, "") || !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	e.Message.GetReactionMessage().Text = proto.String("")
	if !b.capture(e, "") {
		t.Fatal("removal capture failed")
	}
	names, _ := b.spool.pending()
	if len(names) != 2 {
		t.Fatalf("got %d records", len(names))
	}
	for _, n := range names {
		r, err := b.spool.read(n)
		if err != nil {
			t.Fatal(err)
		}
		if r.Update == nil || r.Update.Kind != "reaction" || len(r.Update.EventID) != 64 || r.Update.Timestamp != time.UnixMilli(123456).UTC() || laneFor(r) != textLane || r.MediaPending {
			t.Fatal("bad update record")
		}
		if r.Update.TargetFromMe == nil || *r.Update.TargetFromMe {
			t.Fatal("actor-relative target hint wrong")
		}
	}
}
func TestEditAndDeleteCapture(t *testing.T) {
	for _, kind := range []string{"edit", "delete"} {
		t.Run(kind, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			e := sampleEvent()
			typ := waE2E.ProtocolMessage_REVOKE
			p := &waE2E.ProtocolMessage{Key: &waCommon.MessageKey{ID: proto.String("original")}, Type: &typ, TimestampMS: proto.Int64(567890)}
			if kind == "edit" {
				p.Type = waE2E.ProtocolMessage_MESSAGE_EDIT.Enum()
				p.EditedMessage = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("new caption")}}
			}
			e.RawMessage = &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{ProtocolMessage: p}}}
			e.UnwrapRaw()
			if !b.capture(e, "") {
				t.Fatal("capture failed")
			}
			names, _ := b.spool.pending()
			r, err := b.spool.read(names[0])
			if err != nil {
				t.Fatal(err)
			}
			if r.Update.Kind != kind || r.Update.TargetID != "original" || r.Update.Timestamp != time.UnixMilli(567890).UTC() || r.MediaPending {
				t.Fatal("bad protocol projection")
			}
			if kind == "edit" && (r.Update.Edit.MessageType != "image" || r.Update.Edit.Text != "new caption" || r.Message.Media != nil) {
				t.Fatal("caption edit caused media archival")
			}
		})
	}
}
func TestControlEventsIgnoredAndUpdatePersistenceBarrier(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	for _, v := range []any{&events.Receipt{}, &events.Presence{}, &events.Message{Message: &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_EPHEMERAL_SETTING.Enum()}}}} {
		if !b.event(v) {
			t.Fatal("control rejected")
		}
	}
	names, _ := b.spool.pending()
	if len(names) != 0 {
		t.Fatal("control persisted")
	}
	b.spool.writeRecord = func(string, string, []byte) error { return errors.New("fixture failure") }
	if b.capture(reactionEvent("👍"), "") {
		t.Fatal("update acknowledged before persistence")
	}
	select {
	case <-b.fatal:
	default:
		t.Fatal("capture failure did not stop receiver")
	}
}
func TestUpdateBeforeOriginalRoutingAndArchiveAck(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	ack := false
	paths := []string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if ack {
			w.Header().Set("X-Archive-Format", "2")
		}
		if r.URL.Path != "/internal/message-updates" {
			t.Error("update went to ordinary route")
		}
		var u messageUpdate
		if err := json.NewDecoder(r.Body).Decode(&u); err != nil || u.TargetID != "original" {
			t.Error("bad update payload")
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	if !b.capture(reactionEvent("👍"), "") {
		t.Fatal("capture failed")
	}
	b.drain(context.Background())
	names, _ := b.spool.pending()
	if len(names) != 1 {
		t.Fatal("old API success lost update")
	}
	r, _ := b.spool.read(names[0])
	if r.Retry.Reason != "unsupported_archive" {
		t.Fatal("missing version failure")
	}
	ack = true
	b.now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	b.drain(context.Background())
	names, _ = b.spool.pending()
	if len(names) != 0 || len(paths) != 2 {
		t.Fatal("version ACK not accepted")
	}
}
func TestLegacySpoolCompatibilityAndTypedAck(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	legacy := message{Account: "personal", Chat: "test@s.whatsapp.net", ID: "legacy", Text: "before upgrade"}
	// Match the old minimal record bytes; no newly added fields.
	raw := []byte(`{"message":{"account_id":"personal","chat_jid":"test@s.whatsapp.net","message_id":"legacy","text":"before upgrade"},"protobuf":null,"media_pending":false}`)
	if err := os.WriteFile(filepath.Join(b.spool.dir, recordID(legacy)+".json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	b.drain(context.Background())
	names, _ := b.spool.pending()
	if len(names) != 0 {
		t.Fatal("legacy record now needs format ACK")
	}
	if !b.capture(sampleEvent(), "") {
		t.Fatal("capture failed")
	}
	b.drain(context.Background())
	names, _ = b.spool.pending()
	if len(names) != 1 {
		t.Fatal("typed record lost to old API")
	}
}
func TestUpdateIdentityRejectsCrossChatAndOversizedTarget(t *testing.T) {
	for _, target := range []string{"other@s.whatsapp.net", strings.Repeat("x", 301)} {
		e := reactionEvent("👍")
		if strings.HasSuffix(target, "whatsapp.net") {
			e.Message.GetReactionMessage().Key.RemoteJID = &target
		} else {
			e.Message.GetReactionMessage().Key.ID = &target
		}
		if _, _, err := makeUpdateRecord("personal", e); err == nil {
			t.Fatal("bad update target accepted")
		}
	}
	e := reactionEvent("👍")
	e.Info.Timestamp = time.Time{}
	e.Message.GetReactionMessage().SenderTimestampMS = nil
	if _, _, err := makeUpdateRecord("personal", e); err == nil {
		t.Fatal("missing provider date accepted")
	}
}

type fixtureSecrets struct {
	secret []byte
	sender types.JID
}

func (s *fixtureSecrets) PutMessageSecrets(context.Context, []store.MessageSecretInsert) error {
	return nil
}
func (s *fixtureSecrets) PutMessageSecret(context.Context, types.JID, types.JID, types.MessageID, []byte) error {
	return nil
}
func (s *fixtureSecrets) GetMessageSecret(context.Context, types.JID, types.JID, types.MessageID) ([]byte, types.JID, error) {
	return s.secret, s.sender, nil
}
func TestEncryptedReactionDurableRetryAndLocalDecrypt(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := reactionEvent("👍")
	plainReaction := e.Message.GetReactionMessage()
	e.Info.Chat = types.NewJID("group", types.GroupServer)
	plainReaction.Key.RemoteJID = proto.String(e.Info.Chat.String())
	plainReaction.Key.Participant = proto.String(e.Info.Sender.String())
	secret := []byte(strings.Repeat("s", 32))
	outerKey := proto.Clone(plainReaction.Key).(*waCommon.MessageKey)
	plainReaction.Key = nil // Match the pinned SDK encrypted wire form.
	plain, _ := proto.Marshal(plainReaction)
	material := []byte("original" + e.Info.Sender.String() + e.Info.Sender.String() + string(whatsmeow.EncSecretReaction))
	key := hkdfutil.SHA256(secret, nil, material, 32)
	iv := []byte(strings.Repeat("i", 12))
	cipher, err := gcmutil.Encrypt(key, iv, plain, nil)
	if err != nil {
		t.Fatal(err)
	}
	e.Message = &waE2E.Message{EncReactionMessage: &waE2E.EncReactionMessage{TargetMessageKey: outerKey, EncPayload: cipher, EncIV: iv}}
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		calls++
		var u messageUpdate
		_ = json.NewDecoder(r.Body).Decode(&u)
		if u.Reaction.Emoji != "👍" || u.Timestamp != time.UnixMilli(123456).UTC() {
			t.Error("bad decrypted payload")
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	if !b.capture(e, "") {
		t.Fatal("encrypted capture failed")
	}
	b.drain(context.Background())
	names, _ := b.spool.pending()
	if len(names) != 1 || calls != 0 {
		t.Fatal("missing secret not retained")
	}
	r, _ := b.spool.read(names[0])
	if r.QuarantineReason != "" || r.Retry == nil || r.Retry.Reason != "missing_metadata" || !r.Retry.NextAttemptAt.After(r.Retry.LastAttemptAt) {
		t.Fatal("missing reaction secret must remain retryable")
	}
	eventID := r.Update.EventID
	b.client = &whatsmeow.Client{Store: &store.Device{MsgSecrets: &fixtureSecrets{secret: secret, sender: e.Info.Sender}}}
	b.now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	b.drain(context.Background())
	names, _ = b.spool.pending()
	if len(names) != 0 || calls != 1 {
		t.Fatal("local decrypt replay failed")
	}
	again, _, err := makeUpdateRecord("personal", e)
	if err != nil || again.Update.EventID != eventID {
		t.Fatal("encrypted redelivery identity unstable")
	}
}

func TestUpdateFastLaneDuringBlockedMedia(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	blocked := make(chan struct{})
	posted := make(chan struct{}, 1)
	b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		close(blocked)
		<-ctx.Done()
		return ctx.Err()
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.URL.Path != "/internal/message-updates" {
			t.Error("unexpected route")
		}
		posted <- struct{}{}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	mediaName := captureMedia(t, b, "slow-media")
	stop := startRetryLoop(t, b)
	awaitSignal(t, blocked)
	if !b.capture(reactionEvent("👍"), "") {
		t.Fatal("update capture failed")
	}
	awaitSignal(t, posted)
	awaitSpool(t, func() bool { names, _ := b.spool.pending(); return len(names) == 1 && names[0] == mediaName })
	stop()
}

func TestEditSnapshotIncludesClearValues(t *testing.T) {
	snapshot := projectContent(&waE2E.Message{Conversation: proto.String("updated")}, "test@s.whatsapp.net", true)
	data, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	_ = json.Unmarshal(data, &fields)
	for _, key := range []string{"message_type", "text", "content", "reply_to", "is_forwarded", "forwarding_score", "content_truncated"} {
		if _, ok := fields[key]; !ok {
			t.Fatalf("missing full edit field %s", key)
		}
	}
	if fields["reply_to"] != nil || fields["is_forwarded"] != false || fields["forwarding_score"] != float64(0) {
		t.Fatal("clear snapshot incorrect")
	}
}

func TestMalformedUpdatesQuarantinedWithoutStoppingReceiver(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	now := time.Unix(1800000000, 0).UTC()
	b.now = func() time.Time { return now }
	b.jitter = func(time.Duration) time.Duration { return 0 }
	cases := []*events.Message{reactionEvent(strings.Repeat("x", 65)), reactionEvent("missing"), reactionEvent("cross-chat"), reactionEvent("no-time"), reactionEvent("extreme-time")}
	cases[1].Message.GetReactionMessage().Key = nil
	cases[2].Message.GetReactionMessage().Key.RemoteJID = proto.String("other@s.whatsapp.net")
	cases[3].Info.Timestamp = time.Time{}
	cases[3].Message.GetReactionMessage().SenderTimestampMS = nil
	cases[4].Message.GetReactionMessage().SenderTimestampMS = proto.Int64(1<<63 - 1)
	originalRaw := map[string][]byte{}
	for _, e := range cases {
		if !b.event(e) || !b.event(e) {
			t.Fatal("durable malformed update was not acknowledged")
		}
		select {
		case <-b.fatal:
			t.Fatal("invalid payload stopped receiver")
		default:
		}
		r, err := makeInvalidUpdateRecord("personal", e)
		if err != nil {
			t.Fatal(err)
		}
		originalRaw[recordID(r.Message)+".json"] = r.Proto
	}
	names, _ := b.spool.pending()
	if len(names) != len(cases) {
		t.Fatal("malformed identities collided or duplicates were not deduped")
	}
	for _, name := range names {
		r, err := b.spool.read(name)
		if err != nil {
			t.Fatal(err)
		}
		if !r.InvalidUpdate || r.Update != nil || r.MediaPending || laneFor(r) != textLane || !r.Message.Timestamp.Equal(time.Unix(0, 0)) || string(r.Proto) != string(originalRaw[name]) {
			t.Fatal("unsafe quarantine record")
		}
	}
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("X-Archive-Format", "2")
		var m message
		if r.URL.Path != "/internal/messages" || json.NewDecoder(r.Body).Decode(&m) != nil || m.ID != "valid-after-malformed" {
			t.Error("quarantine reached external ingestion")
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	valid := sampleEvent()
	valid.Info.ID = "valid-after-malformed"
	if !b.event(valid) {
		t.Fatal("valid message blocked")
	}
	b.drain(context.Background())
	if calls != 1 {
		t.Fatal("valid traffic did not progress")
	}
	for _, name := range names {
		r, err := b.spool.read(name)
		if err != nil || r.Retry.Attempts != 0 || r.Retry.Stage != "update_decode" || r.Retry.Reason != "invalid_metadata" || !r.Retry.NextAttemptAt.IsZero() || r.QuarantineReason != "invalid_metadata" {
			t.Fatal("permanent update was not quarantined")
		}
	}
	health := b.spool.retryHealth(now)
	if health.Quarantined != len(cases) || health.Due != 0 || health.CoolingDown != 0 {
		t.Fatal("permanent updates remain scheduled")
	}
	b.spool.close()
	b = newTestBridge(t, dir)
	b.cfg.AppURL = srv.URL
	now = now.Add(24 * time.Hour)
	b.now = func() time.Time { return now }
	b.drain(context.Background())
	for _, name := range names {
		r, err := b.spool.read(name)
		if err != nil || !r.InvalidUpdate || r.Retry.Attempts != 0 || r.QuarantineReason != "invalid_metadata" || string(r.Proto) != string(originalRaw[name]) {
			t.Fatal("restart lost quarantine or retained payload")
		}
	}
	if calls != 1 {
		t.Fatal("quarantine attempted external ingestion after restart")
	}
	select {
	case <-b.fatal:
		t.Fatal("quarantine stopped restarted receiver")
	default:
	}
}

func TestMalformedUpdateSpoolFailureIsFatal(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.spool.writeRecord = func(string, string, []byte) error { return errors.New("fixture persistence failure") }
	if b.event(reactionEvent(strings.Repeat("x", 65))) {
		t.Fatal("failed quarantine persistence was acknowledged")
	}
	select {
	case <-b.fatal:
	default:
		t.Fatal("real spool failure did not stop receiver")
	}
	names, _ := b.spool.pending()
	if len(names) != 0 {
		t.Fatal("failed write produced a record")
	}
}

func TestEncryptedInvalidTimestampDoesNotTriggerSpoolFatal(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := reactionEvent("👍")
	e.Info.Chat = types.NewJID("group", types.GroupServer)
	target := &waCommon.MessageKey{ID: proto.String("original"), RemoteJID: proto.String(e.Info.Chat.String()), FromMe: proto.Bool(true)}
	payload := &waE2E.ReactionMessage{Text: proto.String("👍"), SenderTimestampMS: proto.Int64(1<<63 - 1)}
	plain, err := proto.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	secret := []byte(strings.Repeat("s", 32))
	material := []byte("original" + e.Info.Sender.String() + e.Info.Sender.String() + string(whatsmeow.EncSecretReaction))
	key := hkdfutil.SHA256(secret, nil, material, 32)
	iv := []byte(strings.Repeat("i", 12))
	cipher, err := gcmutil.Encrypt(key, iv, plain, nil)
	if err != nil {
		t.Fatal(err)
	}
	e.Message = &waE2E.Message{EncReactionMessage: &waE2E.EncReactionMessage{TargetMessageKey: target, EncPayload: cipher, EncIV: iv}}
	b.client = &whatsmeow.Client{Store: &store.Device{MsgSecrets: &fixtureSecrets{secret: secret, sender: e.Info.Sender}}}
	if !b.capture(e, "") {
		t.Fatal("encrypted event not captured")
	}
	b.drain(context.Background())
	select {
	case <-b.fatal:
		t.Fatal("invalid decrypted timestamp caused global fatal")
	default:
	}
	names, _ := b.spool.pending()
	if len(names) != 1 {
		t.Fatal("malformed encrypted event lost")
	}
	r, err := b.spool.read(names[0])
	if err != nil || !r.EncryptedReaction || r.Retry.Stage != "update_decode" || r.Retry.Reason != "invalid_metadata" || r.QuarantineReason != "invalid_metadata" || !r.Retry.NextAttemptAt.IsZero() {
		t.Fatal("malformed decrypted event not retained for repair")
	}
	before, _ := json.Marshal(r)
	b.now = func() time.Time { return r.Retry.LastFailureAt.Add(24 * time.Hour) }
	b.drain(context.Background())
	after, err := b.spool.read(names[0])
	encoded, _ := json.Marshal(after)
	if err != nil || string(before) != string(encoded) {
		t.Fatal("permanent encrypted reaction was attempted again")
	}
}

func TestLegacyInvalidUpdateQuarantinedWithoutResettingAttempts(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	now := fixedRetryClock(b)
	e := reactionEvent("invalid")
	e.Message.GetReactionMessage().Key = nil
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	names, _ := b.spool.pending()
	r, err := b.spool.read(names[0])
	if err != nil {
		t.Fatal(err)
	}
	r.Retry = &retryState{Attempts: 14, Stage: "update_decode", Reason: "invalid_metadata", NextAttemptAt: now.Add(time.Hour)}
	if err := b.spool.save(r, false); err != nil {
		t.Fatal(err)
	}
	original, raw := append([]byte(nil), r.Proto...), append([]byte(nil), r.RawProto...)
	b.drain(context.Background())
	got, err := b.spool.read(names[0])
	if err != nil || got.Retry.Attempts != 14 || got.QuarantineReason != "invalid_metadata" || !got.Retry.NextAttemptAt.IsZero() || !bytes.Equal(got.Proto, original) || !bytes.Equal(got.RawProto, raw) {
		t.Fatal("legacy quarantine changed attempts or payloads")
	}
}

func TestInvalidUpdateQuarantinePersistenceFailureStopsBridge(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := reactionEvent("invalid")
	e.Message.GetReactionMessage().Key = nil
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	// Save the correlation ID first so the injected failure exercises quarantine.
	names, _ := b.spool.pending()
	r, _ := b.spool.read(names[0])
	r.ItemID = opaqueID()
	if err := b.spool.save(r, false); err != nil {
		t.Fatal(err)
	}
	b.spool.writeRecord = func(string, string, []byte) error { return errors.New("fixture persistence failure") }
	b.drain(context.Background())
	select {
	case <-b.fatal:
	default:
		t.Fatal("failed quarantine did not stop further work")
	}
	got, err := b.spool.read(names[0])
	if err != nil || !bytes.Equal(got.Proto, r.Proto) || got.QuarantineReason != "" {
		t.Fatal("failed quarantine altered durable record")
	}
}
