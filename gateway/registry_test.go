package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// resetGatewayState gives one test its own registry and session manager, and
// puts the package-level ones back afterwards.
func resetGatewayState(t *testing.T) {
	t.Helper()
	prevReg, prevManager, prevKeys := reg, manager, apiClients
	reg = &registry{entries: map[string]registryEntry{}}
	manager = &Manager{sessions: map[string]*Session{}}
	apiClients = map[string]apiClient{}
	t.Cleanup(func() { reg, manager, apiClients = prevReg, prevManager, prevKeys })
}

// mountSession registers a session for a client and marks it running, which is
// what the handlers need to get past requireSession.
func mountSession(t *testing.T, client, name string) *Session {
	t.Helper()
	uid := client + "_" + name
	reg.mu.Lock()
	reg.entries[registryKey(client, name)] = registryEntry{
		Client: client, Name: name, UID: uid, Store: uid + ".db",
	}
	reg.mu.Unlock()
	s := &Session{Name: name, Owner: client, UID: uid}
	manager.mu.Lock()
	manager.sessions[uid] = s
	manager.mu.Unlock()
	return s
}

func setWebhook(t *testing.T, client, name, url string) {
	t.Helper()
	if _, err := reg.setWebhook(client, name, url, "secret"); err != nil {
		t.Fatalf("setWebhook: %v", err)
	}
}

