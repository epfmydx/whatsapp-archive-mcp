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
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func TestExplicitWrapperOrdersAndBounds(t *testing.T) {
	wrap := []func(*waE2E.Message) *waE2E.Message{
		func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{GroupMentionedMessage: &waE2E.FutureProofMessage{Message: m}}
		},
		func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: m}}
		},
		func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{ViewOnceMessageV2: &waE2E.FutureProofMessage{Message: m}}
		},
		func(m *waE2E.Message) *waE2E.Message {
			return &waE2E.Message{DocumentWithCaptionMessage: &waE2E.FutureProofMessage{Message: m}}
		},
	}
	for _, a := range wrap {
		for _, b := range wrap {
			raw := a(b(&waE2E.Message{Conversation: proto.String("fixture")}))
			e := sampleEvent()
			e.RawMessage = raw
			e.UnwrapRaw()
			r, err := makeRecord("personal", "", e)
			if err != nil || r.Message.Text != "fixture" || r.Message.MessageType != "text" {
				t.Fatal("nested order not normalized")
			}
			original, _ := proto.MarshalOptions{Deterministic: true}.Marshal(raw)
			if string(original) != string(r.RawProto) {
				t.Fatal("original wrapper not preserved")
			}
		}
	}
	m := &waE2E.Message{Conversation: proto.String("deep")}
	for i := 0; i < maxWrapperDepth; i++ {
		m = wrap[0](m)
	}
	if p := projectContent(m, "", false); p.Text != "deep" {
		t.Fatal("accepted depth rejected")
	}
	m = wrap[0](m)
	if p := projectContent(m, "", false); p.Content["reason"] != "wrapper_limit" {
		t.Fatal("missing bounded depth reason")
	}
	m = &waE2E.Message{GroupMentionedMessage: &waE2E.FutureProofMessage{}}
	if p := projectContent(m, "", false); p.Content["reason"] != "invalid_content" {
		t.Fatal("empty wrapper accepted")
	}
	m = &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{Conversation: proto.String("inside")}}, Conversation: proto.String("conflicting")}
	if p := projectContent(m, "", false); p.Content["reason"] != "invalid_content" {
		t.Fatal("conflicting wrapper content accepted")
	}
	m = &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}
	p := projectContent(m, "", false)
	if p.Content["reason"] != "unsupported_message_type" || p.Content["message_fields"].([]string)[0] != "pollUpdateMessage" {
		t.Fatal("unknown details missing")
	}
	if p := projectContent(&waE2E.Message{}, "", false); p.Content["reason"] != "empty_message" {
		t.Fatal("empty message misclassified")
	}
}

func TestHistoryDirectProtocolUpdateRouting(t *testing.T) {
	for _, kind := range []waE2E.ProtocolMessage_Type{waE2E.ProtocolMessage_MESSAGE_EDIT, waE2E.ProtocolMessage_REVOKE} {
		b := newTestBridge(t, t.TempDir())
		b.client = &whatsmeow.Client{}
		payload := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: kind.Enum(), Key: &waCommon.MessageKey{ID: proto.String("target")}, TimestampMS: proto.Int64(123000), EditedMessage: &waE2E.Message{Conversation: proto.String("changed")}}}
		h := &events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{ID: proto.String("chat@s.whatsapp.net"), Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("transport"), FromMe: proto.Bool(false)}, MessageTimestamp: proto.Uint64(123), Message: payload}}}}}}}
		if !b.event(h) {
			t.Fatal("history capture failed")
		}
		names, _ := b.spool.pending()
		if len(names) != 1 {
			t.Fatal("lost history update")
		}
		r, err := b.spool.read(names[0])
		if err != nil || r.Update == nil || r.Update.TargetID != "target" || r.TransportID != "transport" || r.Source != "history" {
			t.Fatal("history update became ordinary message")
		}
		if kind == waE2E.ProtocolMessage_MESSAGE_EDIT && (r.Update.Kind != "edit" || r.Update.Edit.Text != "changed") {
			t.Fatal("history edit content lost")
		}
	}
}

