package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

type attachment interface {
	whatsmeow.DownloadableMessage
	GetFileLength() uint64
	GetMimetype() string
}

func attachmentOf(m *waE2E.Message) (attachment, string, string) {
	switch {
	case m.GetImageMessage() != nil:
		v := m.GetImageMessage()
		return v, "image", v.GetCaption()
	case m.GetVideoMessage() != nil:
		v := m.GetVideoMessage()
		return v, "video", v.GetCaption()
	case m.GetPtvMessage() != nil:
		return m.GetPtvMessage(), "video-note", m.GetPtvMessage().GetCaption()
	case m.GetAudioMessage() != nil:
		return m.GetAudioMessage(), "audio", ""
	case m.GetDocumentMessage() != nil:
		v := m.GetDocumentMessage()
		return v, v.GetFileName(), v.GetCaption()
	case m.GetStickerMessage() != nil:
		return m.GetStickerMessage(), "sticker", ""
	}
	return nil, "", ""
}
func makeRecord(account, chatName string, e *events.Message) (record, error) {
	e, reason := normalizeEvent(e)
	m := message{Account: account, ID: e.Info.ID, Chat: e.Info.Chat.String(), ChatName: chatName, Sender: e.Info.Sender.String(), SenderName: e.Info.PushName, Timestamp: e.Info.Timestamp.UTC(), FromMe: e.Info.IsFromMe}
	projectContent(e.Message, m.Chat, true).apply(&m)
	if reason != "" {
		m.MessageType = "unknown"
		m.Content = unknownContent(e.Message, reason)
		m.ContentTruncated = true
	}
	a, _, _ := attachmentOf(e.Message)
	b, err := proto.Marshal(e.Message)
	raw, rawErr := originalProto(e)
	if err == nil {
		err = rawErr
	}
	if reason != "" {
		a = nil
	}
	return record{Message: m, Proto: b, RawProto: raw, MediaPending: a != nil}, err
}

