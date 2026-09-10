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
	listenAddr = envOr("WMG_LISTEN", "127.0.0.1:8080")
	// Where a single-tenant install's events used to go. Kept only as the
	// defaults adopted on the first boot after the upgrade — from then on every
	// session's own target lives in registry.json. See registry.adoptLegacy.
	odooWebhookURL = os.Getenv("WMG_ODOO_WEBHOOK_URL")
	webhookSecret  = os.Getenv("WMG_WEBHOOK_SECRET")
	dataDir        = envOr("WMG_DATA_DIR", "./data") // one sqlite store per session
	registryPath   = filepath.Join(dataDir, "registry.json")
	nonDigits      = regexp.MustCompile(`\D`)

	// Inbound media is downloaded to disk and fetched by Odoo over the API
	// rather than inlined into the webhook: WhatsApp allows ~100MB files, and
	// webhook delivery retries, so a big payload would be re-sent several times.
	mediaDir      = filepath.Join(dataDir, "media")
	maxMediaBytes = int64(envIntOr("WMG_MAX_MEDIA_MB", 100)) << 20
	mediaTTL      = time.Duration(envIntOr("WMG_MEDIA_TTL_HOURS", 24)) * time.Hour
)

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
	// Name is what Odoo calls this session (its `code`) and what it sees in
	// every payload. Owner is the client the key on the request must belong to.
	// UID is the opaque handle everything on disk and in the caches is filed
	// under, so two clients may both call their number `main`.
	Name      string
	Owner     string
	UID       string
	Client    *whatsmeow.Client
	container *sqlstore.Container
	hooks     *webhookSender

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

// notify queues one event for this session's Odoo. It never blocks: it runs on
// whatsmeow's event handler, and stalling there stalls the WhatsApp connection.
func (s *Session) notify(event string, data map[string]any) {
	if s.hooks == nil {
		return
	}
	body, err := json.Marshal(map[string]any{
		"session": s.Name,
		"event":   event,
		"data":    data,
	})
	if err != nil {
		log.Printf("[%s/%s] webhook build error: %v", s.Owner, s.Name, err)
		return
	}
	s.hooks.enqueue(webhookJob{event: event, body: body})
}

// sendKey scopes an idempotency key to this session. Odoo derives its key from
// the database name and the record id, so two clients whose database happens to
// have the same name would collide — and a collision *replays* the first send,
// silently never delivering the second client's message while Odoo records it
// as sent. An empty key still means "the caller opted out of deduplication".
func (s *Session) sendKey(key string) string {
	if key == "" {
		return ""
	}
	return s.UID + "|" + key
}

// label names the session in logs: the same name may exist for several clients.
func (s *Session) label() string { return s.Owner + "/" + s.Name }

func (s *Session) snapshot() (string, string, string, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	jid := ""
	if s.Client != nil && s.Client.Store.ID != nil {
		jid = s.Client.Store.ID.String()
	}
	return s.Status, s.QRCode, s.LastErr, jid
}

// Sessions are keyed by their registry UID, not by name: two unrelated Odoos
// may both call their number `main`, and they must not meet.
type Manager struct {
	mu       sync.Mutex
	sessions map[string]*Session
}

var manager = &Manager{sessions: map[string]*Session{}}

var sessionNameRe = regexp.MustCompile(`^[a-z0-9_-]{1,40}$`)

func (m *Manager) get(uid string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[uid]
}

// forget disconnects a session and drops it, so its name is free again.
func (m *Manager) forget(uid string) {
	m.mu.Lock()
	s, ok := m.sessions[uid]
	delete(m.sessions, uid)
	m.mu.Unlock()
	if !ok {
		return
	}
	if s.hooks != nil {
		s.hooks.stop()
	}
	if s.Client != nil {
		s.Client.Disconnect()
	}
}

