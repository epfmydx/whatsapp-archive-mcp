package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

var opaquePattern = regexp.MustCompile(`^[a-f0-9]{32}$`)

func opaqueID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic("random source unavailable")
	}
	return hex.EncodeToString(b[:])
}

type mediaRecovery struct {
	Attempts    uint32    `json:"attempts"`
	RequestID   string    `json:"request_id"`
	RequestedAt time.Time `json:"requested_at"`
	Deadline    time.Time `json:"deadline"`
	State       string    `json:"state"` // awaiting, refreshed, failed: never resend an awaiting reservation.
	// The exact key is private durable state; it binds responses to the reserved original.
	MediaKey []byte `json:"media_key"`
}
type requeueAudit struct {
	RequestIDs []string  `json:"request_ids"`
	Count      uint32    `json:"count"`
	LastAt     time.Time `json:"last_at"`
}
type recoveryResponse struct {
	Event      *events.MediaRetry
	ReceivedAt time.Time
}

func recoveryBinding(m message) string { return m.Chat + "\x00" + m.ID }
func (b *bridge) initRecoveryLocked() {
	if b.recoveryActive == nil {
		b.recoveryActive = make(map[string]string)
	}
	if b.recoveryEvents == nil {
		b.recoveryEvents = make(chan recoveryResponse, 64)
	}
}

// Receiver callbacks only copy bounded data for a reservation already known to
// this account's worker. They do no filesystem, downloader or phone I/O.
func (b *bridge) queueMediaRetry(e *events.MediaRetry) {
	if e == nil || len(e.Ciphertext) > 64*1024 || len(e.IV) > 32 {
		return
	}
	// Slow persistence in the worker must never hold up the SDK receiver.
	// Contended responses remain an explicit timeout/review outcome.
	if !b.recoveryMu.TryLock() {
		return
	}
	defer b.recoveryMu.Unlock()
	b.initRecoveryLocked()
	if _, ok := b.recoveryActive[e.ChatID.String()+"\x00"+e.MessageID]; !ok {
		return
	}
	v := *e
	v.Ciphertext = append([]byte(nil), e.Ciphertext...)
	v.IV = append([]byte(nil), e.IV...)
	if e.Error != nil {
		err := *e.Error
		v.Error = &err
	}
	select {
	case b.recoveryEvents <- recoveryResponse{&v, b.nowUTC()}:
	default:
	}
	select {
	case b.wake <- struct{}{}:
	default:
	}
}
func (b *bridge) registerRecovery(r record, name string) {
	b.recoveryMu.Lock()
	defer b.recoveryMu.Unlock()
	b.initRecoveryLocked()
	b.recoveryActive[recoveryBinding(r.Message)] = name
}
func (b *bridge) forgetRecovery(r record) {
	b.recoveryMu.Lock()
	defer b.recoveryMu.Unlock()
	delete(b.recoveryActive, recoveryBinding(r.Message))
}
func (b *bridge) quarantine(r *record, reason string) bool {
	r.QuarantineReason = safeReason(reason)
	if r.Recovery != nil && r.Recovery.State == "awaiting" {
		r.Recovery.State = "failed"
	}
	if !b.saveRetry(*r) {
		return false
	}
	b.forgetRecovery(*r)
	b.emitDiagnostic(diagnostic{Event: "quarantine", Stage: "quarantine", Reason: r.QuarantineReason, ItemID: r.ItemID, AttemptID: r.AttemptID})
	return true
}
func (b *bridge) recoveryTimeout() time.Duration {
	if b.cfg.MediaRecoveryTimeout > 0 {
		return b.cfg.MediaRecoveryTimeout
	}
	return 2 * time.Minute
}
func (b *bridge) recoveryInterval() time.Duration {
	if b.cfg.MediaRecoveryMinInterval > 0 {
		return b.cfg.MediaRecoveryMinInterval
	}
	return 30 * time.Second
}
func (b *bridge) downloadLimit() uint32 {
	if b.cfg.MediaDownloadMaxAttempts > 0 {
		return b.cfg.MediaDownloadMaxAttempts
	}
	return 8
}
func (b *bridge) recoveryLimit() uint32 {
	if b.cfg.MediaRecoveryMaxAttempts > 0 {
		return b.cfg.MediaRecoveryMaxAttempts
	}
	return 1
}
func needsRecovery(r record) bool {
	if r.Retry == nil || (r.Recovery != nil && r.Recovery.State == "refreshed") {
		return false
	}
	s := r.Retry
	return s.Stage == "media_download" && (s.HTTPStatus == 403 || s.HTTPStatus == 404 || s.HTTPStatus == 410 || s.Reason == "missing_url")
}
func recoveryAttachment(r record) (*waE2E.Message, attachment, error) {
	var p waE2E.Message
	if err := proto.Unmarshal(r.Proto, &p); err != nil {
		return nil, nil, err
	}
	a, _, _ := attachmentOf(&p)
	if a == nil {
		return nil, nil, errors.New("missing attachment")
	}
	return &p, a, nil
}
func mediaInfo(m message) (*types.MessageInfo, error) {
	chat, err := types.ParseJID(m.Chat)
	if err != nil || chat.IsEmpty() {
		return nil, errors.New("invalid chat")
	}
	sender, err := types.ParseJID(m.Sender)
	if err != nil || sender.IsEmpty() {
		return nil, errors.New("invalid sender")
	}
	// Broadcast/newsletter routing needs metadata not retained by legacy records.
	if chat.Server != types.GroupServer && chat.Server != types.DefaultUserServer && chat.Server != types.HiddenUserServer {
		return nil, errors.New("unsupported chat")
	}
	return &types.MessageInfo{ID: m.ID, MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsFromMe: m.FromMe, IsGroup: chat.Server == types.GroupServer}}, nil
}