// asClient stamps a request with the caller authMiddleware would have resolved.
func asClient(r *http.Request, label string) *http.Request {
	return r.WithContext(context.WithValue(r.Context(), ctxClient, apiClient{
		Label: label, KeyHash: hashKey("key-for-" + label),
	}))
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

func TestLoadAPIKeys(t *testing.T) {
	resetGatewayState(t)
	t.Setenv("WMG_API_KEY", "")
	t.Setenv("WMG_API_KEYS", "")

	if err := loadAPIKeys(); err == nil {
		t.Fatal("a gateway with no keys at all must refuse to start")
	}

	// The original single-key install keeps working, as the client `default`.
	t.Setenv("WMG_API_KEY", strings.Repeat("a", 32))
	if err := loadAPIKeys(); err != nil {
		t.Fatalf("legacy key rejected: %v", err)
	}
	c, ok := clientFor(strings.Repeat("a", 32))
	if !ok || c.Label != defaultClientLabel {
		t.Fatalf("legacy key resolved to %+v (ok=%v), want the default client", c, ok)
	}
	if _, ok := clientFor("wrong"); ok {
		t.Error("an unknown key must not resolve to a client")
	}

	t.Setenv("WMG_API_KEYS", "acme:"+strings.Repeat("b", 32)+", globex:"+strings.Repeat("c", 32))
	if err := loadAPIKeys(); err != nil {
		t.Fatalf("multi-client keys rejected: %v", err)
	}
	if c, _ := clientFor(strings.Repeat("b", 32)); c.Label != "acme" {
		t.Errorf("key resolved to %q, want acme", c.Label)
	}
	if c, _ := clientFor(strings.Repeat("c", 32)); c.Label != "globex" {
		t.Errorf("key resolved to %q, want globex", c.Label)
	}

	// Two clients sharing a key would see each other's sessions — the whole
	// point of the separation — so it is a startup error, not a warning.
	t.Setenv("WMG_API_KEYS", "acme:"+strings.Repeat("d", 32)+",globex:"+strings.Repeat("d", 32))
	if err := loadAPIKeys(); err == nil {
		t.Error("two clients sharing one key must be refused")
	}

	t.Setenv("WMG_API_KEYS", "Acme Ltd:"+strings.Repeat("e", 32))
	if err := loadAPIKeys(); err == nil {
		t.Error("a label that is not a safe slug must be refused")
	}

	t.Setenv("WMG_API_KEYS", "acme:short")
	if err := loadAPIKeys(); err == nil {
		t.Error("a key too short to be a secret must be refused")
	}
}

// ---------------------------------------------------------------------------
// The registry file
// ---------------------------------------------------------------------------

func TestRegistrySurvivesARestart(t *testing.T) {
	resetGatewayState(t)
	path := filepath.Join(t.TempDir(), "registry.json")
	if err := reg.load(path); err != nil {
		t.Fatalf("load of a missing registry must be a clean start: %v", err)
	}

	acme := apiClient{Label: "acme", KeyHash: hashKey("acme-key")}
	e, err := reg.claim(acme, "main", "https://acme.example.com/whatsmeow/webhook", "s3cret", "Acme prod")
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if e.UID == "" || !strings.HasPrefix(e.Store, "sessions/") {
		t.Fatalf("a new session needs an opaque uid and its own store: %+v", e)
	}

	// A second claim by the owner updates rather than duplicating: this is how
	// an Odoo that moved domain re-points itself.
	moved, err := reg.claim(acme, "main", "https://new.example.com/whatsmeow/webhook", "", "")
	if err != nil {
		t.Fatalf("re-claim: %v", err)
	}
	if moved.UID != e.UID {
		t.Error("re-pointing a session must not give it a new store: that would ask for a fresh QR")
	}
	if moved.WebhookSecret != "s3cret" {
		t.Error("an omitted secret must leave the stored one alone")
	}

	// Reload from disk, as a restart would.
	fresh := &registry{entries: map[string]registryEntry{}}
	if err := fresh.load(path); err != nil {
		t.Fatalf("reload: %v", err)
	}
	got, ok := fresh.get("acme", "main")
	if !ok {
		t.Fatal("the session did not survive a restart; its events would go nowhere")
	}
	if got.WebhookURL != "https://new.example.com/whatsmeow/webhook" || got.UID != e.UID {
		t.Fatalf("reloaded entry differs: %+v", got)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Error("the atomic write left its temporary file behind")
	}
}

// Two unrelated Odoos both call their number `main`, because `main` is what
// everyone types. They must not meet.
func TestTwoClientsMayUseTheSameSessionName(t *testing.T) {
	resetGatewayState(t)
	if err := reg.load(filepath.Join(t.TempDir(), "registry.json")); err != nil {
		t.Fatal(err)
	}
	a, err := reg.claim(apiClient{Label: "acme"}, "main", "https://a.example.com/hook", "sa", "")
	if err != nil {
		t.Fatal(err)
	}
	b, err := reg.claim(apiClient{Label: "globex"}, "main", "https://b.example.com/hook", "sb", "")
	if err != nil {
		t.Fatal(err)
	}
	if a.UID == b.UID || a.Store == b.Store {
		t.Fatal("two clients' sessions share a WhatsApp store: whoever boots first owns the number")
	}
	if pa, _ := mediaPath(a.UID, "3EB0ABC"); func() bool {
		pb, _ := mediaPath(b.UID, "3EB0ABC")
		return pa == pb
	}() {
		t.Fatal("two clients' media land in one directory, readable by message id")
	}
	if url, _ := reg.webhookFor("acme", "main"); url != "https://a.example.com/hook" {
		t.Errorf("acme's events would go to %q", url)
	}
	if url, _ := reg.webhookFor("globex", "main"); url != "https://b.example.com/hook" {
		t.Errorf("globex's events would go to %q", url)
	}
}

// An existing single-tenant install must upgrade with no configuration change:
// its paired stores stay where they are and keep their webhook target.
func TestAdoptLegacyInstall(t *testing.T) {
	resetGatewayState(t)
	dir := t.TempDir()
	prevData, prevMedia := dataDir, mediaDir
	dataDir, mediaDir = dir, filepath.Join(dir, "media")
	t.Cleanup(func() { dataDir, mediaDir = prevData, prevMedia })
	t.Setenv("WMG_API_KEY", strings.Repeat("a", 32))

	for _, name := range []string{"client_acme.db", "spare.db", "notes.txt", "UPPER.db"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := reg.load(filepath.Join(dir, "registry.json")); err != nil {
		t.Fatal(err)
	}
	if err := reg.adoptLegacy("http://127.0.0.1:8069/whatsmeow/webhook", "legacy-secret"); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	e, ok := reg.get(defaultClientLabel, "client_acme")
	if !ok {
		t.Fatal("an existing session was not adopted; it would stop delivering")
	}
	if e.Store != "client_acme.db" || e.storePath() != filepath.Join(dir, "client_acme.db") {
		t.Errorf("a paired store must not be moved: %+v", e)
	}
	if e.UID != "client_acme" {
		t.Errorf("uid %q: media already downloaded under media/client_acme would be orphaned", e.UID)
	}
	if e.WebhookURL != "http://127.0.0.1:8069/whatsmeow/webhook" || e.WebhookSecret != "legacy-secret" {
		t.Errorf("the environment's webhook target was not carried over: %+v", e)
	}
	if _, ok := reg.get(defaultClientLabel, "notes"); ok {
		t.Error("a non-store file was adopted as a session")
	}
	if _, ok := reg.get(defaultClientLabel, "UPPER"); ok {
		t.Error("a file that is not a valid session name was adopted")
	}

	// Idempotent: the second boot must not re-adopt or overwrite.
	if _, err := reg.claim(apiClient{Label: defaultClientLabel}, "client_acme",
		"https://moved.example.com/hook", "", ""); err != nil {
		t.Fatal(err)
	}
	if err := reg.adoptLegacy("http://127.0.0.1:8069/whatsmeow/webhook", "legacy-secret"); err != nil {
		t.Fatal(err)
	}
	if e, _ := reg.get(defaultClientLabel, "client_acme"); e.WebhookURL != "https://moved.example.com/hook" {
		t.Error("a second boot re-adopted and undid a re-point")
	}
}

// ---------------------------------------------------------------------------
// Isolation between clients
// ---------------------------------------------------------------------------

// Table-driven over every session route on purpose: a new endpoint that forgets
// to resolve through requireSession should fail here rather than in production.
func TestOneClientCannotReachAnothersSession(t *testing.T) {
	resetGatewayState(t)
	mountSession(t, "acme", "main")

	routes := []struct {
		name    string
		method  string
		handler http.HandlerFunc
		body    string
	}{
		{"status", http.MethodGet, handleStatus, ""},
		{"qr", http.MethodGet, handleQR, ""},
		{"send", http.MethodPost, handleSend, `{"phone":"447700900123","message":"hi"}`},
		{"send-media", http.MethodPost, handleSendMedia, `{"phone":"447700900123","data":"eA=="}`},
		{"react", http.MethodPost, handleReact, `{"phone":"447700900123","target_id":"3EB0","emoji":"👍"}`},
		{"read", http.MethodPost, handleMarkRead, `{"phone":"447700900123","message_ids":["3EB0"]}`},
		{"check", http.MethodPost, handleCheck, `{"phones":["447700900123"]}`},
		{"get-media", http.MethodGet, handleGetMedia, ""},
		{"delete-media", http.MethodDelete, handleDeleteMedia, ""},
		{"logout", http.MethodPost, handleLogout, ""},
	}
	for _, tc := range routes {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, "/sessions/main/x", strings.NewReader(tc.body))
			req.SetPathValue("name", "main")
			req.SetPathValue("id", "3EB0ABC")
			rec := httptest.NewRecorder()
			tc.handler(rec, asClient(req, "globex"))

			if rec.Code != http.StatusNotFound {
				t.Fatalf("another client reached acme's session: HTTP %d %s",
					rec.Code, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), "unknown session") {
				t.Fatalf("unexpected 404 body: %s", rec.Body.String())
			}
		})
	}

	// The owner does reach it — otherwise the test above proves nothing.
	req := httptest.NewRequest(http.MethodGet, "/sessions/main/status", nil)
	req.SetPathValue("name", "main")
	rec := httptest.NewRecorder()
	handleStatus(rec, asClient(req, "acme"))
	if rec.Code != http.StatusOK {
		t.Fatalf("the owning client got HTTP %d: %s", rec.Code, rec.Body.String())
	}
}

