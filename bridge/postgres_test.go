package main

import (
	"context"
	"os"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/store/sqlstore"
)

// Uses only an explicitly supplied disposable test database. Never derive this
// from SESSION_DATABASE_URL or application credentials.
func TestPostgresSessionMigrationsAndAccountBinding(t *testing.T) {
	dsn := os.Getenv("TEST_POSTGRES_URL")
	if dsn == "" {
		t.Skip("disposable PostgreSQL fixture not supplied")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if e := bindSessionAccount(ctx, dsn, "fixture"); e != nil {
		t.Fatal(e)
	}
	if e := bindSessionAccount(ctx, dsn, "fixture"); e != nil {
		t.Fatal("same account must be idempotent")
	}
	if e := bindSessionAccount(ctx, dsn, "foreign"); e == nil {
		t.Fatal("foreign account reused session database")
	}
	c, e := sqlstore.New(ctx, "postgres", dsn, nil)
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	devices, e := c.GetAllDevices(ctx)
	if e != nil || len(devices) != 0 {
		t.Fatalf("unexpected fixture devices: %v", e)
	}
	d, e := c.GetFirstDevice(ctx)
	if e != nil || d.ID != nil {
		t.Fatalf("fresh pairing device unavailable: %v", e)
	}
}
