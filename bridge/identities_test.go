package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func metadataBridge(t *testing.T) *bridge {
	b := newTestBridge(t, t.TempDir())
	var err error
	b.metadata, err = newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func TestMetadataSourcesVersionsAndRestart(t *testing.T) {
	b := metadataBridge(t)
	jid := types.NewJID("12345", types.DefaultUserServer)
	lid := types.NewJID("999", types.HiddenUserServer)
	group := types.NewJID("321-1", types.GroupServer)
	at := time.Date(2026, 10, 27, 1, 0, 0, 0, time.UTC)
	b.event(&events.Contact{JID: jid, Timestamp: at, Action: &waSyncAction.ContactAction{FullName: proto.String("Full"), FirstName: proto.String("First"), LidJID: proto.String(lid.String()), PnJID: proto.String(jid.String())}})
	b.event(&events.PushName{JID: lid, JIDAlt: jid, Message: &types.MessageInfo{Timestamp: at}, NewPushName: "Push"})
	b.event(&events.BusinessName{JID: jid, Message: &types.MessageInfo{Timestamp: at}, NewBusinessName: "Business"})
	b.event(&events.GroupInfo{JID: group, Timestamp: at, Name: &types.GroupName{Name: "Group", NameSetAt: at}})
	b.event(&events.Contact{JID: jid, Timestamp: at.Add(time.Minute), Action: &waSyncAction.ContactAction{}})
	b.event(&events.PushName{JID: lid, Message: &types.MessageInfo{Timestamp: at.Add(time.Minute)}, NewPushName: ""})
	b.observeHistory(&events.HistorySync{Data: &waHistorySync.HistorySync{Pushnames: []*waHistorySync.Pushname{{ID: proto.String(lid.String()), Pushname: proto.String("Old history")}}, Conversations: []*waHistorySync.Conversation{{ID: proto.String(group.String()), Name: proto.String("Old group")}}}})
	if err := b.metadata.persist(); err != nil {
		t.Fatal(err)
	}
	restored, err := newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	names := restored.state.Names
	if names[jid.String()+"\x00contact_full"].Name != "" || names[jid.String()+"\x00contact_first"].Name != "" {
		t.Fatal("contact clear lost")
	}
	if names[lid.String()+"\x00push"].Name != "" {
		t.Fatal("old history replaced explicit clear")
	}
	if names[group.String()+"\x00chat"].Name != "Group" {
		t.Fatal("old history replaced group observation")
	}
	if names[jid.String()+"\x00business"].Name != "Business" {
		t.Fatal("business metadata missing")
	}
	if restored.state.Links[lid.String()].PN != jid.String() {
		t.Fatal("explicit LID/PN link lost")
	}
	if _, err := newIdentityCache(b.spool.dir, "other"); err == nil {
		t.Fatal("cross-account metadata accepted")
	}
	info, err := os.Stat(filepath.Join(b.spool.dir, identityStateFile))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("metadata permissions")
	}
	pending, err := b.spool.pending()
	if err != nil || len(pending) != 0 {
		t.Fatal("metadata mistaken for message spool")
	}
}
func TestReferencedOnlyMetadataCachedContactsAndBatchRetry(t *testing.T) {
	b := metadataBridge(t)
	// 100 archive references times two cached names require multiple <=100 batches.
	refs := []string{}
	for i := 0; i < 100; i++ {
		refs = append(refs, types.NewJID(strconv.Itoa(10000+i), types.DefaultUserServer).String())
	}
	b.metadata.name("unreferenced@s.whatsapp.net", "contact_full", "Never exported", unversionedObservation)
	lid := types.NewJID("987654", types.HiddenUserServer)
	b.metadata.alternate = func(_ context.Context, j types.JID) (types.JID, error) {
		if j.String() == refs[0] {
			return lid, nil
		}
		return types.JID{}, nil
	}
	b.metadata.contact = func(_ context.Context, j types.JID) (types.ContactInfo, error) {
		return types.ContactInfo{Found: true, FullName: "Full", FirstName: "First"}, nil
	}
	reject := true
	posts := 0
	seenLink := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.Header.Get("Authorization") != "Bearer "+b.cfg.Token {
			t.Error("missing bearer")
		}
		if r.Method == "GET" {
			if r.URL.Path != "/internal/accounts/personal/identities" || r.URL.Query().Get("limit") != "100" {
				t.Error("wrong reference contract")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"items": refs, "next_after": nil, "has_more": false})
			return
		}
		if r.URL.Path != "/internal/identities" {
			t.Error("unexpected callback")
		}
		var batch identityBatch
		if json.NewDecoder(r.Body).Decode(&batch) != nil {
			t.Error("invalid batch")
		}
		if batch.Account != "personal" || len(batch.Names)+len(batch.Links) > 100 {
			t.Error("batch not bounded/scoped")
		}
		for _, n := range batch.Names {
			if strings.Contains(n.JID, "unreferenced") {
				t.Error("phonebook exported")
			}
			if !n.ObservedAt.After(unversionedObservation) {
				t.Error("saved contact cache lacks local observation time")
			}
		}
		if len(batch.Links) > 0 {
			seenLink = true
		}
		posts++
		if reject {
			w.WriteHeader(503)
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err == nil {
		t.Fatal("metadata failure ignored")
	}
	reject = false
	if _, more, err := b.reconcileIdentityPage(context.Background(), ""); err != nil || more {
		t.Fatalf("retry failed: %v", err)
	}
	if posts < 4 || !seenLink {
		t.Fatal("metadata not chunked or link not exported")
	}
}
func TestHistoryPushNamesBeforeUpstreamCacheAndDeviceJIDs(t *testing.T) {
	b := metadataBridge(t)
	b.observeHistory(&events.HistorySync{Data: &waHistorySync.HistorySync{Pushnames: []*waHistorySync.Pushname{{ID: proto.String("12345:7@s.whatsapp.net"), Pushname: proto.String("History push")}}, PhoneNumberToLidMappings: []*waHistorySync.PhoneNumberToLIDMapping{{PnJID: proto.String("12345@s.whatsapp.net"), LidJID: proto.String("987@lid")}}}})
	if b.metadata.state.Names["12345@s.whatsapp.net\x00push"].Name != "History push" {
		t.Fatal("history push names depend on asynchronous upstream cache")
	}
	if b.metadata.state.Links["987@lid"].PN != "12345@s.whatsapp.net" {
		t.Fatal("history explicit mapping missing")
	}
	b.metadata.link("same-digits@lid", "different-prose@g.us", time.Now())
	if len(b.metadata.state.Links) != 1 {
		t.Fatal("invalid pair inferred")
	}
}
func TestHeartbeatIndependentOfBlockedMedia(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := sampleEvent()
	e.Message = &waE2E.Message{AudioMessage: &waE2E.AudioMessage{Mimetype: proto.String("audio/ogg"), FileLength: proto.Uint64(12)}}
	b.capture(e, "")
	started := make(chan struct{})
	unblock := make(chan struct{})
	b.download = func(ctx context.Context, _ whatsmeow.DownloadableMessage, _ whatsmeow.File) error {
		close(started)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-unblock:
			return errors.New("fixture download blocked")
		}
	}
	reports := make(chan map[string]any, 10)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if r.URL.Path != "/internal/accounts/status" {
			t.Error("unexpected callback")
		}
		reports <- payload
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); b.drain(ctx) }()
	<-started
	go func() { defer wg.Done(); b.heartbeat(ctx, 15*time.Millisecond) }()
	for i := 0; i < 2; i++ {
		select {
		case p := <-reports:
			if p["pending_count"] != float64(1) || p["oldest_pending_at"] == nil || p["whatsapp_connected"] != false || p["whatsapp_authenticated"] != false {
				t.Errorf("wrong pending unpaired telemetry: %v", p)
			}
		case <-time.After(time.Second):
			t.Fatal("blocked media stopped heartbeat")
		}
	}
	cancel()
	close(unblock)
	wg.Wait()
}
func TestQueueAgeUsesCaptureTimeAndLegacyUnknown(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	if n, old := b.spool.queueHealth(); n != 0 || old != nil {
		t.Fatal("empty queue has artificial activity")
	}
	before := time.Now().Add(-time.Second)
	b.capture(sampleEvent(), "")
	n, old := b.spool.queueHealth()
	if n != 1 {
		t.Fatal("capture not counted")
	}
	captured, err := time.Parse(time.RFC3339Nano, old.(string))
	if err != nil || captured.Before(before) {
		t.Fatal("author timestamp used for backlog age")
	}
	names, _ := b.spool.pending()
	r, _ := b.spool.read(names[0])
	r.CapturedAt = time.Time{}
	if err = b.spool.save(r, false); err != nil {
		t.Fatal(err)
	}
	if n, old = b.spool.queueHealth(); n != 1 || old != nil {
		t.Fatal("legacy queue time fabricated")
	}
	if err = b.spool.remove(names[0]); err != nil {
		t.Fatal(err)
	}
	if n, old = b.spool.queueHealth(); n != 0 || old != nil {
		t.Fatal("drained queue has artificial activity")
	}
}
func TestPermanentDisconnectSanitized(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	b.setStatus("connected")
	b.event(&events.StreamReplaced{})
	if b.statusPayload()["status"] != "disconnected" {
		t.Fatal("permanent disconnect retained connected state")
	}
	b.event(&events.LoggedOut{})
	if b.statusPayload()["status"] != "logged_out" {
		t.Fatal("logout lost")
	}
}

