package main

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func jid(user, server string) types.JID {
	return types.JID{User: user, Server: server}
}

// WhatsApp addresses users by LID (a random id) as well as by phone number.
// Reporting sender.User blindly puts a LID in Odoo's phone field, where it
// looks like a real number and matches no contact.
func TestResolvePN(t *testing.T) {
	pn := jid("966538952934", types.DefaultUserServer)
	lid := jid("35274583240901", types.HiddenUserServer)

	tests := []struct {
		name      string
		sender    types.JID
		senderAlt types.JID
		want      string
	}{
		{"sender is already a phone number", pn, types.EmptyJID, "966538952934"},
		{"phone number carried in SenderAlt", lid, pn, "966538952934"},
		{"lid with no alt and no client -> empty, never the lid", lid, types.EmptyJID, ""},
		{"lid whose alt is also a lid -> empty", lid, jid("1", types.HiddenUserServer), ""},
		{"device suffix stripped", types.JID{User: "966538952934", Server: types.DefaultUserServer, Device: 2},
			types.EmptyJID, "966538952934"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := resolvePN(nil, tt.sender, tt.senderAlt)
			if got.User != tt.want {
				t.Errorf("resolvePN(%s, %s).User = %q, want %q",
					tt.sender, tt.senderAlt, got.User, tt.want)
			}
			if got.User == tt.sender.User && tt.sender.Server == types.HiddenUserServer {
				t.Errorf("a LID (%s) leaked into the phone field", got.User)
			}
		})
	}
}

func TestExtractText(t *testing.T) {
	tests := []struct {
		name string
		msg  *waE2E.Message
		want string
	}{
		{"nil", nil, ""},
		{"conversation", &waE2E.Message{Conversation: proto.String("hello")}, "hello"},
		{
			"extended text",
			&waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("hi there")}},
			"hi there",
		},
		{
			"image with caption",
			&waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("look")}},
			"[image] look",
		},
		{
			"video with caption",
			&waE2E.Message{VideoMessage: &waE2E.VideoMessage{Caption: proto.String("clip")}},
			"[video] clip",
		},
		{
			"document",
			&waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("bill.pdf")}},
			"[document] bill.pdf",
		},
		{
			"reaction",
			&waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String("👍")}},
			"[reaction] 👍",
		},
		{
			"edited message unwraps to the replacement text",
			&waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
				EditedMessage: &waE2E.Message{Conversation: proto.String("fixed typo")},
			}},
			"[edited] fixed typo",
		},
		{"empty message", &waE2E.Message{}, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractText(tt.msg); got != tt.want {
				t.Errorf("extractText() = %q, want %q", got, tt.want)
			}
		})
	}
}

// A phone number can only address a private chat. Replying to a group, or to a
// sender we only know by LID, means sending to the chat's JID instead.
func TestResolveTarget(t *testing.T) {
	tests := []struct {
		name   string
		target target
		want   string
	}{
		{"phone becomes a private chat", target{Phone: "447700900123"}, "447700900123@s.whatsapp.net"},
		{"phone is stripped of formatting", target{Phone: "+44 7700 900123"}, "447700900123@s.whatsapp.net"},
		{"group jid is kept as-is", target{JID: "120363000000000000@g.us"}, "120363000000000000@g.us"},
		{"lid jid is kept as-is", target{JID: "35274583240901@lid"}, "35274583240901@lid"},
		{"jid wins over phone", target{Phone: "447700900123", JID: "120363000000000000@g.us"},
			"120363000000000000@g.us"},
		{"device suffix is dropped: address the chat, not one device",
			target{JID: "447700900123.0:2@s.whatsapp.net"}, "447700900123@s.whatsapp.net"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := resolveTarget(tt.target)
			if err != nil {
				t.Fatalf("resolveTarget(%+v) errored: %v", tt.target, err)
			}
			if got.String() != tt.want {
				t.Errorf("resolveTarget(%+v) = %q, want %q", tt.target, got, tt.want)
			}
		})
	}

	bad := []struct {
		name   string
		target target
	}{
		{"nothing to address", target{}},
		{"phone with no digits", target{Phone: "not a number"}},
		{"no user part", target{JID: "@g.us"}},
		{"server only", target{JID: "g.us"}},
		// These are receive-only: WhatsApp needs a different send path for them,
		// so fail here with a clear error instead of deep inside whatsmeow.
		{"newsletter is not sendable", target{JID: "120363000000000000@newsletter"}},
		{"status is not sendable", target{JID: "status@broadcast"}},
	}
	for _, tt := range bad {
		t.Run(tt.name, func(t *testing.T) {
			if got, err := resolveTarget(tt.target); err == nil {
				t.Errorf("resolveTarget(%+v) accepted it, returning %q", tt.target, got)
			}
		})
	}
}

