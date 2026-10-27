package main

import (
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/reflect/protoreflect"
)

func protocolField(t *testing.T, m protoreflect.Message, name string) protoreflect.FieldDescriptor {
	t.Helper()
	fd := m.Descriptor().Fields().ByName(protoreflect.Name(name))
	if fd == nil {
		t.Fatalf("pinned protobuf schema lacks expected field %q in %s", name, m.Descriptor().FullName())
	}
	return fd
}

func setProtocolUint(t *testing.T, m protoreflect.Message, name string, value uint64) {
	t.Helper()
	fd := protocolField(t, m, name)
	if fd.Kind() == protoreflect.Uint32Kind {
		m.Set(fd, protoreflect.ValueOfUint32(uint32(value)))
	} else if fd.Kind() == protoreflect.Uint64Kind {
		m.Set(fd, protoreflect.ValueOfUint64(value))
	} else if fd.Kind() == protoreflect.Int32Kind {
		m.Set(fd, protoreflect.ValueOfInt32(int32(value)))
	} else if fd.Kind() == protoreflect.Int64Kind {
		m.Set(fd, protoreflect.ValueOfInt64(int64(value)))
	} else {
		t.Fatalf("field %s has unexpected kind %s", name, fd.Kind())
	}
}

func validProtocolOnlyMessage(t *testing.T) *waE2E.Message {
	t.Helper()
	root := (&waE2E.Message{}).ProtoReflect()
	senderFD := protocolField(t, root, "senderKeyDistributionMessage")
	sender := root.Mutable(senderFD).Message()
	sender.Set(protocolFieldByFold(t, sender, "groupid"), protoreflect.ValueOfString("synthetic-group"))
	sender.Set(protocolFieldByFold(t, sender, "axolotlsenderkeydistributionmessage"), protoreflect.ValueOfBytes([]byte{1}))

	ctx := root.Mutable(protocolField(t, root, "messageContextInfo")).Message()
	setProtocolUint(t, ctx, "deviceListMetadataVersion", 1)
	metadata := ctx.Mutable(protocolField(t, ctx, "deviceListMetadata")).Message()
	setProtocolUint(t, metadata, "recipientTimestamp", 1)
	setProtocolUint(t, metadata, "senderTimestamp", 1)
	metadata.Set(protocolField(t, metadata, "senderKeyHash"), protoreflect.ValueOfBytes([]byte{1}))
	metadata.Set(protocolField(t, metadata, "recipientKeyHash"), protoreflect.ValueOfBytes([]byte{2}))
	return root.Interface().(*waE2E.Message)
}

func wrapProtocolMessage(t *testing.T, name string, child *waE2E.Message) *waE2E.Message {
	t.Helper()
	root := (&waE2E.Message{}).ProtoReflect()
	wrapper := root.Mutable(protocolField(t, root, name)).Message()
	messageFD := protocolField(t, wrapper, "message")
	wrapper.Set(messageFD, protoreflect.ValueOfMessage(child.ProtoReflect()))
	return root.Interface().(*waE2E.Message)
}

func protocolFieldByFold(t *testing.T, m protoreflect.Message, name string) protoreflect.FieldDescriptor {
	t.Helper()
	fields := m.Descriptor().Fields()
	for i := 0; i < fields.Len(); i++ {
		fd := fields.Get(i)
		if strings.EqualFold(string(fd.Name()), name) {
			return fd
		}
	}
	t.Fatalf("pinned protobuf schema lacks expected field %q in %s", name, m.Descriptor().FullName())
	return nil
}

func setUnexpectedTopLevel(t *testing.T, m *waE2E.Message, name string) {
	t.Helper()
	r := m.ProtoReflect()
	fd := protocolField(t, r, name)
	if fd.Kind() == protoreflect.MessageKind || fd.Kind() == protoreflect.GroupKind {
		populate := r.Mutable(fd).Message()
		populateSyntheticLeaf(t, populate)
	} else if fd.Kind() == protoreflect.StringKind {
		r.Set(fd, protoreflect.ValueOfString("synthetic"))
	} else if fd.Kind() == protoreflect.BytesKind {
		r.Set(fd, protoreflect.ValueOfBytes([]byte{1}))
	} else {
		t.Fatalf("unexpected top-level test field %s has unsupported kind %s", name, fd.Kind())
	}
}

func populateSyntheticLeaf(t *testing.T, m protoreflect.Message) {
	populateSyntheticLeafAtDepth(t, m, 0)
}