func TestMetadataUnsupportedFieldsAndUnknownRemainUnknown(t *testing.T) {
	b := metadataBridge(t)
	b.metadata.name("12345@s.whatsapp.net", "push", strings.Repeat("x", 201), time.Now())
	b.metadata.name("12345@s.whatsapp.net", "push", "hidden\u200bname", time.Now())
	b.metadata.name("invalid\n@lid", "push", "bad", time.Now())
	b.metadata.link("letters@lid", "12345@s.whatsapp.net", time.Now())
	if len(b.metadata.state.Names) != 0 || len(b.metadata.state.Links) != 0 {
		t.Fatal("unsupported fields accepted")
	}
	b.metadata.contact = func(context.Context, types.JID) (types.ContactInfo, error) { return types.ContactInfo{}, nil }
	posts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []string{"12345@s.whatsapp.net"}, "has_more": false, "next_after": nil})
			return
		}
		posts++
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if posts != 0 {
		t.Fatal("unknown identity manufactured from unavailable cache")
	}
}
func TestCapturedMetadataRecoveryAndBacklogAfterRestart(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	event := sampleEvent()
	event.Info.Chat = types.NewJID("321-1", types.GroupServer)
	if !b.capture(event, "Recovered group") {
		t.Fatal("capture failed")
	}
	// Simulate metadata worker not running before receiver restart.
	var err error
	b.metadata, err = newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	b.recoverCapturedIdentities()
	if b.metadata.state.Names["321-1@g.us\x00chat"].Name != "Recovered group" {
		t.Fatal("durable group name not recovered")
	}
	if b.metadata.state.Names["sender@s.whatsapp.net\x00push"].Name != "Sender" {
		t.Fatal("durable push name not recovered")
	}
	dir := b.spool.dir
	b.spool.close()
	sp, err := newSpool(dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	defer sp.close()
	n, old := sp.queueHealth()
	if n != 1 || old == nil {
		t.Fatal("restart lost queue capture age")
	}
}

func TestEqualTimeClearAndConflictingExplicitLinks(t *testing.T) {
	b := metadataBridge(t)
	at := time.Date(2026, 10, 27, 1, 0, 0, 0, time.UTC)
	b.metadata.name("12345@s.whatsapp.net", "contact_full", "Full", at)
	b.metadata.name("12345@s.whatsapp.net", "contact_full", "", at)
	b.metadata.name("12345@s.whatsapp.net", "contact_full", "Old full", at)
	if b.metadata.state.Names["12345@s.whatsapp.net\x00contact_full"].Name != "" {
		t.Fatal("equal-time observation replaced clear")
	}
	b.metadata.link("987@lid", "12345@s.whatsapp.net", at)
	b.metadata.link("987@lid", "54321@s.whatsapp.net", at)
	if len(b.metadata.state.LinkConflicts["987@lid"]) != 1 {
		t.Fatal("conflicting explicit link guessed by arrival order")
	}
	if err := b.metadata.persist(); err != nil {
		t.Fatal(err)
	}
	restored, err := newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	if len(restored.state.LinkConflicts["987@lid"]) != 1 {
		t.Fatal("ambiguity lost across restart")
	}
	b.metadata.link("987@lid", "12345@s.whatsapp.net", at.Add(time.Minute))
	if len(b.metadata.state.LinkConflicts["987@lid"]) != 0 {
		t.Fatal("newer explicit evidence did not replace ambiguity")
	}
}

func TestMetadataWakeDoesNotStarvePagination(t *testing.T) {
	b := metadataBridge(t)
	b.metadata.name("10000@s.whatsapp.net", "push", "First", unversionedObservation)
	secondPage := make(chan struct{}, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.Method != http.MethodGet {
			return
		}
		if r.URL.Query().Get("after") == "" {
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []string{"10000@s.whatsapp.net"}, "has_more": true, "next_after": "10000@s.whatsapp.net"})
		} else {
			select {
			case secondPage <- struct{}{}:
			default:
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []string{"20000@s.whatsapp.net"}, "has_more": false, "next_after": nil})
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); b.reconcileIdentities(ctx) }()
	select {
	case <-secondPage:
	case <-time.After(time.Second):
		cancel()
		<-done
		t.Fatal("metadata wake restarted first page")
	}
	cancel()
	<-done
}