// target and quote are embedded, so their fields have to stay promoted to the
// top level of the JSON Odoo actually posts. Embedding them in a struct literal
// or renaming a tag would silently drop the address and send nowhere.
func TestSendRequestDecodesOdooPayload(t *testing.T) {
	// Verbatim from whatsmeow.message._send_payload() for a group reply.
	body := `{"phone": "", "jid": "120363000000000000@g.us",
	          "quoted_id": "GRP-1", "quoted_participant": "447700900123@s.whatsapp.net",
	          "quoted_text": "who is bringing the keys?", "message": "I have them"}`
	var req sendRequest
	if err := json.Unmarshal([]byte(body), &req); err != nil {
		t.Fatalf("Odoo's send payload did not decode: %v", err)
	}
	if req.Message != "I have them" {
		t.Errorf("Message = %q", req.Message)
	}
	jid, err := resolveTarget(req.target)
	if err != nil {
		t.Fatalf("resolveTarget on Odoo's payload: %v", err)
	}
	if jid.String() != "120363000000000000@g.us" {
		t.Errorf("group reply addressed to %q, want the group", jid)
	}
	if ci := req.contextInfo(); ci.GetStanzaID() != "GRP-1" {
		t.Errorf("quote lost in decoding: %v", ci)
	}

	mediaBody := `{"phone": "", "jid": "120363000000000000@g.us", "quoted_id": "GRP-1",
	               "caption": "nice", "filename": "photo.jpg", "mimetype": "image/jpeg",
	               "kind": "image", "ptt": false, "data": "eA=="}`
	var mreq sendMediaRequest
	if err := json.Unmarshal([]byte(mediaBody), &mreq); err != nil {
		t.Fatalf("Odoo's send-media payload did not decode: %v", err)
	}
	if mreq.Caption != "nice" || mreq.Filename != "photo.jpg" || mreq.Kind != "image" {
		t.Errorf("media fields lost: %+v", mreq)
	}
	mjid, err := resolveTarget(mreq.target)
	if err != nil || mjid.String() != "120363000000000000@g.us" {
		t.Errorf("media reply addressed to %q (err %v), want the group", mjid, err)
	}
	if ci := mreq.contextInfo(); ci.GetStanzaID() != "GRP-1" {
		t.Errorf("media quote lost in decoding: %v", ci)
	}
}

func TestQuoteContextInfo(t *testing.T) {
	if ci := (quote{}).contextInfo(); ci != nil {
		t.Errorf("a message that is not a reply got a quote: %v", ci)
	}
	if ci := (quote{QuotedParticipant: "1@s.whatsapp.net"}).contextInfo(); ci != nil {
		t.Errorf("a quote without an id should be no quote at all, got %v", ci)
	}

	ci := quote{
		QuotedID:          "3EB0F4A2C1",
		QuotedParticipant: "447700900123@s.whatsapp.net",
		QuotedText:        "who is bringing the keys?",
	}.contextInfo()
	if ci.GetStanzaID() != "3EB0F4A2C1" {
		t.Errorf("StanzaID = %q, want the quoted message's id", ci.GetStanzaID())
	}
	// Without Participant, a group quote does not resolve to anyone.
	if ci.GetParticipant() != "447700900123@s.whatsapp.net" {
		t.Errorf("Participant = %q, want the original sender", ci.GetParticipant())
	}
	// WhatsApp renders the quote from the copy we send, not from its own history.
	if got := ci.GetQuotedMessage().GetConversation(); got != "who is bringing the keys?" {
		t.Errorf("QuotedMessage = %q, want the original text", got)
	}
}

// Message IDs arrive from the network and are used to build file paths.
func TestMediaPathRejectsTraversal(t *testing.T) {
	for _, id := range []string{
		"../../../../etc/passwd", "..", "a/b", "a\\b", "", "id with space",
		strings.Repeat("x", 129),
	} {
		if _, err := mediaPath("sess", id); err == nil {
			t.Errorf("mediaPath accepted dangerous id %q", id)
		}
	}
	for _, name := range []string{"../evil", "UPPER", "has/slash"} {
		if _, err := mediaPath(name, "3EB0ABC"); err == nil {
			t.Errorf("mediaPath accepted dangerous session %q", name)
		}
	}
	p, err := mediaPath("client_acme", "3EB0F4A2C1")
	if err != nil {
		t.Fatalf("mediaPath rejected a legitimate id: %v", err)
	}
	if !strings.HasSuffix(p, filepath.Join("media", "client_acme", "3EB0F4A2C1")) {
		t.Errorf("unexpected media path %q", p)
	}
}

