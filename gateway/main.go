// whatsmeow-gateway: a minimal multi-session HTTP gateway around whatsmeow,
// designed to be driven entirely from an Odoo 19 module.
//
//   Odoo  --HTTP-->  this gateway  --WebSocket-->  WhatsApp
//   Odoo  <--webhook--  this gateway                (inbound messages/events)
//
// NOTE: whatsmeow's API evolves. This file targets whatsmeow versions from
// ~mid-2025 onward (context-aware sqlstore, waE2E proto package). If your
// pinned version differs, small signature adjustments may be needed.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// ---------------------------------------------------------------------------
// Configuration (all via environment variables; see gateway.env)
// ---------------------------------------------------------------------------

var (
	listenAddr      = envOr("WMG_LISTEN", "127.0.0.1:8080")
	apiKey          = os.Getenv("WMG_API_KEY")            // required
	odooWebhookURL  = os.Getenv("WMG_ODOO_WEBHOOK_URL")   // e.g. https://odoo.example.com/whatsmeow/webhook
	webhookSecret   = os.Getenv("WMG_WEBHOOK_SECRET")     // shared secret sent to Odoo
	dataDir         = envOr("WMG_DATA_DIR", "./data")     // one sqlite DB per session
	nonDigits       = regexp.MustCompile(`\D`)
)

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------

type Session struct {
	Name      string
	Client    *whatsmeow.Client
	container *sqlstore.Container

	mu     sync.Mutex
	Status string // starting | qr | connected | disconnected | logged_out | error
	QRCode string // latest QR code string while Status == "qr"
	LastErr string
}

func (s *Session) set(status, qr, errMsg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Status = status
	s.QRCode = qr
	s.LastErr = errMsg
}

func (s *Session) snapshot() (string, string, string, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	jid := ""
	if s.Client != nil && s.Client.Store.ID != nil {
		jid = s.Client.Store.ID.String()
	}
	return s.Status, s.QRCode, s.LastErr, jid
}

type Manager struct {
	mu       sync.Mutex
	sessions map[string]*Session
}

var manager = &Manager{sessions: map[string]*Session{}}

var sessionNameRe = regexp.MustCompile(`^[a-z0-9_-]{1,40}$`)

func (m *Manager) get(name string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[name]
}

// StartSession creates (or reuses) a session and connects it. If the device
// is not yet paired, the QR pairing loop is started and the latest code is
// exposed via GET /sessions/{name}/qr for Odoo to render.
func (m *Manager) StartSession(name string) (*Session, error) {
	if !sessionNameRe.MatchString(name) {
		return nil, fmt.Errorf("invalid session name (use a-z, 0-9, '-', '_')")
	}

	m.mu.Lock()
	if s, ok := m.sessions[name]; ok {
		m.mu.Unlock()
		st, _, _, _ := s.snapshot()
		if st == "connected" || st == "qr" || st == "starting" {
			return s, nil // already running
		}
		// fall through: restart a dead session
	} else {
		m.mu.Unlock()
	}

	dbPath := filepath.Join(dataDir, name+".db")
	dbLog := waLog.Stdout("db/"+name, "WARN", true)

	ctx := context.Background()
	container, err := sqlstore.New(ctx, "sqlite3",
		fmt.Sprintf("file:%s?_foreign_keys=on&_busy_timeout=5000", dbPath), dbLog)
	if err != nil {
		return nil, fmt.Errorf("open store: %w", err)
	}

	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("get device: %w", err)
	}

	clientLog := waLog.Stdout("wa/"+name, "INFO", true)
	client := whatsmeow.NewClient(device, clientLog)

	s := &Session{Name: name, Client: client, container: container, Status: "starting"}
	client.AddEventHandler(makeEventHandler(s))

	m.mu.Lock()
	m.sessions[name] = s
	m.mu.Unlock()

	if client.Store.ID == nil {
		// Not paired yet -> QR flow. GetQRChannel MUST be called before Connect.
		qrChan, err := client.GetQRChannel(ctx)
		if err != nil {
			s.set("error", "", err.Error())
			return s, fmt.Errorf("qr channel: %w", err)
		}
		if err := client.Connect(); err != nil {
			s.set("error", "", err.Error())
			return s, fmt.Errorf("connect: %w", err)
		}
		go func() {
			for evt := range qrChan {
				switch evt.Event {
				case "code":
					s.set("qr", evt.Code, "")
				case "success":
					s.set("connected", "", "")
					notifyOdoo(s.Name, "session.paired", map[string]any{})
				case "timeout":
					s.set("disconnected", "", "QR pairing timed out; start again")
				default:
					log.Printf("[%s] QR event: %s", s.Name, evt.Event)
				}
			}
		}()
	} else {
		if err := client.Connect(); err != nil {
			s.set("error", "", err.Error())
			return s, fmt.Errorf("connect: %w", err)
		}
		s.set("connected", "", "")
	}
	return s, nil
}

