package main

import (
	"strings"
	"testing"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// A location has no body, so it used to arrive as "[unsupported message type:
// location]". It must now read as a place and carry its coordinates as data.
func TestExtractRichLocation(t *testing.T) {
	rich := extractRich(&waE2E.Message{LocationMessage: &waE2E.LocationMessage{
		DegreesLatitude:  proto.Float64(24.713552),
		DegreesLongitude: proto.Float64(46.675297),
		Name:             proto.String("Kingdom Centre"),
		Address:          proto.String("Al Olaya, Riyadh"),
	}})
	if rich == nil || rich.Kind != "location" {
		t.Fatalf("extractRich() = %+v, want a location", rich)
	}
	want := "📍 Kingdom Centre\nAl Olaya, Riyadh\nhttps://maps.google.com/?q=24.713552,46.675297"
	if rich.Text != want {
		t.Errorf("text = %q, want %q", rich.Text, want)
	}
	if rich.Location == nil || rich.Location.Latitude != 24.713552 ||
		rich.Location.Longitude != 46.675297 || rich.Location.Name != "Kingdom Centre" {
		t.Errorf("location = %+v", rich.Location)
	}
}

func TestExtractRichLocationWithoutAName(t *testing.T) {
	rich := extractRich(&waE2E.Message{LocationMessage: &waE2E.LocationMessage{
		DegreesLatitude: proto.Float64(-33.8568), DegreesLongitude: proto.Float64(151.2153),
	}})
	want := "📍 Location\nhttps://maps.google.com/?q=-33.856800,151.215300"
	if rich == nil || rich.Text != want {
		t.Fatalf("text = %+v, want %q", rich, want)
	}
}

func TestExtractRichLiveLocation(t *testing.T) {
	rich := extractRich(&waE2E.Message{LiveLocationMessage: &waE2E.LiveLocationMessage{
		DegreesLatitude: proto.Float64(1.5), DegreesLongitude: proto.Float64(2.5),
		Caption: proto.String("on my way"),
	}})
	if rich == nil || rich.Kind != "location" || !rich.Location.Live {
		t.Fatalf("extractRich() = %+v, want a live location", rich)
	}
	if !strings.HasPrefix(rich.Text, "📍 Live location\non my way\n") {
		t.Errorf("text = %q", rich.Text)
	}
}

// WhatsApp has moved polls through several fields; whichever carries one, the
// question and its options are the same thing to a reader.
func TestExtractRichPollAcrossVersions(t *testing.T) {
	poll := &waE2E.PollCreationMessage{
		Name: proto.String("Lunch?"),
		Options: []*waE2E.PollCreationMessage_Option{
			{OptionName: proto.String("Pizza")},
			{OptionName: proto.String("Sushi")},
		},
		SelectableOptionsCount: proto.Uint32(1),
	}
	want := "📊 Lunch?\n• Pizza\n• Sushi\n(select one)"
	for name, msg := range map[string]*waE2E.Message{
		"v1": {PollCreationMessage: poll},
		"v2": {PollCreationMessageV2: poll},
		"v3": {PollCreationMessageV3: poll},
		"v5": {PollCreationMessageV5: poll},
		"v4 wrapper": {PollCreationMessageV4: &waE2E.FutureProofMessage{
			Message: &waE2E.Message{PollCreationMessage: poll}}},
		"ephemeral": {EphemeralMessage: &waE2E.FutureProofMessage{
			Message: &waE2E.Message{PollCreationMessageV3: poll}}},
	} {
		t.Run(name, func(t *testing.T) {
			rich := extractRich(msg)
			if rich == nil || rich.Kind != "poll" || rich.Text != want {
				t.Errorf("extractRich() = %+v, want poll %q", rich, want)
			}
		})
	}
}

func TestExtractRichMultiSelectPoll(t *testing.T) {
	rich := extractRich(&waE2E.Message{PollCreationMessageV3: &waE2E.PollCreationMessage{
		Name:    proto.String("Toppings"),
		Options: []*waE2E.PollCreationMessage_Option{{OptionName: proto.String("Olives")}},
	}})
	if rich == nil || !strings.HasSuffix(rich.Text, "(select one or more)") {
		t.Errorf("text = %+v", rich)
	}
}

func TestExtractRichEvent(t *testing.T) {
	rich := extractRich(&waE2E.Message{EventMessage: &waE2E.EventMessage{
		Name:        proto.String("Team dinner"),
		Description: proto.String("Bring an appetite"),
		StartTime:   proto.Int64(1791468000), // 2026-10-08 14:00 UTC
		JoinLink:    proto.String("https://call.whatsapp.com/video/abc"),
		Location: &waE2E.LocationMessage{
			Name:             proto.String("Najd Village"),
			DegreesLatitude:  proto.Float64(24.7),
			DegreesLongitude: proto.Float64(46.7),
		},
	}})
	if rich == nil || rich.Kind != "event" {
		t.Fatalf("extractRich() = %+v, want an event", rich)
	}
	want := "📅 Team dinner\nBring an appetite\nStarts: 2026-10-08 14:00 UTC\n" +
		"Where: Najd Village https://maps.google.com/?q=24.700000,46.700000\n" +
		"Join: https://call.whatsapp.com/video/abc"
	if rich.Text != want {
		t.Errorf("text = %q, want %q", rich.Text, want)
	}
	if rich.Location == nil || rich.Location.Latitude != 24.7 {
		t.Errorf("an event with a pinned venue should carry it: %+v", rich.Location)
	}
}

func TestExtractRichCancelledEventWithoutAPin(t *testing.T) {
	rich := extractRich(&waE2E.Message{EventMessage: &waE2E.EventMessage{
		Name: proto.String("Standup"), IsCanceled: proto.Bool(true),
		Location: &waE2E.LocationMessage{Name: proto.String("Room 4")},
	}})
	if rich == nil || rich.Text != "📅 Standup (cancelled)\nWhere: Room 4" {
		t.Fatalf("text = %+v", rich)
	}
	if rich.Location != nil {
		t.Errorf("a venue with no coordinates is not a location: %+v", rich.Location)
	}
}

func TestExtractRichContact(t *testing.T) {
	vcard := "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\n" +
		"TEL;type=CELL;waid=447700900123:+44 7700 900123\n" +
		"item1.TEL;type=HOME:+44 20 7946 0000\nEND:VCARD"
	rich := extractRich(&waE2E.Message{ContactMessage: &waE2E.ContactMessage{
		DisplayName: proto.String("Jane Doe"), Vcard: proto.String(vcard),
	}})
	want := "👤 Jane Doe\n+44 7700 900123\n+44 20 7946 0000"
	if rich == nil || rich.Kind != "contact" || rich.Text != want {
		t.Errorf("extractRich() = %+v, want %q", rich, want)
	}
}

func TestExtractRichIgnoresOrdinaryMessages(t *testing.T) {
	for name, msg := range map[string]*waE2E.Message{
		"nil":   nil,
		"text":  {Conversation: proto.String("hello")},
		"image": {ImageMessage: &waE2E.ImageMessage{}},
	} {
		if rich := extractRich(msg); rich != nil {
			t.Errorf("%s: extractRich() = %+v, want nil", name, rich)
		}
	}
}

// A business account's OTP is text, but not in Conversation: the server still
// labels it "text", which is how it became "[unsupported message type: text]".
func TestExtractTextReadsBusinessTemplates(t *testing.T) {
	tests := []struct {
		name string
		msg  *waE2E.Message
		want string
	}{
		{
			"interactive OTP with a copy-code button",
			&waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{
				Body:   &waE2E.InteractiveMessage_Body{Text: proto.String("123456 is your verification code.")},
				Footer: &waE2E.InteractiveMessage_Footer{Text: proto.String("Expires in 10 minutes.")},
				InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{
					NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{
						Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{{
							Name:             proto.String("cta_copy"),
							ButtonParamsJSON: proto.String(`{"display_text":"Copy code","copy_code":"123456"}`),
						}},
					},
				},
			}},
			"123456 is your verification code.\nExpires in 10 minutes.\n[Copy code] 123456",
		},
		{
			"the same, inside the view-once envelope WhatsApp wraps it in",
			&waE2E.Message{ViewOnceMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{
				InteractiveMessage: &waE2E.InteractiveMessage{
					Body: &waE2E.InteractiveMessage_Body{Text: proto.String("Your code is 9911")},
				},
			}}},
			"Your code is 9911",
		},
		{
			"hydrated template with a link button",
			&waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{
				HydratedTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{
					HydratedContentText: proto.String("Your order has shipped."),
					HydratedFooterText:  proto.String("ACME"),
					HydratedButtons: []*waE2E.HydratedTemplateButton{{
						HydratedButton: &waE2E.HydratedTemplateButton_UrlButton{
							UrlButton: &waE2E.HydratedTemplateButton_HydratedURLButton{
								DisplayText: proto.String("Track"),
								URL:         proto.String("https://acme.example/t/1"),
							},
						},
					}},
				},
			}},
			"Your order has shipped.\nACME\n[Track] https://acme.example/t/1",
		},
		{
			"template whose format is the hydrated four-row oneof",
			&waE2E.Message{TemplateMessage: &waE2E.TemplateMessage{
				Format: &waE2E.TemplateMessage_HydratedFourRowTemplate_{
					HydratedFourRowTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{
						HydratedContentText: proto.String("Use 4821 to log in."),
					},
				},
			}},
			"Use 4821 to log in.",
		},
		{
			"highly structured message carrying a hydrated template",
			&waE2E.Message{HighlyStructuredMessage: &waE2E.HighlyStructuredMessage{
				HydratedHsm: &waE2E.TemplateMessage{
					HydratedTemplate: &waE2E.TemplateMessage_HydratedFourRowTemplate{
						HydratedContentText: proto.String("Code: 5550"),
					},
				},
			}},
			"Code: 5550",
		},
		{
			"buttons message",
			&waE2E.Message{ButtonsMessage: &waE2E.ButtonsMessage{
				ContentText: proto.String("Confirm your booking?"),
				Buttons: []*waE2E.ButtonsMessage_Button{{
					ButtonText: &waE2E.ButtonsMessage_Button_ButtonText{DisplayText: proto.String("Yes")},
				}},
			}},
			"Confirm your booking?\n[Yes]",
		},
		{
			"plain text still wins over everything",
			&waE2E.Message{Conversation: proto.String("hello")},
			"hello",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractText(tt.msg); got != tt.want {
				t.Errorf("extractText() = %q, want %q", got, tt.want)
			}
		})
	}
}