func TestPushPlaceholderAndAlternateClear(t *testing.T) {
	b := metadataBridge(t)
	pn := types.NewJID("12345", types.DefaultUserServer)
	lid := types.NewJID("987", types.HiddenUserServer)
	at := time.Now().Add(-time.Hour).UTC()
	b.metadata.name(pn.String(), "push", "Old PN", at)
	b.metadata.name(lid.String(), "push", "Old LID", at)
	event := sampleEvent()
	event.Info.Sender = pn
	event.Info.PushName = "-"
	event.Info.Timestamp = at.Add(time.Minute)
	b.observeMessage(event, "")
	b.observeHistory(&events.HistorySync{Data: &waHistorySync.HistorySync{Pushnames: []*waHistorySync.Pushname{{ID: proto.String(pn.String()), Pushname: proto.String("-")}}}})
	b.observePushName(&events.PushName{JID: pn, JIDAlt: lid, NewPushName: "-", Message: &types.MessageInfo{Timestamp: at.Add(2 * time.Minute)}})
	if b.metadata.state.Names[pn.String()+"\x00push"].Name != "Old PN" || b.metadata.state.Names[lid.String()+"\x00push"].Name != "Old LID" {
		t.Fatal("placeholder replaced actual push name")
	}
	clearAt := at.Add(3 * time.Minute)
	b.observePushName(&events.PushName{JID: pn, JIDAlt: lid, NewPushName: "", Message: &types.MessageInfo{Timestamp: clearAt}})
	for _, jid := range []string{pn.String(), lid.String()} {
		n := b.metadata.state.Names[jid+"\x00push"]
		if n.Name != "" || !n.ObservedAt.Equal(clearAt) {
			t.Fatal("explicit alternate retained stale push name")
		}
	}
}
func TestContactCacheRecoveryUsesStableLocalObservation(t *testing.T) {
	b := metadataBridge(t)
	jid := "12345@s.whatsapp.net"
	oldAt := time.Now().Add(-time.Hour).UTC()
	for kind, name := range map[string]string{"contact_full": "Old full", "contact_first": "Old first", "push": "Live push", "business": "Live business"} {
		b.metadata.name(jid, kind, name, oldAt)
	}
	if err := b.metadata.persist(); err != nil {
		t.Fatal(err)
	}
	restored, err := newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	b.metadata = restored
	// Mimic a completed Whatsmeow full sync, which does not emit Contact events
	// by default, surviving a bridge crash before its metadata flush.
	cached := types.ContactInfo{Found: true, FullName: "New full", FirstName: "New first", PushName: "Stale history push", BusinessName: "Stale business"}
	b.metadata.contact = func(context.Context, types.JID) (types.ContactInfo, error) { return cached, nil }
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []string{jid}, "has_more": false, "next_after": nil})
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	before := time.Now().UTC()
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	full := b.metadata.state.Names[jid+"\x00contact_full"]
	first := b.metadata.state.Names[jid+"\x00contact_first"]
	if full.Name != "New full" || first.Name != "New first" || full.ObservedAt.Before(before) {
		t.Fatal("saved contact cache rename remained stale")
	}
	if b.metadata.state.Names[jid+"\x00push"].Name != "Live push" || b.metadata.state.Names[jid+"\x00business"].Name != "Live business" {
		t.Fatal("unversioned historical cache replaced versioned name")
	}
	if !b.metadata.state.LocalContactObservations[jid+"\x00contact_full"].Equal(full.ObservedAt) {
		t.Fatal("local observation provenance missing")
	}
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if !b.metadata.state.Names[jid+"\x00contact_full"].ObservedAt.Equal(full.ObservedAt) {
		t.Fatal("unchanged cache fabricated freshness")
	}
	persisted, err := newIdentityCache(b.spool.dir, b.cfg.Account)
	if err != nil {
		t.Fatal(err)
	}
	if !persisted.state.LocalContactObservations[jid+"\x00contact_full"].Equal(full.ObservedAt) {
		t.Fatal("local provenance lost on restart")
	}
	cached.FullName = ""
	cached.FirstName = ""
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if b.metadata.state.Names[jid+"\x00contact_full"].Name != "" || b.metadata.state.Names[jid+"\x00contact_first"].Name != "" {
		t.Fatal("current saved-contact clears not recovered")
	}
}
func TestContactCacheCannotOverwriteConcurrentEvent(t *testing.T) {
	b := metadataBridge(t)
	jid := "12345@s.whatsapp.net"
	at := time.Now().Add(-time.Hour).UTC()
	b.metadata.name(jid, "contact_full", "Old", at)
	snapshot := b.metadata.contactSnapshot(jid)
	b.metadata.name(jid, "contact_full", "New event", at.Add(time.Minute))
	b.metadata.cachedContactName(jid, "contact_full", "Old cache", time.Now().UTC(), snapshot["contact_full"])
	if b.metadata.state.Names[jid+"\x00contact_full"].Name != "New event" {
		t.Fatal("cache read overwrote racing event")
	}
}
func TestPushOnlyCounterpartCacheCannotClearSavedContact(t *testing.T) {
	b := metadataBridge(t)
	pn := "12345@s.whatsapp.net"
	lid := "987@lid"
	oldAt := time.Now().Add(-time.Hour).UTC()
	b.metadata.name(lid, "contact_full", "Old linked contact", oldAt)
	b.metadata.link(lid, pn, oldAt)
	b.metadata.contact = func(_ context.Context, j types.JID) (types.ContactInfo, error) {
		if j.String() == pn {
			return types.ContactInfo{Found: true, FullName: "Current saved name"}, nil
		}
		return types.ContactInfo{Found: true, PushName: "Push-only LID"}, nil
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Archive-Format", "2")
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []string{lid}, "has_more": false, "next_after": nil})
		}
	}))
	defer srv.Close()
	b.cfg.AppURL = srv.URL
	if _, _, err := b.reconcileIdentityPage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if b.metadata.state.Names[lid+"\x00contact_full"].Name == "" {
		t.Fatal("push-only counterpart cache manufactured saved-contact clear")
	}
	if b.metadata.state.Names[pn+"\x00contact_full"].Name != "Current saved name" {
		t.Fatal("current canonical saved contact missing")
	}
}
