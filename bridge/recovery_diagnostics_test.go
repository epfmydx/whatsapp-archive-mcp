package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func readSingleDiagnostic(t *testing.T, out *bytes.Buffer) diagnostic {
	t.Helper()
	var d diagnostic
	if lines := bytes.Count(out.Bytes(), []byte("\n")); lines != 1 {
		t.Fatalf("want one diagnostic, got %d", lines)
	}
	if err := json.Unmarshal(out.Bytes(), &d); err != nil {
		t.Fatal(err)
	}
	return d
}

func TestRecoveryPathFamiliesAndSingleDecode(t *testing.T) {
	for _, tc := range []struct{ path, family string }{
		{"/mms/synthetic.enc?ext=1&oh=SECRET%2B%2F&x=1&x=2", "mms"},
		{"/v/t62/synthetic.enc?oe=ABCD&oh=SECRET+value", "v"},
		{"/o1/v/t24/synthetic.enc?oh=SECRET%2Bsigned%2Fquery&oe=AA", "o1_v"},
		// A literal escaped percent is decoded once for safety inspection.
		{"/o1/v/t24/synthetic%25literal.enc?oh=SECRET%252B", "o1_v"},
	} {
		t.Run(tc.family+tc.path[:4], func(t *testing.T) {
			family, validation := recoveryPathDiagnostic(tc.path)
			if family != tc.family || validation != "accepted" || !validRecoveryPath(tc.path) {
				t.Fatal("supported fixed-host relative path rejected")
			}
			b := newTestBridge(t, t.TempDir())
			now := fixedRetryClock(b)
			name := recoveryFixture(t, b, "path-preservation")
			r := readRecovery(t, b, name)
			p, _, _ := recoveryAttachment(r)
			p.ImageMessage.FileSHA256 = bytes.Repeat([]byte{11}, 32)
			p.ImageMessage.FileEncSHA256 = bytes.Repeat([]byte{12}, 32)
			p.ImageMessage.URL = proto.String("https://synthetic.invalid/old?secret=KEEP")
			p.ImageMessage.Caption = proto.String("private synthetic caption")
			r.Proto, _ = proto.Marshal(p)
			if err := b.spool.save(r, false); err != nil {
				t.Fatal(err)
			}
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
			b.drain(context.Background())
			r = readRecovery(t, b, name)
			var out bytes.Buffer
			b.diagnosticWriter = &out
			b.queueMediaRetry(encryptedRecovery(t, r, *now, tc.path))
			b.applyRecoveryResponses()
			got := readRecovery(t, b, name)
			updated, _, err := recoveryAttachment(got)
			p.ImageMessage.DirectPath = proto.String(tc.path)
			if err != nil || !proto.Equal(updated, p) || got.Recovery.State != "refreshed" || !bytes.Equal(got.RawProto, r.RawProto) {
				t.Fatal("recovery changed fields other than direct path or lost signed bytes")
			}
			d := readSingleDiagnostic(t, &out)
			if d.PathFamily != tc.family || d.PathValidation != "accepted" || d.NotificationResult != "success" || d.RecoveryBranch != "refreshed" {
				t.Fatal("successful recovery context missing")
			}
			for _, private := range []string{"SECRET", "caption", "synthetic.enc", "secret=KEEP", tc.path} {
				if strings.Contains(out.String(), private) {
					t.Fatal("private path/content leaked")
				}
			}
		})
	}
}

func TestRecoveryPathRejectsUnsafeOrUnsupportedInputs(t *testing.T) {
	for _, path := range []string{
		"", "/", "/o1/v", "/o1/other/t24/a", "/o2/v/t24/a", "/other/v/a", "/O1/v/a",
		"https://evil.invalid/o1/v/t24/a", "//evil.invalid/o1/v/a", "///o1/v/a", "o1/v/a", "/o1/v/../a",
		"/o1/v/%2e%2e/a", "/o1/v/a%2F..%2Fb", "/o1/v/%2Fhost/a", "/o1/v/a\\b", "/o1/v/a%5Cb",
		"/o1/v/a#fragment", "/o1/v/%00a", "/o1/v/%1Fa", "/o1/v/%7Fa", "/o1/v/a\na", "/o1/v/%ZZ", "/o1/v/%252e%252e/a", "/o1/v/%252Fhost/a", "/o1/v/%255ca", "/o1/v/%250aa",
		"/o1/v/a?token=%0aSECRET", "/o1/v/a?token=%7f", "/o1/v/a?token=%00", "/o1/v/a?token=%ZZ",
		"/o1/v/" + strings.Repeat("a", 8192),
	} {
		if validRecoveryPath(path) {
			t.Fatal("unsafe or unsupported path accepted")
		}
	}
}