// The first message a participant sends to a group arrives in two parts under
// one id; the key-only part used to be posted as an unsupported "text" message
// right before the real one.
func TestIsKeyDistribution(t *testing.T) {
	skdm := &waE2E.SenderKeyDistributionMessage{
		GroupID:                             proto.String("120363000000000000@g.us"),
		AxolotlSenderKeyDistributionMessage: []byte{1, 2, 3},
	}
	tests := []struct {
		name string
		msg  *waE2E.Message
		want bool
	}{
		{"nil", nil, false},
		{"key only", &waE2E.Message{SenderKeyDistributionMessage: skdm}, true},
		{
			"key with nothing but context info beside it",
			&waE2E.Message{
				SenderKeyDistributionMessage: skdm,
				MessageContextInfo:           &waE2E.MessageContextInfo{MessageSecret: []byte{9}},
			},
			true,
		},
		{"fast-ratchet key only", &waE2E.Message{FastRatchetKeySenderKeyDistributionMessage: skdm}, true},
		{
			"key riding along with a real message is kept",
			&waE2E.Message{SenderKeyDistributionMessage: skdm, Conversation: proto.String("hi all")},
			false,
		},
		{"an ordinary message", &waE2E.Message{Conversation: proto.String("hi")}, false},
		{"a genuinely empty message stays a placeholder", &waE2E.Message{}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isKeyDistribution(tt.msg); got != tt.want {
				t.Errorf("isKeyDistribution() = %v, want %v", got, tt.want)
			}
		})
	}
	// The check must not eat the key off the message it inspects.
	msg := &waE2E.Message{SenderKeyDistributionMessage: skdm}
	isKeyDistribution(msg)
	if msg.GetSenderKeyDistributionMessage() == nil {
		t.Error("isKeyDistribution mutated its argument")
	}
}

