package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func diskFixture(t *testing.T) (config, string, []byte, string) {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	c := config{Account: "personal", StorageBackend: "disk", MediaRoot: filepath.Join(root, "media"), MaxMedia: 1024}
	key := "accounts/personal/media/" + strings.Repeat("a", 64)
	data := []byte("synthetic private media")
	sum := sha256.Sum256(data)
	return c, key, data, hex.EncodeToString(sum[:])
}

func TestDiskAtomicRoundtripAndDuplicatePreservation(t *testing.T) {
	c, key, data, digest := diskFixture(t)
	if err := persistDisk(context.Background(), c, key, bytes.NewReader(data), int64(len(data)), digest); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(c.MediaRoot, filepath.FromSlash(key))
	original, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if original.Mode().Perm() != 0600 {
		t.Fatal("media is not private")
	}
	for _, dir := range []string{c.MediaRoot, filepath.Dir(target)} {
		st, e := os.Stat(dir)
		if e != nil || st.Mode().Perm() != 0700 {
			t.Fatal("directory is not private")
		}
	}
	if err = persistDisk(context.Background(), c, key, bytes.NewReader(data), int64(len(data)), digest); err != nil {
		t.Fatal(err)
	}
	duplicate, err := os.Stat(target)
	if err != nil || !os.SameFile(original, duplicate) {
		t.Fatal("duplicate replaced existing object")
	}
	other := []byte("different media")
	sum := sha256.Sum256(other)
	if err = persistDisk(context.Background(), c, key, bytes.NewReader(other), int64(len(other)), hex.EncodeToString(sum[:])); err == nil {
		t.Fatal("overwrote a different object")
	}
	actual, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(actual, data) {
		t.Fatal("existing object changed")
	}
	names, _ := filepath.Glob(filepath.Join(filepath.Dir(target), ".tmp-*"))
	if len(names) != 0 {
		t.Fatal("temporary file remains")
	}
}

type failedDiskSource struct{}

func (failedDiskSource) Read([]byte) (int, error) { return 0, errors.New("synthetic source failure") }

func TestDiskFailedBoundedWritesNeverPublish(t *testing.T) {
	for _, mode := range []string{"truncated", "oversize", "hash", "source", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			c, key, data, digest := diskFixture(t)
			var source io.Reader = bytes.NewReader(data)
			ctx := context.Background()
			switch mode {
			case "truncated":
				source = bytes.NewReader(data[:1])
			case "oversize":
				source = bytes.NewReader(append(data, 1))
			case "hash":
				digest = strings.Repeat("0", 64)
			case "source":
				source = failedDiskSource{}
			case "cancel":
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			if err := persistDisk(ctx, c, key, source, int64(len(data)), digest); err == nil {
				t.Fatal("invalid input published")
			}
			if _, err := os.Lstat(filepath.Join(c.MediaRoot, key)); !os.IsNotExist(err) {
				t.Fatal("target unexpectedly exists")
			}
			names, _ := filepath.Glob(filepath.Join(c.MediaRoot, "accounts/personal/media/.tmp-*"))
			if len(names) != 0 {
				t.Fatal("partial file retained")
			}
		})
	}
}

func TestDiskRejectsSymlinksAndTraversal(t *testing.T) {
	c, key, data, digest := diskFixture(t)
	for _, bad := range []string{"../outside", "accounts/personal/media/../../outside", "accounts/other/media/" + strings.Repeat("a", 64), "/absolute", key + "/extra"} {
		if err := persistDisk(context.Background(), c, bad, bytes.NewReader(data), int64(len(data)), digest); err == nil {
			t.Fatal("unsafe key accepted")
		}
	}
	for _, component := range []string{"root", "account", "file"} {
		t.Run(component, func(t *testing.T) {
			c, key, data, digest := diskFixture(t)
			if err := initializeDisk(c); err != nil {
				t.Fatal(err)
			}
			target := filepath.Join(c.MediaRoot, key)
			link := target
			switch component {
			case "root":
				link = c.MediaRoot
			case "account":
				link = filepath.Join(c.MediaRoot, "accounts/personal")
			}
			outside := link + "-outside"
			if component == "file" {
				if err := os.WriteFile(outside, data, 0600); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := os.Rename(link, outside); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.Symlink(outside, link); err != nil {
				t.Fatal(err)
			}
			if err := persistDisk(context.Background(), c, key, bytes.NewReader(data), int64(len(data)), digest); err == nil {
				t.Fatal("symlink accepted")
			}
		})
	}
}

func TestDiskBridgeConfigRequiresNoS3Credentials(t *testing.T) {
	for k, v := range map[string]string{"ACCOUNT_ID": "personal", "INTERNAL_TOKEN": strings.Repeat("t", 32), "SESSION_DATABASE_URL": "postgres://user:pass@localhost/session", "APP_INTERNAL_URL": "http://localhost:8000", "ACCOUNTS_JSON": `[{"id":"personal"}]`, "STORAGE_BACKEND": "disk", "MEDIA_ROOT": "/media", "S3_ENDPOINT_URL": "", "S3_BUCKET": "", "S3_ACCESS_KEY_ID": "", "S3_SECRET_ACCESS_KEY": ""} {
		t.Setenv(k, v)
	}
	c, err := loadConfig()
	if err != nil || c.StorageBackend != "disk" {
		t.Fatal("disk requires unrelated S3 configuration")
	}
	for _, root := range []string{"relative", "/", "/media/../other"} {
		t.Setenv("MEDIA_ROOT", root)
		if _, err := loadConfig(); err == nil {
			t.Fatal("unsafe root accepted")
		}
	}
	t.Setenv("MEDIA_ROOT", "/media")
	t.Setenv("STORAGE_BACKEND", "invalid")
	if _, err := loadConfig(); err == nil {
		t.Fatal("invalid backend accepted")
	}
	t.Setenv("STORAGE_BACKEND", "s3")
	if _, err := loadConfig(); err == nil {
		t.Fatal("missing S3 configuration accepted")
	}
}
