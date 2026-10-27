package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

func needsRetention(r record) bool {
	return r.ProtocolOnly || r.Message.MessageType == "unknown" || (r.Update != nil && r.Update.Edit != nil && r.Update.Edit.MessageType == "unknown")
}

// Retained payloads are a separate, private recovery archive, never queue input
// or an API response. Both original and normalized protobuf survive queue ACK.
func (s *spool) retainLocked(r record) error {
	if !needsRetention(r) {
		return nil
	}
	raw := r.RawProto
	source := "raw_message"
	if len(raw) == 0 {
		raw = r.Proto
		source = "normalized_only"
	}
	data, err := json.Marshal(struct {
		Version     int    `json:"version"`
		Account     string `json:"account_id"`
		Chat        string `json:"chat_jid"`
		MessageID   string `json:"message_id"`
		TransportID string `json:"transport_id,omitempty"`
		RawSource   string `json:"raw_source"`
		Raw         []byte `json:"raw_protobuf"`
		Normalized  []byte `json:"normalized_protobuf"`
	}{1, r.Message.Account, r.Message.Chat, r.Message.ID, r.TransportID, source, raw, r.Proto})
	if err != nil {
		return err
	}
	hash := sha256.Sum256(data)
	name := hex.EncodeToString(hash[:]) + ".json"
	dir := filepath.Join(s.dir, "retained")
	if err = os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("invalid retention directory")
	}
	if err = os.Chmod(dir, 0700); err != nil {
		return err
	}
	if err = syncDir(s.dir); err != nil {
		return err
	}
	if info, err = os.Lstat(filepath.Join(dir, name)); err == nil {
		if !info.Mode().IsRegular() {
			return errors.New("invalid retained payload")
		}
		existing, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		if string(existing) != string(data) {
			return errors.New("retained payload mismatch")
		}
		return os.Chmod(filepath.Join(dir, name), 0600)
	} else if !os.IsNotExist(err) {
		return err
	}
	write := s.writeRetained
	if write == nil {
		write = atomicWrite
	}
	if err = write(dir, name, data); err != nil {
		return err
	}
	s.retainedCount++
	return nil
}
