package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

type media struct {
	ObjectKey string `json:"object_key"`
	MIME      string `json:"mime_type"`
	Filename  string `json:"filename"`
	Size      int64  `json:"size"`
	SHA256    string `json:"sha256"`
}
type message struct {
	Account          string         `json:"account_id"`
	ID               string         `json:"message_id"`
	Chat             string         `json:"chat_jid"`
	ChatName         string         `json:"chat_name"`
	Sender           string         `json:"sender_jid"`
	SenderName       string         `json:"sender_name"`
	Timestamp        time.Time      `json:"timestamp"`
	FromMe           bool           `json:"is_from_me"`
	Text             string         `json:"text"`
	MessageType      string         `json:"message_type,omitempty"`
	Content          map[string]any `json:"content,omitempty"`
	ReplyTo          *replyContext  `json:"reply_to,omitempty"`
	IsForwarded      bool           `json:"is_forwarded,omitempty"`
	ForwardingScore  int            `json:"forwarding_score,omitempty"`
	ContentTruncated bool           `json:"content_truncated,omitempty"`
	Media            *media         `json:"media"`
}
type record struct {
	ProtocolOnly      bool           `json:"protocol_only,omitempty"`
	ItemID            string         `json:"item_id,omitempty"`
	AttemptID         string         `json:"attempt_id,omitempty"`
	MediaAttempts     uint32         `json:"media_attempts,omitempty"`
	Recovery          *mediaRecovery `json:"media_recovery,omitempty"`
	QuarantineReason  string         `json:"quarantine_reason,omitempty"`
	Requeue           *requeueAudit  `json:"requeue,omitempty"`
	Message           message        `json:"message"`
	Update            *messageUpdate `json:"update,omitempty"`
	EncryptedReaction bool           `json:"encrypted_reaction,omitempty"`
	InvalidUpdate     bool           `json:"invalid_update,omitempty"`
	TransportID       string         `json:"transport_id,omitempty"`
	RawProto          []byte         `json:"raw_protobuf,omitempty"`
	Source            string         `json:"source,omitempty"`
	Proto             []byte         `json:"protobuf"`
	MediaPending      bool           `json:"media_pending"`
	ArchiveError      string         `json:"archive_error,omitempty"`
	CapturedAt        time.Time      `json:"captured_at,omitempty"`
	Retry             *retryState    `json:"retry,omitempty"`
}
type spool struct {
	dir, account  string
	mu            sync.Mutex
	lock          *os.File
	healthKnown   bool
	queueTimes    map[string]time.Time
	queueState    map[string]queueSnapshot
	writeRecord   func(string, string, []byte) error
	writeRetained func(string, string, []byte) error
	retainedCount int
	completed     [retryLaneCount]uint64
	lastSuccess   *time.Time
}

