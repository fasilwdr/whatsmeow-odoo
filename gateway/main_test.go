package main

import (
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
