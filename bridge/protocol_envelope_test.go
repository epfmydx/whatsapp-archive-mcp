package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestProtocolSuppressionChecksRawEnvelopeDuringCaptureAndDrain(t *testing.T) {
	for _, mode := range []string{"capture", "legacy_drain"} {
		for _, kind := range []string{"unknown_wrapper", "unfamiliar_outer_context", "edit_wrapper"} {
			t.Run(mode+"/"+kind, func(t *testing.T) {
				b := newTestBridge(t, t.TempDir())
				posts := 0
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
					posts++
					w.Header().Set("X-Archive-Format", "2")
					w.WriteHeader(http.StatusOK)
				}))
				defer srv.Close()
				b.cfg.AppURL = srv.URL
				leaf := validProtocolOnlyMessage(t)
				raw := &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: leaf}}
				switch kind {
				case "unknown_wrapper":
					raw.EphemeralMessage.ProtoReflect().SetUnknown([]byte{0x98, 0x06, 0x01})
				case "unfamiliar_outer_context":
					raw.MessageContextInfo = &waE2E.MessageContextInfo{SupportPayload: proto.String("synthetic unfamiliar metadata")}
				case "edit_wrapper":
					raw = &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: leaf}}
				}
				e := sampleEvent()
				e.Message = leaf
				e.RawMessage = raw
				if mode == "capture" {
					if !b.capture(e, "") {
						t.Fatal("capture failed")
					}
					names, _ := b.spool.pending()
					if len(names) != 1 {
						t.Fatal("unfamiliar content not durably captured")
					}
					r, err := b.spool.read(names[0])
					if err != nil || r.ProtocolOnly {
						t.Fatal("unfamiliar original incorrectly marked protocol-only")
					}
				} else {
					r, err := makeRecord(b.cfg.Account, "", e)
					if err != nil {
						t.Fatal(err)
					}
					// A previous or corrupted flag must never override original-envelope evidence.
					r.ProtocolOnly = true
					if err = b.spool.save(r, true); err != nil {
						t.Fatal(err)
					}
				}
				b.drain(context.Background())
				if posts != 1 {
					t.Fatalf("original unknown/edit content suppressed: posts=%d", posts)
				}
			})
		}
	}
}
