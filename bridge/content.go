package main

import (
	"encoding/json"
	"math"
	"sort"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type replyContext struct {
	MessageID   string `json:"message_id"`
	Chat        string `json:"chat_jid"`
	Sender      string `json:"sender_jid"`
	Text        string `json:"text"`
	MessageType string `json:"message_type"`
}
type contentPayload struct {
	MessageType      string         `json:"message_type"`
	Text             string         `json:"text"`
	Content          map[string]any `json:"content"`
	ReplyTo          *replyContext  `json:"reply_to"`
	IsForwarded      bool           `json:"is_forwarded"`
	ForwardingScore  int            `json:"forwarding_score"`
	ContentTruncated bool           `json:"content_truncated"`
}

func (p contentPayload) apply(m *message) {
	m.MessageType, m.Text, m.Content = p.MessageType, p.Text, p.Content
	m.ReplyTo, m.IsForwarded, m.ForwardingScore, m.ContentTruncated = p.ReplyTo, p.IsForwarded, p.ForwardingScore, p.ContentTruncated
}
func (p *contentPayload) bound(s string, max int) string {
	if utf8.RuneCountInString(s) <= max {
		return s
	}
	p.ContentTruncated = true
	return string([]rune(s)[:max])
}
func sameChat(a, b string) bool {
	aj, ae := types.ParseJID(a)
	bj, be := types.ParseJID(b)
	return ae == nil && be == nil && aj.ToNonAD() == bj.ToNonAD()
}
func contextOf(m *waE2E.Message) *waE2E.ContextInfo {
	if m == nil {
		return nil
	}
	var result *waE2E.ContextInfo
	m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if fd.Kind() == protoreflect.MessageKind && !fd.IsList() && !fd.IsMap() {
			if child, ok := v.Message().Interface().(interface{ GetContextInfo() *waE2E.ContextInfo }); ok {
				result = child.GetContextInfo()
			}
		}
		return result == nil
	})
	return result
}
func projectContent(m *waE2E.Message, chat string, withContext bool) contentPayload {
	normalized, reason := normalizePayload(m)
	m = normalized
	p := contentPayload{MessageType: "unknown", Content: map[string]any{}}
	if reason != "" || m == nil {
		if reason == "" {
			reason = "empty_message"
		}
		p.Content = unknownContent(m, reason)
		p.ContentTruncated = true
		return p
	}
	switch {
	case m.Conversation != nil || m.GetExtendedTextMessage() != nil:
		p.MessageType = "text"
		p.Text = m.GetConversation()
		if p.Text == "" {
			p.Text = m.GetExtendedTextMessage().GetText()
		}
	case m.GetImageMessage() != nil:
		p.MessageType = "image"
		p.Text = m.GetImageMessage().GetCaption()
	case m.GetVideoMessage() != nil:
		p.MessageType = "video"
		p.Text = m.GetVideoMessage().GetCaption()
	case m.GetPtvMessage() != nil:
		p.MessageType = "video_note"
		p.Text = m.GetPtvMessage().GetCaption()
	case m.GetAudioMessage() != nil:
		p.MessageType = "audio"
	case m.GetDocumentMessage() != nil:
		p.MessageType = "document"
		p.Text = m.GetDocumentMessage().GetCaption()
	case m.GetStickerMessage() != nil:
		p.MessageType = "sticker"
	case m.GetContactMessage() != nil || m.GetContactsArrayMessage() != nil:
		p.MessageType = "contact"
		contacts := []*waE2E.ContactMessage{m.GetContactMessage()}
		if m.GetContactsArrayMessage() != nil {
			p.MessageType = "contacts"
			contacts = m.GetContactsArrayMessage().GetContacts()
		}
		out := []map[string]string{}
		if len(contacts) > 20 {
			contacts = contacts[:20]
			p.ContentTruncated = true
		}
		for _, c := range contacts {
			if c != nil {
				out = append(out, map[string]string{"name": p.bound(c.GetDisplayName(), 200), "vcard": p.bound(c.GetVcard(), 8192)})
			}
		}
		p.Content = map[string]any{"contacts": out}
	case m.GetLocationMessage() != nil:
		v := m.GetLocationMessage()
		p.MessageType = "location"
		if v.GetIsLive() {
			p.MessageType = "live_location"
		}
		p.Content = locationContent(&p, v.GetDegreesLatitude(), v.GetDegreesLongitude(), v.GetName(), v.GetAddress())
	case m.GetLiveLocationMessage() != nil:
		v := m.GetLiveLocationMessage()
		p.MessageType = "live_location"
		p.Content = locationContent(&p, v.GetDegreesLatitude(), v.GetDegreesLongitude(), v.GetCaption(), "")
	case pollOf(m) != nil:
		v := pollOf(m)
		p.MessageType = "poll"
		options := v.GetOptions()
		out := []string{}
		if len(options) > 100 {
			options = options[:100]
			p.ContentTruncated = true
		}
		for _, o := range options {
			out = append(out, p.bound(o.GetOptionName(), 1000))
		}
		count := v.GetSelectableOptionsCount()
		if count > 100 {
			count = 100
			p.ContentTruncated = true
		}
		p.Content = map[string]any{"name": p.bound(v.GetName(), 1000), "options": out, "selectable_count": int(count)}
	case m.GetButtonsResponseMessage() != nil:
		v := m.GetButtonsResponseMessage()
		interactiveContent(&p, "button_response", v.GetSelectedDisplayText(), v.GetSelectedButtonID())
	case m.GetListResponseMessage() != nil:
		v := m.GetListResponseMessage()
		interactiveContent(&p, "list_response", v.GetTitle(), v.GetSingleSelectReply().GetSelectedRowID())
	case m.GetTemplateButtonReplyMessage() != nil:
		v := m.GetTemplateButtonReplyMessage()
		interactiveContent(&p, "template_button_reply", v.GetSelectedDisplayText(), v.GetSelectedID())
	case m.GetInteractiveResponseMessage() != nil:
		v := m.GetInteractiveResponseMessage()
		interactiveContent(&p, "native_flow_response", v.GetBody().GetText(), "")
		if flow := v.GetNativeFlowResponseMessage(); flow != nil {
			// Only known scalar selection IDs are projected; never forward the arbitrary JSON.
			p.Content["kind"] = p.bound(flow.GetName(), 100)
			raw := flow.GetParamsJSON()
			var fields map[string]json.RawMessage
			if len(raw) <= 65536 && json.Unmarshal([]byte(raw), &fields) == nil {
				for _, key := range []string{"id", "selected_id", "selected_row_id"} {
					var id string
					if value, ok := fields[key]; ok && json.Unmarshal(value, &id) == nil {
						p.Content["selected_id"] = p.bound(id, 1000)
						delete(fields, key)
						break
					}
				}
				if len(fields) != 0 {
					p.ContentTruncated = true
				}
			} else {
				p.ContentTruncated = true
			}
		}
	case m.GetInteractiveMessage() != nil:
		v := m.GetInteractiveMessage()
		interactiveContent(&p, "interactive", v.GetBody().GetText(), "")
		p.ContentTruncated = true // Structured actions/media are outside this bounded projection.
	case m.GetButtonsMessage() != nil:
		interactiveContent(&p, "buttons", m.GetButtonsMessage().GetContentText(), "")
		p.ContentTruncated = true
	case m.GetListMessage() != nil:
		interactiveContent(&p, "list", m.GetListMessage().GetDescription(), "")
		p.ContentTruncated = true
	case m.GetTemplateMessage() != nil:
		v := m.GetTemplateMessage()
		text := v.GetHydratedTemplate().GetHydratedContentText()
		if text == "" {
			text = v.GetHydratedFourRowTemplate().GetHydratedContentText()
		}
		interactiveContent(&p, "template", text, "")
		p.ContentTruncated = true
	}
	if p.MessageType == "unknown" {
		p.ContentTruncated = true
		reason := "unsupported_message_type"
		if len(messageFields(m)) == 0 && len(m.ProtoReflect().GetUnknown()) == 0 {
			reason = "empty_message"
		}
		if p.Content["reason"] == "invalid_content" {
			reason = "invalid_content"
		}
		p.Content = unknownContent(m, reason)
	}
	p.Text = p.bound(p.Text, 65536)
	if withContext {
		ctx := contextOf(m)
		if ctx == nil && pollOf(m) != nil {
			ctx = pollOf(m).GetContextInfo()
		}
		if ctx != nil {
			p.IsForwarded = ctx.GetIsForwarded()
			score := ctx.GetForwardingScore()
			if score > math.MaxInt32 {
				score = math.MaxInt32
				p.ContentTruncated = true
			}
			p.ForwardingScore = int(score)
			remote := ctx.GetRemoteJID()
			if ctx.GetStanzaID() != "" && (remote == "" || sameChat(remote, chat)) {
				if utf8.RuneCountInString(ctx.GetStanzaID()) <= 300 && utf8.RuneCountInString(ctx.GetParticipant()) <= 300 {
					q := projectContent(ctx.GetQuotedMessage(), chat, false)
					p.ReplyTo = &replyContext{MessageID: ctx.GetStanzaID(), Chat: chat, Sender: ctx.GetParticipant(), Text: p.bound(q.Text, 4096), MessageType: q.MessageType}
					p.ContentTruncated = p.ContentTruncated || q.ContentTruncated
				} else {
					p.ContentTruncated = true
				}
			}
		}
	}
	return p
}
func locationContent(p *contentPayload, lat, lon float64, name, address string) map[string]any {
	if math.IsNaN(lat) || math.IsInf(lat, 0) || math.IsNaN(lon) || math.IsInf(lon, 0) || lat < -90 || lat > 90 || lon < -180 || lon > 180 {
		p.MessageType = "unknown"
		p.ContentTruncated = true
		return map[string]any{"reason": "invalid_content"}
	}
	return map[string]any{"latitude": lat, "longitude": lon, "name": p.bound(name, 1000), "address": p.bound(address, 2000)}
}
func interactiveContent(p *contentPayload, kind, text, id string) {
	p.MessageType = "interactive"
	p.Content = map[string]any{"kind": p.bound(kind, 100), "text": p.bound(text, 10000), "selected_id": p.bound(id, 1000)}
}
func pollOf(m *waE2E.Message) *waE2E.PollCreationMessage {
	for _, p := range []*waE2E.PollCreationMessage{m.GetPollCreationMessage(), m.GetPollCreationMessageV2(), m.GetPollCreationMessageV3(), m.GetPollCreationMessageV5(), m.GetPollCreationMessageV6()} {
		if p != nil {
			return p
		}
	}
	if v := m.GetPollCreationMessageV4().GetMessage(); v != nil {
		return v.GetPollCreationMessage()
	}
	return nil
}

func messageFields(m *waE2E.Message) []string {
	fields := []string{}
	if m != nil {
		m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
			fields = append(fields, string(fd.Name()))
			return true
		})
	}
	sort.Strings(fields)
	if len(fields) > 32 {
		fields = fields[:32]
	}
	return fields
}
func unknownContent(m *waE2E.Message, reason string) map[string]any {
	return map[string]any{"reason": reason, "message_fields": messageFields(m), "recovery": "operator_retained_payload"}
}
