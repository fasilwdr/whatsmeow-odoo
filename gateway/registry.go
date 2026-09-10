// Registry: which Odoo owns a session, and where its events go.
//
// One gateway used to mean one Odoo — the API key, the webhook URL and the
// webhook secret were three package-level variables read from the environment.
// Serving several Odoos from one process needs those three facts *per session*
// instead, and they have to survive a restart: restoreExisting reconnects
// paired sessions at boot, and WhatsApp starts delivering before any Odoo has
// said a word to us.
//
// So they live in one small JSON file, `$WMG_DATA_DIR/registry.json`, loaded
// into memory at boot. It is deliberately not a database: a handful of sessions
// per host, and an operator who can read (and, when a client moves domain,
// edit) the file with nothing but `cat` is worth more here than SQL.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// Client credentials
// ---------------------------------------------------------------------------

// A client is one Odoo install: one `whatsmeow.connection` record, one API key.
// The key names the client, and a client can only ever address the sessions
// filed under its own name — that separation is what makes a shared gateway
// safe, and it is why the key is no longer a single global comparison.
type apiClient struct {
	Label   string
	KeyHash string
}

// apiClients maps sha256(key) -> client. Hashed so a heap dump or a stray log
// line cannot hand over a working credential.
var apiClients = map[string]apiClient{}

var clientLabelRe = regexp.MustCompile(`^[a-z0-9_-]{1,40}$`)

