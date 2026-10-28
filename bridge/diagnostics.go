package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net"
	"os"
	"syscall"
	"time"

	"github.com/minio/minio-go/v7"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
)

type stageError struct {
	stage, reason string
	status        int
	cause         error
}

func (e *stageError) Error() string { return "archive operation failed" }
func (e *stageError) Unwrap() error { return e.cause }
func safeStage(s string) string {
	switch s {
	case "media_recovery", "quarantine", "manual_requeue", "update_decode", "media_metadata", "media_cache", "media_download", "s3_upload", "spool_persist", "ingestion", "cleanup":
		return s
	}
	return "unknown"
}
func safeReason(s string) string {
	switch s {
	case "recovery_timeout", "recovery_unavailable", "recovery_no_key", "recovery_rejected", "recovery_invalid_path", "recovery_exhausted", "download_exhausted", "requeued", "unsupported_archive", "ok", "unknown", "invalid_metadata", "missing_metadata", "bound_exceeded", "integrity_hash", "integrity_hmac", "integrity_ciphertext", "too_short", "missing_url", "unsupported_media", "nothing_downloadable", "http_status", "timeout", "cancelled", "network", "tls", "filesystem", "storage_full", "permission", "not_found", "encoding", "request", "s3_access_denied", "s3_not_found", "s3_throttled", "s3_invalid_request", "s3_unavailable", "protocol_denied", "protocol_rate_limited", "protocol_unavailable", "protocol_error", "disconnected":
		return s
	}
	return "unknown"
}
func safeStatus(n int) int {
	if n >= 100 && n <= 599 {
		return n
	}
	return 0
}
func classifyStage(stage string, err error) (string, string, int) {
	stage = safeStage(stage)
	if err == nil {
		return stage, "ok", 0
	}
	var wrapped *stageError
	// Outer stage is authoritative, while inner metadata reasons are preserved.
	if errors.As(err, &wrapped) && wrapped.stage != "" {
		stage = safeStage(wrapped.stage)
	}
	for current := err; current != nil; current = errors.Unwrap(current) {
		if v, ok := current.(*stageError); ok && v.reason != "" {
			return stage, safeReason(v.reason), safeStatus(v.status)
		}
	}
	if errors.Is(err, context.Canceled) {
		return stage, "cancelled", 0
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return stage, "timeout", 0
	}
	var httpErr whatsmeow.DownloadHTTPError
	if errors.As(err, &httpErr) && httpErr.Response != nil {
		return stage, "http_status", safeStatus(httpErr.StatusCode)
	}
	var httpPtr *whatsmeow.DownloadHTTPError
	if errors.As(err, &httpPtr) && httpPtr != nil && httpPtr.Response != nil {
		return stage, "http_status", safeStatus(httpPtr.StatusCode)
	}
	// IQ codes belong to the WhatsApp protocol, not HTTP. Neither its text nor
	// XML nodes are safe diagnostics, even when the request precedes a download.
	var iq *whatsmeow.IQError
	if errors.As(err, &iq) {
		reason := "protocol_error"
		switch {
		case iq.Code == 401 || iq.Code == 403:
			reason = "protocol_denied"
		case iq.Code == 419 || iq.Code == 429:
			reason = "protocol_rate_limited"
		case iq.Code >= 500 && iq.Code <= 599:
			reason = "protocol_unavailable"
		}
		return stage, reason, 0
	}
	var disconnected *whatsmeow.DisconnectedError
	if errors.As(err, &disconnected) || errors.Is(err, whatsmeow.ErrNotConnected) {
		return stage, "disconnected", 0
	}
	for _, v := range []struct {
		e error
		r string
	}{
		{errMediaBoundExceeded, "bound_exceeded"}, {whatsmeow.ErrNoURLPresent, "missing_url"}, {whatsmeow.ErrTooShortFile, "too_short"},
		{whatsmeow.ErrInvalidMediaHMAC, "integrity_hmac"}, {whatsmeow.ErrInvalidMediaEncSHA256, "integrity_ciphertext"},
		{whatsmeow.ErrInvalidMediaSHA256, "integrity_hash"}, {whatsmeow.ErrInvalidUnencryptedMediaSHA256, "integrity_hash"},
		{whatsmeow.ErrFileLengthMismatch, "integrity_hash"}, {whatsmeow.ErrUnknownMediaType, "unsupported_media"}, {whatsmeow.ErrNothingDownloadableFound, "nothing_downloadable"},
	} {
		if errors.Is(err, v.e) {
			return stage, v.r, 0
		}
	}
	var verification *tls.CertificateVerificationError
	var authority x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	var host x509.HostnameError
	var record tls.RecordHeaderError
	if errors.As(err, &verification) || errors.As(err, &authority) || errors.As(err, &invalid) || errors.As(err, &host) || errors.As(err, &record) {
		return stage, "tls", 0
	}
	var s3 minio.ErrorResponse
	if errors.As(err, &s3) {
		reason := "unknown"
		switch s3.Code {
		case "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch":
			reason = "s3_access_denied"
		case "NoSuchBucket", "NoSuchKey":
			reason = "s3_not_found"
		case "SlowDown", "Throttling":
			reason = "s3_throttled"
		case "InvalidArgument", "InvalidRequest":
			reason = "s3_invalid_request"
		case "InternalError", "ServiceUnavailable":
			reason = "s3_unavailable"
		}
		if reason == "unknown" && safeStatus(s3.StatusCode) != 0 {
			reason = "http_status"
		}
		return stage, reason, safeStatus(s3.StatusCode)
	}
	if errors.Is(err, syscall.ENOSPC) || errors.Is(err, syscall.EDQUOT) {
		return stage, "storage_full", 0
	}
	if errors.Is(err, fs.ErrPermission) {
		return stage, "permission", 0
	}
	if errors.Is(err, fs.ErrNotExist) {
		return stage, "not_found", 0
	}
	var path *fs.PathError
	if errors.As(err, &path) {
		return stage, "filesystem", 0
	}
	var network net.Error
	if errors.As(err, &network) {
		if network.Timeout() {
			return stage, "timeout", 0
		}
		return stage, "network", 0
	}
	if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF) {
		return stage, "network", 0
	}
	return stage, "unknown", 0
}
func coarseMediaType(p *waE2E.Message) string {
	switch {
	case p.GetImageMessage() != nil:
		return "image"
	case p.GetVideoMessage() != nil || p.GetPtvMessage() != nil:
		return "video"
	case p.GetAudioMessage() != nil:
		return "audio"
	case p.GetDocumentMessage() != nil:
		return "document"
	case p.GetStickerMessage() != nil:
		return "sticker"
	}
	return "unknown"
}