func TestRecoveryReservationDiagnostics(t *testing.T) {
	for _, tc := range []struct{ scenario, reason, branch, attachment string }{
		{"protobuf_invalid", "recovery_no_key", "retained_attachment", "protobuf_invalid"},
		{"protobuf_missing", "recovery_no_key", "retained_attachment", "protobuf_invalid"},
		{"attachment_missing", "recovery_no_key", "retained_attachment", "attachment_missing"},
		{"key_missing", "recovery_no_key", "retained_attachment", "key_missing"},
		{"key_invalid_length", "recovery_no_key", "retained_attachment", "key_invalid_length"},
		{"routing", "recovery_rejected", "routing", ""},
		{"sender_unavailable", "recovery_unavailable", "sender_unavailable", ""},
	} {
		t.Run(tc.scenario, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			fixedRetryClock(b)
			name := recoveryFixture(t, b, "pre-send")
			r := readRecovery(t, b, name)
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error {
				t.Fatal("pre-send failure sent a receipt")
				return nil
			}
			switch tc.scenario {
			case "protobuf_invalid":
				r.Proto = []byte{0xff}
			case "protobuf_missing":
				r.Proto = nil
			case "attachment_missing":
				r.Proto, _ = proto.Marshal(&waE2E.Message{Conversation: proto.String("private content")})
			case "key_missing", "key_invalid_length":
				p, _, _ := recoveryAttachment(r)
				p.ImageMessage.MediaKey = nil
				if tc.scenario == "key_invalid_length" {
					p.ImageMessage.MediaKey = []byte("PRIVATE-KEY")
				}
				r.Proto, _ = proto.Marshal(p)
			case "routing":
				r.Message.Chat = "private@broadcast"
			case "sender_unavailable":
				b.sendMediaRetry = nil
			}
			original := append([]byte(nil), r.Proto...)
			var out bytes.Buffer
			b.diagnosticWriter = &out
			if !b.reserveRecovery(context.Background(), &r, name) || r.QuarantineReason != tc.reason || r.Recovery != nil || !bytes.Equal(original, r.Proto) {
				t.Fatal("pre-send failure changed payload/budget or quarantine reason")
			}
			d := readSingleDiagnostic(t, &out)
			if d.RecoveryBranch != tc.branch || d.AttachmentState != tc.attachment || d.Reason != tc.reason {
				t.Fatal("pre-send diagnostic did not identify safe branch")
			}
			if strings.Contains(out.String(), "PRIVATE") || strings.Contains(out.String(), "private") {
				t.Fatal("private pre-send data logged")
			}
		})
	}
}

