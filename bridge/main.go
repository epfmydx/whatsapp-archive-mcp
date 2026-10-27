package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	_ "github.com/lib/pq"
	"github.com/minio/minio-go/v7"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
)

type bridge struct {
	recoveryMu          sync.Mutex
	recoveryActive      map[string]string
	recoveryEvents      chan recoveryResponse
	recoveryNext        time.Time
	recoveryClockLoaded bool
	sendMediaRetry      func(context.Context, *types.MessageInfo, []byte) error
	cfg                 config
	spool               *spool
	client              *whatsmeow.Client
	download            func(context.Context, whatsmeow.DownloadableMessage, whatsmeow.File) error
	s3                  *minio.Client
	http                *http.Client
	mu                  sync.RWMutex
	status, qr          string
	qrUntil             time.Time
	drainMu             [retryLaneCount]sync.Mutex
	queueHealthMu       sync.Mutex
	laneErrors          [retryLaneCount]bool
	diagnosticMu        sync.Mutex
	now                 func() time.Time
	jitter              func(time.Duration) time.Duration
	diagnosticWriter    io.Writer
	queueError          atomic.Bool
	metadataError       atomic.Bool
	wake                chan struct{}
	statusWake          chan struct{}
	metadata            *identityCache
	fatal               chan struct{}
	fatalOnce           sync.Once
}