func TestIsAnnotation(t *testing.T) {
	tests := []struct {
		name string
		msg  *waE2E.Message
		want bool
	}{
		{"nil", nil, false},
		{"poll vote", &waE2E.Message{PollUpdateMessage: &waE2E.PollUpdateMessage{}}, true},
		{"event rsvp", &waE2E.Message{EncEventResponseMessage: &waE2E.EncEventResponseMessage{}}, true},
		{"the poll itself", &waE2E.Message{PollCreationMessageV3: &waE2E.PollCreationMessage{}}, false},
		{"text", &waE2E.Message{Conversation: proto.String("hi")}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isAnnotation(tt.msg); got != tt.want {
				t.Errorf("isAnnotation() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPopulatedFields(t *testing.T) {
	got := populatedFields(&waE2E.Message{
		OrderMessage:       &waE2E.OrderMessage{},
		MessageContextInfo: &waE2E.MessageContextInfo{},
	})
	if got != "messageContextInfo,orderMessage" {
		t.Errorf("populatedFields() = %q", got)
	}
	if got := populatedFields(&waE2E.Message{}); got != "(empty)" {
		t.Errorf("populatedFields(empty) = %q", got)
	}
}

func TestExtractMediaTreatsAVideoNoteAsVideo(t *testing.T) {
	info, ok := extractMedia(&waE2E.Message{PtvMessage: &waE2E.VideoMessage{
		Mimetype: proto.String("video/mp4"), Seconds: proto.Uint32(7),
	}})
	if !ok || info.Kind != "video" || info.Seconds != 7 {
		t.Errorf("extractMedia() = %+v, %v", info, ok)
	}
}

func TestGroupRow(t *testing.T) {
	g := &types.GroupInfo{
		JID:           types.JID{User: "120363000000000000", Server: types.GroupServer},
		GroupName:     types.GroupName{Name: "Sales Team"},
		GroupAnnounce: types.GroupAnnounce{IsAnnounce: true},
		Participants:  []types.GroupParticipant{{}, {}, {}},
	}
	row := groupRow(g)
	if row["jid"] != "120363000000000000@g.us" || row["name"] != "Sales Team" ||
		row["participants"] != 3 || row["announce"] != true || row["community"] != false {
		t.Errorf("groupRow() = %+v", row)
	}
	// The listing can omit the member list and send only a count.
	g.Participants, g.ParticipantCount = nil, 42
	if got := groupRow(g)["participants"]; got != 42 {
		t.Errorf("participants = %v, want the reported count", got)
	}
}

func TestGroupNameCacheStoreSparesTheLookup(t *testing.T) {
	cache := &groupNames{entries: map[string]groupNameEntry{}}
	chat := types.JID{User: "120363000000000001", Server: types.GroupServer}
	cache.store("uid1", chat, "Ops")
	// A nil client would return "" if lookup had to ask WhatsApp.
	if got := cache.lookup("uid1", nil, chat); got != "Ops" {
		t.Errorf("lookup() = %q, want the stored subject", got)
	}
	if got := cache.lookup("uid2", nil, chat); got != "" {
		t.Errorf("another session was served this one's subject: %q", got)
	}
}