// restoreExisting reconnects every previously-paired session found on disk
// so the gateway survives restarts without re-pairing.
func (m *Manager) restoreExisting() {
	entries, err := os.ReadDir(dataDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".db") {
			continue
		}
		name := strings.TrimSuffix(e.Name(), ".db")
		if !sessionNameRe.MatchString(name) {
			continue
		}
		if _, err := m.StartSession(name); err != nil {
			log.Printf("[%s] restore failed: %v", name, err)
		} else {
			log.Printf("[%s] restored", name)
		}
	}
}

// ---------------------------------------------------------------------------
// WhatsApp event handling -> forward to Odoo webhook
// ---------------------------------------------------------------------------

func makeEventHandler(s *Session) func(interface{}) {
	return func(evt interface{}) {
		switch v := evt.(type) {

		case *events.Message:
			if v.Info.IsFromMe {
				return // don't loop our own outbound back into Odoo
			}
			text := extractText(v.Message)
			if text == "" {
				// Non-text (media etc.) - still tell Odoo something arrived.
				text = "[unsupported message type: " + v.Info.Type + "]"
			}
			notifyOdoo(s.Name, "message.received", map[string]any{
				"wa_message_id": v.Info.ID,
				"sender_jid":    v.Info.Sender.ToNonAD().String(),
				"sender_phone":  v.Info.Sender.User,
				"push_name":     v.Info.PushName,
				"is_group":      v.Info.IsGroup,
				"chat_jid":      v.Info.Chat.String(),
				"body":          text,
				"timestamp":     v.Info.Timestamp.UTC().Format(time.RFC3339),
			})

		case *events.Receipt:
			if v.Type == types.ReceiptTypeDelivered || v.Type == types.ReceiptTypeRead {
				notifyOdoo(s.Name, "message.receipt", map[string]any{
					"receipt_type":   string(v.Type),
					"wa_message_ids": v.MessageIDs,
					"chat_jid":       v.Chat.String(),
					"timestamp":      v.Timestamp.UTC().Format(time.RFC3339),
				})
			}

		case *events.Connected:
			s.set("connected", "", "")
			notifyOdoo(s.Name, "session.connected", map[string]any{})

		case *events.Disconnected:
			s.set("disconnected", "", "")
			notifyOdoo(s.Name, "session.disconnected", map[string]any{})

		case *events.LoggedOut:
			s.set("logged_out", "", "logged out from phone or banned")
			notifyOdoo(s.Name, "session.logged_out", map[string]any{
				"reason": v.Reason.String(),
			})
		}
	}
}

func extractText(msg *waE2E.Message) string {
	if msg == nil {
		return ""
	}
	if t := msg.GetConversation(); t != "" {
		return t
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil {
		return ext.GetText()
	}
	if img := msg.GetImageMessage(); img != nil && img.GetCaption() != "" {
		return "[image] " + img.GetCaption()
	}
	if doc := msg.GetDocumentMessage(); doc != nil {
		return "[document] " + doc.GetFileName()
	}
	return ""
}

// notifyOdoo posts an event to the Odoo webhook with retries, so a short
// Odoo outage doesn't silently drop inbound messages.
func notifyOdoo(session, event string, data map[string]any) {
	if odooWebhookURL == "" {
		return
	}
	payload := map[string]any{
		"session": session,
		"event":   event,
		"data":    data,
	}
	body, _ := json.Marshal(payload)

	go func() {
		backoff := 2 * time.Second
		for attempt := 1; attempt <= 4; attempt++ {
			req, err := http.NewRequest(http.MethodPost, odooWebhookURL, bytes.NewReader(body))
			if err != nil {
				log.Printf("webhook build error: %v", err)
				return
			}
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Webhook-Secret", webhookSecret)

			clientHTTP := &http.Client{Timeout: 15 * time.Second}
			resp, err := clientHTTP.Do(req)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode >= 200 && resp.StatusCode < 300 {
					return
				}
				log.Printf("[%s] odoo webhook HTTP %d (attempt %d)", session, resp.StatusCode, attempt)
			} else {
				log.Printf("[%s] odoo webhook error: %v (attempt %d)", session, err, attempt)
			}
			time.Sleep(backoff)
			backoff *= 2
		}
		log.Printf("[%s] odoo webhook: giving up on event %s", session, event)
	}()
}