// StartSession creates (or reuses) a session and connects it. If the device
// is not yet paired, the QR pairing loop is started and the latest code is
// exposed via GET /sessions/{name}/qr for Odoo to render.
func (m *Manager) StartSession(e registryEntry) (*Session, error) {
	if !sessionNameRe.MatchString(e.Name) {
		return nil, fmt.Errorf("invalid session name (use a-z, 0-9, '-', '_')")
	}

	m.mu.Lock()
	prev, running := m.sessions[e.UID]
	m.mu.Unlock()
	if running {
		st, _, _, _ := prev.snapshot()
		if st == "connected" || st == "qr" || st == "starting" {
			return prev, nil // already running
		}
		// fall through: restart a dead session, keeping its webhook sender so
		// a restart loop cannot leak a goroutine per attempt.
	}

	dbPath := e.storePath()
	if err := os.MkdirAll(filepath.Dir(dbPath), 0o700); err != nil {
		return nil, fmt.Errorf("create store dir: %w", err)
	}
	label := e.Client + "/" + e.Name
	dbLog := waLog.Stdout("db/"+label, "WARN", true)

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

	clientLog := waLog.Stdout("wa/"+label, "INFO", true)
	client := whatsmeow.NewClient(device, clientLog)

	hooks := newWebhookSender(e.Client, e.Name)
	if running && prev.hooks != nil {
		hooks = prev.hooks
	}
	s := &Session{
		Name: e.Name, Owner: e.Client, UID: e.UID,
		Client: client, container: container, hooks: hooks, Status: "starting",
	}
	client.AddEventHandler(makeEventHandler(s))

	m.mu.Lock()
	m.sessions[e.UID] = s
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
					s.notify("session.paired", map[string]any{})
				case "timeout":
					s.set("disconnected", "", "QR pairing timed out; start again")
				default:
					log.Printf("[%s] QR event: %s", label, evt.Event)
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

// restoreExisting reconnects every previously-paired session so the gateway
// survives restarts without re-pairing. It walks the registry rather than the
// directory: a session's owner and webhook target are not recoverable from a
// filename, and a stray .db copied into the data dir is no longer silently
// promoted into a live session.
func (m *Manager) restoreExisting() {
	for _, e := range reg.all() {
		if _, err := os.Stat(e.storePath()); err != nil {
			// Claimed but never paired: there is nothing to reconnect, and
			// Odoo will POST /start when it wants the QR.
			continue
		}
		if _, err := m.StartSession(e); err != nil {
			log.Printf("[%s/%s] restore failed: %v", e.Client, e.Name, err)
		} else {
			log.Printf("[%s/%s] restored", e.Client, e.Name)
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
			// A reaction is not a message, it annotates one. Surface it as its
			// own event so Odoo can put it on the target message instead of
			// storing a noisy "[reaction] 👍" line of its own.
			if react := reactionOf(v.Message); react != nil {
				s.notify("message.reaction", s.reactionPayload(v, react))
				return
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
					log.Printf("[%s] media download failed for %s: %v", s.label(), v.Info.ID, err)
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
				kind := describeMessage(v)
				if isProtocolKind(kind) {
					// WhatsApp's own bookkeeping — an ephemeral-timer sync, an
					// app-state notification, a revoke — not something a person
					// wrote. Nobody sent it and nobody can act on it, so it is
					// dropped here rather than reaching Odoo as an
					// "[unsupported message type: protocol/...]" line an
					// operator has to read and dismiss. Odoo's inbound filter
					// cannot catch these: a keyword rule deliberately never
					// matches a placeholder body.
					return
				}
				// Nothing we can render - still tell Odoo something arrived.
				text = "[unsupported message type: " + kind + "]"
				placeholder = true
			}
			sender := v.Info.Sender.ToNonAD()
			senderPN := resolvePN(s.Client, sender, v.Info.SenderAlt)
			lid := senderLID(v)
			if senderPN.IsEmpty() {
				// Better an empty phone Odoo can flag than a LID masquerading as one.
				log.Printf("[%s] could not resolve a phone number for sender %s (mode=%s)",
					s.label(), sender, v.Info.AddressingMode)
			}
			// Chat is the conversation (the group, or the contact for a 1:1);
			// Sender is the individual who wrote. They differ only in groups,
			// and a reply has to go to the Chat.
			chatName := ""
			if v.Info.IsGroup {
				chatName = groupNameCache.lookup(s.UID, s.Client, v.Info.Chat)
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
			if q := quotedID(v.Message); q != "" {
				// The message this one replies to, so Odoo can thread it.
				payload["quoted_id"] = q
			}
			if media != nil {
				// Metadata only; Odoo pulls the bytes from /media/{id}.
				payload["media"] = media
			}
			s.notify("message.received", payload)

		case *events.Receipt:
			if v.Type == types.ReceiptTypeDelivered || v.Type == types.ReceiptTypeRead {
				s.notify("message.receipt", map[string]any{
					"receipt_type":   string(v.Type),
					"wa_message_ids": v.MessageIDs,
					"chat_jid":       v.Chat.String(),
					"timestamp":      v.Timestamp.UTC().Format(time.RFC3339),
				})
			}

		case *events.GroupInfo:
			// A rename would otherwise sit stale in the cache for an hour.
			groupNameCache.forget(s.UID, v.JID)

		case *events.Connected:
			s.set("connected", "", "")
			s.notify("session.connected", map[string]any{})

		case *events.Disconnected:
			s.set("disconnected", "", "")
			s.notify("session.disconnected", map[string]any{})

		case *events.LoggedOut:
			s.set("logged_out", "", "logged out from phone or banned")
			s.notify("session.logged_out", map[string]any{
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

// uidRe covers both shapes a session UID takes: the generated hex of a session
// claimed through the registry, and the old session name of one adopted from a
// single-tenant install, whose media directory must keep working.
var uidRe = regexp.MustCompile(`^[a-z0-9_-]{1,64}$`)

func mediaPath(uid, id string) (string, error) {
	if !uidRe.MatchString(uid) || !safeID.MatchString(id) {
		return "", fmt.Errorf("invalid session or message id")
	}
	// safeID permits dots, so "." and ".." still slip through the regex and
	// would climb out of the session directory via filepath.Join.
	if strings.Trim(id, ".") == "" {
		return "", fmt.Errorf("invalid message id")
	}
	return filepath.Join(mediaDir, uid, id), nil
}

// extractMedia returns the downloadable part of a message, if it has one.
// unwrap peels the container messages WhatsApp uses to carry a real payload:
// view-once photos/videos/voice notes, disappearing (ephemeral) messages,
// documents-with-caption and our own device-synced sends. The actual content
// sits one (occasionally more) levels down, so without unwrapping, extractText
// and extractMedia see an empty shell and fall back to the "unsupported message
// type" placeholder — which is exactly what a view-once message hit.
func unwrap(msg *waE2E.Message) *waE2E.Message {
	// Bounded so a malformed self-referential message can't spin here.
	for i := 0; msg != nil && i < 8; i++ {
		switch {
		case msg.GetViewOnceMessage().GetMessage() != nil:
			msg = msg.GetViewOnceMessage().GetMessage()
		case msg.GetViewOnceMessageV2().GetMessage() != nil:
			msg = msg.GetViewOnceMessageV2().GetMessage()
		case msg.GetViewOnceMessageV2Extension().GetMessage() != nil:
			msg = msg.GetViewOnceMessageV2Extension().GetMessage()
		case msg.GetEphemeralMessage().GetMessage() != nil:
			msg = msg.GetEphemeralMessage().GetMessage()
		case msg.GetDocumentWithCaptionMessage().GetMessage() != nil:
			msg = msg.GetDocumentWithCaptionMessage().GetMessage()
		case msg.GetDeviceSentMessage().GetMessage() != nil:
			msg = msg.GetDeviceSentMessage().GetMessage()
		default:
			return msg
		}
	}
	return msg
}

func extractMedia(msg *waE2E.Message) (*mediaInfo, bool) {
	msg = unwrap(msg)
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
	path, err := mediaPath(s.UID, id)
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
//
// Keyed by session as well as by group: a subject fetched with one client's
// credentials must not be served to another, even though the JID is global.
func (g *groupNames) lookup(uid string, cli *whatsmeow.Client, chat types.JID) string {
	key := uid + "|" + chat.String()
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

func (g *groupNames) forget(uid string, chat types.JID) {
	g.mu.Lock()
	delete(g.entries, uid+"|"+chat.String())
	g.mu.Unlock()
}

// describeMessage names the concrete payload we could not turn into text, so the
// Odoo log says "audio" rather than the server's generic "text" label.
func describeMessage(v *events.Message) string {
	msg := unwrap(v.Message)
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
		return protocolKindPrefix + msg.GetProtocolMessage().GetType().String()
	default:
		return v.Info.Type
	}
}

// protocolKindPrefix is what describeMessage() labels a ProtocolMessage with.
const protocolKindPrefix = "protocol/"

// isProtocolKind says whether describeMessage() named WhatsApp's own protocol
// traffic rather than something a person sent. Kept beside describeMessage so
// the two cannot drift apart.
func isProtocolKind(kind string) bool {
	return strings.HasPrefix(kind, protocolKindPrefix)
}

// reactionOf returns the reaction inside a message (unwrapping ephemeral/
// view-once envelopes), or nil when the message is not a reaction.
func reactionOf(msg *waE2E.Message) *waE2E.ReactionMessage {
	if m := unwrap(msg); m != nil {
		return m.GetReactionMessage()
	}
	return nil
}

// senderLID pulls the LID of whoever sent an event, whichever side of the
// phone/LID pair carries it, or "" when the sender is known by phone only.
func senderLID(v *events.Message) string {
	sender := v.Info.Sender.ToNonAD()
	if sender.Server == types.HiddenUserServer {
		return sender.User
	}
	if v.Info.SenderAlt.Server == types.HiddenUserServer {
		return v.Info.SenderAlt.User
	}
	return ""
}

// reactionPayload describes an inbound reaction for Odoo: which message it
// targets, the emoji ("" means the sender removed their reaction) and who
// reacted, mirroring the identity fields of a normal message.
func (s *Session) reactionPayload(v *events.Message, react *waE2E.ReactionMessage) map[string]any {
	sender := v.Info.Sender.ToNonAD()
	senderPN := resolvePN(s.Client, sender, v.Info.SenderAlt)
	key := react.GetKey()
	return map[string]any{
		"wa_message_id":  v.Info.ID,       // the reaction's own id
		"target_id":      key.GetID(),     // the message being reacted to
		"target_from_me": key.GetFromMe(), // was that message ours?
		"emoji":          react.GetText(), // "" when the reaction was removed
		"sender_jid":     sender.String(),
		"sender_phone":   senderPN.User, // "" when only a LID is known
		"sender_lid":     senderLID(v),
		"chat_jid":       v.Info.Chat.String(),
		"timestamp":      v.Info.Timestamp.UTC().Format(time.RFC3339),
	}
}

func extractText(msg *waE2E.Message) string {
	msg = unwrap(msg)
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

// quotedID returns the id of the message this one quotes, "" when it quotes
// nothing. A quote rides in the ContextInfo of whichever part carries it, so
// this is the mirror of `quote.contextInfo()` on the sending side: it lets Odoo
// link an inbound reply back to the message it answers instead of losing the
// thread.
func quotedID(msg *waE2E.Message) string {
	msg = unwrap(msg)
	if msg == nil {
		return ""
	}
	for _, ci := range []*waE2E.ContextInfo{
		msg.GetExtendedTextMessage().GetContextInfo(),
		msg.GetImageMessage().GetContextInfo(),
		msg.GetVideoMessage().GetContextInfo(),
		msg.GetAudioMessage().GetContextInfo(),
		msg.GetStickerMessage().GetContextInfo(),
		msg.GetDocumentMessage().GetContextInfo(),
	} {
		if id := ci.GetStanzaID(); id != "" {
			return id
		}
	}
	return ""
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

// typing carries how long, in milliseconds, to hold the "typing…" indicator on
// the recipient's phone before the message lands. A number whose messages
// always appear fully formed with no keystrokes in front of them is one of the
// cheaper automation tells; Odoo decides the duration (it knows the message and
// the session's settings) and we just perform it.
type typing struct {
	TypingMS int `json:"typing_ms"`
}

// maxTypingMS bounds what a caller can make the handler sleep for. Odoo clamps
// this already, but the handler holds an HTTP request open for it, so it must
// not be at the mercy of the payload.
const maxTypingMS = 10000

// typingDuration turns a requested pause into the one we will actually take:
// nothing at all for a non-positive request, and never more than maxTypingMS.
func typingDuration(ms int) time.Duration {
	if ms <= 0 {
		return 0
	}
	if ms > maxTypingMS {
		ms = maxTypingMS
	}
	return time.Duration(ms) * time.Millisecond
}

// simulateTyping shows the typing indicator, waits, then clears it. Every step
// is best-effort: a chat state is a courtesy, and failing to set one is never a
// reason not to send the message. Returns early if the context is cancelled, so
// a client that gives up does not leave us sleeping.
func simulateTyping(ctx context.Context, cli *whatsmeow.Client, jid types.JID, ms int, media types.ChatPresenceMedia) {
	pause := typingDuration(ms)
	if pause == 0 || cli == nil {
		return
	}
	if err := cli.SendChatPresence(ctx, jid, types.ChatPresenceComposing, media); err != nil {
		log.Printf("typing indicator for %s failed: %v", jid, err)
		return
	}
	select {
	case <-time.After(pause):
	case <-ctx.Done():
	}
	// Paused, not available: "paused" is what a real client sends when the
	// person stops typing, and the message follows immediately after.
	if err := cli.SendChatPresence(ctx, jid, types.ChatPresencePaused, media); err != nil {
		log.Printf("clearing typing indicator for %s failed: %v", jid, err)
	}
}

type sendRequest struct {
	target
	quote
	idempotent
	typing
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

// ---------------------------------------------------------------------------
// Recipient validation (PLAN.md §12.4)
//
// Sending to numbers that are not on WhatsApp is one of the clearest
// bulk-sender fingerprints there is, and whatsmeow can ask before we burn a
// send. But bulk-querying IsOnWhatsApp is a fingerprint of its own, so the
// gateway does not simply pass the batch through: it answers from a cache
// where it can, and spends a per-session budget where it cannot.
// ---------------------------------------------------------------------------

type checkEntry struct {
	registered bool
	jid        string
	at         time.Time
}

type checkCache struct {
	mu sync.Mutex
	// Keyed by bare digits. Whether a number is on WhatsApp is a property of
	// the number, not of the session that asked, so the cache is global.
	byNumber map[string]checkEntry
	// Per-session spend, keyed by UID so one busy client cannot burn another's
	// budget by naming its session the same thing.
	spent map[string]*checkBudget
	ttl   time.Duration
}

type checkBudget struct {
	count       int
	windowStart time.Time
}

var (
	// A registration does not change often, so a long cache is both safe and
	// the main thing keeping the query rate down.
	checkTTL = time.Duration(envIntOr("WMG_CHECK_TTL_DAYS", 30)) * 24 * time.Hour
	// Numbers one request may ask about, and how many *uncached* lookups a
	// session may make per hour.
	checkMaxBatch = envIntOr("WMG_CHECK_MAX_BATCH", 50)
	checkPerHour  = envIntOr("WMG_CHECK_PER_HOUR", 500)

	checkGuard = &checkCache{
		byNumber: map[string]checkEntry{},
		spent:    map[string]*checkBudget{},
	}
)

// lookup answers from the cache, or reports a miss.
func (c *checkCache) lookup(number string) (checkEntry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.byNumber[number]
	if !ok || time.Since(e.at) > c.ttlOr() {
		return checkEntry{}, false
	}
	return e, true
}

func (c *checkCache) ttlOr() time.Duration {
	if c.ttl > 0 {
		return c.ttl
	}
	return checkTTL
}

func (c *checkCache) store(number string, registered bool, jid string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.byNumber[number] = checkEntry{registered: registered, jid: jid, at: time.Now()}
}

// grant hands out up to `want` lookups from this session's hourly budget. A
// partial grant is normal and useful: the caller checks what it can now and
// comes back for the rest, which is exactly the drip we want anyway.
func (c *checkCache) grant(session string, want int) int {
	if want <= 0 {
		return 0
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b, ok := c.spent[session]
	if !ok || time.Since(b.windowStart) >= time.Hour {
		b = &checkBudget{windowStart: time.Now()}
		c.spent[session] = b
	}
	free := checkPerHour - b.count
	if free <= 0 {
		return 0
	}
	if want > free {
		want = free
	}
	b.count += want
	return want
}

type checkRequest struct {
	Phones []string `json:"phones"`
}

// handleCheck reports which of the given numbers are registered on WhatsApp.
//
// Cached answers are free; only numbers we have to ask about spend the hourly
// budget, and a request that runs out of budget still returns everything it
// knew, flagged `throttled` so Odoo can come back later rather than treat the
// unanswered numbers as "not on WhatsApp".
func handleCheck(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	var req checkRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Phones) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'phones' is required"})
		return
	}
	if len(req.Phones) > checkMaxBatch {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": fmt.Sprintf("too many numbers: %d (max %d per request)",
				len(req.Phones), checkMaxBatch)})
		return
	}

	// Normalise and de-duplicate first: Odoo stores numbers however a human
	// typed them, and asking WhatsApp about the same number twice in one batch
	// spends budget for nothing.
	results := make([]map[string]any, 0, len(req.Phones))
	var ask []string
	seen := map[string]bool{}
	for _, raw := range req.Phones {
		digits := nonDigits.ReplaceAllString(raw, "")
		if digits == "" || seen[digits] {
			continue
		}
		seen[digits] = true
		if e, ok := checkGuard.lookup(digits); ok {
			results = append(results, map[string]any{
				"number": digits, "registered": e.registered, "jid": e.jid,
				"cached": true,
			})
			continue
		}
		ask = append(ask, digits)
	}

	throttled := false
	if len(ask) > 0 {
		status, _, _, _ := s.snapshot()
		if status != "connected" {
			writeJSON(w, http.StatusConflict, map[string]string{
				"error": "session not connected (status: " + status + ")"})
			return
		}
		granted := checkGuard.grant(s.UID, len(ask))
		if granted < len(ask) {
			throttled = true
			log.Printf("[%s] check: hourly budget allows %d of %d lookups",
				s.label(), granted, len(ask))
		}
		ask = ask[:granted]
	}
	if len(ask) > 0 {
		// IsOnWhatsApp wants international format with the leading '+'.
		query := make([]string, len(ask))
		for i, n := range ask {
			query[i] = "+" + n
		}
		resp, err := s.Client.IsOnWhatsApp(r.Context(), query)
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{
				"error": "check failed: " + err.Error()})
			return
		}
		answered := map[string]bool{}
		for _, item := range resp {
			digits := nonDigits.ReplaceAllString(item.Query, "")
			if digits == "" {
				digits = item.JID.User
			}
			answered[digits] = true
			jidStr := ""
			if item.IsIn {
				jidStr = item.JID.String()
			}
			checkGuard.store(digits, item.IsIn, jidStr)
			results = append(results, map[string]any{
				"number": digits, "registered": item.IsIn, "jid": jidStr,
			})
		}
		// A number WhatsApp said nothing about is not a "no" — it is an
		// unanswered question, and caching it as a no would silently stop Odoo
		// ever messaging that contact again.
		for _, n := range ask {
			if !answered[n] {
				throttled = true
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"results":   results,
		"throttled": throttled,
	})
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

// ctxClient carries the calling Odoo, resolved from its API key by
// authMiddleware. Every session lookup goes through it.
type ctxKey int

const ctxClient ctxKey = 0

func callerOf(r *http.Request) apiClient {
	c, _ := r.Context().Value(ctxClient).(apiClient)
	return c
}

// requireSession resolves {name} inside the calling client's namespace. A name
// belonging to a different Odoo is simply not found: the client is half of the
// key, so there is no path from one install to another's number, its media or
// its logout button.
func requireSession(w http.ResponseWriter, r *http.Request) *Session {
	entry, ok := reg.get(callerOf(r).Label, r.PathValue("name"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown session; start it first"})
		return nil
	}
	s := manager.get(entry.UID)
	if s == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "session is registered but not running; start it first"})
		return nil
	}
	return s
}

// registration is what Odoo tells the gateway about itself: where to post this
// session's events, and with which secret. Every field is optional so a
// pre-registry Odoo keeps working — it starts its session with whatever target
// is already on file.
type registration struct {
	WebhookURL    string `json:"webhook_url"`
	WebhookSecret string `json:"webhook_secret"`
	Label         string `json:"label"`
}

func decodeRegistration(r *http.Request) (registration, error) {
	var req registration
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil && err != io.EOF {
		return req, err
	}
	if req.WebhookURL != "" {
		if err := validateWebhookURL(req.WebhookURL); err != nil {
			return req, err
		}
	}
	return req, nil
}

// handleStart claims the name for the calling client on first use and connects
// it. Odoo re-asserts its webhook target on every start rather than only at
// pairing: a session that is already paired never pairs again, so pairing is
// the one moment that cannot be relied on to re-point an Odoo that has moved.
func handleStart(w http.ResponseWriter, r *http.Request) {
	c := callerOf(r)
	name := r.PathValue("name")
	if !sessionNameRe.MatchString(name) {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid session name (use a-z, 0-9, '-', '_')"})
		return
	}
	req, err := decodeRegistration(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if _, known := reg.get(c.Label, name); !known &&
		maxSessionsPerClient > 0 && reg.count(c.Label) >= maxSessionsPerClient {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": fmt.Sprintf("client %q already has %d sessions (WMG_MAX_SESSIONS_PER_CLIENT)",
				c.Label, maxSessionsPerClient)})
		return
	}
	entry, err := reg.claim(c, name, req.WebhookURL, req.WebhookSecret, req.Label)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	if entry.WebhookURL == "" {
		log.Printf("[%s/%s] no webhook_url registered: this session's events will be dropped",
			entry.Client, entry.Name)
	}

	s, err := manager.StartSession(entry)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	status, qr, lastErr, jid := s.snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"session": name, "status": status, "qr": qr, "error": lastErr, "jid": jid,
		"webhook_url": entry.WebhookURL,
	})
}

// handleSetWebhook re-points a session without restarting it.
func handleSetWebhook(w http.ResponseWriter, r *http.Request) {
	c := callerOf(r)
	name := r.PathValue("name")
	if _, ok := reg.get(c.Label, name); !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown session"})
		return
	}
	req, err := decodeRegistration(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.WebhookURL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'webhook_url' is required"})
		return
	}
	entry, err := reg.setWebhook(c.Label, name, req.WebhookURL, req.WebhookSecret)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"session": entry.Name, "webhook_url": entry.WebhookURL,
	})
}

