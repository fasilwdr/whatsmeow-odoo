// whatsmeow-gateway: a minimal multi-session HTTP gateway around whatsmeow,
// designed to be driven entirely from an Odoo 19 module.
//
//	Odoo  --HTTP-->  this gateway  --WebSocket-->  WhatsApp
//	Odoo  <--webhook--  this gateway                (inbound messages/events)
//
// NOTE: whatsmeow's API evolves. This file targets whatsmeow versions from
// ~mid-2025 onward (context-aware sqlstore, waE2E proto package). If your
// pinned version differs, small signature adjustments may be needed.
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
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
	listenAddr     = envOr("WMG_LISTEN", "127.0.0.1:8080")
	apiKey         = os.Getenv("WMG_API_KEY")          // required
	odooWebhookURL = os.Getenv("WMG_ODOO_WEBHOOK_URL") // e.g. https://odoo.example.com/whatsmeow/webhook
	webhookSecret  = os.Getenv("WMG_WEBHOOK_SECRET")   // shared secret sent to Odoo
	dataDir        = envOr("WMG_DATA_DIR", "./data")   // one sqlite DB per session
	nonDigits      = regexp.MustCompile(`\D`)

	// Inbound media is downloaded to disk and fetched by Odoo over the API
	// rather than inlined into the webhook: WhatsApp allows ~100MB files, and
	// notifyOdoo retries, so a big payload would be re-sent several times.
	mediaDir      = filepath.Join(dataDir, "media")
	maxMediaBytes = int64(envIntOr("WMG_MAX_MEDIA_MB", 100)) << 20
	mediaTTL      = time.Duration(envIntOr("WMG_MEDIA_TTL_HOURS", 24)) * time.Hour

	// WhatsApp emits a delivery *and* a read receipt per participant, so one
	// message to a large group turns into dozens of events at once. Posting
	// them all concurrently once took an Odoo down: its threaded dev server
	// spawns a thread per request, and it ran out of threads. Webhooks
	// therefore go through a fixed pool of senders.
	webhookWorkers   = envIntOr("WMG_WEBHOOK_WORKERS", 4)
	webhookQueueSize = envIntOr("WMG_WEBHOOK_QUEUE", 2048)
	webhookQueue     chan webhookJob
)