func TestKindFor(t *testing.T) {
	tests := []struct{ mimetype, want string }{
		{"image/jpeg", "image"},
		{"image/png", "image"},
		{"image/webp", "sticker"}, // webp is how WhatsApp does stickers
		{"video/mp4", "video"},
		{"audio/ogg; codecs=opus", "audio"},
		{"application/pdf", "document"},
		{"text/plain", "document"},
		{"", "document"},
	}
	for _, tt := range tests {
		if got := kindFor(tt.mimetype); got != tt.want {
			t.Errorf("kindFor(%q) = %q, want %q", tt.mimetype, got, tt.want)
		}
	}
}

func TestExtractMedia(t *testing.T) {
	tests := []struct {
		name     string
		msg      *waE2E.Message
		wantKind string
		wantOK   bool
	}{
		{"text has no media", &waE2E.Message{Conversation: proto.String("hi")}, "", false},
		{"nil", nil, "", false},
		{
			"image",
			&waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg")}},
			"image", true,
		},
		{
			"voice note",
			&waE2E.Message{AudioMessage: &waE2E.AudioMessage{
				Mimetype: proto.String("audio/ogg"), PTT: proto.Bool(true), Seconds: proto.Uint32(5)}},
			"audio", true,
		},
		{
			"document keeps its filename",
			&waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				Mimetype: proto.String("application/pdf"), FileName: proto.String("invoice.pdf")}},
			"document", true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := extractMedia(tt.msg)
			if ok != tt.wantOK {
				t.Fatalf("extractMedia() ok = %v, want %v", ok, tt.wantOK)
			}
			if ok && got.Kind != tt.wantKind {
				t.Errorf("kind = %q, want %q", got.Kind, tt.wantKind)
			}
		})
	}

	// A voice note must survive as one, and a document must keep its name.
	if m, _ := extractMedia(&waE2E.Message{AudioMessage: &waE2E.AudioMessage{
		Mimetype: proto.String("audio/ogg"), PTT: proto.Bool(true), Seconds: proto.Uint32(5)}}); !m.PTT || m.Seconds != 5 {
		t.Errorf("voice note lost PTT/Seconds: %+v", m)
	}
	if m, _ := extractMedia(&waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
		FileName: proto.String("invoice.pdf")}}); m.Filename != "invoice.pdf" {
		t.Errorf("document filename = %q", m.Filename)
	}
}

func TestFilenameFor(t *testing.T) {
	tests := []struct {
		name string
		info *mediaInfo
		want string
	}{
		{"document keeps its name", &mediaInfo{Kind: "document", Filename: "invoice.pdf"}, "invoice.pdf"},
		{
			"a filename with a path is stripped to its base",
			&mediaInfo{Kind: "document", Filename: "../../etc/passwd"}, "passwd",
		},
		{"image gets an extension", &mediaInfo{Kind: "image", Mimetype: "image/jpeg"}, "image_ID1.jpeg"},
		{"unknown mimetype falls back per kind", &mediaInfo{Kind: "audio", Mimetype: "bogus/x"}, "audio_ID1.ogg"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := filenameFor(tt.info, "ID1")
			if tt.info.Kind == "image" {
				// mime.ExtensionsByType ordering varies by system; just require a sane name.
				if !strings.HasPrefix(got, "image_ID1.") {
					t.Errorf("filenameFor() = %q, want image_ID1.*", got)
				}
				return
			}
			if got != tt.want {
				t.Errorf("filenameFor() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestDescribeMessage(t *testing.T) {
	tests := []struct {
		name string
		evt  *events.Message
		want string
	}{
		{
			"audio is named, not reported as the server's generic label",
			&events.Message{
				Info:    types.MessageInfo{MessageSource: types.MessageSource{}, Type: "media"},
				Message: &waE2E.Message{AudioMessage: &waE2E.AudioMessage{}},
			},
			"audio",
		},
		{
			"sticker",
			&events.Message{
				Info:    types.MessageInfo{Type: "media"},
				Message: &waE2E.Message{StickerMessage: &waE2E.StickerMessage{}},
			},
			"sticker",
		},
		{
			"falls back to the server type when unrecognised",
			&events.Message{Info: types.MessageInfo{Type: "text"}, Message: &waE2E.Message{}},
			"text",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := describeMessage(tt.evt); got != tt.want {
				t.Errorf("describeMessage() = %q, want %q", got, tt.want)
			}
		})
	}
}
