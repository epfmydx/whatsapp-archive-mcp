package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

type reactionContent struct {
	Emoji string `json:"emoji"`
}
type messageUpdate struct {
	Account      string           `json:"account_id"`
	EventID      string           `json:"event_id"`
	Chat         string           `json:"chat_jid"`
	TargetID     string           `json:"target_message_id"`
	Actor        string           `json:"actor_jid"`
	ActorFromMe  bool             `json:"actor_is_from_me"`
	Kind         string           `json:"kind"`
	Timestamp    time.Time        `json:"timestamp"`
	Reaction     *reactionContent `json:"reaction,omitempty"`
	Edit         *contentPayload  `json:"edit,omitempty"`
	TargetSender string           `json:"target_sender_jid,omitempty"`
	TargetFromMe *bool            `json:"target_is_from_me,omitempty"`
}

func validUpdateBinding(r record) bool {
	if r.InvalidUpdate {
		return r.Update == nil && !r.EncryptedReaction && !r.MediaPending && r.Message.Media == nil && r.Message.ID == invalidUpdateID(r.Message, r.TransportID, r.Proto)
	}
	if r.Update == nil {
		return !r.EncryptedReaction
	}
	u := r.Update
	_, err := hex.DecodeString(u.EventID)
	return err == nil && len(u.EventID) == 64 && u.Account == r.Message.Account && u.Chat == r.Message.Chat && r.Message.ID == "update-"+u.EventID && u.TargetID != "" && !r.MediaPending && r.Message.Media == nil
}