// webhookJob is one event waiting to be posted to Odoo.
type webhookJob struct {
	session string
	event   string
	body    []byte
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envIntOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
		log.Printf("invalid %s=%q, using %d", key, v, def)
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

	mu      sync.Mutex
	Status  string // starting | qr | connected | disconnected | logged_out | error
	QRCode  string // latest QR code string while Status == "qr"
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
			// Media is downloaded now, not on demand: WhatsApp expires it from
			// its servers, so a later fetch would find nothing.
			var media *mediaInfo
			if info, ok := extractMedia(v.Message); ok {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
				err := s.downloadMedia(ctx, v.Info.ID, info)
				cancel()
				if err != nil {
					log.Printf("[%s] media download failed for %s: %v", s.Name, v.Info.ID, err)
					if text == "" {
						text = "[" + info.Kind + " could not be downloaded: " + err.Error() + "]"
					}
				} else {
					media = info
				}
			}
			// WhatsApp sometimes delivers a message twice: once as a stub with
			// nothing in it, once for real. Both carry the same ID, so Odoo can
			// dedupe them — but only if it knows which copy is the empty one,
			// otherwise it keeps whichever landed first and the real text is
			// lost. Say so explicitly rather than making Odoo guess from the
			// placeholder text.
			placeholder := false
			if text == "" && media == nil {
				// Nothing we can render - still tell Odoo something arrived.
				text = "[unsupported message type: " + describeMessage(v) + "]"
				placeholder = true
			}
			sender := v.Info.Sender.ToNonAD()
			senderPN := resolvePN(s.Client, sender, v.Info.SenderAlt)
			lid := ""
			if sender.Server == types.HiddenUserServer {
				lid = sender.User
			} else if v.Info.SenderAlt.Server == types.HiddenUserServer {
				lid = v.Info.SenderAlt.User
			}
			if senderPN.IsEmpty() {
				// Better an empty phone Odoo can flag than a LID masquerading as one.
				log.Printf("[%s] could not resolve a phone number for sender %s (mode=%s)",
					s.Name, sender, v.Info.AddressingMode)
			}
			// Chat is the conversation (the group, or the contact for a 1:1);
			// Sender is the individual who wrote. They differ only in groups,
			// and a reply has to go to the Chat.
			chatName := ""
			if v.Info.IsGroup {
				chatName = groupNameCache.lookup(s.Client, v.Info.Chat)
			}
			payload := map[string]any{
				"wa_message_id":   v.Info.ID,
				"sender_jid":      sender.String(),
				"sender_phone":    senderPN.User, // "" when only a LID is known
				"sender_lid":      lid,
				"addressing_mode": string(v.Info.AddressingMode),
				"push_name":       v.Info.PushName,
				"is_group":        v.Info.IsGroup,
				"chat_jid":        v.Info.Chat.String(),
				"chat_name":       chatName, // "" unless this is a group
				"body":            text,     // caption, for media
				"placeholder":     placeholder,
				"timestamp":       v.Info.Timestamp.UTC().Format(time.RFC3339),
			}
			if media != nil {
				// Metadata only; Odoo pulls the bytes from /media/{id}.
				payload["media"] = media
			}
			notifyOdoo(s.Name, "message.received", payload)

		case *events.Receipt:
			if v.Type == types.ReceiptTypeDelivered || v.Type == types.ReceiptTypeRead {
				notifyOdoo(s.Name, "message.receipt", map[string]any{
					"receipt_type":   string(v.Type),
					"wa_message_ids": v.MessageIDs,
					"chat_jid":       v.Chat.String(),
					"timestamp":      v.Timestamp.UTC().Format(time.RFC3339),
				})
			}

		case *events.GroupInfo:
			// A rename would otherwise sit stale in the cache for an hour.
			groupNameCache.forget(v.JID)

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

// ---------------------------------------------------------------------------
// Media storage: downloaded once on receipt (WhatsApp drops media from its
// servers after a while), served to Odoo on demand, and garbage-collected.
// ---------------------------------------------------------------------------

// mediaInfo is the metadata sent to Odoo in the webhook; Odoo then fetches the
// bytes from GET /sessions/{name}/media/{id}.
type mediaInfo struct {
	Kind     string `json:"kind"` // image|video|audio|document|sticker
	Mimetype string `json:"mimetype"`
	Filename string `json:"filename"`
	Size     int64  `json:"size"`
	Seconds  uint32 `json:"seconds,omitempty"`
	PTT      bool   `json:"ptt,omitempty"`

	dl whatsmeow.DownloadableMessage `json:"-"`
}

// safeID only allows the characters WhatsApp actually uses in message IDs.
// Message IDs arrive from the network and are used to build file paths, so
// anything else must never reach the filesystem.
var safeID = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

func mediaPath(session, id string) (string, error) {
	if !sessionNameRe.MatchString(session) || !safeID.MatchString(id) {
		return "", fmt.Errorf("invalid session or message id")
	}
	// safeID permits dots, so "." and ".." still slip through the regex and
	// would climb out of the session directory via filepath.Join.
	if strings.Trim(id, ".") == "" {
		return "", fmt.Errorf("invalid message id")
	}
	return filepath.Join(mediaDir, session, id), nil
}

// extractMedia returns the downloadable part of a message, if it has one.
func extractMedia(msg *waE2E.Message) (*mediaInfo, bool) {
	if msg == nil {
		return nil, false
	}
	switch {
	case msg.GetImageMessage() != nil:
		m := msg.GetImageMessage()
		return &mediaInfo{Kind: "image", Mimetype: m.GetMimetype(), dl: m}, true
	case msg.GetVideoMessage() != nil:
		m := msg.GetVideoMessage()
		return &mediaInfo{Kind: "video", Mimetype: m.GetMimetype(), Seconds: m.GetSeconds(), dl: m}, true
	case msg.GetAudioMessage() != nil:
		m := msg.GetAudioMessage()
		return &mediaInfo{Kind: "audio", Mimetype: m.GetMimetype(), Seconds: m.GetSeconds(),
			PTT: m.GetPTT(), dl: m}, true
	case msg.GetStickerMessage() != nil:
		m := msg.GetStickerMessage()
		return &mediaInfo{Kind: "sticker", Mimetype: m.GetMimetype(), dl: m}, true
	case msg.GetDocumentMessage() != nil:
		m := msg.GetDocumentMessage()
		return &mediaInfo{Kind: "document", Mimetype: m.GetMimetype(),
			Filename: m.GetFileName(), dl: m}, true
	}
	return nil, false
}

// filenameFor invents a reasonable filename when WhatsApp doesn't supply one
// (only documents carry a real name).
func filenameFor(info *mediaInfo, id string) string {
	if info.Filename != "" {
		return filepath.Base(info.Filename)
	}
	ext := ""
	if exts, err := mime.ExtensionsByType(info.Mimetype); err == nil && len(exts) > 0 {
		ext = exts[0]
	}
	if ext == "" {
		switch info.Kind {
		case "image":
			ext = ".jpg"
		case "video":
			ext = ".mp4"
		case "audio":
			ext = ".ogg"
		case "sticker":
			ext = ".webp"
		default:
			ext = ".bin"
		}
	}
	return info.Kind + "_" + id + ext
}

// downloadMedia fetches the media for a message and writes it next to a small
// JSON sidecar holding its metadata.
func (s *Session) downloadMedia(ctx context.Context, id string, info *mediaInfo) error {
	path, err := mediaPath(s.Name, id)
	if err != nil {
		return err
	}
	data, err := s.Client.Download(ctx, info.dl)
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}
	if int64(len(data)) > maxMediaBytes {
		return fmt.Errorf("media is %d bytes, over the %d byte limit", len(data), maxMediaBytes)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(path+".bin", data, 0o600); err != nil {
		return err
	}
	info.Size = int64(len(data))
	info.Filename = filenameFor(info, id)
	meta, _ := json.Marshal(info)
	if err := os.WriteFile(path+".json", meta, 0o600); err != nil {
		return err
	}
	return nil
}

// mediaGC drops media Odoo never collected, so the disk can't grow forever.
// It also expires resolved send keys, which leak at the same lazy pace.
func mediaGC() {
	for {
		time.Sleep(time.Hour)
		sendGuard.sweep()
		cutoff := time.Now().Add(-mediaTTL)
		_ = filepath.Walk(mediaDir, func(path string, fi os.FileInfo, err error) error {
			if err != nil || fi.IsDir() {
				return nil //nolint:nilerr // a vanished file is not an error worth stopping for
			}
			if fi.ModTime().Before(cutoff) {
				if err := os.Remove(path); err == nil {
					log.Printf("media gc: removed %s", filepath.Base(path))
				}
			}
			return nil
		})
	}
}

// resolvePN returns the phone-number JID for a user, which is NOT simply
// sender.User: WhatsApp addresses users by LID (a privacy-preserving random id,
// e.g. 126864760766535@lid) and then sender.User is that id, not a phone number.
//
// Order: the JID itself if it is already a phone number, else the alternative
// address the server sent alongside it, else the client's LID<->PN mapping.
// Returns an empty JID when the phone number genuinely isn't known.
func resolvePN(cli *whatsmeow.Client, sender, senderAlt types.JID) types.JID {
	if sender.Server == types.DefaultUserServer {
		return sender.ToNonAD()
	}
	if senderAlt.Server == types.DefaultUserServer {
		return senderAlt.ToNonAD()
	}
	if sender.Server == types.HiddenUserServer && cli != nil && cli.Store != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if pn, err := cli.Store.GetAltJID(ctx, sender.ToNonAD()); err == nil &&
			pn.Server == types.DefaultUserServer {
			return pn.ToNonAD()
		}
	}
	return types.EmptyJID
}