func (b *bridge) setStatus(status string) {
	b.mu.Lock()
	b.status = status
	if status != "awaiting_pairing" {
		b.qr = ""
		b.qrUntil = time.Time{}
	}
	b.mu.Unlock()
	select {
	case b.statusWake <- struct{}{}:
	default:
	}
}
func (b *bridge) statusPayload() map[string]any {
	b.mu.RLock()
	s := b.status
	b.mu.RUnlock()
	var problem any
	if b.metadataError.Load() {
		problem = "identity_reconciliation_pending"
	}
	if b.queueError.Load() {
		problem = "archive_or_ingestion_pending"
	}
	connected, authenticated := false, false
	if b.client != nil {
		connected, authenticated = b.client.IsConnected(), b.client.IsLoggedIn()
	}
	var count, oldest any
	var details *detailedQueueHealth
	if b.spool != nil {
		count, oldest, details = b.spool.queueStatus()
	}
	var retries any
	if b.spool != nil {
		h := b.spool.retryHealth(b.nowUTC())
		retries = h
		if h.Failed > 0 || h.CoolingDown > 0 || h.Quarantined > 0 || h.AwaitingMediaRetry > 0 {
			problem = "archive_or_ingestion_pending"
		}
	}
	return map[string]any{"retry_health": retries, "queue_health": details, "account_id": b.cfg.Account, "status": s, "error": problem,
		"whatsapp_connected": connected, "whatsapp_authenticated": authenticated,
		"pending_count": count, "oldest_pending_at": oldest}
}
func (b *bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path == "/media/requeue" {
		b.serveRequeue(w, r)
		return
	}
	if r.URL.Path != "/healthz" && r.URL.Path != "/status" && r.URL.Path != "/qr" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		http.Error(w, "method not allowed", 405)
		return
	}
	if r.URL.Path == "/healthz" {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, "ok\n")
		return
	}
	if b.cfg.Token == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+b.cfg.Token)) != 1 {
		http.Error(w, "unauthorized", 401)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Path == "/status" {
		_ = json.NewEncoder(w).Encode(b.statusPayload())
		return
	}
	b.mu.RLock()
	var code any
	if b.qr != "" && time.Now().Before(b.qrUntil) {
		code = b.qr
	}
	status := b.status
	b.mu.RUnlock()
	_ = json.NewEncoder(w).Encode(map[string]any{"code": code, "status": status})
}
func (b *bridge) post(ctx context.Context, path string, payload any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return &stageError{stage: "ingestion", reason: "encoding", cause: err}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.cfg.AppURL+path, bytes.NewReader(data))
	if err != nil {
		return &stageError{stage: "ingestion", reason: "request", cause: err}
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := b.http.Do(req)
	if err != nil {
		return &stageError{stage: "ingestion", cause: err}
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &stageError{stage: "ingestion", reason: "http_status", status: resp.StatusCode}
	}
	needsFormat := false
	switch v := payload.(type) {
	case message:
		needsFormat = v.MessageType != ""
	case *messageUpdate:
		needsFormat = true
	}
	if needsFormat && resp.Header.Get("X-Archive-Format") != "2" {
		return &stageError{stage: "ingestion", reason: "unsupported_archive"}
	}
	return nil
}
func (b *bridge) reportStatus(ctx context.Context) {
	c, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_ = b.post(c, "/internal/accounts/status", b.statusPayload())
}
func (b *bridge) connect(ctx context.Context) {
	for ctx.Err() == nil {
		if b.client.Store.ID != nil {
			if b.client.ConnectContext(ctx) == nil {
				return
			}
			b.setStatus("disconnected")
		} else {
			b.setStatus("awaiting_pairing")
			channel, err := b.client.GetQRChannel(ctx)
			if err == nil {
				err = b.client.ConnectContext(ctx)
			}
			if err == nil {
				for item := range channel {
					switch item.Event {
					case "code":
						b.mu.Lock()
						b.qr = item.Code
						b.qrUntil = time.Now().Add(item.Timeout)
						b.mu.Unlock()
					case "success":
						b.setStatus("connected")
						return
					default:
						b.mu.Lock()
						b.qr = ""
						b.qrUntil = time.Time{}
						b.mu.Unlock()
					}
				}
			}
			b.client.Disconnect()
			b.setStatus("awaiting_pairing")
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(10 * time.Second):
		}
	}
}
func run() error {
	c, err := loadConfig()
	if err != nil {
		return err
	}
	sp, err := newSpool(c.Spool, c.Account)
	if err != nil {
		return errors.New("spool initialization failed")
	}
	defer sp.close()
	// Remove only unfinished temporary downloads/writes left by this instance.
	entries, _ := os.ReadDir(c.Spool)
	for _, entry := range entries {
		if entry.Type().IsRegular() && (len(entry.Name()) > 10 && entry.Name()[:10] == ".download-" || len(entry.Name()) > 5 && entry.Name()[:5] == ".tmp-") {
			_ = os.Remove(c.Spool + "/" + entry.Name())
		}
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	dbctx, dbcancel := context.WithTimeout(ctx, 30*time.Second)
	if err = bindSessionAccount(dbctx, c.SessionDB, c.Account); err != nil {
		dbcancel()
		return errors.New("session account binding failed")
	}
	container, err := sqlstore.New(dbctx, "postgres", c.SessionDB, nil)
	dbcancel()
	if err != nil {
		return errors.New("session database initialization failed")
	}
	defer container.Close()
	devices, err := container.GetAllDevices(ctx)
	if err != nil || len(devices) > 1 {
		return errors.New("session database must contain at most one account")
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return errors.New("session lookup failed")
	}
	// Device properties are advertised when pairing a new companion. Existing
	// sessions retain the name WhatsApp registered; do not force a re-pair.
	store.SetOSInfo("Linux", [3]uint32{0, 1, 0})
	store.DeviceProps.PlatformType = waCompanionReg.DeviceProps_DESKTOP.Enum()
	client := whatsmeow.NewClient(device, nil)
	client.SetMediaHTTPClient(&http.Client{Timeout: 2 * time.Minute, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }})
	var s3 *minio.Client
	if c.StorageBackend == "disk" {
		if err = initializeDisk(c); err != nil {
			return errors.New("archive configuration failed")
		}
	} else {
		s3, err = newS3(c)
		if err != nil {
			return errors.New("archive configuration failed")
		}
	}
	b := &bridge{cfg: c, spool: sp, client: client, download: client.DownloadToFile, s3: s3, http: &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, status: "disconnected", wake: make(chan struct{}, 1), fatal: make(chan struct{})}
	b.sendMediaRetry = client.SendMediaRetryReceipt
	b.statusWake = make(chan struct{}, 1)
	b.metadata, err = newIdentityCache(sp.dir, c.Account)
	if err != nil {
		return errors.New("identity metadata initialization failed")
	}
	b.recoverCapturedIdentities()
	b.registerEvents()
	server := &http.Server{Addr: ":8080", Handler: b, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	serverErr := make(chan error, 1)
	go func() { serverErr <- server.ListenAndServe() }()
	var wg sync.WaitGroup
	wg.Add(4)
	go func() { defer wg.Done(); b.retry(ctx) }()
	go func() { defer wg.Done(); b.heartbeat(ctx, 30*time.Second) }()
	go func() { defer wg.Done(); b.reconcileIdentities(ctx) }()
	go func() { defer wg.Done(); b.connect(ctx) }()
	select {
	case <-ctx.Done():
	case <-b.fatal:
		err = errors.New("durable spool persistence failed; stopping receiver")
	case e := <-serverErr:
		if !errors.Is(e, http.ErrServerClosed) {
			err = errors.New("private HTTP listener failed")
		}
	}
	cancel()
	client.Disconnect()
	shutdown, done := context.WithTimeout(context.Background(), 10*time.Second)
	defer done()
	_ = server.Shutdown(shutdown)
	wg.Wait()
	return err
}
func main() {
	if err := run(); err != nil {
		log.Print(err.Error())
		os.Exit(1)
	}
}

// The database belongs to one logical account even if a PVC is accidentally
// remounted or a second instance is configured with the wrong credentials.
func bindSessionAccount(ctx context.Context, dsn, account string) error {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	if _, err = db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS bridge_account (singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton), account_id text NOT NULL)`); err != nil {
		return err
	}
	if _, err = db.ExecContext(ctx, `INSERT INTO bridge_account (singleton,account_id) VALUES (TRUE,$1) ON CONFLICT (singleton) DO NOTHING`, account); err != nil {
		return err
	}
	var stored string
	if err = db.QueryRowContext(ctx, `SELECT account_id FROM bridge_account WHERE singleton=TRUE`).Scan(&stored); err != nil {
		return err
	}
	if stored != account {
		return errors.New("session account mismatch")
	}
	return nil
}

// Status delivery has its own scheduler and request timeout: a media download
// or a slow archive callback cannot stop liveness reports.
func (b *bridge) heartbeat(ctx context.Context, interval time.Duration) {
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		b.reportStatus(ctx)
		select {
		case <-ctx.Done():
			return
		case <-b.statusWake:
		case <-tick.C:
		}
	}
}
