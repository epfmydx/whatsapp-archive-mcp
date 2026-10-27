package main

import (
	"strings"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const (
	maxProtocolProtoDepth = 16
	maxProtocolProtoNodes = 256
)

// isProtocolOnly recognizes just the sender-key distribution envelope observed
// in synced multi-device traffic. It intentionally does not inspect or decode
// the cryptographic payload. The allowlist is conservative: any human content,
// unfamiliar context metadata, malformed metadata, or unknown protobuf field
// remains an ordinary message for the normal unknown-content/recovery path.
func isProtocolOnly(m *waE2E.Message) bool {
	if m == nil {
		return false
	}
	r := m.ProtoReflect()

	var sender, context protoreflect.Value
	seenSender, seenContext := false, false
	validTop := true
	r.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch string(fd.Name()) {
		case "senderKeyDistributionMessage":
			if seenSender || fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
				validTop = false
				return false
			}
			seenSender, sender = true, v
		case "messageContextInfo":
			if seenContext || fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
				validTop = false
				return false
			}
			seenContext, context = true, v
		default:
			validTop = false
			return false
		}
		return true
	})
	if !validTop || !seenSender || !seenContext || !validSenderKeyPayload(sender.Message()) {
		return false
	}
	if !validSenderKeyContext(context.Message()) {
		return false
	}
	return !hasUnknownRecursive(r)
}

// isProtocolOnlyEvent accepts protocol-only payloads only when all SDK/raw
// representations agree and the event itself is not an edit. RawMessage is
// checked before normalization by the capture path; Message is checked too
// because pending records may already contain the normalized projection.
func isProtocolOnlyEvent(e *events.Message) bool {
	if e == nil || e.IsEdit || (e.Message == nil && e.RawMessage == nil) {
		return false
	}
	if e.Message != nil && !isProtocolOnlyEnvelope(e.Message) {
		return false
	}
	if e.RawMessage != nil && !isProtocolOnlyEnvelope(e.RawMessage) {
		return false
	}
	return true
}

// isProtocolOnlyEnvelope recognizes the strict sender-key leaf through only
// explicitly supported transport wrappers. Every wrapper is inspected before
// descent, so siblings, wrapper-specific extras, edits, unknown fields and
// unfamiliar context are retained as ordinary message content.
func isProtocolOnlyEnvelope(m *waE2E.Message) bool {
	if m == nil {
		return false
	}
	original := m
	seen := map[*waE2E.Message]bool{}
	for depth := 0; ; depth++ {
		if m == nil || depth > maxProtocolProtoDepth || seen[m] {
			return false
		}
		seen[m] = true
		if isProtocolOnly(m) {
			return !hasUnknownRecursive(original.ProtoReflect())
		}
		r := m.ProtoReflect()
		var wrapper protoreflect.Message
		wrapperName := ""
		valid := true
		r.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
			name := string(fd.Name())
			if name == "messageContextInfo" {
				if fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() || !validSenderKeyContext(v.Message()) {
					valid = false
					return false
				}
				return true
			}
			if !protocolWrapperName(name) || name == "editedMessage" || wrapper != nil || fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
				valid = false
				return false
			}
			wrapperName, wrapper = name, v.Message()
			return true
		})
		if !valid {
			return false
		}
		if wrapper == nil {
			return false
		}
		child, ok := protocolWrapperChild(wrapper, wrapperName == "deviceSentMessage")
		if !ok {
			return false
		}
		m = child
	}
}

func protocolWrapperName(name string) bool {
	switch name {
	case "deviceSentMessage", "botInvokeMessage", "ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension", "lottieStickerMessage", "documentWithCaptionMessage", "groupMentionedMessage", "editedMessage":
		return true
	default:
		return false
	}
}

func protocolWrapperChild(wrapper protoreflect.Message, deviceSent bool) (*waE2E.Message, bool) {
	var child *waE2E.Message
	valid := true
	wrapper.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(fd.Name())
		switch name {
		case "message":
			if child != nil || fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
				valid = false
				return false
			}
			child, _ = v.Message().Interface().(*waE2E.Message)
			if child == nil {
				valid = false
				return false
			}
		case "destinationJID", "phash":
			if !deviceSent || (fd.Kind() != protoreflect.StringKind && fd.Kind() != protoreflect.BytesKind) {
				valid = false
				return false
			}
		default:
			valid = false
			return false
		}
		return true
	})
	return child, valid && child != nil
}