// A URL is never accepted from a retry notification. Only a bounded WhatsApp
// direct path can enter the existing SDK downloader's fixed-host path API.
func hasControl(s string) bool {
	for _, r := range s {
		if r < 32 || r == 127 {
			return true
		}
	}
	return false
}
func validRecoveryPath(path string) bool {
	if hasControl(path) {
		return false
	}
	if len(path) < 2 || len(path) > 8192 || (!strings.HasPrefix(path, "/mms/") && !strings.HasPrefix(path, "/v/")) || strings.HasPrefix(path, "//") || strings.ContainsAny(path, "\r\n\x00\\#") {
		return false
	}
	u, err := url.Parse(path)
	if err != nil || u.IsAbs() || u.Host != "" || u.User != nil || u.Fragment != "" {
		return false
	}
	query, err := url.QueryUnescape(u.RawQuery)
	if err != nil || hasControl(query) {
		return false
	}
	decoded, err := url.PathUnescape(u.Path)
	if err != nil || strings.Contains(decoded, "..") || strings.Contains(decoded, "//") || strings.Contains(decoded, "\\") || hasControl(decoded) {
		return false
	}
	return true
}
func setRecoveryPath(p *waE2E.Message, path string) {
	// Preserve the signed query exactly, along with every integrity/key field.
	switch {
	case p.ImageMessage != nil:
		p.ImageMessage.DirectPath = proto.String(path)
	case p.VideoMessage != nil:
		p.VideoMessage.DirectPath = proto.String(path)
	case p.PtvMessage != nil:
		p.PtvMessage.DirectPath = proto.String(path)
	case p.AudioMessage != nil:
		p.AudioMessage.DirectPath = proto.String(path)
	case p.DocumentMessage != nil:
		p.DocumentMessage.DirectPath = proto.String(path)
	case p.StickerMessage != nil:
		p.StickerMessage.DirectPath = proto.String(path)
	}
}
func mediaURLExpiry(a attachment, now time.Time) string {
	// Existence is diagnostic context only, never expiry proof or retry policy.
	v, ok := a.(interface{ GetDirectPath() string })
	if !ok {
		return "unknown"
	}
	if len(v.GetDirectPath()) > 8192 {
		return "unknown"
	}
	u, err := url.Parse(v.GetDirectPath())
	if err != nil {
		return "unknown"
	}
	q, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return "unknown"
	}
	var expiry string
	for k, values := range q {
		if strings.EqualFold(k, "oe") {
			if expiry != "" || len(values) != 1 || len(values[0]) == 0 || len(values[0]) > 16 {
				return "unknown"
			}
			expiry = values[0]
		}
	}
	if expiry != "" {
		seconds, err := strconv.ParseUint(expiry, 16, 63)
		if err != nil {
			return "unknown"
		}
		if int64(seconds) <= now.Unix() {
			return "past"
		}
		return "future"
	}
	for k := range q {
		if strings.EqualFold(k, "ext") || strings.EqualFold(k, "expires") || strings.EqualFold(k, "expiration") {
			return "present"
		}
	}
	return "absent"
}