// handleForget drops a session entirely: its registry row, its WhatsApp store
// and any media Odoo never collected. Destructive on purpose and deliberately
// not wired to Odoo's own record deletion — this unpairs a device.
func handleForget(w http.ResponseWriter, r *http.Request) {
	c := callerOf(r)
	entry, ok := reg.remove(c.Label, r.PathValue("name"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown session"})
		return
	}
	manager.forget(entry.UID)
	store := entry.storePath()
	for _, suffix := range []string{"", "-wal", "-shm"} {
		_ = os.Remove(store + suffix)
	}
	_ = os.RemoveAll(entry.mediaDirPath())
	log.Printf("[%s/%s] forgotten: store and media removed", entry.Client, entry.Name)
	writeJSON(w, http.StatusOK, map[string]string{"status": "forgotten"})
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	status, _, lastErr, jid := s.snapshot()
	out := map[string]any{
		"session": s.Name, "status": status, "error": lastErr, "jid": jid,
	}
	// The webhook target and its health ride along, so Odoo can tell an
	// operator that the broken half is its own end rather than the number.
	if e, ok := reg.get(s.Owner, s.Name); ok {
		out["webhook_url"] = e.WebhookURL
	}
	if s.hooks != nil {
		for k, v := range s.hooks.health() {
			out[k] = v
		}
	}
	writeJSON(w, http.StatusOK, out)
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
	key := s.sendKey(req.Key)
	out, replay := sendGuard.begin(key)
	if replay {
		writeReplay(w, s.label(), req.Key, out, false)
		return
	}
	defer sendGuard.resolve(key, out, "", "", time.Time{}, errSendIncomplete)

	simulateTyping(r.Context(), s.Client, jid, req.TypingMS, types.ChatPresenceMediaText)

	// A quote has to hang off ContextInfo, which plain Conversation has no room
	// for; ExtendedTextMessage is the same text with somewhere to put it.
	msg := &waE2E.Message{Conversation: proto.String(req.Message)}
	if ci := req.contextInfo(); ci != nil {
		msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(req.Message), ContextInfo: ci,
		}}
	}
	resp, err := s.Client.SendMessage(r.Context(), jid, msg)
	sendGuard.resolve(key, out, resp.ID, "", resp.Timestamp, err)
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