// A group message carries only the group's JID, never its name, so the subject
// has to be fetched separately. Cache it: otherwise every inbound group message
// costs an extra round trip to WhatsApp, and busy groups are exactly where that
// hurts. Failures are cached too, so an unreadable group isn't retried per
// message.
type groupNames struct {
	mu      sync.Mutex
	entries map[string]groupNameEntry
}

type groupNameEntry struct {
	name    string
	fetched time.Time
}

const groupNameTTL = time.Hour

var groupNameCache = &groupNames{entries: map[string]groupNameEntry{}}

// lookup returns the group's subject, or "" when WhatsApp won't tell us.
func (g *groupNames) lookup(cli *whatsmeow.Client, chat types.JID) string {
	key := chat.String()
	g.mu.Lock()
	entry, ok := g.entries[key]
	g.mu.Unlock()
	if ok && time.Since(entry.fetched) < groupNameTTL {
		return entry.name
	}
	if cli == nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	name := ""
	if info, err := cli.GetGroupInfo(ctx, chat); err != nil {
		log.Printf("group info for %s failed: %v", chat, err)
	} else {
		name = info.Name
	}
	g.mu.Lock()
	g.entries[key] = groupNameEntry{name: name, fetched: time.Now()}
	g.mu.Unlock()
	return name
}