// reserveRecovery consumes the durable item and global budgets before any
// protocol request. Crashes and ambiguous send failures never replay a receipt.
func (b *bridge) reserveRecovery(ctx context.Context, r *record, name string) bool {
	if !b.cfg.MediaRecoveryEnabled {
		return false
	}
	if r.Recovery != nil && r.Recovery.Attempts >= b.recoveryLimit() {
		b.quarantine(r, "recovery_exhausted")
		return true
	}
	_, a, err := recoveryAttachment(*r)
	if err != nil || len(a.GetMediaKey()) != 32 {
		b.quarantine(r, "recovery_no_key")
		return true
	}
	info, err := mediaInfo(r.Message)
	if err != nil {
		b.quarantine(r, "recovery_rejected")
		return true
	}
	if b.sendMediaRetry == nil {
		b.quarantine(r, "recovery_unavailable")
		return true
	}
	b.recoveryMu.Lock()
	b.initRecoveryLocked()
	if !b.recoveryClockLoaded {
		var state struct {
			NextAt time.Time `json:"next_at"`
		}
		data, err := os.ReadFile(filepath.Join(b.spool.dir, "media-recovery-clock.json"))
		if err == nil {
			err = json.Unmarshal(data, &state)
			if err == nil && state.NextAt.IsZero() {
				err = errors.New("invalid recovery clock")
			}
		}
		if err != nil && !os.IsNotExist(err) {
			b.recoveryMu.Unlock()
			b.stopForSpoolFailure()
			return true
		}
		b.recoveryNext = state.NextAt
		b.recoveryClockLoaded = true
	}
	now := b.nowUTC()
	if b.recoveryNext.After(now) {
		next := b.recoveryNext
		b.recoveryMu.Unlock()
		if !r.Retry.NextAttemptAt.Equal(next) {
			r.Retry.NextAttemptAt = next
			b.saveRetry(*r)
		}
		return true
	}
	attempts := uint32(1)
	if r.Recovery != nil {
		attempts = r.Recovery.Attempts + 1
	}
	r.Recovery = &mediaRecovery{Attempts: attempts, RequestID: opaqueID(), RequestedAt: now, Deadline: now.Add(b.recoveryTimeout()), State: "awaiting", MediaKey: append([]byte(nil), a.GetMediaKey()...)}
	r.AttemptID = r.Recovery.RequestID
	if !b.saveRetry(*r) {
		b.recoveryMu.Unlock()
		return true
	}
	next := now.Add(b.recoveryInterval())
	data, _ := json.Marshal(struct {
		NextAt time.Time `json:"next_at"`
	}{next})
	if err := atomicWrite(b.spool.dir, "media-recovery-clock.json", data); err != nil {
		b.recoveryMu.Unlock()
		b.stopForSpoolFailure()
		return true
	}
	b.recoveryNext = next
	b.recoveryActive[recoveryBinding(r.Message)] = name
	b.recoveryMu.Unlock()
	if b.stopped(ctx) {
		return true
	}
	sendCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	err = b.sendMediaRetry(sendCtx, info, append([]byte(nil), a.GetMediaKey()...))
	cancel()
	reason := "ok"
	if err != nil {
		_, reason, _ = classifyStage("media_recovery", err)
	}
	// A valid response may have arrived before a send error. Keep the reservation
	// awaiting until its deadline and consume queued responses first next sweep.
	b.emitDiagnostic(diagnostic{Event: "media_recovery", Stage: "media_recovery", Reason: reason, ItemID: r.ItemID, AttemptID: r.AttemptID, Attempt: attempts})
	return true
}
func retryResponseMatches(r record, response recoveryResponse) bool {
	e := response.Event
	v := r.Recovery
	if e == nil || v == nil || v.State != "awaiting" || r.QuarantineReason != "" || !r.MediaPending || response.ReceivedAt.After(v.Deadline) || response.ReceivedAt.Before(v.RequestedAt) {
		return false
	}
	// Provider timestamps have second resolution; unknown timestamps fail closed.
	if e.Timestamp.IsZero() || e.Timestamp.Before(v.RequestedAt.Truncate(time.Second)) || e.Timestamp.After(v.Deadline.Truncate(time.Second)) {
		return false
	}
	info, err := mediaInfo(r.Message)
	if err != nil {
		return false
	}
	if e.MessageID != r.Message.ID || e.ChatID != info.Chat || e.FromMe != info.IsFromMe {
		return false
	}
	if info.IsGroup {
		if e.SenderID != info.Sender {
			return false
		}
	} else if !e.SenderID.IsEmpty() {
		return false
	}
	_, a, err := recoveryAttachment(r)
	return err == nil && len(v.MediaKey) == 32 && subtle.ConstantTimeCompare(a.GetMediaKey(), v.MediaKey) == 1
}
func (b *bridge) applyRecoveryResponses() {
	for {
		b.recoveryMu.Lock()
		b.initRecoveryLocked()
		var response recoveryResponse
		select {
		case response = <-b.recoveryEvents:
		default:
			b.recoveryMu.Unlock()
			return
		}
		name := b.recoveryActive[response.Event.ChatID.String()+"\x00"+response.Event.MessageID]
		b.recoveryMu.Unlock()
		if name == "" {
			continue
		}
		r, err := b.spool.read(name)
		if err != nil || !retryResponseMatches(r, response) {
			continue
		}
		notification, err := whatsmeow.DecryptMediaRetryNotification(response.Event, r.Recovery.MediaKey)
		if err != nil { // Unauthenticated/wrong-key ciphertext cannot change item state.
			if errors.Is(err, whatsmeow.ErrMediaNotAvailableOnPhone) {
				b.quarantine(&r, "recovery_unavailable")
			}
			continue
		}
		if notification.GetStanzaID() != "" && notification.GetStanzaID() != r.Message.ID {
			continue
		}
		if notification.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
			b.quarantine(&r, "recovery_rejected")
			continue
		}
		if !validRecoveryPath(notification.GetDirectPath()) {
			b.quarantine(&r, "recovery_invalid_path")
			continue
		}
		p, _, err := recoveryAttachment(r)
		if err != nil {
			b.quarantine(&r, "recovery_rejected")
			continue
		}
		setRecoveryPath(p, notification.GetDirectPath())
		r.Proto, err = proto.Marshal(p)
		if err != nil {
			b.quarantine(&r, "recovery_rejected")
			continue
		}
		r.Recovery.State = "refreshed"
		r.MediaAttempts = 0
		r.Retry.NextAttemptAt = b.nowUTC()
		if !b.saveRetry(r) {
			return
		}
		b.forgetRecovery(r)
		b.emitDiagnostic(diagnostic{Event: "media_recovery", Stage: "media_recovery", Reason: "ok", ItemID: r.ItemID, AttemptID: r.AttemptID})
	}
}
func (b *bridge) serveRequeue(w http.ResponseWriter, req *http.Request) {
	if b.cfg.Token == "" || subtle.ConstantTimeCompare([]byte(req.Header.Get("Authorization")), []byte("Bearer "+b.cfg.Token)) != 1 {
		http.Error(w, "unauthorized", 401)
		return
	}
	if req.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		http.Error(w, "method not allowed", 405)
		return
	}
	var input struct {
		ItemID    string `json:"item_id"`
		RequestID string `json:"request_id"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, req.Body, 1024))
	dec.DisallowUnknownFields()
	if dec.Decode(&input) != nil || dec.Decode(new(any)) != io.EOF || !opaquePattern.MatchString(input.ItemID) || !opaquePattern.MatchString(input.RequestID) {
		http.Error(w, "invalid request", 400)
		return
	}
	// Serialize with media worker before resolving the opaque ID. No paths/JIDs
	// enter this route, and every requeue is a durable, idempotent local action.
	if !b.drainMu[mediaLane].TryLock() {
		w.Header().Set("Retry-After", "10")
		http.Error(w, "media worker busy", 503)
		return
	}
	defer b.drainMu[mediaLane].Unlock()
	if b.stopped(req.Context()) {
		http.Error(w, "unavailable", 503)
		return
	}
	names, err := b.spool.pending()
	if err != nil {
		http.Error(w, "unavailable", 503)
		return
	}
	for _, name := range names {
		r, err := b.spool.read(name)
		if err != nil || r.ItemID != input.ItemID {
			continue
		}
		if r.Requeue != nil {
			for _, id := range r.Requeue.RequestIDs {
				if id == input.RequestID {
					w.WriteHeader(204)
					return
				}
			}
		}
		if r.QuarantineReason == "" {
			http.Error(w, "item is not quarantined", 409)
			return
		}
		if r.Requeue == nil {
			r.Requeue = &requeueAudit{}
		}
		// Bounded audit never forgets an idempotency key: require explicit private
		// operator repair after 64 manual cycles instead of replaying an old key.
		if len(r.Requeue.RequestIDs) >= 64 {
			http.Error(w, "manual cycle limit", 409)
			return
		}
		r.Requeue.RequestIDs = append(r.Requeue.RequestIDs, input.RequestID)
		r.Requeue.Count++
		r.Requeue.LastAt = b.nowUTC()
		r.QuarantineReason = ""
		r.MediaAttempts = 0
		r.Recovery = nil
		if r.Retry == nil {
			r.Retry = &retryState{}
		}
		r.Retry.NextAttemptAt = b.nowUTC()
		r.AttemptID = opaqueID()
		if !b.saveRetry(r) {
			http.Error(w, "unavailable", 503)
			return
		}
		b.emitDiagnostic(diagnostic{Event: "manual_requeue", Stage: "manual_requeue", Reason: "requeued", ItemID: r.ItemID, AttemptID: r.AttemptID})
		select {
		case b.wake <- struct{}{}:
		default:
		}
		w.WriteHeader(204)
		return
	}
	http.Error(w, "not found", 404)
}