type reactRequest struct {
	target
	TargetID     string `json:"target_id"`     // message being reacted to
	TargetSender string `json:"target_sender"` // JID that authored it (group/participant)
	FromMe       bool   `json:"from_me"`       // was the target our own message?
	Emoji        string `json:"emoji"`         // "" removes the reaction
}

// handleReact reacts to a message with an emoji (or clears the reaction when
// emoji is empty). A reaction is a normal message under the hood, built by
// BuildReaction, so it flows through SendMessage like everything else.
func handleReact(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	var req reactRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil ||
		strings.TrimSpace(req.TargetID) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'target_id' is required"})
		return
	}
	chat, err := resolveTarget(req.target)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	status, _, _, _ := s.snapshot()
	if status != "connected" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "session not connected (status: " + status + ")"})
		return
	}
	// The reaction key must name whoever authored the target message: our own
	// JID when reacting to our message, otherwise the participant (the contact
	// in a 1:1, or the specific group member). Defaults to the chat, which is
	// the contact for a private chat.
	sender := chat
	if req.FromMe {
		if s.Client.Store.ID == nil {
			writeJSON(w, http.StatusConflict, map[string]string{"error": "session has no identity yet"})
			return
		}
		sender = s.Client.Store.ID.ToNonAD()
	} else if raw := strings.TrimSpace(req.TargetSender); raw != "" {
		if p, perr := types.ParseJID(raw); perr == nil {
			sender = p.ToNonAD()
		}
	}
	msg := s.Client.BuildReaction(chat, sender, types.MessageID(req.TargetID), req.Emoji)
	resp, err := s.Client.SendMessage(r.Context(), chat, msg)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "reaction failed: " + err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":        "sent",
		"wa_message_id": resp.ID,
		"timestamp":     resp.Timestamp.UTC().Format(time.RFC3339),
	})
}