func validSenderKeyPayload(sender protoreflect.Message) bool {
	groupIDSeen, distributionSeen := false, false
	valid := true
	sender.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		// The pinned schema uses groupID; tolerate the generated descriptor's
		// conventional groupId spelling while keeping every other field rejected.
		switch strings.ToLower(string(fd.Name())) {
		case "groupid":
			if groupIDSeen || fd.Kind() != protoreflect.StringKind || v.String() == "" {
				valid = false
				return false
			}
			groupIDSeen = true
		case "axolotlsenderkeydistributionmessage":
			if distributionSeen || fd.Kind() != protoreflect.BytesKind || len(v.Bytes()) == 0 {
				valid = false
				return false
			}
			distributionSeen = true
		default:
			valid = false
			return false
		}
		return true
	})
	return valid && groupIDSeen && distributionSeen
}

func validSenderKeyContext(ctx protoreflect.Message) bool {
	var metadata protoreflect.Message
	versionSeen := false
	valid := true
	ctx.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch string(fd.Name()) {
		case "deviceListMetadata":
			if fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
				valid = false
				return false
			}
			metadata = v.Message()
		case "deviceListMetadataVersion":
			if versionSeen || (fd.Kind() != protoreflect.Uint32Kind && fd.Kind() != protoreflect.Uint64Kind && fd.Kind() != protoreflect.Int32Kind && fd.Kind() != protoreflect.Int64Kind) {
				valid = false
				return false
			}
			versionSeen = true
			if fd.Kind() == protoreflect.Uint32Kind || fd.Kind() == protoreflect.Uint64Kind {
				valid = v.Uint() > 0
			} else {
				valid = v.Int() > 0
			}
		default:
			valid = false
			return false
		}
		return true
	})
	if !valid || metadata == nil || !versionSeen {
		return false
	}
	seen := map[string]bool{}
	metadata.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(fd.Name())
		switch name {
		case "recipientTimestamp", "senderTimestamp":
			if seen[name] || (fd.Kind() != protoreflect.Uint32Kind && fd.Kind() != protoreflect.Uint64Kind && fd.Kind() != protoreflect.Int32Kind && fd.Kind() != protoreflect.Int64Kind) {
				valid = false
				return false
			}
			seen[name] = true
			if fd.Kind() == protoreflect.Uint32Kind || fd.Kind() == protoreflect.Uint64Kind {
				valid = v.Uint() > 0
			} else {
				valid = v.Int() > 0
			}
		case "senderKeyHash", "recipientKeyHash":
			if seen[name] || fd.Kind() != protoreflect.BytesKind {
				valid = false
				return false
			}
			seen[name] = true
			valid = len(v.Bytes()) > 0
		default:
			valid = false
			return false
		}
		return valid
	})
	return valid && len(seen) == 4 && seen["recipientTimestamp"] && seen["senderTimestamp"] && seen["senderKeyHash"] && seen["recipientKeyHash"]
}

func hasUnknownRecursive(m protoreflect.Message) bool {
	budget := maxProtocolProtoNodes
	return hasUnknownRecursiveBounded(m, 0, &budget)
}

func hasUnknownRecursiveBounded(m protoreflect.Message, depth int, budget *int) bool {
	if depth > maxProtocolProtoDepth || *budget <= 0 {
		return true
	}
	*budget = *budget - 1
	if len(m.GetUnknown()) != 0 {
		return true
	}
	unknown := false
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if fd.IsList() && (fd.Kind() == protoreflect.MessageKind || fd.Kind() == protoreflect.GroupKind) {
			list := v.List()
			for i := 0; i < list.Len(); i++ {
				if hasUnknownRecursiveBounded(list.Get(i).Message(), depth+1, budget) {
					unknown = true
					return false
				}
			}
		} else if fd.IsMap() && fd.MapValue().Kind() == protoreflect.MessageKind {
			v.Map().Range(func(_ protoreflect.MapKey, value protoreflect.Value) bool {
				if hasUnknownRecursiveBounded(value.Message(), depth+1, budget) {
					unknown = true
					return false
				}
				return true
			})
		} else if !fd.IsList() && !fd.IsMap() && (fd.Kind() == protoreflect.MessageKind || fd.Kind() == protoreflect.GroupKind) && hasUnknownRecursiveBounded(v.Message(), depth+1, budget) {
			unknown = true
		}
		return !unknown
	})
	return unknown
}