// Invalid updates remain private spool entries until an explicit repair or a
// future decoder supports them. They never become blank ordinary messages.
func invalidUpdateID(m message, transportID string, raw []byte) string {
	// The outer transport identity and payload bind the quarantine entry, even
	// when its target or provider timestamp is malformed. Never use wall time.
	input, _ := json.Marshal([]any{m.Account, m.Chat, m.Sender, m.FromMe, transportID, raw})
	sum := sha256.Sum256(input)
	return "invalid-update-" + hex.EncodeToString(sum[:])
}
func makeInvalidUpdateRecord(account string, e *events.Message) (record, error) {
	if account == "" || e.Info.Chat.IsEmpty() {
		return record{}, errors.New("missing update binding")
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(e.Message)
	if err != nil {
		return record{}, err
	}
	// No target, user-visible text, or arbitrary error is projected. The
	// original protobuf and transport ID remain private durable evidence.
	m := message{Account: account, Chat: e.Info.Chat.String(), Sender: e.Info.Sender.String(), FromMe: e.Info.IsFromMe, Timestamp: time.Unix(0, 0).UTC()}
	m.ID = invalidUpdateID(m, e.Info.ID, raw)
	original, err := originalProto(e)
	if err != nil {
		return record{}, err
	}
	return record{Message: m, Proto: raw, RawProto: original, TransportID: e.Info.ID, InvalidUpdate: true}, nil
}
func makeUpdateRecord(account string, e *events.Message) (record, bool, error) {
	var r record
	e, normalizationReason := normalizeEvent(e)
	if normalizationReason != "" {
		return r, false, nil
	}
	m := e.Message
	var key *waCommon.MessageKey
	var reaction *waE2E.ReactionMessage
	var edit *waE2E.Message
	kind := ""
	ts := e.Info.Timestamp.UTC()
	encrypted := false
	switch {
	case m.GetReactionMessage() != nil:
		reaction = m.GetReactionMessage()
		key = reaction.GetKey()
		kind = "reaction"
		if reaction.GetSenderTimestampMS() > 0 {
			ts = time.UnixMilli(reaction.GetSenderTimestampMS()).UTC()
		}
	case m.GetEncReactionMessage() != nil:
		encrypted = true
		kind = "reaction"
		key = m.GetEncReactionMessage().GetTargetMessageKey()
	case m.GetProtocolMessage() != nil:
		p := m.GetProtocolMessage()
		if p.Type == nil {
			return r, true, nil
		}
		switch p.GetType() {
		case waE2E.ProtocolMessage_REVOKE:
			kind = "delete"
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			kind = "edit"
			edit = p.GetEditedMessage()
			if edit == nil {
				return r, true, errors.New("missing edit content")
			}
		default:
			return r, true, nil
		}
		key = p.GetKey()
		if p.GetTimestampMS() > 0 {
			ts = time.UnixMilli(p.GetTimestampMS()).UTC()
		}
	case e.IsEdit:
		// Unwrapped edits without a ProtocolMessage use the provider's original ID.
		kind = "edit"
		edit = m
		key = &waCommon.MessageKey{ID: proto.String(e.Info.ID)}
	default:
		return r, false, nil
	}
	if key == nil || key.GetID() == "" || utf8.RuneCountInString(key.GetID()) > 300 || (key.GetRemoteJID() != "" && !sameChat(key.GetRemoteJID(), e.Info.Chat.String())) {
		return r, true, errors.New("invalid update target")
	}
	if ts.IsZero() {
		return r, true, errors.New("missing update timestamp")
	}
	if _, err := ts.MarshalJSON(); err != nil {
		return r, true, errors.New("invalid update timestamp")
	}
	u := &messageUpdate{Account: account, Chat: e.Info.Chat.String(), TargetID: key.GetID(), Actor: e.Info.Sender.String(), ActorFromMe: e.Info.IsFromMe, Kind: kind, Timestamp: ts}
	if key.GetParticipant() != "" && utf8.RuneCountInString(key.GetParticipant()) <= 300 {
		u.TargetSender = key.GetParticipant()
	}
	// A true key.FromMe identifies the actor's own target. False only proves
	// 'not the actor', so omit the device-relative hint for incoming events.
	if key.FromMe != nil && (key.GetFromMe() || e.Info.IsFromMe) {
		fromMe := key.GetFromMe() && e.Info.IsFromMe
		u.TargetFromMe = &fromMe
	}
	if reaction != nil {
		if utf8.RuneCountInString(reaction.GetText()) > 64 {
			return r, true, errors.New("invalid reaction")
		}
		u.Reaction = &reactionContent{Emoji: reaction.GetText()}
	}
	if edit != nil {
		v := projectContent(edit, u.Chat, true)
		u.Edit = &v
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(m)
	if err != nil {
		return r, true, err
	}
	// Include the original transport ID and full deterministic protobuf, so bounded
	// projections do not collapse distinct edits or encrypted reactions.
	hashInput, err := json.Marshal(struct {
		Transport string
		Update    *messageUpdate
		Raw       []byte
	}{e.Info.ID, u, raw})
	if err != nil {
		return r, true, err
	}
	digest := sha256.Sum256(hashInput)
	u.EventID = hex.EncodeToString(digest[:])
	original, originalErr := originalProto(e)
	if originalErr != nil {
		return r, true, originalErr
	}
	r = record{RawProto: original, Message: message{Account: account, Chat: u.Chat, ID: "update-" + u.EventID, Sender: u.Actor, FromMe: u.ActorFromMe, Timestamp: ts}, Update: u, Proto: raw, EncryptedReaction: encrypted, TransportID: e.Info.ID}
	return r, true, nil
}
func (b *bridge) resolveReaction(ctx context.Context, r *record) error {
	var m waE2E.Message
	if err := proto.Unmarshal(r.Proto, &m); err != nil {
		return &stageError{reason: "invalid_metadata"}
	}
	if b.client == nil {
		return &stageError{reason: "missing_metadata"}
	}
	chat, err := types.ParseJID(r.Update.Chat)
	if err != nil {
		return &stageError{reason: "invalid_metadata"}
	}
	sender, err := types.ParseJID(r.Update.Actor)
	if err != nil {
		return &stageError{reason: "invalid_metadata"}
	}
	e := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsFromMe: r.Update.ActorFromMe}, ID: r.TransportID, Timestamp: r.Update.Timestamp}, Message: &m}
	// The pinned helper reads only MsgSecrets and performs local cryptography.
	reaction, err := b.client.DecryptReaction(ctx, e)
	if err != nil {
		return &stageError{reason: "missing_metadata"}
	}
	if (reaction.GetKey() != nil && (reaction.GetKey().GetID() != r.Update.TargetID || (reaction.GetKey().GetRemoteJID() != "" && !sameChat(reaction.GetKey().GetRemoteJID(), r.Update.Chat)))) || utf8.RuneCountInString(reaction.GetText()) > 64 {
		return &stageError{reason: "invalid_metadata"}
	}
	if reaction.GetSenderTimestampMS() > 0 {
		stamp := time.UnixMilli(reaction.GetSenderTimestampMS()).UTC()
		if _, err := stamp.MarshalJSON(); err != nil {
			return &stageError{reason: "invalid_metadata"}
		}
		r.Update.Timestamp = stamp
	}
	r.Update.Reaction = &reactionContent{Emoji: reaction.GetText()}
	r.EncryptedReaction = false
	if err = b.spool.save(*r, false); err != nil {
		b.stopForSpoolFailure()
		return err
	}
	return nil
}
