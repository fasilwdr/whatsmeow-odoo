// Inbound content that is neither plain text nor a downloadable file: a
// location, a poll, an event, a contact card, and the structured "template"
// messages a business account sends (an OTP is the usual one).
//
// None of these has a body of its own, so each used to reach Odoo as an
// "[unsupported message type: ...]" stand-in. They are rendered here into the
// text a person would read off the phone, and the kind (plus, for a location,
// the coordinates) rides along so Odoo can type and filter the message instead
// of parsing that text back.
package main

import (
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"time"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// richContent is a message rendered from structure rather than read from a
// text field.
type richContent struct {
	Kind     string // location | poll | event | contact
	Text     string
	Location *locationInfo
}

// locationInfo is the part of a location Odoo stores as data. The text
// rendering carries the same thing for a reader; this is for a map.
type locationInfo struct {
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Name      string  `json:"name,omitempty"`
	Address   string  `json:"address,omitempty"`
	Live      bool    `json:"live,omitempty"`
}

func coordinate(v float64) string {
	return strconv.FormatFloat(v, 'f', 6, 64)
}

// mapsURL is what makes a location useful in a chatter: a pair of coordinates
// nobody can click is barely better than the placeholder it replaces.
func mapsURL(lat, lng float64) string {
	return "https://maps.google.com/?q=" + coordinate(lat) + "," + coordinate(lng)
}

// joinLines drops the empty ones, so an absent optional field does not leave a
// blank line in the middle of the rendering.
func joinLines(lines ...string) string {
	kept := lines[:0:0]
	for _, l := range lines {
		if l = strings.TrimSpace(l); l != "" {
			kept = append(kept, l)
		}
	}
	return strings.Join(kept, "\n")
}

// extractRich renders the structured kinds, or returns nil when the message is
// not one of them.
func extractRich(msg *waE2E.Message) *richContent {
	msg = unwrap(msg)
	if msg == nil {
		return nil
	}
	switch {
	case msg.GetLocationMessage() != nil:
		return locationContent(msg.GetLocationMessage())
	case msg.GetLiveLocationMessage() != nil:
		m := msg.GetLiveLocationMessage()
		lat, lng := m.GetDegreesLatitude(), m.GetDegreesLongitude()
		return &richContent{
			Kind:     "location",
			Text:     joinLines("📍 Live location", m.GetCaption(), mapsURL(lat, lng)),
			Location: &locationInfo{Latitude: lat, Longitude: lng, Live: true},
		}
	case pollOf(msg) != nil:
		return pollContent(pollOf(msg))
	case msg.GetEventMessage() != nil:
		return eventContent(msg.GetEventMessage())
	case msg.GetContactMessage() != nil:
		return &richContent{Kind: "contact", Text: contactText(msg.GetContactMessage())}
	case msg.GetContactsArrayMessage() != nil:
		cards := []string{}
		for _, c := range msg.GetContactsArrayMessage().GetContacts() {
			cards = append(cards, contactText(c))
		}
		if len(cards) == 0 {
			cards = append(cards, "👤 "+msg.GetContactsArrayMessage().GetDisplayName())
		}
		return &richContent{Kind: "contact", Text: strings.Join(cards, "\n\n")}
	}
	return nil
}

func locationContent(m *waE2E.LocationMessage) *richContent {
	lat, lng := m.GetDegreesLatitude(), m.GetDegreesLongitude()
	title := "📍 " + m.GetName()
	if m.GetName() == "" {
		title = "📍 Location"
		if m.GetIsLive() {
			title = "📍 Live location"
		}
	}
	return &richContent{
		Kind: "location",
		Text: joinLines(title, m.GetAddress(), m.GetComment(), mapsURL(lat, lng)),
		Location: &locationInfo{
			Latitude: lat, Longitude: lng,
			Name: m.GetName(), Address: m.GetAddress(), Live: m.GetIsLive(),
		},
	}
}

// pollOf finds a poll whichever of WhatsApp's versioned fields carries it. The
// versions differ in what a poll may do (multi-select, quiz, end time), not in
// the question and the options, which is all that is rendered here.
func pollOf(msg *waE2E.Message) *waE2E.PollCreationMessage {
	for _, p := range []*waE2E.PollCreationMessage{
		msg.GetPollCreationMessage(),
		msg.GetPollCreationMessageV2(),
		msg.GetPollCreationMessageV3(),
		msg.GetPollCreationMessageV5(),
		msg.GetPollCreationMessageV6(),
	} {
		if p != nil {
			return p
		}
	}
	return nil
}

func pollContent(p *waE2E.PollCreationMessage) *richContent {
	lines := []string{"📊 " + p.GetName()}
	for _, o := range p.GetOptions() {
		if name := strings.TrimSpace(o.GetOptionName()); name != "" {
			lines = append(lines, "• "+name)
		}
	}
	// 0 means "as many as you like"; 1 is the single-choice poll.
	if p.GetSelectableOptionsCount() == 1 {
		lines = append(lines, "(select one)")
	} else {
		lines = append(lines, "(select one or more)")
	}
	return &richContent{Kind: "poll", Text: joinLines(lines...)}
}

// eventTime renders an event's start or end. WhatsApp sends seconds; a value
// too large to be seconds this side of the year 33000 is milliseconds from a
// client that disagrees.
func eventTime(ts int64) string {
	if ts <= 0 {
		return ""
	}
	if ts > 1_000_000_000_000 {
		ts /= 1000
	}
	return time.Unix(ts, 0).UTC().Format("2006-01-02 15:04") + " UTC"
}

func labelled(label, value string) string {
	if strings.TrimSpace(value) == "" {
		return ""
	}
	return label + ": " + value
}

func eventContent(e *waE2E.EventMessage) *richContent {
	title := "📅 " + e.GetName()
	if e.GetIsCanceled() {
		title += " (cancelled)"
	}
	place := ""
	var loc *locationInfo
	if l := e.GetLocation(); l != nil {
		place = joinLines(l.GetName(), l.GetAddress())
		place = strings.ReplaceAll(place, "\n", ", ")
		lat, lng := l.GetDegreesLatitude(), l.GetDegreesLongitude()
		if lat != 0 || lng != 0 {
			// An event's venue may be a name with no pin; only a real pin is
			// worth a link, and (0, 0) is the Atlantic, not a venue.
			loc = &locationInfo{Latitude: lat, Longitude: lng,
				Name: l.GetName(), Address: l.GetAddress()}
			place = joinLines(place, mapsURL(lat, lng))
			place = strings.ReplaceAll(place, "\n", " ")
		}
	}
	return &richContent{
		Kind: "event",
		Text: joinLines(
			title,
			e.GetDescription(),
			labelled("Starts", eventTime(e.GetStartTime())),
			labelled("Ends", eventTime(e.GetEndTime())),
			labelled("Where", place),
			labelled("Join", e.GetJoinLink()),
		),
		Location: loc,
	}
}

// contactText renders one shared contact card: the name, then every number in
// the vCard. The numbers are the point — a card is shared so it can be dialled.
func contactText(c *waE2E.ContactMessage) string {
	lines := []string{"👤 " + c.GetDisplayName()}
	seen := map[string]bool{}
	for _, raw := range strings.Split(c.GetVcard(), "\n") {
		line := strings.TrimSpace(raw)
		// "TEL;type=CELL;waid=447700900123:+44 7700 900123" — the value is
		// whatever follows the last colon; item1.TEL is how iOS prefixes it.
		upper := strings.ToUpper(line)
		if !strings.HasPrefix(upper, "TEL") && !strings.Contains(upper, ".TEL") {
			continue
		}
		idx := strings.LastIndex(line, ":")
		if idx < 0 {
			continue
		}
		if number := strings.TrimSpace(line[idx+1:]); number != "" && !seen[number] {
			seen[number] = true
			lines = append(lines, number)
		}
	}
	return joinLines(lines...)
}

// ---------------------------------------------------------------------------
// Structured ("template") messages
//
// A business account does not send a plain Conversation: it sends a template —
// a title, a body, a footer and buttons — in one of several containers that
// accreted over the years. The server labels every one of them "text", which is
// why an OTP used to arrive as "[unsupported message type: text]": it is text,
// just not in either of the two fields extractText knew to look in.
// ---------------------------------------------------------------------------

// structuredText returns the readable content of a template-style message, or
// "" when the message is not one.
func structuredText(msg *waE2E.Message) string {
	switch {
	case msg.GetInteractiveMessage() != nil:
		return interactiveText(msg.GetInteractiveMessage())
	case msg.GetTemplateMessage() != nil:
		return templateText(msg.GetTemplateMessage(), 0)
	case msg.GetHighlyStructuredMessage() != nil:
		return hsmText(msg.GetHighlyStructuredMessage(), 0)
	case msg.GetButtonsMessage() != nil:
		b := msg.GetButtonsMessage()
		lines := []string{b.GetText(), b.GetContentText(), b.GetFooterText()}
		for _, btn := range b.GetButtons() {
			lines = append(lines, buttonLine(btn.GetButtonText().GetDisplayText(), ""))
		}
		return joinLines(lines...)
	case msg.GetListMessage() != nil:
		l := msg.GetListMessage()
		return joinLines(l.GetTitle(), l.GetDescription(), l.GetFooterText())
	case msg.GetTemplateButtonReplyMessage() != nil:
		return msg.GetTemplateButtonReplyMessage().GetSelectedDisplayText()
	case msg.GetInteractiveResponseMessage() != nil:
		return msg.GetInteractiveResponseMessage().GetBody().GetText()
	}
	return ""
}

// buttonLine renders a button as text: its label, and what pressing it would
// have given the reader — the link, or the code an OTP's "Copy code" copies.
func buttonLine(label, value string) string {
	label, value = strings.TrimSpace(label), strings.TrimSpace(value)
	switch {
	case label == "" && value == "":
		return ""
	case value == "":
		return "[" + label + "]"
	case label == "":
		return value
	}
	return "[" + label + "] " + value
}

func interactiveText(m *waE2E.InteractiveMessage) string {
	lines := []string{
		m.GetHeader().GetTitle(),
		m.GetHeader().GetSubtitle(),
		m.GetBody().GetText(),
		m.GetFooter().GetText(),
	}
	for _, btn := range m.GetNativeFlowMessage().GetButtons() {
		// The parameters are JSON whose keys depend on the button; these are
		// the ones that carry something a person would want to read.
		var params map[string]any
		_ = json.Unmarshal([]byte(btn.GetButtonParamsJSON()), &params)
		str := func(key string) string {
			s, _ := params[key].(string)
			return s
		}
		value := str("copy_code")
		if value == "" {
			value = str("url")
		}
		lines = append(lines, buttonLine(str("display_text"), value))
	}
	return joinLines(lines...)
}

// maxTemplateDepth bounds the template -> HSM -> template recursion, which a
// malformed message could otherwise make self-referential.
const maxTemplateDepth = 4

func templateText(t *waE2E.TemplateMessage, depth int) string {
	if t == nil || depth > maxTemplateDepth {
		return ""
	}
	hydrated := t.GetHydratedTemplate()
	if hydrated == nil {
		hydrated = t.GetHydratedFourRowTemplate()
	}
	if hydrated != nil {
		lines := []string{
			hydrated.GetHydratedTitleText(),
			hydrated.GetHydratedContentText(),
			hydrated.GetHydratedFooterText(),
		}
		for _, btn := range hydrated.GetHydratedButtons() {
			switch {
			case btn.GetUrlButton() != nil:
				lines = append(lines, buttonLine(
					btn.GetUrlButton().GetDisplayText(), btn.GetUrlButton().GetURL()))
			case btn.GetQuickReplyButton() != nil:
				lines = append(lines, buttonLine(btn.GetQuickReplyButton().GetDisplayText(), ""))
			case btn.GetCallButton() != nil:
				lines = append(lines, buttonLine(
					btn.GetCallButton().GetDisplayText(), btn.GetCallButton().GetPhoneNumber()))
			}
		}
		if text := joinLines(lines...); text != "" {
			return text
		}
	}
	if im := t.GetInteractiveMessageTemplate(); im != nil {
		if text := interactiveText(im); text != "" {
			return text
		}
	}
	if four := t.GetFourRowTemplate(); four != nil {
		return joinLines(
			hsmText(four.GetHighlyStructuredMessage(), depth+1),
			hsmText(four.GetContent(), depth+1),
			hsmText(four.GetFooter(), depth+1),
		)
	}
	return ""
}

func hsmText(h *waE2E.HighlyStructuredMessage, depth int) string {
	if h == nil || depth > maxTemplateDepth {
		return ""
	}
	return templateText(h.GetHydratedHsm(), depth+1)
}

// ---------------------------------------------------------------------------
// Traffic that is not a message
// ---------------------------------------------------------------------------

// isKeyDistribution says whether a message exists only to hand over a group's
// encryption key.
//
// The first thing a participant sends to a group after we join (or after their
// key rotates) arrives as two encrypted parts under one message id: the sender
// key, addressed to us alone, and the message itself. whatsmeow raises an event
// for each, and the key-only one has no body and no media — so it reached Odoo
// as "[unsupported message type: text]" a moment before the real text, and both
// were posted. It is bookkeeping, already consumed by whatsmeow before the
// event fires, and there is nothing in it to show anybody.
func isKeyDistribution(msg *waE2E.Message) bool {
	if msg == nil {
		return false
	}
	if msg.GetSenderKeyDistributionMessage() == nil &&
		msg.GetFastRatchetKeySenderKeyDistributionMessage() == nil {
		return false
	}
	// "Only" is the operative word: a key can also ride along with a real
	// message in the same part, and that one must not be dropped.
	rest := proto.Clone(msg).(*waE2E.Message)
	rest.SenderKeyDistributionMessage = nil
	rest.FastRatchetKeySenderKeyDistributionMessage = nil
	rest.MessageContextInfo = nil
	return proto.Size(rest) == 0
}

// isAnnotation says whether a message only acts on an earlier one rather than
// saying anything itself: a vote on a poll, an RSVP to an event. Both arrive
// encrypted against the original and carry no text; like a reaction with no
// surface to land on, they are dropped rather than logged as noise.
func isAnnotation(msg *waE2E.Message) bool {
	msg = unwrap(msg)
	if msg == nil {
		return false
	}
	return msg.GetPollUpdateMessage() != nil || msg.GetEncEventResponseMessage() != nil
}

// populatedFields names the top-level fields set on a message, for the log line
// written when one cannot be rendered. The placeholder Odoo shows can only say
// what the server called it ("text", "media"); this says what it actually was,
// which is what somebody adding support for it needs to know.
func populatedFields(msg *waE2E.Message) string {
	if msg == nil {
		return "(nil)"
	}
	names := []string{}
	msg.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		names = append(names, string(fd.Name()))
		return true
	})
	if len(names) == 0 {
		return "(empty)"
	}
	sort.Strings(names)
	return strings.Join(names, ",")
}
