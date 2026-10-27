package main

import (
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const maxWrapperDepth = 16

// Only explicitly recognized transport/content wrappers are traversed. Protocol
// edits and controls remain intact for update routing, never generic reflection.
func normalizeEvent(e *events.Message) (*events.Message, string) {
	out := *e
	if e.RawMessage != nil {
		out.Message = e.RawMessage
	}
	seen := map[*waE2E.Message]bool{}
	for depth := 0; out.Message != nil; depth++ {
		m := out.Message
		if seen[m] {
			return &out, "wrapper_limit"
		}
		seen[m] = true
		var next *waE2E.Message
		n := 0
		choose := func(p *waE2E.Message) { n++; next = p }
		if v := m.GetDeviceSentMessage(); v != nil {
			choose(v.GetMessage())
			out.Info.DeviceSentMeta = &types.DeviceSentMeta{DestinationJID: v.GetDestinationJID(), Phash: v.GetPhash()}
		}
		if v := m.GetBotInvokeMessage(); v != nil {
			choose(v.GetMessage())
			out.IsBotInvoke = true
		}
		if v := m.GetEphemeralMessage(); v != nil {
			choose(v.GetMessage())
			out.IsEphemeral = true
		}
		if v := m.GetViewOnceMessage(); v != nil {
			choose(v.GetMessage())
			out.IsViewOnce = true
		}
		if v := m.GetViewOnceMessageV2(); v != nil {
			choose(v.GetMessage())
			out.IsViewOnce = true
			out.IsViewOnceV2 = true
		}
		if v := m.GetViewOnceMessageV2Extension(); v != nil {
			choose(v.GetMessage())
			out.IsViewOnce = true
			out.IsViewOnceV2 = true
			out.IsViewOnceV2Extension = true
		}
		if v := m.GetLottieStickerMessage(); v != nil {
			choose(v.GetMessage())
			out.IsLottieSticker = true
		}
		if v := m.GetDocumentWithCaptionMessage(); v != nil {
			choose(v.GetMessage())
			out.IsDocumentWithCaption = true
		}
		if v := m.GetGroupMentionedMessage(); v != nil {
			choose(v.GetMessage())
		}
		if v := m.GetEditedMessage(); v != nil {
			choose(v.GetMessage())
			out.IsEdit = true
		}
		if n == 0 {
			return &out, ""
		}
		populated := 0
		m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
			if string(fd.Name()) != "messageContextInfo" {
				populated++
			}
			return true
		})
		if populated != 1 || len(m.ProtoReflect().GetUnknown()) != 0 {
			return &out, "invalid_content"
		}
		if n != 1 || next == nil {
			return &out, "invalid_content"
		}
		if depth >= maxWrapperDepth {
			return &out, "wrapper_limit"
		}
		out.Message = next
	}
	return &out, ""
}
func normalizePayload(m *waE2E.Message) (*waE2E.Message, string) {
	e, reason := normalizeEvent(&events.Message{Message: m})
	return e.Message, reason
}
