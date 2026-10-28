package main

import (
	"context"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"math"
	"math/rand/v2"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Retry is private spool state, not part of the application ingestion contract.
// The pre-attempt write makes a crash subject to the same durable cooldown.
type retryState struct {
	Attempts      uint32    `json:"attempts"`
	LastAttemptAt time.Time `json:"last_attempt_at"`
	NextAttemptAt time.Time `json:"next_attempt_at"`
	LastFailureAt time.Time `json:"last_failure_at,omitempty"`
	Stage         string    `json:"stage,omitempty"`
	Reason        string    `json:"reason,omitempty"`
	HTTPStatus    int       `json:"http_status,omitempty"`
	DurationMS    int64     `json:"duration_ms,omitempty"`
}

type queueSnapshot struct {
	CapturedAt         time.Time
	Lane               retryLane
	Source             string
	Known              bool
	Retry              retryState
	HasRetry           bool
	QuarantineReason   string
	AwaitingMediaRetry bool
}
type retryHealth struct {
	Quarantined        int            `json:"quarantined"`
	AwaitingMediaRetry int            `json:"awaiting_media_retry"`
	QuarantineReasons  map[string]int `json:"quarantine_reasons"`
	Due                int            `json:"due"`
	CoolingDown        int            `json:"cooling_down"`
	Failed             int            `json:"failed"`
	ErrorsByCode       map[string]int `json:"errors_by_code"`
	ErrorsByHTTPStatus map[int]int    `json:"errors_by_http_status"`
	EarliestNextAt     *time.Time     `json:"earliest_next_at"`
	LastFailureAt      *time.Time     `json:"last_failure_at"`
	LastFailureReason  string         `json:"last_failure_reason,omitempty"`
}

func snapshot(r record) queueSnapshot {
	q := queueSnapshot{CapturedAt: r.CapturedAt, Lane: laneFor(r), Source: r.Source, Known: true}
	q.QuarantineReason = r.QuarantineReason
	q.AwaitingMediaRetry = r.Recovery != nil && r.Recovery.State == "awaiting"
	if r.Retry != nil {
		q.Retry = *r.Retry
		q.HasRetry = true
	}
	return q
}
func (s *spool) retryHealth(now time.Time) retryHealth {
	s.mu.Lock()
	defer s.mu.Unlock()
	h := retryHealth{ErrorsByCode: map[string]int{}, ErrorsByHTTPStatus: map[int]int{}, QuarantineReasons: map[string]int{}}
	for _, q := range s.queueState {
		r := q.Retry
		if q.QuarantineReason != "" {
			h.Quarantined++
			h.QuarantineReasons[safeReason(q.QuarantineReason)]++
		} else if q.AwaitingMediaRetry {
			h.AwaitingMediaRetry++
		} else if r.NextAttemptAt.After(now) {
			h.CoolingDown++
			if h.EarliestNextAt == nil || r.NextAttemptAt.Before(*h.EarliestNextAt) {
				v := r.NextAttemptAt
				h.EarliestNextAt = &v
			}
		} else {
			h.Due++
		}
		if !r.LastFailureAt.IsZero() {
			h.Failed++
			reason := safeReason(r.Reason)
			h.ErrorsByCode[reason]++
			if code := safeStatus(r.HTTPStatus); code != 0 {
				h.ErrorsByHTTPStatus[code]++
			}
			if h.LastFailureAt == nil || r.LastFailureAt.After(*h.LastFailureAt) {
				v := r.LastFailureAt
				h.LastFailureAt = &v
				h.LastFailureReason = reason
			}
		}
	}
	return h
}
func (b *bridge) nowUTC() time.Time {
	if b.now != nil {
		return b.now().UTC()
	}
	return time.Now().UTC()
}
func (b *bridge) retryDelay(attempt uint32) time.Duration {
	d := time.Minute
	for n := uint32(1); n < attempt && d < time.Hour; n++ {
		d *= 2
	}
	if d > time.Hour {
		d = time.Hour
	}
	// Positive bounded jitter keeps first retries >=60s and every retry <=1h.
	if d < time.Hour {
		if b.jitter != nil {
			d += b.jitter(d / 5)
		} else {
			d += time.Duration(rand.Int64N(int64(d/5) + 1))
		}
	}
	if d > time.Hour {
		d = time.Hour
	}
	return d
}

type retryLane int

const (
	textLane retryLane = iota
	mediaLane
	retryLaneCount
)

func laneFor(r record) retryLane {
	// Archive saves these fields together. The archived descriptor keeps media
	// in the same lane after MediaPending becomes false, including on restart.
	if !r.InvalidUpdate && r.Update == nil && (r.MediaPending || r.Message.Media != nil) {
		return mediaLane
	}
	return textLane
}

func (b *bridge) stopped(ctx context.Context) bool {
	if ctx.Err() != nil {
		return true
	}
	select {
	case <-b.fatal:
		return true
	default:
		return false
	}
}

func (b *bridge) setLaneError(lane retryLane, failed bool) {
	b.queueHealthMu.Lock()
	defer b.queueHealthMu.Unlock()
	b.laneErrors[lane] = failed
	b.queueError.Store(b.laneErrors[textLane] || b.laneErrors[mediaLane] || b.stopped(context.Background()))
}

func (b *bridge) stopForSpoolFailure() {
	b.queueHealthMu.Lock()
	defer b.queueHealthMu.Unlock()
	b.queueError.Store(true)
	b.fatalOnce.Do(func() {
		if b.fatal != nil {
			close(b.fatal)
		}
	})
}
func (b *bridge) saveRetry(r record) bool {
	if err := b.spool.save(r, false); err != nil {
		b.stopForSpoolFailure()
		return false
	}
	return true
}

// Validation failures cannot change by replaying the same retained payload.
// Preserve it for an explicit decoder repair rather than scheduling it forever.
func (b *bridge) quarantineInvalidUpdate(r *record) bool {
	state := retryState{}
	if r.Retry != nil {
		state = *r.Retry
	}
	state.Stage, state.Reason, state.HTTPStatus = "update_decode", "invalid_metadata", 0
	state.LastFailureAt = b.nowUTC()
	state.NextAttemptAt = time.Time{}
	r.Retry = &state
	r.ArchiveError = state.Reason
	if r.AttemptID == "" {
		r.AttemptID = opaqueID()
	}
	return b.quarantine(r, "invalid_metadata")
}

// drain is a synchronous sweep used by local callers and tests. Production
// runs the same lane sweeps independently so blocked media cannot hold text.
func (b *bridge) drain(ctx context.Context) {
	b.drainLane(ctx, textLane)
	b.drainLane(ctx, mediaLane)
}
func (b *bridge) drainLane(ctx context.Context, lane retryLane) {
	b.drainMu[lane].Lock()
	defer b.drainMu[lane].Unlock()
	if b.stopped(ctx) {
		return
	}
	if lane == mediaLane {
		b.applyRecoveryResponses()
		if b.stopped(ctx) {
			return
		}
	}
	names, err := b.spool.pending()
	if err != nil {
		b.setLaneError(lane, true)
		return
	}
	failed := false
	for _, name := range names {
		if b.stopped(ctx) {
			return
		}
		r, e := b.spool.read(name)
		if e != nil {
			// The other lane may have acknowledged and removed this snapshot entry.
			if !os.IsNotExist(e) {
				failed = true
			}
			continue
		}
		if laneFor(r) != lane {
			continue
		}
		now := b.nowUTC()
		if r.ItemID == "" {
			r.ItemID = opaqueID()
			if !b.saveRetry(r) {
				return
			}
		}
		// Reclassify conservatively normalized legacy controls before ingestion.
		r.ProtocolOnly = false
		if r.Update == nil && !r.InvalidUpdate {
			var p waE2E.Message
			if proto.Unmarshal(r.Proto, &p) == nil {
				controlEvent := &events.Message{Message: &p}
				rawValid := true
				if len(r.RawProto) > 0 {
					var raw waE2E.Message
					rawValid = proto.Unmarshal(r.RawProto, &raw) == nil
					controlEvent.RawMessage = &raw
				}
				r.ProtocolOnly = rawValid && isProtocolOnlyEvent(controlEvent)
			}
		}
		if r.ProtocolOnly {
			if !b.saveRetry(r) {
				return
			}
			if err := b.spool.remove(name); err != nil {
				b.stopForSpoolFailure()
				return
			}
			continue
		}
		if r.QuarantineReason != "" {
			failed = true
			continue
		}
		if r.InvalidUpdate {
			if !b.quarantineInvalidUpdate(&r) {
				return
			}
			failed = true
			continue
		}
		if r.Recovery != nil && r.Recovery.State == "awaiting" {
			b.registerRecovery(r, name)
			// Responses can arrive while earlier items occupy this sweep. Apply them
			// before checking this deadline and discard this stale local copy.
			b.applyRecoveryResponses()
			if b.stopped(ctx) {
				return
			}
			latest, err := b.spool.read(name)
			if err != nil {
				if !os.IsNotExist(err) {
					failed = true
				}
				continue
			}
			r = latest
			if r.QuarantineReason != "" {
				failed = true
				continue
			}
		}
		if r.Recovery != nil && r.Recovery.State == "awaiting" {
			if !r.Recovery.Deadline.After(now) {
				if !b.quarantine(&r, "recovery_timeout") {
					return
				}
			}
			failed = true
			continue
		}
		// Legacy Attempt counts are adopted only until the first recovery/manual
		// cycle. This gives old exhausted failures one bounded phone opportunity.
		networkPending := r.MediaPending
		if networkPending {
			if _, err := os.Stat(filepath.Join(b.spool.dir, recordID(r.Message)+".media")); err == nil {
				networkPending = false
			}
		}
		if networkPending && r.MediaAttempts == 0 && r.Recovery == nil && r.Requeue == nil && r.Retry != nil && (r.Retry.Stage == "media_metadata" || r.Retry.Stage == "media_download") {
			r.MediaAttempts = r.Retry.Attempts
		}
		if networkPending && b.cfg.MediaRecoveryEnabled && needsRecovery(r) {
			b.reserveRecovery(ctx, &r, name)
			failed = true
			if b.stopped(ctx) {
				return
			}
			continue
		}
		if networkPending && r.MediaAttempts >= b.downloadLimit() {
			if !b.quarantine(&r, "download_exhausted") {
				return
			}
			failed = true
			continue
		}
		if r.Retry != nil && r.Retry.NextAttemptAt.After(now) {
			failed = true
			continue
		}
		rs := retryState{}
		if r.Retry != nil {
			rs = *r.Retry
		}
		if rs.Attempts < math.MaxUint32 {
			rs.Attempts++
		}
		rs.LastAttemptAt = now
		rs.NextAttemptAt = now.Add(b.retryDelay(rs.Attempts))
		r.Retry = &rs
		r.AttemptID = opaqueID()
		if networkPending {
			r.MediaAttempts++
		}
		if !b.saveRetry(r) || b.stopped(ctx) {
			return
		}
		started := time.Now()
		jobCtx, cancel := context.WithTimeout(ctx, 3*time.Minute)
		stage := "media_metadata"
		if e == nil && r.Update == nil && r.MediaPending {
			e = b.archive(jobCtx, &r)
		}
		if b.stopped(ctx) {
			cancel()
			return
		}
		if e == nil && r.EncryptedReaction {
			stage = "update_decode"
			e = b.resolveReaction(jobCtx, &r)
		}
		if e == nil {
			stage = "ingestion"
			if r.Update != nil {
				e = b.post(jobCtx, "/internal/message-updates", r.Update)
			} else {
				e = b.post(jobCtx, "/internal/messages", r.Message)
			}
		}
		if e == nil && !b.stopped(ctx) {
			stage = "cleanup"
			if r.Update == nil {
				e = os.Remove(filepath.Join(b.spool.dir, recordID(r.Message)+".media"))
			}
			if os.IsNotExist(e) {
				e = nil
			}
			if e == nil {
				e = b.spool.remove(name)
				if e == nil {
					b.spool.completedAt(lane, b.nowUTC())
				}
			}
		}
		cancel()
		// Shutdown is not an item failure. Preserve the already written cooldown and
		// prior failure metadata, and do not erase data after cancellation.
		if b.stopped(ctx) {
			return
		}
		if e != nil {
			failed = true
			rs = *r.Retry
			rs.LastFailureAt = b.nowUTC()
			rs.NextAttemptAt = rs.LastFailureAt.Add(b.retryDelay(rs.Attempts))
			rs.Stage, rs.Reason, rs.HTTPStatus = classifyStage(stage, e)
			if networkPending && rs.Stage != "media_metadata" && rs.Stage != "media_download" && r.MediaAttempts > 0 {
				r.MediaAttempts--
			}
			if r.Recovery != nil && r.Recovery.State == "refreshed" && rs.Stage == "media_download" {
				r.Recovery.State = "used"
			}
			rs.DurationMS = time.Since(started).Milliseconds()
			r.Retry = &rs
			r.ArchiveError = rs.Reason
			if rs.Stage == "update_decode" && rs.Reason == "invalid_metadata" {
				if !b.quarantineInvalidUpdate(&r) {
					return
				}
				continue
			}
			if !b.saveRetry(r) {
				return
			}
			b.emitDiagnostic(diagnostic{ItemID: r.ItemID, AttemptID: r.AttemptID, Event: "spool_attempt", Stage: rs.Stage, Reason: rs.Reason, HTTPStatus: rs.HTTPStatus, ElapsedMS: rs.DurationMS, Attempt: rs.Attempts, NextRetryAt: &rs.NextAttemptAt})
		}
	}
	b.setLaneError(lane, failed)
}
func (b *bridge) retry(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	var workers sync.WaitGroup
	var wakes [retryLaneCount]chan struct{}
	for lane := textLane; lane < retryLaneCount; lane++ {
		wakes[lane] = make(chan struct{}, 1)
		workers.Add(1)
		go func(lane retryLane) {
			defer workers.Done()
			for {
				b.drainLane(ctx, lane)
				select {
				case <-ctx.Done():
					return
				case <-b.fatal:
					return
				case <-wakes[lane]:
				}
			}
		}(lane)
	}
	// Cancel in-flight I/O before joining; spool ownership outlives both lanes.
	defer func() { cancel(); workers.Wait() }()
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-b.fatal:
			return
		case <-b.wake:
		case <-tick.C:
		}
		// One notification per lane is sufficient: each sweep takes a fresh
		// snapshot, and a capture during a sweep queues the next sweep.
		for _, wake := range wakes {
			select {
			case wake <- struct{}{}:
			default:
			}
		}
	}
}