func newSpool(dir, account string) (*spool, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	f, e := os.OpenFile(filepath.Join(dir, ".lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, errors.New("spool already in use")
	}
	owner := filepath.Join(dir, "account")
	if data, e := os.ReadFile(owner); e == nil {
		if string(data) != account {
			f.Close()
			return nil, errors.New("spool account mismatch")
		}
	} else if !os.IsNotExist(e) {
		f.Close()
		return nil, e
	} else if e = atomicWrite(dir, "account", []byte(account)); e != nil {
		f.Close()
		return nil, e
	}
	s := &spool{dir: dir, account: account, lock: f, queueTimes: make(map[string]time.Time), queueState: make(map[string]queueSnapshot)}
	// Startup scans once; heartbeats read only this synchronized in-memory view.
	if names, err := s.pending(); err == nil {
		s.healthKnown = true
		for _, name := range names {
			r, err := s.read(name)
			if err == nil {
				s.queueTimes[name] = r.CapturedAt
				s.queueState[name] = snapshot(r)
			} else {
				s.queueTimes[name] = time.Time{}
				s.queueState[name] = queueSnapshot{}
			}
		}
	}
	if entries, err := os.ReadDir(filepath.Join(dir, "retained")); err == nil {
		for _, v := range entries {
			if v.Type().IsRegular() && recordPattern.MatchString(v.Name()) {
				s.retainedCount++
			}
		}
	} else if !os.IsNotExist(err) {
		s.close()
		return nil, err
	}
	return s, nil
}
func (s *spool) close() { _ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN); _ = s.lock.Close() }
func recordID(m message) string {
	b, _ := json.Marshal([]string{m.Account, m.Chat, m.ID})
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}
func atomicWrite(dir, name string, b []byte) error {
	f, e := os.CreateTemp(dir, ".tmp-")
	if e != nil {
		return e
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(tmp, filepath.Join(dir, name)); e != nil {
		return e
	}
	return syncDir(dir)
}
func syncDir(dir string) error {
	d, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
func (s *spool) save(r record, initial bool) error {
	if r.Message.Account != s.account || r.Message.ID == "" || r.Message.Chat == "" || !validUpdateBinding(r) {
		return errors.New("invalid message binding")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	name := recordID(r.Message) + ".json"
	if err := s.retainLocked(r); err != nil {
		return err
	}
	if initial {
		if _, e := os.Stat(filepath.Join(s.dir, name)); e == nil {
			return nil
		} else if !os.IsNotExist(e) {
			return e
		}
	}
	if r.ItemID == "" {
		r.ItemID = opaqueID()
	}
	if initial && r.CapturedAt.IsZero() {
		r.CapturedAt = time.Now().UTC()
	}
	b, e := json.Marshal(r)
	if e != nil {
		return e
	}
	write := s.writeRecord
	if write == nil {
		write = atomicWrite
	}
	if e = write(s.dir, name, b); e != nil {
		return e
	}
	s.queueTimes[name] = r.CapturedAt
	s.queueState[name] = snapshot(r)
	return nil
}
func (s *spool) pending() ([]string, error) {
	entries, e := os.ReadDir(s.dir)
	if e != nil {
		return nil, e
	}
	names := []string{}
	for _, v := range entries {
		if v.Type().IsRegular() && recordPattern.MatchString(v.Name()) {
			names = append(names, v.Name())
		}
	}
	return names, nil
}
func (s *spool) read(name string) (record, error) {
	var r record
	if !recordPattern.MatchString(name) {
		return r, errors.New("invalid record")
	}
	b, e := os.ReadFile(filepath.Join(s.dir, name))
	if e != nil {
		return r, e
	}
	e = json.Unmarshal(b, &r)
	if e == nil && (r.Message.Account != s.account || name != recordID(r.Message)+".json" || !validUpdateBinding(r)) {
		e = errors.New("record account mismatch")
	}
	return r, e
}
func (s *spool) remove(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !recordPattern.MatchString(name) {
		return errors.New("invalid record")
	}
	r, err := s.read(name)
	if err != nil {
		return err
	}
	if err = s.retainLocked(r); err != nil {
		return err
	}
	if e := os.Remove(filepath.Join(s.dir, name)); e != nil {
		return e
	}
	if e := syncDir(s.dir); e != nil {
		return e
	}
	delete(s.queueTimes, name)
	delete(s.queueState, name)
	return nil
}

// Unknown legacy/corrupt capture times make the oldest queue age unknown. The
// original WhatsApp author timestamp is never used as queue waiting time.
func (s *spool) queueHealth() (any, any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.healthKnown {
		return nil, nil
	}
	var oldest time.Time
	for _, captured := range s.queueTimes {
		if captured.IsZero() {
			return len(s.queueTimes), nil
		}
		if oldest.IsZero() || captured.Before(oldest) {
			oldest = captured
		}
	}
	if oldest.IsZero() {
		return len(s.queueTimes), nil
	}
	return len(s.queueTimes), oldest.UTC().Format(time.RFC3339Nano)
}