// ---------------------------------------------------------------------------
// HTTP API (consumed by the Odoo module)
// ---------------------------------------------------------------------------

type sendRequest struct {
	Phone   string `json:"phone"`   // digits with country code, e.g. "447700900123"
	Message string `json:"message"` // plain text body
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func requireSession(w http.ResponseWriter, r *http.Request) *Session {
	name := r.PathValue("name")
	s := manager.get(name)
	if s == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown session; start it first"})
		return nil
	}
	return s
}

func handleStart(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	s, err := manager.StartSession(name)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	status, qr, lastErr, jid := s.snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"session": name, "status": status, "qr": qr, "error": lastErr, "jid": jid,
	})
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	status, _, lastErr, jid := s.snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"session": s.Name, "status": status, "error": lastErr, "jid": jid,
	})
}

func handleQR(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	status, qr, _, _ := s.snapshot()
	if status != "qr" || qr == "" {
		writeJSON(w, http.StatusConflict, map[string]any{
			"session": s.Name, "status": status,
			"error": "no QR available (already paired, or start the session first)",
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"session": s.Name, "status": status, "qr": qr})
}

func handleSend(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	var req sendRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil ||
		strings.TrimSpace(req.Message) == "" || strings.TrimSpace(req.Phone) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'phone' and 'message' are required"})
		return
	}
	status, _, _, _ := s.snapshot()
	if status != "connected" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "session not connected (status: " + status + ")"})
		return
	}

	phone := nonDigits.ReplaceAllString(req.Phone, "")
	jid := types.NewJID(phone, types.DefaultUserServer)

	msg := &waE2E.Message{Conversation: proto.String(req.Message)}
	resp, err := s.Client.SendMessage(r.Context(), jid, msg)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "send failed: " + err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":        "sent",
		"wa_message_id": resp.ID,
		"timestamp":     resp.Timestamp.UTC().Format(time.RFC3339),
	})
}

func handleLogout(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	if err := s.Client.Logout(r.Context()); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	s.set("logged_out", "", "")
	writeJSON(w, http.StatusOK, map[string]string{"status": "logged_out"})
}

func handleList(w http.ResponseWriter, r *http.Request) {
	manager.mu.Lock()
	names := make([]string, 0, len(manager.sessions))
	for n := range manager.sessions {
		names = append(names, n)
	}
	manager.mu.Unlock()

	out := []map[string]any{}
	for _, n := range names {
		s := manager.get(n)
		status, _, lastErr, jid := s.snapshot()
		out = append(out, map[string]any{"session": n, "status": status, "error": lastErr, "jid": jid})
	}
	writeJSON(w, http.StatusOK, out)
}

func authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		if apiKey == "" || r.Header.Get("X-Api-Key") != apiKey {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid or missing X-Api-Key"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ---------------------------------------------------------------------------

func main() {
	if apiKey == "" {
		log.Fatal("WMG_API_KEY must be set")
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		log.Fatalf("cannot create data dir: %v", err)
	}

	manager.restoreExisting()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /sessions", handleList)
	mux.HandleFunc("POST /sessions/{name}/start", handleStart)
	mux.HandleFunc("GET /sessions/{name}/status", handleStatus)
	mux.HandleFunc("GET /sessions/{name}/qr", handleQR)
	mux.HandleFunc("POST /sessions/{name}/send", handleSend)
	mux.HandleFunc("POST /sessions/{name}/logout", handleLogout)

	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           authMiddleware(mux),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("whatsmeow-gateway listening on %s (data dir: %s)", listenAddr, dataDir)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	log.Println("shutting down...")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)

	manager.mu.Lock()
	for _, s := range manager.sessions {
		s.Client.Disconnect()
	}
	manager.mu.Unlock()
}
