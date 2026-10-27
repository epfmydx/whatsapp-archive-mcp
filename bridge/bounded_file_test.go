package main

import (
	"io"
	"os"
	"strings"
	"testing"
)

// Whatsmeow copies a TeeReader into the File interface. Embedding *os.File
// must not promote a ReaderFrom method that bypasses bounded Write.
func TestBoundedFileCopyCannotBypassLimit(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "bounded-copy")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	_, err = io.Copy(boundedFile{f, 8}, io.TeeReader(strings.NewReader(strings.Repeat("x", 32)), io.Discard))
	if err == nil {
		t.Fatal("io.Copy bypassed the media bound")
	}
	stat, statErr := f.Stat()
	if statErr != nil {
		t.Fatal(statErr)
	}
	if stat.Size() > 8 {
		t.Fatal("io.Copy wrote beyond media bound")
	}
}

func TestBoundedFileCopyWithinLimitPreservesBytes(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "bounded-valid")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	bf := boundedFile{f, 8}
	n, err := io.Copy(bf, io.TeeReader(strings.NewReader("original"), io.Discard))
	if err != nil || n != 8 {
		t.Fatal("bounded copy failed for valid media")
	}
	if _, err = bf.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(bf)
	if err != nil || string(got) != "original" {
		t.Fatal("bounded copy changed valid media bytes")
	}
}