func TestListShowsOnlyTheCallersSessions(t *testing.T) {
	resetGatewayState(t)
	mountSession(t, "acme", "main")
	mountSession(t, "acme", "support")
	mountSession(t, "globex", "main")

	req := httptest.NewRequest(http.MethodGet, "/sessions", nil)
	rec := httptest.NewRecorder()
	handleList(rec, asClient(req, "acme"))

	var out []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if len(out) != 2 {
		t.Fatalf("acme sees %d sessions, want its own 2: %s", len(out), rec.Body.String())
	}
	for _, row := range out {
		if row["session"] != "main" && row["session"] != "support" {
			t.Errorf("acme was shown %v", row["session"])
		}
	}
}

// Odoo derives its idempotency key from the database name and the record id.
// Two clients whose database is called `odoo` would collide — and a collision
// replays the first send, so the second client's message is never delivered
// while Odoo records it as sent. Silent, and it loses data.
func TestIdempotencyKeysAreScopedPerSession(t *testing.T) {
	a := &Session{Name: "main", Owner: "acme", UID: "u_acme"}
	b := &Session{Name: "main", Owner: "globex", UID: "u_globex"}
	const odooKey = "odoo:whatsmeow.message:372"

	c := &sendCache{byKey: map[string]*sendOutcome{}}
	out, replay := c.begin(a.sendKey(odooKey))
	if replay {
		t.Fatal("first send must not replay")
	}
	c.resolve(a.sendKey(odooKey), out, "3EB0ACME", "", time.Now(), nil)

	if _, replay := c.begin(b.sendKey(odooKey)); replay {
		t.Fatal("another client's message was swallowed as a duplicate of ours")
	}
	if _, replay := c.begin(a.sendKey(odooKey)); !replay {
		t.Fatal("our own retry must still replay")
	}
	if a.sendKey("") != "" {
		t.Error("an empty key means the caller opted out; it must stay empty")
	}
}