func (g *groupNames) forget(chat types.JID) {
	g.mu.Lock()
	delete(g.entries, chat.String())
	g.mu.Unlock()
}

// describeMessage names the concrete payload we could not turn into text, so the
// Odoo log says "audio" rather than the server's generic "text" label.
func describeMessage(v *events.Message) string {
	msg := v.Message
	switch {
	case msg == nil:
		return v.Info.Type
	case msg.GetAudioMessage() != nil:
		return "audio"
	case msg.GetVideoMessage() != nil:
		return "video"
	case msg.GetStickerMessage() != nil:
		return "sticker"
	case msg.GetImageMessage() != nil:
		return "image"
	case msg.GetLocationMessage() != nil, msg.GetLiveLocationMessage() != nil:
		return "location"
	case msg.GetContactMessage() != nil, msg.GetContactsArrayMessage() != nil:
		return "contact"
	case msg.GetPollCreationMessageV3() != nil:
		return "poll"
	case msg.GetPollUpdateMessage() != nil:
		return "poll vote"
	case msg.GetReactionMessage() != nil:
		return "reaction"
	case msg.GetProtocolMessage() != nil:
		return "protocol/" + msg.GetProtocolMessage().GetType().String()
	default:
		return v.Info.Type
	}
}

func extractText(msg *waE2E.Message) string {
	if msg == nil {
		return ""
	}
	if t := msg.GetConversation(); t != "" {
		return t
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil && ext.GetText() != "" {
		return ext.GetText()
	}
	// An edit arrives as a ProtocolMessage wrapping the replacement message.
	if pm := msg.GetProtocolMessage(); pm != nil {
		if edited := pm.GetEditedMessage(); edited != nil {
			if t := extractText(edited); t != "" {
				return "[edited] " + t
			}
		}
	}
	if r := msg.GetReactionMessage(); r != nil && r.GetText() != "" {
		return "[reaction] " + r.GetText()
	}
	if img := msg.GetImageMessage(); img != nil && img.GetCaption() != "" {
		return "[image] " + img.GetCaption()
	}
	if vid := msg.GetVideoMessage(); vid != nil && vid.GetCaption() != "" {
		return "[video] " + vid.GetCaption()
	}
	if doc := msg.GetDocumentMessage(); doc != nil {
		if cap := doc.GetCaption(); cap != "" {
			return "[document] " + doc.GetFileName() + ": " + cap
		}
		return "[document] " + doc.GetFileName()
	}
	if btn := msg.GetButtonsResponseMessage(); btn != nil {
		return btn.GetSelectedDisplayText()
	}
	if lst := msg.GetListResponseMessage(); lst != nil {
		return lst.GetTitle()
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
	body, err := json.Marshal(payload)
	if err != nil {
		log.Printf("[%s] webhook build error: %v", session, err)
		return
	}

	// Never block: this runs on whatsmeow's event handler, and stalling there
	// stalls the WhatsApp connection itself. A full queue means Odoo has been
	// unreachable for a long while, and the workers are already giving up on
	// events anyway — dropping here is the same loss, without the backpressure.
	select {
	case webhookQueue <- webhookJob{session: session, event: event, body: body}:
	default:
		log.Printf("[%s] webhook queue full (%d), dropping event %s",
			session, webhookQueueSize, event)
	}
}

// startWebhookWorkers must run before any session connects, so no event can
// find a nil queue and be dropped on the floor at boot.
func startWebhookWorkers() {
	webhookQueue = make(chan webhookJob, webhookQueueSize)
	for i := 0; i < webhookWorkers; i++ {
		go func() {
			for job := range webhookQueue {
				postToOdoo(job)
			}
		}()
	}
	log.Printf("webhook: %d workers, queue %d", webhookWorkers, webhookQueueSize)
}

// webhookClient is shared so the pool reuses connections instead of opening a
// fresh one per attempt.
var webhookClient = &http.Client{Timeout: 15 * time.Second}

func postToOdoo(job webhookJob) {
	backoff := 2 * time.Second
	for attempt := 1; attempt <= 4; attempt++ {
		req, err := http.NewRequest(http.MethodPost, odooWebhookURL, bytes.NewReader(job.body))
		if err != nil {
			log.Printf("[%s] webhook build error: %v", job.session, err)
			return
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Webhook-Secret", webhookSecret)

		resp, err := webhookClient.Do(req)
		if err == nil {
			// Drain before closing, so the connection can be reused.
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return
			}
			log.Printf("[%s] odoo webhook HTTP %d (attempt %d)", job.session, resp.StatusCode, attempt)
		} else {
			log.Printf("[%s] odoo webhook error: %v (attempt %d)", job.session, err, attempt)
		}
		if attempt < 4 {
			time.Sleep(backoff)
			backoff *= 2
		}
	}
	log.Printf("[%s] odoo webhook: giving up on event %s", job.session, job.event)
}

// ---------------------------------------------------------------------------
// HTTP API (consumed by the Odoo module)
// ---------------------------------------------------------------------------

// target says where a message goes. A phone number can only ever address a
// private chat, so replying to a group (or to a sender WhatsApp only gave us a
// LID for) needs the full JID instead.
type target struct {
	Phone string `json:"phone"` // digits with country code, e.g. "447700900123"
	JID   string `json:"jid"`   // full JID; takes precedence over Phone
}

// quote turns a message into a reply to an earlier one. In a group this is
// what tells everyone which message is being answered.
type quote struct {
	QuotedID          string `json:"quoted_id"`          // the original's WhatsApp message id
	QuotedParticipant string `json:"quoted_participant"` // JID of who sent the original
	QuotedText        string `json:"quoted_text"`        // original body, redisplayed in the quote
}

type sendRequest struct {
	target
	quote
	idempotent
	Message string `json:"message"` // plain text body
}

// ---------------------------------------------------------------------------
// Send idempotency
//
// Odoo can hand us the same message twice: its transaction may roll back after
// we have already given the message to WhatsApp, leaving the record queued so
// the send cron picks it up again. A resend is not a harmless retry — the
// recipient sees the message twice. So a send carries a key that is stable
// across attempts of the same message, and we replay the first result instead
// of sending again.
// ---------------------------------------------------------------------------

type idempotent struct {
	Key string `json:"idempotency_key"`
}

// sendOutcome is one send's result, shared by every caller replaying its key.
type sendOutcome struct {
	done      chan struct{} // closed once the send has resolved
	once      sync.Once     // resolve exactly once, however we leave the handler
	waID      string
	kind      string // media only; "" for text
	timestamp time.Time
	err       error
	storedAt  time.Time
}

type sendCache struct {
	mu    sync.Mutex
	byKey map[string]*sendOutcome
	ttl   time.Duration
}

var sendGuard = &sendCache{
	byKey: map[string]*sendOutcome{},
	// A duplicate arrives within seconds (the next cron run). A day is
	// generous and costs a few hundred bytes per message.
	ttl: 24 * time.Hour,
}

var errSendIncomplete = fmt.Errorf("send did not complete")

// begin claims a key. It returns replay=true when this key has been seen, in
// which case the outcome is already resolved (waiting first if a concurrent
// attempt is still in flight) and must be replayed rather than sent again.
//
// An empty key means the caller opted out — a request composed by hand rather
// than by the Odoo module — so it always sends.
func (c *sendCache) begin(key string) (*sendOutcome, bool) {
	if key == "" {
		return &sendOutcome{done: make(chan struct{})}, false
	}
	c.mu.Lock()
	if out, ok := c.byKey[key]; ok {
		c.mu.Unlock()
		<-out.done // a concurrent attempt is still sending; use its result
		return out, true
	}
	out := &sendOutcome{done: make(chan struct{}), storedAt: time.Now()}
	c.byKey[key] = out
	c.mu.Unlock()
	return out, false
}

// resolve publishes a send's result to anyone replaying the key. A failed send
// is forgotten rather than cached: nothing reached WhatsApp, so a later retry
// must be free to really send.
//
// Safe to call twice: handlers defer a resolve so that a panic cannot leave a
// key claimed forever, with every later attempt blocked on a channel that will
// never close.
func (c *sendCache) resolve(key string, out *sendOutcome, waID, kind string, ts time.Time, err error) {
	out.once.Do(func() {
		out.waID, out.kind, out.timestamp, out.err = waID, kind, ts, err
		close(out.done)
		if err != nil && key != "" {
			c.mu.Lock()
			delete(c.byKey, key)
			c.mu.Unlock()
		}
	})
}

// writeReplay answers a send whose key we have already resolved.
func writeReplay(w http.ResponseWriter, session, key string, out *sendOutcome, media bool) {
	if out.err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error": "send failed: " + out.err.Error()})
		return
	}
	log.Printf("[%s] replaying send %s -> %s (not sending again)", session, key, out.waID)
	body := map[string]any{
		"status":        "sent",
		"wa_message_id": out.waID,
		"timestamp":     out.timestamp.UTC().Format(time.RFC3339),
		"replayed":      true,
	}
	if media {
		body["kind"] = out.kind
	}
	writeJSON(w, http.StatusOK, body)
}

func (c *sendCache) sweep() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for key, out := range c.byKey {
		select {
		case <-out.done:
			if time.Since(out.storedAt) > c.ttl {
				delete(c.byKey, key)
			}
		default: // still in flight, leave it alone
		}
	}
}