type readRequest struct {
	target
	Sender     string   `json:"sender"`      // who wrote them; only groups need it
	MessageIDs []string `json:"message_ids"` // all by the same author
	Timestamp  string   `json:"timestamp"`   // RFC3339 read time; defaults to now
}

// handleMarkRead sends a read receipt — the blue ticks — for messages someone
// sent us. WhatsApp takes one receipt per author, so a batch must not mix
// senders; Odoo marks one message at a time, which trivially satisfies that.
func handleMarkRead(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	var req readRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.MessageIDs) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'message_ids' is required"})
		return
	}
	ids := make([]types.MessageID, 0, len(req.MessageIDs))
	for _, id := range req.MessageIDs {
		if id = strings.TrimSpace(id); id != "" {
			ids = append(ids, types.MessageID(id))
		}
	}
	if len(ids) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "'message_ids' is required"})
		return
	}
	chat, err := resolveTarget(req.target)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	status, _, _, _ := s.snapshot()
	if status != "connected" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "session not connected (status: " + status + ")"})
		return
	}
	// Only a group receipt names the author; in a 1:1 the chat *is* the sender
	// and whatsmeow drops the field, so an empty JID is the right default.
	sender := types.EmptyJID
	if raw := strings.TrimSpace(req.Sender); raw != "" {
		if p, perr := types.ParseJID(raw); perr == nil {
			sender = p.ToNonAD()
		}
	}
	ts := time.Now()
	if raw := strings.TrimSpace(req.Timestamp); raw != "" {
		if parsed, perr := time.Parse(time.RFC3339, raw); perr == nil {
			ts = parsed
		}
	}
	if err := s.Client.MarkRead(r.Context(), ids, ts, chat, sender); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "mark read failed: " + err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "read", "count": len(ids)})
}

