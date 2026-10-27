package main

import (
	"encoding/json"
	"math"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestTypedContentProjection(t *testing.T) {
	for _, tc := range []struct {
		name, kind string
		m          *waE2E.Message
		key        string
		value      any
	}{
		{"text", "text", &waE2E.Message{Conversation: proto.String("hi")}, "", ""},
		{"image", "image", &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("caption")}}, "", ""},
		{"video", "video", &waE2E.Message{VideoMessage: &waE2E.VideoMessage{}}, "", ""},
		{"audio", "audio", &waE2E.Message{AudioMessage: &waE2E.AudioMessage{}}, "", ""},
		{"document", "document", &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{}}, "", ""},
		{"sticker", "sticker", &waE2E.Message{StickerMessage: &waE2E.StickerMessage{}}, "", ""},
		{"contact", "contact", &waE2E.Message{ContactMessage: &waE2E.ContactMessage{DisplayName: proto.String("name"), Vcard: proto.String("vcard")}}, "", ""},
		{"contacts", "contacts", &waE2E.Message{ContactsArrayMessage: &waE2E.ContactsArrayMessage{Contacts: []*waE2E.ContactMessage{{DisplayName: proto.String("name")}}}}, "", ""},
		{"location", "location", &waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: proto.Float64(3.14), DegreesLongitude: proto.Float64(101.1), Name: proto.String("Here")}}, "name", "Here"},
		{"live location", "live_location", &waE2E.Message{LiveLocationMessage: &waE2E.LiveLocationMessage{Caption: proto.String("Live")}}, "name", "Live"},
		{"poll", "poll", &waE2E.Message{PollCreationMessageV3: &waE2E.PollCreationMessage{Name: proto.String("Lunch"), Options: []*waE2E.PollCreationMessage_Option{{OptionName: proto.String("A")}}, SelectableOptionsCount: proto.Uint32(1)}}, "name", "Lunch"},
		{"button", "interactive", &waE2E.Message{ButtonsResponseMessage: &waE2E.ButtonsResponseMessage{SelectedButtonID: proto.String("choice")}}, "selected_id", "choice"},
		{"list", "interactive", &waE2E.Message{ListResponseMessage: &waE2E.ListResponseMessage{SingleSelectReply: &waE2E.ListResponseMessage_SingleSelectReply{SelectedRowID: proto.String("row")}}}, "selected_id", "row"},
		{"template reply", "interactive", &waE2E.Message{TemplateButtonReplyMessage: &waE2E.TemplateButtonReplyMessage{SelectedID: proto.String("template-choice")}}, "selected_id", "template-choice"},
		{"native flow", "interactive", &waE2E.Message{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage{InteractiveResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage_{NativeFlowResponseMessage: &waE2E.InteractiveResponseMessage_NativeFlowResponseMessage{Name: proto.String("quick_reply"), ParamsJSON: proto.String(`{"id":"native-choice","private":"omit"}`)}}}}, "selected_id", "native-choice"},
		{"unknown", "unknown", &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := projectContent(tc.m, "test@s.whatsapp.net", true)
			if got.MessageType != tc.kind {
				t.Fatalf("kind=%s", got.MessageType)
			}
			if tc.key != "" && got.Content[tc.key] != tc.value {
				t.Fatalf("content=%v", got.Content)
			}
			if _, err := json.Marshal(got); err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestReplyAndForwardContextProjection(t *testing.T) {
	chat := "test@s.whatsapp.net"
	ctx := &waE2E.ContextInfo{StanzaID: proto.String("original"), Participant: proto.String("sender@s.whatsapp.net"), RemoteJID: &chat, IsForwarded: proto.Bool(true), ForwardingScore: proto.Uint32(4), QuotedMessage: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("quoted caption"), URL: proto.String("https://secret.invalid"), MediaKey: []byte("private")}}}
	for _, m := range []*waE2E.Message{{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("reply"), ContextInfo: ctx}}, {ImageMessage: &waE2E.ImageMessage{Caption: proto.String("reply caption"), ContextInfo: ctx}}} {
		got := projectContent(m, chat, true)
		if got.ReplyTo == nil || got.ReplyTo.MessageType != "image" || got.ReplyTo.Text != "quoted caption" || got.ReplyTo.Chat != chat || !got.IsForwarded || got.ForwardingScore != 4 {
			t.Fatalf("bad reply: %#v", got)
		}
		data, _ := json.Marshal(got)
		if strings.Contains(string(data), "private") || strings.Contains(string(data), "secret.invalid") {
			t.Fatal("quoted attachment leaked")
		}
	}
	ctx.RemoteJID = proto.String("other@s.whatsapp.net")
	got := projectContent(&waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{ContextInfo: ctx}}, chat, true)
	if got.ReplyTo != nil {
		t.Fatal("cross-chat reply accepted")
	}
}
func TestContentBoundsAndInvalidCoordinates(t *testing.T) {
	ctx := &waE2E.ContextInfo{StanzaID: proto.String("original"), QuotedMessage: &waE2E.Message{Conversation: proto.String(strings.Repeat("文", 5000))}, ForwardingScore: proto.Uint32(math.MaxUint32)}
	got := projectContent(&waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(strings.Repeat("文", 70000)), ContextInfo: ctx}}, "test@s.whatsapp.net", true)
	if len([]rune(got.Text)) != 65536 || len([]rune(got.ReplyTo.Text)) != 4096 || !got.ContentTruncated || got.ForwardingScore != math.MaxInt32 {
		t.Fatal("content bounds missing")
	}
	contacts := make([]*waE2E.ContactMessage, 30)
	for i := range contacts {
		contacts[i] = &waE2E.ContactMessage{DisplayName: proto.String(strings.Repeat("n", 300)), Vcard: proto.String(strings.Repeat("v", 9000))}
	}
	got = projectContent(&waE2E.Message{ContactsArrayMessage: &waE2E.ContactsArrayMessage{Contacts: contacts}}, "", false)
	out := got.Content["contacts"].([]map[string]string)
	if len(out) != 20 || len(out[0]["name"]) != 200 || len(out[0]["vcard"]) != 8192 || !got.ContentTruncated {
		t.Fatal("contacts not bounded")
	}
	got = projectContent(&waE2E.Message{LocationMessage: &waE2E.LocationMessage{DegreesLatitude: proto.Float64(math.NaN())}}, "", false)
	if got.MessageType != "unknown" || !got.ContentTruncated {
		t.Fatal("invalid coordinate not marked")
	}
	if _, err := json.Marshal(got); err != nil {
		t.Fatal(err)
	}
}

func TestPollSelectableCountIsBounded(t *testing.T) {
	got := projectContent(&waE2E.Message{PollCreationMessage: &waE2E.PollCreationMessage{SelectableOptionsCount: proto.Uint32(math.MaxUint32)}}, "", false)
	if got.Content["selectable_count"] != 100 || !got.ContentTruncated {
		t.Fatal("poll count exceeds service contract")
	}
}