// Servers we can actually SendMessage to. Newsletters and broadcasts need
// their own send paths, so reject them with a clear error rather than
// failing deep inside whatsmeow.
var sendableServers = map[string]bool{
	types.DefaultUserServer: true, // s.whatsapp.net - a normal contact
	types.HiddenUserServer:  true, // lid - a contact we only know by LID
	types.GroupServer:       true, // g.us - a group chat
}

// resolveTarget picks the JID to send to: the explicit one when given, else
// the phone number as a private chat.
func resolveTarget(t target) (types.JID, error) {
	if raw := strings.TrimSpace(t.JID); raw != "" {
		jid, err := types.ParseJID(raw)
		if err != nil {
			return types.EmptyJID, fmt.Errorf("invalid 'jid': %w", err)
		}
		jid = jid.ToNonAD() // address the chat, not one of its devices
		if jid.User == "" {
			return types.EmptyJID, fmt.Errorf("invalid 'jid': no user part")
		}
		if !sendableServers[jid.Server] {
			return types.EmptyJID, fmt.Errorf("cannot send to a %q address", jid.Server)
		}
		return jid, nil
	}
	digits := nonDigits.ReplaceAllString(t.Phone, "")
	if digits == "" {
		return types.EmptyJID, fmt.Errorf("'phone' or 'jid' is required")
	}
	return types.NewJID(digits, types.DefaultUserServer), nil
}

