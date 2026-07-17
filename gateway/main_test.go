package main

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
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