// capture returns success only after the complete event is durably persisted,
// or after explicitly ignoring a non-content/control event. Whatsmeow uses this
// result to suppress its synchronous transport ACK when persistence fails.
func (b *bridge) capture(e *events.Message, chatName string) bool {
	return b.captureSource(e, chatName, "live")
}
func (b *bridge) captureSource(e *events.Message, chatName, source string) bool {
	// Check every original envelope before normalization can discard wrappers.
	protocolOnly := isProtocolOnlyEvent(e)
	e, normalizationReason := normalizeEvent(e)
	protocolOnly = protocolOnly && normalizationReason == ""
	if e.Message == nil && normalizationReason == "" {
		return true
	}
	r, handled, err := makeUpdateRecord(b.cfg.Account, e)
	if normalizationReason != "" {
		handled = false
	}
	if !handled || protocolOnly {
		r, err = makeRecord(b.cfg.Account, chatName, e)
	} else if err != nil {
		// Invalid sender-controlled content is retained per item. It must not
		// turn validation into a global receiver/persistence failure.
		r, err = makeInvalidUpdateRecord(b.cfg.Account, e)
	} else if r.Update == nil {
		return true // Non-content protocol/control event.
	}
	if err != nil {
		// No safe account/chat-bound record can be produced. Do not acknowledge
		// it, but keep other traffic alive; no durable write was attempted.
		return false
	}
	r.ProtocolOnly = protocolOnly
	r.Source = source
	if err = b.spool.save(r, true); err != nil {
		b.stopForSpoolFailure()
		return false
	}
	if !r.InvalidUpdate && !r.ProtocolOnly {
		b.observeMessage(e, chatName)
	}
	select {
	case b.wake <- struct{}{}:
	default:
	}
	return true
}
func (b *bridge) event(evt any) bool {
	switch e := evt.(type) {
	case *events.MediaRetry:
		b.queueMediaRetry(e)
	case *events.Message:
		return b.capture(e, "")
	case *events.HistorySync:
		b.observeHistory(e)
		for _, chat := range e.Data.GetConversations() {
			jid, err := types.ParseJID(chat.GetID())
			// Retain the existing treatment of unsupported/malformed history entries:
			// only entries Whatsmeow can convert into chat messages are archived.
			if err != nil {
				continue
			}
			for _, hm := range chat.GetMessages() {
				parsed, err := b.client.ParseWebMessage(jid, hm.GetMessage())
				if err == nil && parsed != nil {
					// ParseWebMessage replaces direct edit payloads and IDs. Restore the original
					// transport identity; normalizeEvent routes the retained protocol envelope.
					parsed.Info.ID = hm.GetMessage().GetKey().GetID()
					if !b.captureSource(parsed, chat.GetName(), "history") {
						return false
					}
				}
			}
		}
		if b.metadata != nil {
			b.metadata.signal()
		}
	case *events.Contact:
		b.observeContact(e)
	case *events.PushName:
		b.observePushName(e)
	case *events.BusinessName:
		b.observeBusinessName(e)
	case *events.JoinedGroup:
		b.observeGroup(e.JID, e.Name, e.NameSetAt)
		if b.metadata != nil {
			if e.Sender != nil && e.SenderPN != nil {
				b.metadata.link(e.Sender.String(), e.SenderPN.String(), e.NameSetAt)
			}
			for _, p := range e.Participants {
				b.metadata.link(p.LID.String(), p.PhoneNumber.String(), e.NameSetAt)
			}
		}
	case *events.GroupInfo:
		if b.metadata != nil && e.Sender != nil && e.SenderPN != nil {
			b.metadata.link(e.Sender.String(), e.SenderPN.String(), e.Timestamp)
		}
		if e.Name != nil {
			b.observeGroup(e.JID, e.Name.Name, sourceTime(e.Name.NameSetAt, e.Timestamp))
		}
	case *events.Connected:
		b.setStatus("connected")
	case *events.Disconnected:
		b.setStatus("disconnected")
	case *events.LoggedOut:
		b.setStatus("logged_out")
	case events.PermanentDisconnect:
		// Never publish provider descriptions, which may contain private details.
		b.setStatus("disconnected")
	}
	// Lifecycle and non-message transport events need no application journal.
	return true
}
func (b *bridge) registerEvents() {
	b.client.SynchronousAck = true
	// The PostgreSQL buffer commits plaintext alongside Signal state so that a
	// failed-handler redelivery remains decryptable after receiver restart.
	b.client.EnableDecryptedEventBuffer = true
	b.client.AddEventHandlerWithSuccessStatus(b.event)
}