// contextInfo renders a quote, or nil when this message isn't a reply.
func (q quote) contextInfo() *waE2E.ContextInfo {
	if strings.TrimSpace(q.QuotedID) == "" {
		return nil
	}
	ci := &waE2E.ContextInfo{
		StanzaID: proto.String(q.QuotedID),
		// WhatsApp renders the quote from the copy we send, not from its own
		// history, so the original text has to travel with the reply.
		QuotedMessage: &waE2E.Message{Conversation: proto.String(q.QuotedText)},
	}
	if p := strings.TrimSpace(q.QuotedParticipant); p != "" {
		ci.Participant = proto.String(p)
	}
	return ci
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
		strings.TrimSpace(req.Message) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'message' is required"})
		return
	}
	jid, err := resolveTarget(req.target)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	status, _, _, _ := s.snapshot()
	if status != "connected" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "session not connected (status: " + status + ")"})
		return
	}

	// Replay a send we have already made rather than delivering it twice.
	out, replay := sendGuard.begin(req.Key)
	if replay {
		writeReplay(w, s.Name, req.Key, out, false)
		return
	}
	defer sendGuard.resolve(req.Key, out, "", "", time.Time{}, errSendIncomplete)

	// A quote has to hang off ContextInfo, which plain Conversation has no room
	// for; ExtendedTextMessage is the same text with somewhere to put it.
	msg := &waE2E.Message{Conversation: proto.String(req.Message)}
	if ci := req.contextInfo(); ci != nil {
		msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(req.Message), ContextInfo: ci,
		}}
	}
	resp, err := s.Client.SendMessage(r.Context(), jid, msg)
	sendGuard.resolve(req.Key, out, resp.ID, "", resp.Timestamp, err)
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