func TestRecoveryNotificationDiagnostics(t *testing.T) {
	for _, tc := range []struct{ scenario, reason, branch, result, validation string }{
		{"general", "recovery_rejected", "phone_result", "general_error", ""},
		{"not_found", "recovery_rejected", "phone_result", "not_found", ""},
		{"decryption_result", "recovery_rejected", "phone_result", "decryption_error", ""},
		{"missing_result", "recovery_rejected", "phone_result", "missing", ""},
		{"unknown_result", "recovery_rejected", "phone_result", "other", ""},
		{"invalid_path", "recovery_invalid_path", "path_validation", "success", "unsupported_family"},
		{"stanza", "", "stanza_mismatch", "success", ""},
		{"ciphertext", "", "decrypt_error", "", ""},
		{"phone_unavailable", "recovery_unavailable", "phone_error", "", ""},
		{"phone_other", "", "phone_error", "", ""},
	} {
		t.Run(tc.scenario, func(t *testing.T) {
			b := newTestBridge(t, t.TempDir())
			now := fixedRetryClock(b)
			name := recoveryFixture(t, b, "response")
			b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error { return nil }
			b.drain(context.Background())
			r := readRecovery(t, b, name)
			// Valid random identifiers can contain the same digits as an unknown
			// provider enum. Exercise that collision deterministically.
			r.ItemID = "999" + strings.Repeat("a", 29)
			r.AttemptID = "999" + strings.Repeat("b", 29)
			if err := b.spool.save(r, false); err != nil {
				t.Fatal(err)
			}
			n := &waMmsRetry.MediaRetryNotification{StanzaID: proto.String(r.Message.ID), Result: waMmsRetry.MediaRetryNotification_SUCCESS.Enum(), DirectPath: proto.String("/o1/v/t24/PRIVATE?secret=PRIVATE")}
			switch tc.scenario {
			case "general":
				n.Result = waMmsRetry.MediaRetryNotification_GENERAL_ERROR.Enum()
			case "not_found":
				n.Result = waMmsRetry.MediaRetryNotification_NOT_FOUND.Enum()
			case "decryption_result":
				n.Result = waMmsRetry.MediaRetryNotification_DECRYPTION_ERROR.Enum()
			case "missing_result":
				n.Result = nil
			case "unknown_result":
				n.Result = waMmsRetry.MediaRetryNotification_ResultType(999).Enum()
			case "invalid_path":
				n.DirectPath = proto.String("/unfamiliar/PRIVATE?secret=PRIVATE")
			case "stanza":
				n.StanzaID = proto.String("PRIVATE-wrong-stanza")
			}
			e := encryptedRecoveryNotification(t, r, *now, n)
			switch tc.scenario {
			case "ciphertext":
				e.Ciphertext[0] ^= 1
			case "phone_unavailable", "phone_other":
				e.Ciphertext = nil
				e.Error = &events.MediaRetryError{Code: 2}
				if tc.scenario == "phone_other" {
					e.Error.Code = 999
				}
			}
			var out bytes.Buffer
			b.diagnosticWriter = &out
			b.queueMediaRetry(e)
			b.applyRecoveryResponses()
			got := readRecovery(t, b, name)
			if got.QuarantineReason != tc.reason || !bytes.Equal(got.Proto, r.Proto) || got.Recovery.Attempts != 1 {
				t.Fatal("notification failure changed original/budget or quarantine reason")
			}
			if tc.reason == "" && got.Recovery.State != "awaiting" {
				t.Fatal("unverified notification consumed reservation")
			}
			d := readSingleDiagnostic(t, &out)
			if d.RecoveryBranch != tc.branch || d.NotificationResult != tc.result || d.PathValidation != tc.validation {
				t.Fatal("notification failure branch not identifiable")
			}
			if d.ItemID != r.ItemID || d.AttemptID != r.AttemptID || !opaquePattern.MatchString(d.ItemID) || !opaquePattern.MatchString(d.AttemptID) {
				t.Fatal("diagnostic correlation identifiers changed")
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(out.Bytes(), &fields); err != nil {
				t.Fatal(err)
			}
			delete(fields, "item_id")
			delete(fields, "attempt_id")
			withoutIDs, err := json.Marshal(fields)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(out.String(), "PRIVATE") || strings.Contains(string(withoutIDs), "999") {
				t.Fatal("notification data escaped diagnostic allowlist")
			}
		})
	}
}

func TestRecoveryDiagnosticAllowlist(t *testing.T) {
	var out bytes.Buffer
	b := &bridge{diagnosticWriter: &out}
	secret := "/o1/v/t24/PRIVATE?key=PRIVATE"
	b.emitDiagnostic(diagnostic{Event: secret, Stage: secret, Reason: secret, ItemID: secret, AttemptID: secret, URLExpiry: secret, PathFamily: secret, PathValidation: secret, NotificationResult: secret, RecoveryBranch: secret, AttachmentState: secret})
	d := readSingleDiagnostic(t, &out)
	if strings.Contains(out.String(), "PRIVATE") || d.PathFamily != "unknown" || d.PathValidation != "unknown" || d.NotificationResult != "unknown" || d.RecoveryBranch != "unknown" || d.AttachmentState != "unknown" {
		t.Fatal("unsafe diagnostic context escaped allowlist")
	}
	// Defensive branches use the same finite vocabulary even when their failure
	// cannot be induced after protobuf validation without injecting test hooks.
	out.Reset()
	b.emitDiagnostic(diagnostic{RecoveryBranch: "marshal"})
	if readSingleDiagnostic(t, &out).RecoveryBranch != "marshal" {
		t.Fatal("marshal failure branch unavailable")
	}
}

func TestRecoverySendErrorDiagnosticsStaySanitizedAndAwaiting(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	fixedRetryClock(b)
	name := recoveryFixture(t, b, "send-error")
	b.sendMediaRetry = func(context.Context, *types.MessageInfo, []byte) error {
		return errors.New("PRIVATE raw provider error https://synthetic.invalid?key=PRIVATE")
	}
	var out bytes.Buffer
	b.diagnosticWriter = &out
	b.drain(context.Background())
	r := readRecovery(t, b, name)
	d := readSingleDiagnostic(t, &out)
	if r.Recovery.State != "awaiting" || r.Recovery.Attempts != 1 || r.QuarantineReason != "" || d.RecoveryBranch != "request_attempt" || d.Reason != "unknown" || strings.Contains(out.String(), "PRIVATE") {
		t.Fatal("ambiguous send failure replayed, lost reservation or leaked error")
	}
}