type diagnostic struct {
	PathFamily         string     `json:"path_family,omitempty"`
	PathValidation     string     `json:"path_validation,omitempty"`
	NotificationResult string     `json:"notification_result,omitempty"`
	RecoveryBranch     string     `json:"recovery_branch,omitempty"`
	AttachmentState    string     `json:"attachment_state,omitempty"`
	ItemID             string     `json:"item_id,omitempty"`
	AttemptID          string     `json:"attempt_id,omitempty"`
	URLExpiry          string     `json:"url_expiry,omitempty"`
	Event              string     `json:"event"`
	Stage              string     `json:"stage"`
	Reason             string     `json:"reason"`
	HTTPStatus         int        `json:"http_status"`
	ElapsedMS          int64      `json:"elapsed_ms"`
	PartialBytes       int64      `json:"partial_bytes"`
	ExpectedBytes      uint64     `json:"expected_bytes"`
	MediaType          string     `json:"media_type"`
	Attempt            uint32     `json:"attempt"`
	NextRetryAt        *time.Time `json:"next_retry_at,omitempty"`
}

func (b *bridge) emitDiagnostic(d diagnostic) {
	d.PathFamily = safeDiagnosticValue(d.PathFamily, "mms", "v", "o1_v", "other")
	d.PathValidation = safeDiagnosticValue(d.PathValidation, "accepted", "invalid_length", "unsafe_characters", "unsupported_family", "invalid_url", "invalid_query", "unsafe_path")
	d.NotificationResult = safeDiagnosticValue(d.NotificationResult, "success", "general_error", "not_found", "decryption_error", "missing", "other")
	d.RecoveryBranch = safeDiagnosticValue(d.RecoveryBranch, "retained_attachment", "routing", "sender_unavailable", "request_attempt", "phone_error", "decrypt_error", "stanza_mismatch", "phone_result", "path_validation", "marshal", "refreshed")
	d.AttachmentState = safeDiagnosticValue(d.AttachmentState, "protobuf_invalid", "attachment_missing", "key_missing", "key_invalid_length", "available")
	if !opaquePattern.MatchString(d.ItemID) {
		d.ItemID = ""
	}
	if !opaquePattern.MatchString(d.AttemptID) {
		d.AttemptID = ""
	}
	if d.URLExpiry != "past" && d.URLExpiry != "future" && d.URLExpiry != "present" && d.URLExpiry != "absent" {
		d.URLExpiry = "unknown"
	}
	if d.Event != "media_recovery" && d.Event != "manual_requeue" && d.Event != "quarantine" && d.Event != "media_download" && d.Event != "spool_attempt" {
		d.Event = "spool_attempt"
	}
	d.Stage = safeStage(d.Stage)
	d.Reason = safeReason(d.Reason)
	d.HTTPStatus = safeStatus(d.HTTPStatus)
	switch d.MediaType {
	case "image", "video", "audio", "document", "sticker":
	default:
		d.MediaType = "unknown"
	}
	if d.ElapsedMS < 0 {
		d.ElapsedMS = 0
	}
	if d.PartialBytes < 0 {
		d.PartialBytes = 0
	}
	w := b.diagnosticWriter
	if w == nil {
		w = os.Stdout
	}
	// Marshal once and write once: ordinary log prefixes cannot corrupt JSON lines.
	data, err := json.Marshal(d)
	if err == nil {
		data = append(data, '\n')
		b.diagnosticMu.Lock()
		_, _ = w.Write(data)
		b.diagnosticMu.Unlock()
	}
}

// Diagnostic contexts contain only fixed vocabulary, never provider strings.
// Preserve omission for unrelated events, but make an unsafe supplied value explicit.
func safeDiagnosticValue(value string, allowed ...string) string {
	if value == "" {
		return ""
	}
	for _, candidate := range allowed {
		if value == candidate {
			return value
		}
	}
	return "unknown"
}
