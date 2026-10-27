package main

import (
	"go.mau.fi/whatsmeow/proto/waE2E"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestQueueHealthSourcesLanesAndUnknownTimes(t *testing.T) {
	b := newTestBridge(t, t.TempDir())
	e := sampleEvent()
	if !b.captureSource(e, "", "history") {
		t.Fatal("history capture")
	}
	e.Info.ID = "live"
	e.Message = &waE2E.Message{PtvMessage: &waE2E.VideoMessage{}}
	if !b.capture(e, "") {
		t.Fatal("live capture")
	}
	legacy, _ := makeRecord("personal", "", sampleEvent())
	legacy.Message.ID = "legacy"
	if err := b.spool.save(legacy, false); err != nil {
		t.Fatal(err)
	}
	count, old, h := b.spool.queueStatus()
	if count != 3 || old != nil || h.Text.Pending != 2 || h.Text.UnknownTimes != 1 || h.Text.Oldest == nil || h.Text.Origins["history"] != 1 || h.Text.Origins["unknown"] != 1 || h.Media.Pending != 1 || h.Media.Origins["live"] != 1 {
		t.Fatalf("bad health %#v", h)
	}
	b.spool.completedAt(textLane, time.Unix(456, 0))
	_, _, h = b.spool.queueStatus()
	if h.Completed["text"] != 1 || h.LastSuccess == nil {
		t.Fatal("completion missing")
	}
	// A malformed startup record has unknown lane and origin, never guessed.
	b.spool.close()
	if err := os.WriteFile(filepath.Join(b.spool.dir, recordID(message{Account: "personal", Chat: "x", ID: "broken"})+".json"), []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	b = newTestBridge(t, b.spool.dir)
	_, _, h = b.spool.queueStatus()
	if h.Completed["text"] != 0 || h.LastSuccess != nil {
		t.Fatal("process counters survived restart")
	}
	if h.Unknown.Pending != 1 || h.Unknown.UnknownTimes != 1 || h.Unknown.Origins["unknown"] != 1 {
		t.Fatal("corrupt lane guessed")
	}
}
