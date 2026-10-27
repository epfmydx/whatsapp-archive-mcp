package main

import "time"

type laneHealth struct {
	Pending      int            `json:"pending"`
	Oldest       *time.Time     `json:"oldest_captured_at"`
	UnknownTimes int            `json:"unknown_capture_timestamps"`
	Origins      map[string]int `json:"origins"`
}
type detailedQueueHealth struct {
	Text        laneHealth        `json:"text"`
	Media       laneHealth        `json:"media"`
	Unknown     laneHealth        `json:"unknown_lane"`
	Completed   map[string]uint64 `json:"completed_since_start"`
	LastSuccess *time.Time        `json:"last_successful_processing_at"`
	Scope       string            `json:"completion_scope"`
	Retained    int               `json:"retained_unsupported_count"`
}

func newLaneHealth() laneHealth {
	return laneHealth{Origins: map[string]int{"live": 0, "history": 0, "unknown": 0}}
}

// All counters derive from one synchronized in-memory snapshot, not disk scans.
func (s *spool) queueStatus() (any, any, *detailedQueueHealth) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.healthKnown {
		return nil, nil, nil
	}
	h := &detailedQueueHealth{Text: newLaneHealth(), Media: newLaneHealth(), Unknown: newLaneHealth(), Completed: map[string]uint64{"text": s.completed[textLane], "media": s.completed[mediaLane]}, LastSuccess: s.lastSuccess, Scope: "process_lifetime", Retained: s.retainedCount}
	var oldest *time.Time
	unknown := false
	for _, q := range s.queueState {
		lane := &h.Unknown
		if q.Known {
			lane = &h.Text
			if q.Lane == mediaLane {
				lane = &h.Media
			}
		}
		lane.Pending++
		origin := q.Source
		if origin != "live" && origin != "history" {
			origin = "unknown"
		}
		lane.Origins[origin]++
		if q.CapturedAt.IsZero() {
			lane.UnknownTimes++
			unknown = true
		} else {
			t := q.CapturedAt.UTC()
			if lane.Oldest == nil || t.Before(*lane.Oldest) {
				lane.Oldest = &t
			}
			if oldest == nil || t.Before(*oldest) {
				oldest = &t
			}
		}
	}
	var old any
	if !unknown && oldest != nil {
		old = oldest.Format(time.RFC3339Nano)
	}
	return len(s.queueState), old, h
}
func (s *spool) completedAt(lane retryLane, now time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.completed[lane]++
	t := now.UTC()
	s.lastSuccess = &t
}
