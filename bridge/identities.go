package main

// Metadata reconciliation reads only the already synchronized local Whatsmeow
// stores. It never requests contacts/groups from WhatsApp or writes its stores.
import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const identityStateFile = ".identity-state" // Deliberately outside recordPattern.
var unversionedObservation = time.Unix(0, 0).UTC()
var lidIdentityPattern = regexp.MustCompile(`^[0-9]{1,30}@lid$`)
var pnIdentityPattern = regexp.MustCompile(`^[1-9][0-9]{4,14}@s[.]whatsapp[.]net$`)
var identityKinds = []string{"contact_full", "contact_first", "business", "push", "chat"}

type identityName struct {
	JID        string    `json:"jid"`
	Kind       string    `json:"kind"`
	Name       string    `json:"name"`
	ObservedAt time.Time `json:"observed_at"`
}
type identityLink struct {
	LID        string    `json:"lid"`
	PN         string    `json:"pn"`
	ObservedAt time.Time `json:"observed_at"`
}
type identityBatch struct {
	Account string         `json:"account_id"`
	Names   []identityName `json:"names"`
	Links   []identityLink `json:"links"`
}
type identityState struct {
	Account                  string                    `json:"account_id"`
	Names                    map[string]identityName   `json:"names"`
	Links                    map[string]identityLink   `json:"links"`
	LinkConflicts            map[string][]identityLink `json:"link_conflicts,omitempty"`
	LocalContactObservations map[string]time.Time      `json:"local_contact_observations,omitempty"`
}
type identityCache struct {
	mu    sync.Mutex
	state identityState
	dir   string
	wake  chan struct{}
	// Injectable read-only cache functions keep synthetic fixtures independent of
	// session databases. Production uses account-scoped Contacts and bound LIDs.
	contact   func(context.Context, types.JID) (types.ContactInfo, error)
	alternate func(context.Context, types.JID) (types.JID, error)
}