// ---------------------------------------------------------------------------
// Webhook URL validation
// ---------------------------------------------------------------------------

func TestValidateWebhookURL(t *testing.T) {
	prevPrivate, prevInsecure := allowPrivateWebhooks, allowInsecureWebhooks
	defer func() { allowPrivateWebhooks, allowInsecureWebhooks = prevPrivate, prevInsecure }()

	allowPrivateWebhooks, allowInsecureWebhooks = false, false
	for _, raw := range []string{
		"https://odoo.example.com/whatsmeow/webhook",
	} {
		if err := validateWebhookURL(raw); err != nil {
			t.Errorf("rejected a good URL %q: %v", raw, err)
		}
	}
	for _, raw := range []string{
		"", "not a url", "ftp://odoo.example.com/hook", "/whatsmeow/webhook",
		"http://odoo.example.com/hook",  // plaintext to the open internet
		"http://127.0.0.1:8069/hook",    // this host's own network
		"http://192.168.1.10:8069/hook", // ...and the LAN behind it
		"http://localhost:8069/hook",
	} {
		if err := validateWebhookURL(raw); err == nil {
			t.Errorf("accepted %q on a shared gateway", raw)
		}
	}

	// A gateway bound to loopback is a single-host install: Odoo really is on
	// 127.0.0.1 there, and requiring TLS to talk to it would be theatre.
	allowPrivateWebhooks = true
	if err := validateWebhookURL("http://127.0.0.1:8069/whatsmeow/webhook"); err != nil {
		t.Errorf("a local install's own URL was rejected: %v", err)
	}
	if err := validateWebhookURL("http://odoo.example.com/hook"); err == nil {
		t.Error("plaintext to a public host is not covered by ALLOW_PRIVATE")
	}
	allowInsecureWebhooks = true
	if err := validateWebhookURL("http://odoo.example.com/hook"); err != nil {
		t.Errorf("the explicit override did not work: %v", err)
	}
}