// handleGetMedia streams a previously downloaded file to Odoo.
func handleGetMedia(w http.ResponseWriter, r *http.Request) {
	s := requireSession(w, r)
	if s == nil {
		return
	}
	path, err := mediaPath(s.UID, r.PathValue("id"))
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
	path, err := mediaPath(s.UID, r.PathValue("id"))
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
	typing
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
	key := s.sendKey(req.Key)
	out, replay := sendGuard.begin(key)
	if replay {
		writeReplay(w, s.label(), req.Key, out, true)
		return
	}
	defer sendGuard.resolve(key, out, "", "", time.Time{}, errSendIncomplete)

	up, err := s.Client.Upload(r.Context(), data, mediaType)
	if err != nil {
		sendGuard.resolve(key, out, "", "", time.Time{}, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "upload failed: " + err.Error()})
		return
	}
	msg, err := buildMediaMessage(kind, req, up)
	if err != nil {
		sendGuard.resolve(key, out, "", "", time.Time{}, err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	// A voice note is preceded by "recording audio", not "typing" — that is
	// what the recipient's phone shows when a person is actually holding the
	// mic button.
	presenceMedia := types.ChatPresenceMediaText
	if req.PTT || kind == "audio" {
		presenceMedia = types.ChatPresenceMediaAudio
	}
	simulateTyping(r.Context(), s.Client, jid, req.TypingMS, presenceMedia)

	resp, err := s.Client.SendMessage(r.Context(), jid, msg)
	sendGuard.resolve(key, out, resp.ID, kind, resp.Timestamp, err)
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

// handleList shows the calling client its own sessions and nobody else's,
// including ones registered but not currently running.
func handleList(w http.ResponseWriter, r *http.Request) {
	c := callerOf(r)
	out := []map[string]any{}
	for _, e := range reg.list(c.Label) {
		row := map[string]any{
			"session": e.Name, "status": "stopped", "error": "", "jid": "",
			"webhook_url": e.WebhookURL,
		}
		if s := manager.get(e.UID); s != nil {
			status, _, lastErr, jid := s.snapshot()
			row["status"], row["error"], row["jid"] = status, lastErr, jid
		}
		out = append(out, row)
	}
	writeJSON(w, http.StatusOK, out)
}

func authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		c, ok := clientFor(r.Header.Get("X-Api-Key"))
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid or missing X-Api-Key"})
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxClient, c)))
	})
}