func populateSyntheticLeafAtDepth(t *testing.T, m protoreflect.Message, depth int) {
	t.Helper()
	if depth >= 12 {
		t.Fatalf("no bounded scalar field available to populate %s", m.Descriptor().FullName())
	}
	fields := m.Descriptor().Fields()
	for i := 0; i < fields.Len(); i++ {
		fd := fields.Get(i)
		if fd.IsList() || fd.IsMap() {
			continue
		}
		switch fd.Kind() {
		case protoreflect.BytesKind:
			m.Set(fd, protoreflect.ValueOfBytes([]byte{1}))
			return
		case protoreflect.StringKind:
			m.Set(fd, protoreflect.ValueOfString("synthetic"))
			return
		case protoreflect.MessageKind, protoreflect.GroupKind:
			populateSyntheticLeafAtDepth(t, m.Mutable(fd).Message(), depth+1)
			return
		}
	}
	t.Fatalf("no scalar field available to populate %s", m.Descriptor().FullName())
}

func TestIsProtocolOnlyConservativeAllowlist(t *testing.T) {
	t.Run("recognized sender-key envelope", func(t *testing.T) {
		if !isProtocolOnly(validProtocolOnlyMessage(t)) {
			t.Fatal("recognized sender-key envelope was not classified as protocol-only")
		}
	})
	t.Run("empty", func(t *testing.T) {
		if isProtocolOnly(&waE2E.Message{}) {
			t.Fatal("empty message classified as protocol-only")
		}
	})
	t.Run("metadata only", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		m.ProtoReflect().Clear(protocolField(t, m.ProtoReflect(), "senderKeyDistributionMessage"))
		if isProtocolOnly(m) {
			t.Fatal("metadata-only message classified as protocol-only")
		}
	})
	t.Run("empty sender-key payload", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		r := m.ProtoReflect()
		senderFD := protocolField(t, r, "senderKeyDistributionMessage")
		sender := r.Mutable(senderFD).Message()
		fields := sender.Descriptor().Fields()
		for i := 0; i < fields.Len(); i++ {
			sender.Clear(fields.Get(i))
		}
		if isProtocolOnly(m) {
			t.Fatal("empty sender-key payload classified as protocol-only")
		}
	})
	t.Run("group ID without distribution bytes", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		sender := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "senderKeyDistributionMessage")).Message()
		sender.Clear(protocolFieldByFold(t, sender, "axolotlsenderkeydistributionmessage"))
		if isProtocolOnly(m) {
			t.Fatal("incomplete sender-key payload classified as protocol-only")
		}
	})
	t.Run("distribution bytes without group ID", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		sender := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "senderKeyDistributionMessage")).Message()
		sender.Clear(protocolFieldByFold(t, sender, "groupid"))
		if isProtocolOnly(m) {
			t.Fatal("incomplete sender-key payload classified as protocol-only")
		}
	})
	t.Run("mixed human text", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		m.Conversation = protoString("human text")
		if isProtocolOnly(m) {
			t.Fatal("mixed human content classified as protocol-only")
		}
	})
	for _, field := range []string{"imageMessage", "reactionMessage", "editedMessage"} {
		field := field
		t.Run("mixed "+field, func(t *testing.T) {
			m := validProtocolOnlyMessage(t)
			setUnexpectedTopLevel(t, m, field)
			if isProtocolOnly(m) {
				t.Fatalf("mixed %s classified as protocol-only", field)
			}
		})
	}
	t.Run("unknown top-level field", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		m.ProtoReflect().SetUnknown([]byte{0x98, 0x06, 0x01})
		if isProtocolOnly(m) {
			t.Fatal("unknown top-level protobuf field was dropped")
		}
	})
	t.Run("unknown nested sender field", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		sender := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "senderKeyDistributionMessage")).Message()
		sender.SetUnknown([]byte{0x98, 0x06, 0x01})
		if isProtocolOnly(m) {
			t.Fatal("unknown nested protobuf field was dropped")
		}
	})
	t.Run("unfamiliar context metadata", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		ctx := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "messageContextInfo")).Message()
		fd := firstUnexpectedContextField(ctx)
		if fd == nil {
			ctx.SetUnknown([]byte{0x98, 0x06, 0x01})
		} else if fd.Kind() == protoreflect.StringKind {
			ctx.Set(fd, protoreflect.ValueOfString("unexpected contextual metadata"))
		} else if fd.Kind() == protoreflect.BytesKind {
			ctx.Set(fd, protoreflect.ValueOfBytes([]byte{1}))
		} else if fd.Kind() == protoreflect.MessageKind {
			populateSyntheticLeaf(t, ctx.Mutable(fd).Message())
		} else {
			t.Fatalf("unexpected context field %s has unsupported kind %s", fd.Name(), fd.Kind())
		}
		if isProtocolOnly(m) {
			t.Fatal("unfamiliar context metadata was dropped")
		}
	})
	t.Run("unknown metadata field", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		ctx := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "messageContextInfo")).Message()
		metadata := ctx.Get(protocolField(t, ctx, "deviceListMetadata")).Message()
		metadata.SetUnknown([]byte{0x98, 0x06, 0x01})
		if isProtocolOnly(m) {
			t.Fatal("unknown nested context field classified as protocol-only")
		}
	})
	t.Run("missing sender-key hash", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		ctx := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "messageContextInfo")).Message()
		metadata := ctx.Get(protocolField(t, ctx, "deviceListMetadata")).Message()
		metadata.Clear(protocolField(t, metadata, "senderKeyHash"))
		if isProtocolOnly(m) {
			t.Fatal("malformed sender-key metadata was dropped")
		}
	})
	t.Run("missing context", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		m.MessageContextInfo = nil
		if isProtocolOnly(m) {
			t.Fatal("missing sender-key context was dropped")
		}
	})
}