// handleGetMedia streams a previously downloaded file to Odoo.
func handleGetMedia(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	path, err := mediaPath(s.Name, r.PathValue("id"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	meta := &mediaInfo{}
	if raw, err := os.ReadFile(path + ".json"); err == nil {
		_ = json.Unmarshal(raw, meta)
	}
	f, err := os.Open(path + ".bin")
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "media not found (already collected, expired, or never downloaded)"})
		return
	}
	defer f.Close()

	if meta.Mimetype != "" {
		w.Header().Set("Content-Type", meta.Mimetype)
	} else {
		w.Header().Set("Content-Type", "application/octet-stream")
	}
	if meta.Filename != "" {
		w.Header().Set("Content-Disposition",
			fmt.Sprintf("attachment; filename=%q", filepath.Base(meta.Filename)))
	}
	if fi, err := f.Stat(); err == nil {
		w.Header().Set("Content-Length", strconv.FormatInt(fi.Size(), 10))
	}
	_, _ = io.Copy(w, f)
}

// handleDeleteMedia lets Odoo release a file once it has stored it.
func handleDeleteMedia(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	path, err := mediaPath(s.Name, r.PathValue("id"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	_ = os.Remove(path + ".bin")
	_ = os.Remove(path + ".json")
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
}

type sendMediaRequest struct {
	target
	quote
	idempotent
	Caption  string `json:"caption"`
	Filename string `json:"filename"`
	Mimetype string `json:"mimetype"`
	Kind     string `json:"kind"` // optional; inferred from mimetype
	PTT      bool   `json:"ptt"`  // send an audio file as a voice note
	Data     string `json:"data"` // base64
}

// kindFor maps a mimetype onto how WhatsApp should present the file.
func kindFor(mimetype string) string {
	switch {
	case strings.HasPrefix(mimetype, "image/webp"):
		return "sticker"
	case strings.HasPrefix(mimetype, "image/"):
		return "image"
	case strings.HasPrefix(mimetype, "video/"):
		return "video"
	case strings.HasPrefix(mimetype, "audio/"):
		return "audio"
	default:
		return "document"
	}
}

var uploadMediaType = map[string]whatsmeow.MediaType{
	"image":    whatsmeow.MediaImage,
	"video":    whatsmeow.MediaVideo,
	"audio":    whatsmeow.MediaAudio,
	"document": whatsmeow.MediaDocument,
	"sticker":  whatsmeow.MediaImage, // stickers upload as images
}

func buildMediaMessage(kind string, req sendMediaRequest, up whatsmeow.UploadResponse) (*waE2E.Message, error) {
	ci := req.contextInfo() // nil unless this is a reply
	switch kind {
	case "image":
		return &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
			Caption: proto.String(req.Caption), Mimetype: proto.String(req.Mimetype),
			URL: &up.URL, DirectPath: &up.DirectPath, MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: &up.FileLength,
			ContextInfo: ci,
		}}, nil
	case "video":
		return &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
			Caption: proto.String(req.Caption), Mimetype: proto.String(req.Mimetype),
			URL: &up.URL, DirectPath: &up.DirectPath, MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: &up.FileLength,
			ContextInfo: ci,
		}}, nil
	case "audio":
		return &waE2E.Message{AudioMessage: &waE2E.AudioMessage{
			Mimetype: proto.String(req.Mimetype), PTT: proto.Bool(req.PTT),
			URL: &up.URL, DirectPath: &up.DirectPath, MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: &up.FileLength,
			ContextInfo: ci,
		}}, nil
	case "sticker":
		return &waE2E.Message{StickerMessage: &waE2E.StickerMessage{
			Mimetype: proto.String(req.Mimetype),
			URL:      &up.URL, DirectPath: &up.DirectPath, MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: &up.FileLength,
			ContextInfo: ci,
		}}, nil
	case "document":
		return &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
			Caption: proto.String(req.Caption), Mimetype: proto.String(req.Mimetype),
			FileName: proto.String(req.Filename), Title: proto.String(req.Filename),
			URL: &up.URL, DirectPath: &up.DirectPath, MediaKey: up.MediaKey,
			FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256, FileLength: &up.FileLength,
			ContextInfo: ci,
		}}, nil
	}
	return nil, fmt.Errorf("unsupported media kind %q", kind)
}