// ---------------------------------------------------------------------------

func main() {
	if err := loadAPIKeys(); err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		log.Fatalf("cannot create data dir: %v", err)
	}
	if err := os.MkdirAll(mediaDir, 0o700); err != nil {
		log.Fatalf("cannot create media dir: %v", err)
	}

	// Before restoreExisting: reconnecting a session emits events immediately,
	// and an event with nowhere to go is a lost message.
	if err := reg.load(registryPath); err != nil {
		log.Fatalf("registry: %v", err)
	}
	if err := reg.adoptLegacy(odooWebhookURL, webhookSecret); err != nil {
		log.Printf("registry: adopting existing sessions failed: %v", err)
	}

	manager.restoreExisting()
	go mediaGC()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /sessions", handleList)
	mux.HandleFunc("POST /sessions/{name}/start", handleStart)
	mux.HandleFunc("PUT /sessions/{name}/webhook", handleSetWebhook)
	mux.HandleFunc("DELETE /sessions/{name}", handleForget)
	mux.HandleFunc("GET /sessions/{name}/status", handleStatus)
	mux.HandleFunc("GET /sessions/{name}/qr", handleQR)
	mux.HandleFunc("POST /sessions/{name}/send", handleSend)
	mux.HandleFunc("POST /sessions/{name}/send-media", handleSendMedia)
	mux.HandleFunc("POST /sessions/{name}/react", handleReact)
	mux.HandleFunc("POST /sessions/{name}/read", handleMarkRead)
	mux.HandleFunc("POST /sessions/{name}/check", handleCheck)
	mux.HandleFunc("GET /sessions/{name}/media/{id}", handleGetMedia)
	mux.HandleFunc("DELETE /sessions/{name}/media/{id}", handleDeleteMedia)
	mux.HandleFunc("POST /sessions/{name}/logout", handleLogout)

	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           authMiddleware(mux),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("whatsmeow-gateway listening on %s (data dir: %s, %d client(s), %d session(s))",
			listenAddr, dataDir, len(apiClients), len(reg.all()))
		// Say the webhook policy out loud: a refused registration is otherwise
		// a puzzle at exactly the wrong moment, halfway through pairing.
		if allowPrivateWebhooks {
			log.Printf("webhook targets: private addresses allowed (this gateway is not on a public network)")
		} else {
			log.Printf("webhook targets: https only, no private addresses — " +
				"set WMG_WEBHOOK_ALLOW_PRIVATE=1 if Odoo is on this gateway's own network")
		}
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
		if s.hooks != nil {
			s.hooks.stop()
		}
		s.Client.Disconnect()
	}
	manager.mu.Unlock()
}