func TestIsProtocolOnlyEnvelopeAndEvent(t *testing.T) {
	t.Run("known wrapper accepted", func(t *testing.T) {
		m := wrapProtocolMessage(t, "ephemeralMessage", validProtocolOnlyMessage(t))
		if !isProtocolOnlyEnvelope(m) {
			t.Fatal("known protocol wrapper was not classified as protocol-only")
		}
	})
	t.Run("unknown nested wrapper field rejected", func(t *testing.T) {
		m := wrapProtocolMessage(t, "ephemeralMessage", validProtocolOnlyMessage(t))
		wrapper := m.ProtoReflect().Get(protocolField(t, m.ProtoReflect(), "ephemeralMessage")).Message()
		wrapper.SetUnknown([]byte{0x98, 0x06, 0x01})
		if isProtocolOnlyEnvelope(m) {
			t.Fatal("unknown wrapper field was classified as protocol-only")
		}
	})
	t.Run("unfamiliar outer context rejected", func(t *testing.T) {
		m := wrapProtocolMessage(t, "ephemeralMessage", validProtocolOnlyMessage(t))
		r := m.ProtoReflect()
		ctxFD := protocolField(t, r, "messageContextInfo")
		ctx := r.Mutable(ctxFD).Message()
		setProtocolUint(t, ctx, "deviceListMetadataVersion", 1)
		metadata := ctx.Mutable(protocolField(t, ctx, "deviceListMetadata")).Message()
		setProtocolUint(t, metadata, "recipientTimestamp", 1)
		setProtocolUint(t, metadata, "senderTimestamp", 1)
		metadata.Set(protocolField(t, metadata, "senderKeyHash"), protoreflect.ValueOfBytes([]byte{1}))
		metadata.Set(protocolField(t, metadata, "recipientKeyHash"), protoreflect.ValueOfBytes([]byte{2}))
		supportFD := protocolField(t, ctx, "supportPayload")
		ctx.Set(supportFD, protoreflect.ValueOfString("unfamiliar"))
		if isProtocolOnlyEnvelope(m) {
			t.Fatal("unfamiliar outer context was classified as protocol-only")
		}
	})
	t.Run("edit wrapper rejected", func(t *testing.T) {
		m := wrapProtocolMessage(t, "editedMessage", validProtocolOnlyMessage(t))
		if isProtocolOnlyEnvelope(m) {
			t.Fatal("edited wrapper was classified as protocol-only")
		}
	})
	t.Run("event edit provenance rejected", func(t *testing.T) {
		leaf := validProtocolOnlyMessage(t)
		e := &events.Message{Message: leaf, RawMessage: leaf, IsEdit: true}
		if isProtocolOnlyEvent(e) {
			t.Fatal("edit event was classified as protocol-only")
		}
	})
	t.Run("raw and normalized human content must both match", func(t *testing.T) {
		leaf := validProtocolOnlyMessage(t)
		raw := &waE2E.Message{Conversation: protoString("human content")}
		e := &events.Message{Message: leaf, RawMessage: raw}
		if isProtocolOnlyEvent(e) {
			t.Fatal("mixed raw and normalized event was classified as protocol-only")
		}
	})
	t.Run("wrapper depth bound", func(t *testing.T) {
		m := validProtocolOnlyMessage(t)
		for i := 0; i <= maxProtocolProtoDepth; i++ {
			m = wrapProtocolMessage(t, "ephemeralMessage", m)
		}
		if isProtocolOnlyEnvelope(m) {
			t.Fatal("over-depth wrapper envelope was classified as protocol-only")
		}
	})
}

func firstUnexpectedContextField(ctx protoreflect.Message) protoreflect.FieldDescriptor {
	fields := ctx.Descriptor().Fields()
	for i := 0; i < fields.Len(); i++ {
		fd := fields.Get(i)
		if string(fd.Name()) != "deviceListMetadata" && string(fd.Name()) != "deviceListMetadataVersion" {
			if fd.Kind() == protoreflect.StringKind || fd.Kind() == protoreflect.BytesKind || fd.Kind() == protoreflect.MessageKind || fd.Kind() == protoreflect.GroupKind {
				return fd
			}
		}
	}
	return nil
}

func protoString(s string) *string { return &s }