func newS3(c config) (*minio.Client, error) {
	endpoint := c.S3Endpoint
	if endpoint == "" {
		endpoint = "https://s3." + c.S3Region + ".amazonaws.com"
	}
	u, e := url.Parse(endpoint)
	if e != nil {
		return nil, e
	}
	lookup := minio.BucketLookupPath
	if c.S3Style == "virtual" {
		lookup = minio.BucketLookupDNS
	}
	return minio.New(u.Host, &minio.Options{Creds: credentials.NewStaticV4(c.S3Access, c.S3Secret, ""), Secure: u.Scheme == "https", Region: c.S3Region, BucketLookup: lookup})
}
func (b *bridge) archive(ctx context.Context, r *record) (result error) {
	stage := "media_metadata"
	defer func() {
		if result != nil {
			result = &stageError{stage: stage, cause: result}
		}
	}()
	var p waE2E.Message
	if err := proto.Unmarshal(r.Proto, &p); err != nil {
		return &stageError{reason: "invalid_metadata"}
	}
	a, filename, _ := attachmentOf(&p)
	if a == nil {
		return &stageError{reason: "missing_metadata"}
	}
	if a.GetFileLength() > uint64(b.cfg.MaxMedia) {
		return errMediaBoundExceeded
	}
	// Keep a verified original across S3/app outages; a partial download is never
	// renamed to .media. Metadata is retained separately until app acknowledgment.
	id := recordID(r.Message)
	local := filepath.Join(b.spool.dir, id+".media")
	stage = "media_cache"
	f, err := os.Open(local)
	if os.IsNotExist(err) {
		tmp, e := os.CreateTemp(b.spool.dir, ".download-")
		if e != nil {
			return e
		}
		name := tmp.Name()
		defer os.Remove(name)
		stage = "media_download"
		started := time.Now()
		e = b.download(ctx, a, boundedFile{tmp, b.cfg.MaxMedia + 32})
		partial := int64(0)
		if st, se := tmp.Stat(); se == nil {
			partial = st.Size()
		}
		_, reason, status := classifyStage(stage, e)
		var attempt uint32
		if r.Retry != nil {
			attempt = r.Retry.Attempts
		}
		b.emitDiagnostic(diagnostic{Event: "media_download", Stage: stage, Reason: reason, HTTPStatus: status, ElapsedMS: time.Since(started).Milliseconds(), PartialBytes: partial, ExpectedBytes: a.GetFileLength(), MediaType: coarseMediaType(&p), Attempt: attempt, ItemID: r.ItemID, AttemptID: r.AttemptID, URLExpiry: mediaURLExpiry(a, b.nowUTC())})
		if e == nil {
			stage = "media_cache"
		}
		if e == nil {
			var s os.FileInfo
			s, e = tmp.Stat()
			if e == nil && s.Size() > b.cfg.MaxMedia {
				e = errMediaBoundExceeded
			}
		}
		if e == nil {
			e = tmp.Sync()
		}
		ce := tmp.Close()
		if e == nil {
			e = ce
		}
		if e != nil {
			return e
		}
		if e = os.Rename(name, local); e != nil {
			return e
		}
		if e = syncDir(b.spool.dir); e != nil {
			return e
		}
		f, err = os.Open(local)
	}
	if err != nil {
		return err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if st.Size() > b.cfg.MaxMedia {
		return errMediaBoundExceeded
	}
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil {
		return err
	}
	digest := hex.EncodeToString(h.Sum(nil))
	if expected := a.GetFileSHA256(); len(expected) > 0 && digest != hex.EncodeToString(expected) {
		return &stageError{reason: "integrity_hash"}
	}
	if _, err = f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	filename = strings.ReplaceAll(filename, "\\", "/")
	filename = filepath.Base(filename)
	if filename == "." || filename == "/" {
		filename = "attachment"
	}
	if len(filename) > 255 {
		filename = "attachment"
	}
	key := "accounts/" + b.cfg.Account + "/media/" + id
	stage = "s3_upload"
	if b.cfg.StorageBackend == "disk" {
		stage = "media_cache"
		err = persistDisk(ctx, b.cfg, key, f, st.Size(), digest)
	} else {
		_, err = b.s3.PutObject(ctx, b.cfg.S3Bucket, key, f, st.Size(), minio.PutObjectOptions{ContentType: a.GetMimetype(), DisableMultipart: true, UserMetadata: map[string]string{"sha256": digest}})
	}
	if err != nil {
		return err
	}
	r.Message.Media = &media{ObjectKey: key, MIME: a.GetMimetype(), Filename: filename, Size: st.Size(), SHA256: digest}
	r.MediaPending = false
	r.ArchiveError = ""
	stage = "spool_persist"
	if err := b.spool.save(*r, false); err != nil {
		b.stopForSpoolFailure()
		return err
	}
	return nil
}

var _ http.Handler = (*bridge)(nil)

func originalProto(e *events.Message) ([]byte, error) {
	m := e.RawMessage
	if m == nil {
		m = e.Message
	}
	return proto.MarshalOptions{Deterministic: true}.Marshal(m)
}