func hashKey(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

// loadAPIKeys reads WMG_API_KEYS ("acme:key,globex:key") plus the original
// single-key WMG_API_KEY, which keeps working forever as the client `default`
// so an existing install upgrades without touching its configuration.
func loadAPIKeys() error {
	apiClients = map[string]apiClient{}
	add := func(label, key string) error {
		if !clientLabelRe.MatchString(label) {
			return fmt.Errorf("invalid client label %q (use a-z, 0-9, '-', '_')", label)
		}
		if len(key) < 16 {
			return fmt.Errorf("client %q: key is too short to be a secret", label)
		}
		h := hashKey(key)
		if prev, ok := apiClients[h]; ok {
			return fmt.Errorf("clients %q and %q share one key; they would see "+
				"each other's sessions", prev.Label, label)
		}
		apiClients[h] = apiClient{Label: label, KeyHash: h}
		return nil
	}

	if legacy := os.Getenv("WMG_API_KEY"); legacy != "" {
		if err := add(defaultClientLabel, legacy); err != nil {
			return err
		}
	}
	for _, pair := range strings.Split(os.Getenv("WMG_API_KEYS"), ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		label, key, ok := strings.Cut(pair, ":")
		if !ok {
			return fmt.Errorf("WMG_API_KEYS entry %q is not label:key", pair)
		}
		if err := add(strings.TrimSpace(label), strings.TrimSpace(key)); err != nil {
			return err
		}
	}
	if len(apiClients) == 0 {
		return fmt.Errorf("no API keys configured: set WMG_API_KEY or WMG_API_KEYS")
	}
	return nil
}

const defaultClientLabel = "default"

// clientFor resolves a presented key. The lookup is by hash, so an unknown key
// costs the same as a known one and never reaches a string comparison.
func clientFor(key string) (apiClient, bool) {
	if key == "" {
		return apiClient{}, false
	}
	c, ok := apiClients[hashKey(key)]
	return c, ok
}

// ---------------------------------------------------------------------------
// The registry file
// ---------------------------------------------------------------------------

// registryEntry is one session: who owns it, where its store lives, and where
// its events go.
type registryEntry struct {
	// Client owns the session. Sessions are namespaced by it, so two unrelated
	// Odoos may both call their number `main` without colliding — which they
	// will, because `main` is what everyone types.
	Client string `json:"client"`
	Name   string `json:"name"`
	// UID scopes everything on disk and in memory: the store file, the media
	// directory, the idempotency keys, the hourly check budget. Opaque and
	// generated here, never client-supplied, so no client's naming reaches the
	// filesystem of a shared host.
	UID string `json:"uid"`
	// Store is the sqlite store's path relative to WMG_DATA_DIR. Recorded
	// rather than derived so an adopted single-tenant install keeps the file it
	// already has, paired, exactly where it is.
	Store string `json:"store"`

	WebhookURL    string `json:"webhook_url"`
	WebhookSecret string `json:"webhook_secret"`
	// APIKeyHash records which key last claimed this session. Authorisation is
	// by Client, not by this — a client that rotates its key keeps its sessions
	// — but it is what tells an operator which credential is actually in use.
	APIKeyHash string `json:"api_key_hash,omitempty"`
	Label      string `json:"label,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (e registryEntry) storePath() string {
	return filepath.Join(dataDir, filepath.FromSlash(e.Store))
}

func (e registryEntry) mediaDirPath() string {
	return filepath.Join(mediaDir, e.UID)
}

type registryFile struct {
	Version  int             `json:"version"`
	Sessions []registryEntry `json:"sessions"`
}

type registry struct {
	mu      sync.RWMutex
	path    string
	entries map[string]registryEntry // "client/name"
}

var reg = &registry{entries: map[string]registryEntry{}}

func registryKey(client, name string) string { return client + "/" + name }

func newUID() string {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		// Never expected; a predictable uid is still unique enough to be safe
		// here because it only has to not collide with an existing one.
		return fmt.Sprintf("%016x", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}

func (r *registry) load(path string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.path = path
	r.entries = map[string]registryEntry{}

	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil // first boot, or a fresh install
	}
	if err != nil {
		return err
	}
	var f registryFile
	if err := json.Unmarshal(raw, &f); err != nil {
		// Refuse to start rather than run with an empty registry: every session
		// would be un-owned, and the next start would claim a *new* store and
		// ask for a fresh QR on a number that is already paired.
		return fmt.Errorf("registry %s is not valid JSON: %w", path, err)
	}
	for _, e := range f.Sessions {
		if e.Client == "" || e.Name == "" || e.UID == "" || e.Store == "" {
			log.Printf("registry: skipping incomplete entry %+v", e)
			continue
		}
		r.entries[registryKey(e.Client, e.Name)] = e
	}
	return nil
}

// saveLocked writes the file atomically. os.WriteFile truncates before it
// writes, so a crash mid-write would leave a zero-byte registry — every session
// silently loses its webhook target, with nothing in any log to say why. Write
// a temporary file, fsync it, rename it over the top, then fsync the directory
// so the rename itself survives a power cut.
func (r *registry) saveLocked() error {
	if r.path == "" {
		return nil // tests that never persist
	}
	f := registryFile{Version: 1, Sessions: make([]registryEntry, 0, len(r.entries))}
	for _, e := range r.entries {
		f.Sessions = append(f.Sessions, e)
	}
	sort.Slice(f.Sessions, func(i, j int) bool {
		if f.Sessions[i].Client != f.Sessions[j].Client {
			return f.Sessions[i].Client < f.Sessions[j].Client
		}
		return f.Sessions[i].Name < f.Sessions[j].Name
	})
	buf, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	buf = append(buf, '\n')

	tmp := r.path + ".tmp"
	fh, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := fh.Write(buf); err != nil {
		fh.Close()
		os.Remove(tmp)
		return err
	}
	if err := fh.Sync(); err != nil {
		fh.Close()
		os.Remove(tmp)
		return err
	}
	if err := fh.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, r.path); err != nil {
		os.Remove(tmp)
		return err
	}
	if d, err := os.Open(filepath.Dir(r.path)); err == nil {
		_ = d.Sync()
		d.Close()
	}
	return nil
}

func (r *registry) get(client, name string) (registryEntry, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.entries[registryKey(client, name)]
	return e, ok
}

// webhookFor is read on the delivery goroutine, once per event, so a re-point
// takes effect on the very next event rather than at the next restart.
func (r *registry) webhookFor(client, name string) (string, string) {
	e, ok := r.get(client, name)
	if !ok {
		return "", ""
	}
	return e.WebhookURL, e.WebhookSecret
}

func (r *registry) list(client string) []registryEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := []registryEntry{}
	for _, e := range r.entries {
		if e.Client == client {
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (r *registry) all() []registryEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]registryEntry, 0, len(r.entries))
	for _, e := range r.entries {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool {
		return registryKey(out[i].Client, out[i].Name) < registryKey(out[j].Client, out[j].Name)
	})
	return out
}

func (r *registry) count(client string) int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	n := 0
	for _, e := range r.entries {
		if e.Client == client {
			n++
		}
	}
	return n
}

// claim returns the session, creating it on first use. A session already filed
// under this client is *updated* — that is how a client whose Odoo moved to a
// new domain re-points it, and it is why Odoo re-asserts the URL on every
// start rather than only at pairing time. It cannot reach another client's
// session: the client is half of the key.
func (r *registry) claim(c apiClient, name, webhookURL, webhookSecret, label string) (registryEntry, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	key := registryKey(c.Label, name)
	e, exists := r.entries[key]
	now := time.Now().UTC()
	if !exists {
		// The per-client session cap is enforced by handleStart, which can
		// answer 409; here it would only ever be a 500.
		uid := newUID()
		e = registryEntry{
			Client:    c.Label,
			Name:      name,
			UID:       uid,
			Store:     "sessions/" + uid + ".db",
			CreatedAt: now,
		}
	}
	if webhookURL != "" {
		e.WebhookURL = webhookURL
	}
	if webhookSecret != "" {
		e.WebhookSecret = webhookSecret
	}
	if label != "" {
		e.Label = label
	}
	e.APIKeyHash = c.KeyHash
	e.UpdatedAt = now
	r.entries[key] = e
	if err := r.saveLocked(); err != nil {
		return registryEntry{}, err
	}
	return e, nil
}

func (r *registry) setWebhook(client, name, webhookURL, webhookSecret string) (registryEntry, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	key := registryKey(client, name)
	e, ok := r.entries[key]
	if !ok {
		return registryEntry{}, os.ErrNotExist
	}
	e.WebhookURL = webhookURL
	if webhookSecret != "" {
		e.WebhookSecret = webhookSecret
	}
	e.UpdatedAt = time.Now().UTC()
	r.entries[key] = e
	if err := r.saveLocked(); err != nil {
		return registryEntry{}, err
	}
	return e, nil
}

func (r *registry) remove(client, name string) (registryEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	key := registryKey(client, name)
	e, ok := r.entries[key]
	if !ok {
		return registryEntry{}, false
	}
	delete(r.entries, key)
	if err := r.saveLocked(); err != nil {
		log.Printf("registry: removing %s failed to persist: %v", key, err)
	}
	return e, true
}

// ---------------------------------------------------------------------------
// Adopting a single-tenant install
// ---------------------------------------------------------------------------

// adoptLegacy files the sessions of a pre-registry install under the `default`
// client, with the webhook target that used to live in the environment. It runs
// once, on the first boot after the upgrade, and is a no-op afterwards — so an
// existing gateway keeps sending and receiving with no configuration change at
// all, and the operator adds other clients when they have one.
//
// The store files are left exactly where they are. Moving a paired device's
// sqlite store to a tidier path would be a needless risk for zero benefit.
func (r *registry) adoptLegacy(defaultURL, defaultSecret string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.entries) > 0 {
		return nil
	}
	legacyKey := os.Getenv("WMG_API_KEY")
	if legacyKey == "" {
		return nil
	}
	entries, err := os.ReadDir(dataDir)
	if err != nil {
		return err
	}
	now := time.Now().UTC()
	adopted := 0
	for _, de := range entries {
		if de.IsDir() || !strings.HasSuffix(de.Name(), ".db") {
			continue
		}
		name := strings.TrimSuffix(de.Name(), ".db")
		if !sessionNameRe.MatchString(name) {
			continue
		}
		r.entries[registryKey(defaultClientLabel, name)] = registryEntry{
			Client: defaultClientLabel,
			Name:   name,
			// The uid *is* the old name here, which keeps `media/<name>/`
			// pointing at the files Odoo has not collected yet.
			UID:           name,
			Store:         de.Name(),
			WebhookURL:    defaultURL,
			WebhookSecret: defaultSecret,
			APIKeyHash:    hashKey(legacyKey),
			Label:         "adopted from WMG_ODOO_WEBHOOK_URL",
			CreatedAt:     now,
			UpdatedAt:     now,
		}
		adopted++
	}
	if adopted == 0 {
		return nil
	}
	log.Printf("registry: adopted %d existing session(s) as client %q", adopted, defaultClientLabel)
	return r.saveLocked()
}

// ---------------------------------------------------------------------------
// Webhook URL validation
// ---------------------------------------------------------------------------

// The gateway now POSTs to URLs supplied by its clients, so a mistyped or
// hostile one turns it into a request forwarder for whatever it can reach. The
// guard is deliberately mild — the caller is already authenticated — but a
// shared host should not be talking to its own internal network on request.
//
// A gateway bound to loopback is a single-host install where Odoo *is* on
// 127.0.0.1, so private targets are allowed there by default; one bound to an
// interface is shared, and they are not.
var (
	allowPrivateWebhooks  = envBoolOr("WMG_WEBHOOK_ALLOW_PRIVATE", listensOnLoopback())
	allowInsecureWebhooks = envBoolOr("WMG_WEBHOOK_ALLOW_INSECURE", false)
	maxSessionsPerClient  = envIntOr("WMG_MAX_SESSIONS_PER_CLIENT", 10)
)

func envBoolOr(key string, def bool) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "":
		return def
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		log.Printf("invalid %s, using %v", key, def)
		return def
	}
}

func listensOnLoopback() bool {
	host, _, err := net.SplitHostPort(listenAddr)
	if err != nil {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func isPrivateHost(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ips := []net.IP{}
	if ip := net.ParseIP(host); ip != nil {
		ips = append(ips, ip)
	} else {
		resolved, err := net.LookupIP(host)
		if err != nil {
			// A name we cannot resolve is not evidence of anything. Let it
			// through; delivery will fail loudly and visibly instead.
			return false
		}
		ips = resolved
	}
	for _, ip := range ips {
		if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsUnspecified() {
			return true
		}
	}
	return false
}

func validateWebhookURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("webhook_url is not a URL")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("webhook_url must be http or https")
	}
	private := isPrivateHost(u.Hostname())
	if private && !allowPrivateWebhooks {
		return fmt.Errorf("webhook_url points inside this host's network; " +
			"set WMG_WEBHOOK_ALLOW_PRIVATE=1 if that is deliberate")
	}
	if u.Scheme == "http" && !private && !allowInsecureWebhooks {
		return fmt.Errorf("webhook_url must use https (the webhook secret and " +
			"every message travel over it); set WMG_WEBHOOK_ALLOW_INSECURE=1 to override")
	}
	return nil
}
