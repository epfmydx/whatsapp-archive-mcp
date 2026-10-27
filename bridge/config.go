package main

import (
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var accountPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`)
var recordPattern = regexp.MustCompile(`^[a-f0-9]{64}\.json$`)

type config struct {
	Account, Label, Token, SessionDB, Spool, AppURL, S3Endpoint, S3Bucket, S3Region, S3Access, S3Secret, S3Style string
	StorageBackend, MediaRoot                                                                                    string
	MaxMedia                                                                                                     int64
	MediaRecoveryEnabled                                                                                         bool
	MediaRecoveryMaxAttempts, MediaDownloadMaxAttempts                                                           uint32
	MediaRecoveryTimeout, MediaRecoveryMinInterval                                                               time.Duration
}

func env(k, fallback string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return fallback
}
func loadConfig() (config, error) {
	c := config{Account: env("ACCOUNT_ID", "personal"), Label: env("ACCOUNT_LABEL", "Personal"), Token: os.Getenv("INTERNAL_TOKEN"), SessionDB: os.Getenv("SESSION_DATABASE_URL"), Spool: env("SPOOL_DIR", "/spool"), AppURL: strings.TrimRight(os.Getenv("APP_INTERNAL_URL"), "/"), S3Endpoint: os.Getenv("S3_ENDPOINT_URL"), S3Bucket: os.Getenv("S3_BUCKET"), S3Region: env("S3_REGION", "us-east-1"), S3Access: os.Getenv("S3_ACCESS_KEY_ID"), S3Secret: os.Getenv("S3_SECRET_ACCESS_KEY"), S3Style: env("S3_ADDRESSING_STYLE", "path")}
	c.StorageBackend, c.MediaRoot = env("STORAGE_BACKEND", "s3"), env("MEDIA_ROOT", "/media")
	if c.StorageBackend != "s3" && c.StorageBackend != "disk" {
		return c, errors.New("invalid STORAGE_BACKEND")
	}
	if c.StorageBackend == "disk" && (!filepath.IsAbs(c.MediaRoot) || filepath.Clean(c.MediaRoot) != c.MediaRoot || c.MediaRoot == "/" || strings.ContainsAny(c.MediaRoot, "\x00\r\n\t ")) {
		return c, errors.New("invalid MEDIA_ROOT")
	}
	c.MaxMedia = 64 * 1024 * 1024
	c.MediaRecoveryEnabled = true
	if raw := os.Getenv("MEDIA_RECOVERY_ENABLED"); raw != "" {
		v, err := strconv.ParseBool(raw)
		if err != nil {
			return c, errors.New("invalid MEDIA_RECOVERY_ENABLED")
		}
		c.MediaRecoveryEnabled = v
	}
	for _, setting := range []struct {
		name          string
		fallback, max int
		target        *uint32
	}{
		{"MEDIA_RECOVERY_MAX_ATTEMPTS", 1, 3, &c.MediaRecoveryMaxAttempts}, {"MEDIA_DOWNLOAD_MAX_ATTEMPTS", 8, 100, &c.MediaDownloadMaxAttempts},
	} {
		n, err := strconv.Atoi(env(setting.name, strconv.Itoa(setting.fallback)))
		if err != nil || n < 1 || n > setting.max {
			return c, errors.New("invalid " + setting.name)
		}
		*setting.target = uint32(n)
	}
	for _, setting := range []struct {
		name               string
		fallback, min, max int
		target             *time.Duration
	}{
		{"MEDIA_RECOVERY_TIMEOUT_SECONDS", 120, 15, 3600, &c.MediaRecoveryTimeout}, {"MEDIA_RECOVERY_MIN_INTERVAL_SECONDS", 30, 5, 3600, &c.MediaRecoveryMinInterval},
	} {
		n, err := strconv.Atoi(env(setting.name, strconv.Itoa(setting.fallback)))
		if err != nil || n < setting.min || n > setting.max {
			return c, errors.New("invalid " + setting.name)
		}
		*setting.target = time.Duration(n) * time.Second
	}
	if v := os.Getenv("MAX_MEDIA_BYTES"); v != "" {
		n, e := strconv.ParseInt(v, 10, 64)
		if e != nil || n < 1 || n > 1024*1024*1024 {
			return c, errors.New("invalid MAX_MEDIA_BYTES")
		}
		c.MaxMedia = n
	}
	if !accountPattern.MatchString(c.Account) || len(c.Token) < 32 || c.SessionDB == "" || (c.StorageBackend == "s3" && (c.S3Bucket == "" || c.S3Access == "" || c.S3Secret == "")) {
		return c, errors.New("missing or invalid bridge configuration")
	}
	var accounts []struct {
		ID string `json:"id"`
	}
	if json.Unmarshal([]byte(env("ACCOUNTS_JSON", `[{"id":"personal","label":"Personal"}]`)), &accounts) != nil {
		return c, errors.New("invalid account configuration")
	}
	known := false
	for _, a := range accounts {
		if a.ID == c.Account {
			known = true
		}
	}
	if !known {
		return c, errors.New("unknown account")
	}
	endpoints := []string{c.AppURL}
	if c.StorageBackend == "s3" && c.S3Endpoint != "" {
		endpoints = append(endpoints, c.S3Endpoint)
	}
	for _, raw := range endpoints {
		u, e := url.Parse(raw)
		if e != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
			return c, errors.New("invalid internal endpoint")
		}
	}
	u, e := url.Parse(c.SessionDB)
	if e != nil || (u.Scheme != "postgres" && u.Scheme != "postgresql") {
		return c, errors.New("invalid session database")
	}
	if c.S3Style != "path" && c.S3Style != "virtual" {
		return c, errors.New("invalid S3 addressing style")
	}
	return c, nil
}