func handleSendMedia(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	var req sendMediaRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxMediaBytes*2)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad request: " + err.Error()})
		return
	}
	if strings.TrimSpace(req.Data) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'data' is required"})
		return
	}
	jid, err := resolveTarget(req.target)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	data, err := base64.StdEncoding.DecodeString(req.Data)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'data' is not valid base64"})
		return
	}
	if int64(len(data)) > maxMediaBytes {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{
			"error": fmt.Sprintf("media is %d bytes, over the %d byte limit", len(data), maxMediaBytes)})
		return
	}
	if status, _, _, _ := s.snapshot(); status != "connected" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "session not connected (status: " + status + ")"})
		return
	}

	if req.Mimetype == "" {
		req.Mimetype = http.DetectContentType(data)
	}
	kind := req.Kind
	if kind == "" {
		kind = kindFor(req.Mimetype)
	}
	mediaType, ok := uploadMediaType[kind]
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unsupported media kind: " + kind})
		return
	}

	// Claimed before the upload: replaying must not re-upload the file either.
	out, replay := sendGuard.begin(req.Key)
	if replay {
		writeReplay(w, s.Name, req.Key, out, true)
		return
	}
	defer sendGuard.resolve(req.Key, out, "", "", time.Time{}, errSendIncomplete)

	up, err := s.Client.Upload(r.Context(), data, mediaType)
	if err != nil {
		sendGuard.resolve(req.Key, out, "", "", time.Time{}, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "upload failed: " + err.Error()})
		return
	}
	msg, err := buildMediaMessage(kind, req, up)
	if err != nil {
		sendGuard.resolve(req.Key, out, "", "", time.Time{}, err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	resp, err := s.Client.SendMessage(r.Context(), jid, msg)
	sendGuard.resolve(req.Key, out, resp.ID, kind, resp.Timestamp, err)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "send failed: " + err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "sent", "kind": kind, "wa_message_id": resp.ID,
		"timestamp": resp.Timestamp.UTC().Format(time.RFC3339),
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
	if err := os.MkdirAll(mediaDir, 0o700); err != nil {
		log.Fatalf("cannot create media dir: %v", err)
	}

	// Before restoreExisting: reconnecting a session emits events immediately.
	startWebhookWorkers()

	manager.restoreExisting()
	go mediaGC()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /sessions", handleList)
	mux.HandleFunc("POST /sessions/{name}/start", handleStart)
	mux.HandleFunc("GET /sessions/{name}/status", handleStatus)
	mux.HandleFunc("GET /sessions/{name}/qr", handleQR)
	mux.HandleFunc("POST /sessions/{name}/send", handleSend)
	mux.HandleFunc("POST /sessions/{name}/send-media", handleSendMedia)
	mux.HandleFunc("GET /sessions/{name}/media/{id}", handleGetMedia)
	mux.HandleFunc("DELETE /sessions/{name}/media/{id}", handleDeleteMedia)
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