func newIdentityCache(dir, account string) (*identityCache, error) {
	c := &identityCache{dir: dir, wake: make(chan struct{}, 1), state: identityState{Account: account, Names: map[string]identityName{}, Links: map[string]identityLink{}, LinkConflicts: map[string][]identityLink{}}}
	data, err := os.ReadFile(filepath.Join(dir, identityStateFile))
	if err == nil {
		if json.Unmarshal(data, &c.state) != nil || c.state.Account != account || c.state.Names == nil || c.state.Links == nil {
			return nil, errors.New("invalid identity state binding")
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if c.state.LinkConflicts == nil {
		c.state.LinkConflicts = map[string][]identityLink{}
	}
	if c.state.LocalContactObservations == nil {
		c.state.LocalContactObservations = map[string]time.Time{}
	}
	return c, nil
}
func identityJID(raw string) (types.JID, bool) {
	if len(raw) > 300 || strings.IndexFunc(raw, func(r rune) bool {
		return unicode.IsSpace(r) || unicode.IsControl(r) || unicode.In(r, unicode.Cf, unicode.Cs)
	}) >= 0 {
		return types.JID{}, false
	}
	j, err := types.ParseJID(raw)
	j = j.ToNonAD()
	if err != nil || j.User == "" || j.Server == "" || strings.ContainsAny(j.User, " \t\r\n/@") {
		return types.JID{}, false
	}
	switch j.Server {
	case types.DefaultUserServer, types.HiddenUserServer, types.GroupServer, types.BroadcastServer, types.NewsletterServer:
		return j, true
	}
	return types.JID{}, false
}
func sourceTime(values ...time.Time) time.Time {
	for _, t := range values {
		if !t.IsZero() {
			return t.UTC()
		}
	}
	return unversionedObservation
}
func (c *identityCache) signal() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}
func (c *identityCache) name(jid, kind, name string, at time.Time) {
	name = strings.TrimSpace(name)
	if kind == "push" && name == "-" {
		return
	}
	j, ok := identityJID(jid)
	if utf8.RuneCountInString(name) > 200 || strings.IndexFunc(name, func(r rune) bool { return unicode.IsControl(r) || unicode.In(r, unicode.Cf, unicode.Cs) }) >= 0 {
		return
	}
	if !ok {
		return
	}
	valid := false
	for _, v := range identityKinds {
		if kind == v {
			valid = true
		}
	}
	if !valid {
		return
	}
	n := identityName{j.String(), kind, name, sourceTime(at)}
	c.mu.Lock()
	key := n.JID + "\x00" + kind
	old, exists := c.state.Names[key]
	// Cache/history snapshots without a source version are fallback only. They
	// never acquire the reconcile time, which would hide newer source events.
	changed := !exists || n.ObservedAt.After(old.ObservedAt) || (n.ObservedAt.Equal(old.ObservedAt) && n.Name == "" && old.Name != "")
	if changed {
		c.state.Names[key] = n
		delete(c.state.LocalContactObservations, key)
	}
	c.mu.Unlock()
	if changed {
		c.signal()
	}
}
func (c *identityCache) link(a, b string, at time.Time) {
	x, ok := identityJID(a)
	if !ok {
		return
	}
	y, ok := identityJID(b)
	if !ok {
		return
	}
	if x.Server == types.DefaultUserServer && y.Server == types.HiddenUserServer {
		x, y = y, x
	}
	if x.Server != types.HiddenUserServer || y.Server != types.DefaultUserServer || !lidIdentityPattern.MatchString(x.String()) || !pnIdentityPattern.MatchString(y.String()) {
		return
	}
	n := identityLink{x.String(), y.String(), sourceTime(at)}
	c.mu.Lock()
	old, exists := c.state.Links[n.LID]
	changed := !exists || n.ObservedAt.After(old.ObservedAt)
	if changed {
		c.state.Links[n.LID] = n
		delete(c.state.LinkConflicts, n.LID)
	} else if n.ObservedAt.Equal(old.ObservedAt) && n.PN != old.PN {
		// Preserve conflicting explicit evidence for the service's ambiguity rule;
		// never choose a phone number based on arrival order.
		duplicate := false
		for _, v := range c.state.LinkConflicts[n.LID] {
			if v.PN == n.PN {
				duplicate = true
			}
		}
		if !duplicate {
			c.state.LinkConflicts[n.LID] = append(c.state.LinkConflicts[n.LID], n)
			changed = true
		}
	}
	c.mu.Unlock()
	if changed {
		c.signal()
	}
}
func (c *identityCache) persist() error {
	c.mu.Lock()
	data, err := json.Marshal(c.state)
	c.mu.Unlock()
	if err != nil {
		return err
	}
	return atomicWrite(c.dir, identityStateFile, data)
}
func (b *bridge) observeMessage(e *events.Message, chatName string) {
	c := b.metadata
	if c == nil {
		return
	}
	// Source timestamps are retained even for old history messages. Missing names
	// in ordinary message envelopes are not name-removal events.
	if e.Info.PushName != "" {
		c.name(e.Info.Sender.String(), "push", e.Info.PushName, e.Info.Timestamp)
	}
	if chatName != "" {
		c.name(e.Info.Chat.String(), "chat", chatName, unversionedObservation)
	}
	c.link(e.Info.Sender.String(), e.Info.SenderAlt.String(), e.Info.Timestamp)
	c.link(e.Info.Chat.String(), e.Info.RecipientAlt.String(), e.Info.Timestamp)
}
func (b *bridge) observeContact(e *events.Contact) {
	c := b.metadata
	if c == nil || e.Action == nil {
		return
	}
	// The action is a full contact-name observation; empty names are tombstones.
	c.name(e.JID.String(), "contact_full", e.Action.GetFullName(), e.Timestamp)
	c.name(e.JID.String(), "contact_first", e.Action.GetFirstName(), e.Timestamp)
	c.link(e.Action.GetLidJID(), e.Action.GetPnJID(), e.Timestamp)
}
func (b *bridge) observePushName(e *events.PushName) {
	c := b.metadata
	if c == nil {
		return
	}
	at := time.Now().UTC()
	if e.Message != nil {
		at = sourceTime(e.Message.Timestamp)
	}
	c.name(e.JID.String(), "push", e.NewPushName, at)
	c.name(e.JIDAlt.String(), "push", e.NewPushName, at)
	c.link(e.JID.String(), e.JIDAlt.String(), at)
}
func (b *bridge) observeBusinessName(e *events.BusinessName) {
	c := b.metadata
	if c == nil {
		return
	}
	at := time.Now().UTC()
	if e.Message != nil {
		at = sourceTime(e.Message.Timestamp)
	}
	c.name(e.JID.String(), "business", e.NewBusinessName, at)
}
func (b *bridge) observeGroup(jid types.JID, name string, at time.Time) {
	if b.metadata != nil && jid.Server == types.GroupServer {
		b.metadata.name(jid.String(), "chat", name, at)
	}
}
func (b *bridge) observeHistory(e *events.HistorySync) {
	c := b.metadata
	if c == nil || e.Data == nil {
		return
	}
	// Upstream populates historical push-name/LID caches asynchronously. Consume
	// the explicit payload as well, then wake a fresh referenced-identity scan.
	for _, n := range e.Data.GetPushnames() {
		if n.Pushname != nil {
			c.name(n.GetID(), "push", n.GetPushname(), unversionedObservation)
		}
	}
	for _, l := range e.Data.GetPhoneNumberToLidMappings() {
		c.link(l.GetLidJID(), l.GetPnJID(), unversionedObservation)
	}
	for _, chat := range e.Data.GetConversations() {
		if chat.Name != nil {
			c.name(chat.GetID(), "chat", chat.GetName(), unversionedObservation)
		}
	}
	c.signal()
}
func (b *bridge) configureIdentityReads() {
	c := b.metadata
	if b.client == nil || b.client.Store == nil {
		return
	}
	if c.contact == nil && b.client.Store.Contacts != nil {
		c.contact = b.client.Store.Contacts.GetContact
	}
	if c.alternate == nil && b.client.Store.LIDs != nil {
		c.alternate = func(ctx context.Context, j types.JID) (types.JID, error) {
			switch j.Server {
			case types.HiddenUserServer:
				return b.client.Store.LIDs.GetPNForLID(ctx, j)
			case types.DefaultUserServer:
				return b.client.Store.LIDs.GetLIDForPN(ctx, j)
			}
			return types.JID{}, nil
		}
	}
}
func (b *bridge) identityReferences(ctx context.Context, after string) ([]string, string, bool, error) {
	target := b.cfg.AppURL + "/internal/accounts/" + url.PathEscape(b.cfg.Account) + "/identities?limit=100&after=" + url.QueryEscape(after)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, "", false, err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	resp, err := b.http.Do(req)
	if err != nil {
		return nil, "", false, errors.New("identity references unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "", false, errors.New("identity references rejected")
	}
	var page struct {
		Items []string `json:"items"`
		Next  *string  `json:"next_after"`
		More  bool     `json:"has_more"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 128*1024)).Decode(&page) != nil || len(page.Items) > 100 {
		return nil, "", false, errors.New("invalid identity references")
	}
	next := ""
	if page.Next != nil {
		next = *page.Next
	}
	if page.More && (next == "" || next <= after) {
		return nil, "", false, errors.New("invalid identity cursor")
	}
	return page.Items, next, page.More, nil
}
func (b *bridge) reconcileIdentityPage(ctx context.Context, after string) (string, bool, error) {
	c := b.metadata
	refs, next, more, err := b.identityReferences(ctx, after)
	if err != nil {
		return after, true, err
	}
	allowed := map[string]bool{}
	for _, raw := range refs {
		if j, ok := identityJID(raw); ok {
			allowed[j.String()] = true
		}
	}
	// Expand only explicit cached LID/PN pairs for referenced archive identities.
	// No phonebook enumeration, matching digits, or name-based entity guesses.
	for raw := range allowed {
		j, _ := identityJID(raw)
		if c.alternate != nil {
			alt, e := c.alternate(ctx, j)
			if e != nil {
				return after, true, e
			}
			c.link(j.String(), alt.String(), unversionedObservation)
		}
	}
	c.mu.Lock()
	links := []identityLink{}
	unambiguous := []identityLink{}
	for _, l := range c.state.Links {
		conflicts := c.state.LinkConflicts[l.LID]
		referenced := allowed[l.LID] || allowed[l.PN]
		for _, v := range conflicts {
			if allowed[v.PN] {
				referenced = true
			}
		}
		if referenced {
			links = append(links, l)
			links = append(links, conflicts...)
			if len(conflicts) == 0 {
				unambiguous = append(unambiguous, l)
			}
		}
	}
	c.mu.Unlock()
	for _, l := range unambiguous {
		allowed[l.LID], allowed[l.PN] = true, true
	}
	if c.contact != nil {
		contacts := map[string]types.ContactInfo{}
		snapshots := map[string]map[string]identityName{}
		observed := map[string]time.Time{}
		for raw := range allowed {
			j, _ := identityJID(raw)
			if j.Server != types.DefaultUserServer && j.Server != types.HiddenUserServer {
				continue
			}
			snapshots[raw] = c.contactSnapshot(raw)
			contact, e := c.contact(ctx, j)
			if e != nil {
				return after, true, e
			}
			if contact.Found {
				contacts[raw] = contact
				observed[raw] = time.Now().UTC()
			}
		}
		for raw, contact := range contacts {
			for kind, name := range map[string]string{"contact_full": contact.FullName, "contact_first": contact.FirstName} {
				// An empty field on a push-only counterpart row is not evidence that the
				// saved name on the other explicitly linked address was removed.
				counterpartPopulated := false
				if strings.TrimSpace(name) == "" {
					for _, l := range unambiguous {
						other := ""
						if raw == l.LID {
							other = l.PN
						} else if raw == l.PN {
							other = l.LID
						}
						if alt, ok := contacts[other]; ok && ((kind == "contact_full" && strings.TrimSpace(alt.FullName) != "") || (kind == "contact_first" && strings.TrimSpace(alt.FirstName) != "")) {
							counterpartPopulated = true
						}
					}
				}
				if !counterpartPopulated {
					c.cachedContactName(raw, kind, name, observed[raw], snapshots[raw][kind])
				}
			}
			// Business/push caches can contain asynchronously replayed history and
			// remain unversioned fallback observations, unlike saved contact fields.
			for kind, name := range map[string]string{"business": contact.BusinessName, "push": contact.PushName} {
				if name != "" {
					c.name(raw, kind, name, unversionedObservation)
				}
			}
		}
	}

	if err = c.persist(); err != nil {
		return after, true, err
	}
	batch := identityBatch{Account: b.cfg.Account, Names: []identityName{}, Links: []identityLink{}}
	c.mu.Lock()
	for _, n := range c.state.Names {
		if allowed[n.JID] {
			batch.Names = append(batch.Names, n)
		}
	}
	c.mu.Unlock()
	sort.Slice(batch.Names, func(i, j int) bool {
		a, z := batch.Names[i], batch.Names[j]
		return a.JID+"\x00"+a.Kind < z.JID+"\x00"+z.Kind
	})
	sort.Slice(links, func(i, j int) bool { return links[i].LID < links[j].LID })
	// Each request has at most 100 combined observations, including links.
	for len(batch.Names) > 0 || len(links) > 0 {
		out := identityBatch{Account: b.cfg.Account, Names: []identityName{}, Links: []identityLink{}}
		n := min(100, len(batch.Names))
		out.Names = batch.Names[:n]
		batch.Names = batch.Names[n:]
		n = min(100-len(out.Names), len(links))
		out.Links = links[:n]
		links = links[n:]
		if err = b.post(ctx, "/internal/identities", out); err != nil {
			return after, true, err
		}
	}
	return next, more, nil
}
func (b *bridge) reconcileIdentities(ctx context.Context) {
	c := b.metadata
	if c == nil {
		return
	}
	b.configureIdentityReads()
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	after := ""
	for {
		// Persist passive group/name changes even while the app is unavailable.
		// Failures retain the in-memory state and retry; no name data is logged.
		persistErr := c.persist()
		if persistErr != nil {
			b.metadataError.Store(true)
		}
		if persistErr == nil {
			pass, cancel := context.WithTimeout(ctx, 20*time.Second)
			next, more, err := b.reconcileIdentityPage(pass, after)
			cancel()
			b.metadataError.Store(err != nil)
			if err == nil {
				if more {
					after = next
				} else {
					after = ""
				}
			}
		}
		select {
		case <-ctx.Done():
			_ = c.persist()
			return
		case <-c.wake:
			// Continue this bounded pass. Restarting at page one for every live
			// observation would starve later identities on a continuously busy account.
			// Completed passes automatically restart, including new history references.
		case <-tick.C:
		}
	}
}

// Recover names already durably captured before the metadata worker could run.
// Message ingestion remains the ACK boundary; this is a separate startup scan.
func (b *bridge) recoverCapturedIdentities() {
	if b.metadata == nil {
		return
	}
	names, err := b.spool.pending()
	if err != nil {
		b.metadataError.Store(true)
		return
	}
	for _, name := range names {
		r, err := b.spool.read(name)
		if err != nil {
			continue
		}
		if r.Message.SenderName != "" {
			b.metadata.name(r.Message.Sender, "push", r.Message.SenderName, r.Message.Timestamp)
		}
		if r.Message.ChatName != "" {
			b.metadata.name(r.Message.Chat, "chat", r.Message.ChatName, unversionedObservation)
		}
	}
}

// The saved contact-name cache is the current account-local address book. It
// can change in a full sync without Contact events, or outlive a crash before
// our asynchronous metadata flush. Changes are local observations at read time,
// not invented provider update times. Unchanged values do not refresh the time.
func (c *identityCache) contactSnapshot(jid string) map[string]identityName {
	c.mu.Lock()
	defer c.mu.Unlock()
	return map[string]identityName{"contact_full": c.state.Names[jid+"\x00contact_full"], "contact_first": c.state.Names[jid+"\x00contact_first"]}
}
func (c *identityCache) cachedContactName(jid, kind, name string, at time.Time, expected identityName) {
	name = strings.TrimSpace(name)
	j, ok := identityJID(jid)
	if !ok || (kind != "contact_full" && kind != "contact_first") || utf8.RuneCountInString(name) > 200 || strings.IndexFunc(name, func(r rune) bool { return unicode.IsControl(r) || unicode.In(r, unicode.Cf, unicode.Cs) }) >= 0 {
		return
	}
	key := j.String() + "\x00" + kind
	c.mu.Lock()
	old, exists := c.state.Names[key]
	// Never overwrite a newer event which raced the cache read. A missing or
	// never-populated field also cannot manufacture a removal observation.
	if old != expected || (exists && old.Name == name) || (!exists && name == "") || (!old.ObservedAt.IsZero() && !at.After(old.ObservedAt)) {
		c.mu.Unlock()
		return
	}
	c.state.Names[key] = identityName{j.String(), kind, name, at.UTC()}
	c.state.LocalContactObservations[key] = at.UTC()
	c.mu.Unlock()
	c.signal()
}