func TestUnknownRetentionBeforeAckAndAfterRestart(t *testing.T) {
	dir := t.TempDir()
	b := newTestBridge(t, dir)
	e := sampleEvent()
	e.RawMessage = &waE2E.Message{GroupMentionedMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}}}
	e.UnwrapRaw()
	b.spool.writeRetained = func(string, string, []byte) error { return errors.New("fixture") }
	if b.capture(e, "") {
		t.Fatal("retention failure acknowledged")
	}
	names, _ := b.spool.pending()
	if len(names) != 0 {
		t.Fatal("retention failure queued API delivery")
	}
	b.spool.close()
	b = newTestBridge(t, dir)
	if !b.capture(e, "") || !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "retained"))
	if len(entries) != 1 {
		t.Fatal("duplicate retention")
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "retained", entries[0].Name()))
	var retained struct {
		Raw, Normalized []byte
		Account         string `json:"account_id"`
		Chat            string `json:"chat_jid"`
		RawBytes        []byte `json:"raw_protobuf"`
		NormalizedBytes []byte `json:"normalized_protobuf"`
	}
	if json.Unmarshal(raw, &retained) != nil || retained.Account != "personal" || retained.Chat != e.Info.Chat.String() || len(retained.RawBytes) <= len(retained.NormalizedBytes) {
		t.Fatal("raw wrapper evidence missing")
	}
	for _, p := range []string{filepath.Join(dir, "retained"), filepath.Join(dir, "retained", entries[0].Name())} {
		st, _ := os.Stat(p)
		want := os.FileMode(0600)
		if st.IsDir() {
			want = 0700
		}
		if st.Mode().Perm() != want {
			t.Fatal("retention permissions")
		}
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Header().Set("X-Archive-Format", "2") }))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	b.drain(context.Background())
	names, _ = b.spool.pending()
	if len(names) != 0 {
		t.Fatal("unknown did not finish")
	}
	b.spool.close()
	b = newTestBridge(t, dir)
	if b.spool.retainedCount != 1 {
		t.Fatal("retention or process reset wrong")
	}
	if !b.capture(e, "") {
		t.Fatal("redelivery failed")
	}
	entries, _ = os.ReadDir(filepath.Join(dir, "retained"))
	if len(entries) != 1 {
		t.Fatal("restart duplicated retained payload")
	}
}

func TestUnknownEditRetained(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := sampleEvent()
	e.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("target")}, EditedMessage: &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}}}
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	if b.spool.retainedCount != 1 {
		t.Fatal("unknown edit not retained")
	}
	names, _ := b.spool.pending()
	r, _ := b.spool.read(names[0])
	if r.Update == nil || r.Update.Edit.Content["recovery"] != "operator_retained_payload" {
		t.Fatal("unknown edit metadata missing")
	}
}

func TestVideoNoteArchiveAndLane(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	data := []byte("video note fixture")
	hash := sha256.Sum256(data)
	e := sampleEvent()
	e.Message = &waE2E.Message{PtvMessage: &waE2E.VideoMessage{Mimetype: proto.String("video/mp4"), FileLength: proto.Uint64(uint64(len(data))), FileSHA256: hash[:]}}
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	names, _ := b.spool.pending()
	r, _ := b.spool.read(names[0])
	if r.Message.MessageType != "video_note" || !r.MediaPending || laneFor(r) != mediaLane {
		t.Fatal("video note not in media lane")
	}
	b.download = func(_ context.Context, a whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		if _, ok := a.(*waE2E.VideoMessage); !ok {
			t.Fatal("wrong media descriptor")
		}
		_, err := f.Write(data)
		return err
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if len(body) == 0 {
			t.Error("empty upload")
		}
		w.Header().Set("ETag", "\"fixture\"")
	}))
	defer srv.Close()
	b.cfg.S3Endpoint = srv.URL
	b.cfg.S3Bucket = "private"
	b.cfg.S3Region = "us-east-1"
	b.cfg.S3Access = "synthetic"
	b.cfg.S3Secret = "synthetic"
	var err error
	b.s3, err = newS3(b.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err = b.archive(context.Background(), &r); err != nil || r.MediaPending || r.Message.Media == nil || r.Message.Media.MIME != "video/mp4" || laneFor(r) != mediaLane {
		t.Fatal("video note archive failed", err)
	}
}

func TestRetentionFailurePreventsRemoval(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := sampleEvent()
	e.Message = &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}
	if !b.capture(e, "") {
		t.Fatal("capture failed")
	}
	names, _ := b.spool.pending()
	entries, _ := os.ReadDir(filepath.Join(b.spool.dir, "retained"))
	if len(entries) != 1 {
		t.Fatal("missing retention")
	}
	if err := os.Remove(filepath.Join(b.spool.dir, "retained", entries[0].Name())); err != nil {
		t.Fatal(err)
	}
	b.spool.writeRetained = func(string, string, []byte) error { return errors.New("fixture") }
	if b.spool.remove(names[0]) == nil {
		t.Fatal("removed unknown without retained payload")
	}
	if _, err := b.spool.read(names[0]); err != nil {
		t.Fatal("pending evidence removed")
	}
}

func TestNormalizationPreservesExistingAndNestedEditFlags(t *testing.T) {
	e := sampleEvent()
	e.IsEdit = true
	e.IsEphemeral = true
	e.RawMessage = &waE2E.Message{GroupMentionedMessage: &waE2E.FutureProofMessage{Message: e.Message}}
	normalized, reason := normalizeEvent(e)
	if reason != "" || !normalized.IsEdit || !normalized.IsEphemeral || normalized.Message.GetConversation() == "" || e.RawMessage.GetGroupMentionedMessage() == nil {
		t.Fatal("semantic flags or original envelope lost")
	}
	e = sampleEvent()
	e.RawMessage = &waE2E.Message{GroupMentionedMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: e.Message}}}}
	normalized, reason = normalizeEvent(e)
	if reason != "" || !normalized.IsEdit || e.IsEdit {
		t.Fatal("nested edit flag lost or source event mutated")
	}
	r, handled, err := makeUpdateRecord("personal", e)
	if err != nil || !handled || r.Update.Kind != "edit" || r.Update.TargetID != e.Info.ID {
		t.Fatal("nested edit not routed as update")
	}
}
