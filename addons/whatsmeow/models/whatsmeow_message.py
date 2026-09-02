import base64
import logging
import mimetypes
import re
import time
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models, modules
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

from .whatsmeow_connection import MEDIA_TIMEOUT
from .whatsmeow_markup import render_markup

_logger = logging.getLogger(__name__)
DIGITS = re.compile(r"\D")

# Odoo 19's res.partner has no `mobile` field (it was merged into `phone`), but
# localisation/OCA modules may add one back. Probe the model instead of assuming.
PHONE_FIELDS = ("phone", "mobile")

# A JID's server says what kind of chat it is. Mirrors the constants in
# whatsmeow's types/jid.go.
CHAT_TYPE_BY_SERVER = {
    "s.whatsapp.net": "private",
    "lid": "private",  # a contact WhatsApp only identifies by LID
    "g.us": "group",
    "broadcast": "broadcast",
    "newsletter": "newsletter",
}
STATUS_JID = "status@broadcast"

# The canonical selections a message can carry, factored out so the inbound
# filter rules (whatsmeow.session.rule) match on exactly the same sets and can
# never drift from what a message actually is — the same discipline as
# SESSION_CODE_RE <-> sessionNameRe. chat_type is the rendering of
# CHAT_TYPE_BY_SERVER; message_type is the media-kind set the gateway emits.
CHAT_TYPES = [
    ("private", "Private"),
    ("group", "Group"),
    ("broadcast", "Broadcast"),
    ("status", "Status Update"),
    ("newsletter", "Channel"),
    ("unknown", "Unknown"),
]
MESSAGE_TYPES = [
    ("text", "Text"),
    ("image", "Image"),
    ("video", "Video"),
    ("audio", "Audio"),
    ("document", "Document"),
    ("sticker", "Sticker"),
]


def chat_type_for_jid(chat_jid):
    """Derive a chat type from a chat JID's server, with no ORM in sight.

    Shared by the message's stored `chat_type` compute and the inbound filter,
    which needs the same answer before any record exists.
    """
    jid = (chat_jid or "").strip().lower()
    if not jid:
        # No JID means the message is addressed by phone number, which can
        # only ever be a private chat.
        return "private"
    if jid == STATUS_JID:
        return "status"
    server = jid.rpartition("@")[2]
    return CHAT_TYPE_BY_SERVER.get(server, "unknown")

# Chat kinds the gateway can actually send to; the rest have their own send
# paths in WhatsApp and are receive-only here.
REPLYABLE_CHAT_TYPES = ("private", "group")

# The queue cron runs every minute and paces its sends, so it must hand the
# worker back before the next run is due. The rest of the minute is slack for
# a slow final send.
CRON_BUDGET_SECONDS = 50
# Ceiling on how many messages one run will even consider. With a 3-10s pace a
# run places far fewer than this; the limit just stops a huge backlog from
# being read into memory at once.
CRON_MAX_MESSAGES = 200

# How long a registration answer is trusted. It must agree with the gateway's
# WMG_CHECK_TTL_DAYS (30): past it the gateway asks WhatsApp again anyway, so
# Odoo treating the old answer as final would block sends the gateway is
# perfectly willing to re-examine.
REGISTRATION_TTL_DAYS = 30
# How many numbers one validation pass looks at. The gateway rations lookups
# per hour; this just bounds the work in a single cron run.
CHECK_CRON_LIMIT = 200


class WhatsmeowMessage(models.Model):
    _name = "whatsmeow.message"
    _description = "Whatsmeow Message"
    _order = "create_date desc, id desc"
    _rec_name = "body"

    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
    )
    connection_id = fields.Many2one(
        related="session_id.connection_id", store=True, index=True,
    )
    partner_id = fields.Many2one("res.partner", index=True)
    phone = fields.Char(
        help="Digits with country code, e.g. 447700900123. Empty on an inbound "
             "message whose sender is only known by LID.",
    )
    sender_lid = fields.Char(
        string="Sender LID", readonly=True, index=True,
        help="WhatsApp's privacy-preserving id for the sender. WhatsApp does not "
             "always reveal the phone number behind it, so this may be the only "
             "identity we have.",
    )
    sender_jid = fields.Char(
        string="Sender JID", readonly=True,
        help="Full address of whoever wrote the message. In a group this is the "
             "participant, not the group; quoting a message needs it.",
    )
    chat_jid = fields.Char(
        string="Chat JID", index=True,
        help="The conversation this message belongs to: the group's JID for a "
             "group, the contact's own JID for a private chat. This is what a "
             "reply is addressed to — a phone number can only reach a private chat.",
    )
    chat_type = fields.Selection(
        CHAT_TYPES,
        compute="_compute_chat_type", store=True, index=True,
        help="Derived from the chat JID's server.",
    )
    chat_name = fields.Char(
        string="Group Name", readonly=True,
        help="Subject of the group, as fetched by the gateway. Empty for private chats.",
    )
    push_name = fields.Char(
        string="Sender Name", readonly=True,
        help="The display name the sender set in WhatsApp. Kept as a fallback "
             "label when the sender resolves to no partner — e.g. to name an "
             "auto-created contact for a Discuss conversation.",
    )
    reply_to_id = fields.Many2one(
        "whatsmeow.message", string="In Reply To", ondelete="set null", index=True,
        help="Quote this message in WhatsApp. In a group, it is what tells "
             "everyone which message is being answered.",
    )
    can_reply = fields.Boolean(compute="_compute_can_reply")
    direction = fields.Selection(
        [("out", "Outgoing"), ("in", "Incoming")], required=True, default="out",
    )
    body = fields.Text(string="Message / Caption")
    message_type = fields.Selection(
        MESSAGE_TYPES, default="text", required=True,
    )
    # attachment=True keeps the bytes in the filestore instead of the database.
    media_data = fields.Binary(string="Media", attachment=True)
    media_filename = fields.Char(string="Filename")
    media_mimetype = fields.Char(string="MIME Type")
    media_size = fields.Integer(string="Size (bytes)", readonly=True)
    media_state = fields.Selection(
        [
            ("none", "No Media"),
            ("pending", "Waiting for Download"),
            ("fetched", "Downloaded"),
            ("error", "Download Failed"),
        ],
        default="none", required=True, readonly=True,
        help="Inbound media is downloaded from the gateway by a scheduled action, "
             "so a large file cannot stall the webhook.",
    )
    is_voice_note = fields.Boolean(
        readonly=True, help="Audio recorded in WhatsApp rather than an attached file.",
    )
    media_duration = fields.Integer(
        string="Duration (s)", readonly=True, help="For audio and video.",
    )
    wa_message_id = fields.Char(readonly=True, index=True)
    sent_date = fields.Datetime(
        readonly=True, copy=False, index=True,
        help="When the gateway accepted this message. The daily volume cap "
             "counts these, not write_date — a delivery receipt landing today "
             "must not make yesterday's message count against today.",
    )
    is_placeholder = fields.Boolean(
        readonly=True,
        help="The gateway had nothing to render for this message, so the body "
             "is a stand-in. WhatsApp sometimes delivers a message twice — once "
             "empty, once for real — and the real copy replaces this one when "
             "it lands.",
    )
    state = fields.Selection(
        [
            ("outgoing", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("read", "Read"),
            ("received", "Received"),
            ("error", "Error"),
        ],
        default="outgoing", required=True,
    )
    error_message = fields.Char(readonly=True)

    # The gateway retries a webhook it thinks failed, and those retries can
    # land at the same moment. A search-then-create cannot dedupe that: both
    # requests look, both find nothing, both insert. Only the database can
    # settle it. Outgoing messages are excluded because they have no id until
    # the gateway answers, so they would all collide on NULL... which Postgres
    # permits, but the index would still be meaningless for them.
    _inbound_wa_id_uniq = models.UniqueIndex(
        "(wa_message_id) WHERE direction = 'in' AND wa_message_id IS NOT NULL",
    )

    @api.depends("chat_jid")
    def _compute_chat_type(self):
        for rec in self:
            rec.chat_type = chat_type_for_jid(rec.chat_jid)

    @api.depends("direction", "chat_type", "chat_jid", "phone")
    def _compute_can_reply(self):
        for rec in self:
            rec.can_reply = bool(
                rec.direction == "in"
                and rec.chat_type in REPLYABLE_CHAT_TYPES
                and (rec.chat_jid or rec.phone)
            )

    @api.constrains("direction", "phone", "chat_jid")
    def _check_target_for_outgoing(self):
        # Inbound may legitimately have neither (a LID-only sender has no phone);
        # outbound cannot be sent anywhere without an address of some kind.
        for rec in self:
            if rec.direction == "out" and not (rec.phone or "").strip() \
                    and not (rec.chat_jid or "").strip():
                raise ValidationError(self.env._(
                    "A phone number or a chat is required to send a WhatsApp message."
                ))

    @api.constrains("direction", "chat_type")
    def _check_chat_is_sendable(self):
        for rec in self:
            if rec.direction == "out" and rec.chat_type not in REPLYABLE_CHAT_TYPES:
                raise ValidationError(self.env._(
                    "WhatsApp messages can only be sent to a private or group chat, "
                    "not to a %s.", rec.chat_type,
                ))

    @api.constrains("direction", "message_type", "body", "media_data")
    def _check_content_for_outgoing(self):
        # Inbound records are created by the webhook before the media is fetched,
        # so only outgoing messages are required to be complete.
        for rec in self.filtered(lambda r: r.direction == "out"):
            if rec.message_type == "text" and not (rec.body or "").strip():
                raise ValidationError(self.env._("A text message needs a body."))
            if rec.message_type != "text" and not rec.media_data:
                raise ValidationError(self.env._(
                    "A %s message needs a file attached.", rec.message_type,
                ))

    @api.onchange("media_filename")
    def _onchange_media_filename(self):
        """Guess the type from the file the user picked, so they rarely have to
        set it by hand."""
        if not self.media_filename:
            return
        mimetype = mimetypes.guess_type(self.media_filename)[0] or ""
        self.media_mimetype = mimetype
        self.message_type = self._kind_for_mimetype(mimetype)

    @api.model
    def _kind_for_mimetype(self, mimetype):
        """Mirrors kindFor() in the gateway: webp is how WhatsApp does stickers."""
        mimetype = (mimetype or "").lower()
        if mimetype.startswith("image/webp"):
            return "sticker"
        if mimetype.startswith("image/"):
            return "image"
        if mimetype.startswith("video/"):
            return "video"
        if mimetype.startswith("audio/"):
            return "audio"
        return "document"

    @api.model
    def _find_partner(self, phone):
        """Match a partner on the last 10 digits of their number.

        A plain `('phone', 'like', tail)` domain cannot do this: stored numbers
        are formatted ('+44 7700 900123'), so LIKE '%7700900123%' never matches.
        Strip non-digits in SQL and compare the tails instead.
        """
        digits = DIGITS.sub("", phone or "")
        if not digits:
            return self.env["res.partner"]
        tail = digits[-10:]
        partner = self.env["res.partner"]
        columns = [f for f in PHONE_FIELDS if f in partner._fields]
        if not columns:
            return partner

        condition = SQL(" OR ").join(
            SQL(
                "regexp_replace(COALESCE(%s, ''), '\\D', '', 'g') LIKE %s",
                SQL.identifier("res_partner", col), f"%{tail}",
            )
            for col in columns
        )
        self.env.cr.execute(SQL(
            "SELECT id FROM res_partner WHERE active AND (%s) ORDER BY id LIMIT 1",
            condition,
        ))
        row = self.env.cr.fetchone()
        return partner.browse(row[0]) if row else partner

    def _target_payload(self):
        """Where the gateway should send this. A JID addresses the chat itself,
        which is the only way to reach a group or a LID-only contact; the phone
        number stays as the fallback for messages composed by hand."""
        self.ensure_one()
        payload = {"phone": self.phone or ""}
        if self.chat_jid:
            payload["jid"] = self.chat_jid
        return payload

    def _mark_read(self):
        """Send WhatsApp's read receipt for this inbound message.

        Called once, the moment the inbound filter accepts it — so the tick
        means "this reached the inbox", not "a human read it". That gap is why
        it is opt-in per session (`auto_mark_read`), and why a rejected message
        never gets one: blue-ticking something nobody will ever see is a lie.

        Inline like a reaction, and never raising: a courtesy receipt must not
        fail the webhook that is storing the message.
        """
        self.ensure_one()
        if not self.session_id.auto_mark_read:
            return
        if self.direction != "in" or not self.wa_message_id:
            return
        payload = {
            **self._target_payload(),
            # Only a group receipt names the author; in a 1:1 the chat is the
            # sender, and the gateway leaves the field out.
            "sender": (self.sender_jid or "") if self.chat_type == "group" else "",
            "message_ids": [self.wa_message_id],
        }
        try:
            self.session_id._gw(
                "POST", f"/sessions/{self.session_id.code}/read", payload)
        except Exception as exc:  # noqa: BLE001 - a failed receipt must not raise
            _logger.warning("whatsmeow: read receipt for message %s failed: %s",
                            self.id, exc)

    def _quote_payload(self):
        """Ask the gateway to quote the message this one replies to."""
        self.ensure_one()
        source = self.reply_to_id
        if not source or not source.wa_message_id:
            return {}
        payload = {"quoted_id": source.wa_message_id}
        if source.sender_jid:
            payload["quoted_participant"] = source.sender_jid
        # WhatsApp renders the quote from the copy we send it. Media has no body
        # of its own, so fall back to the filename rather than quoting nothing.
        text = (source.body or "").strip() or (source.media_filename or "")
        if text:
            payload["quoted_text"] = text
        return payload

    def _idempotency_key(self):
        """A key that is stable across every send attempt of this message.

        Handing the gateway the same message twice is not a harmless retry —
        the recipient sees it twice. It happens because the send and the
        record live in one transaction: if that transaction rolls back after
        the gateway POST (a crash, a serialisation failure, a wedged worker),
        the message is already on its way to WhatsApp but the record still
        reads 'outgoing', so the queue sends it again. The gateway replays the
        first result for a key it has already seen.

        The database name and record id are enough: the id is assigned at
        create and committed with the record, so it survives the rollback that
        causes the resend in the first place. That also means no extra column,
        and no backfill for messages that already exist.
        """
        self.ensure_one()
        return f"{self.env.cr.dbname}:{self._name}:{self.id}"

    def _send_payload(self):
        """Build the gateway call for this message: text and media use
        different endpoints and payloads."""
        self.ensure_one()
        code = self.session_id.code
        common = {
            **self._target_payload(),
            **self._quote_payload(),
            "idempotency_key": self._idempotency_key(),
            # Hold the "typing…" indicator on the recipient's phone for a
            # moment first, the way a real client does. The gateway sleeps for
            # this, so the session clamps it.
            "typing_ms": self.session_id._typing_ms(self.body or ""),
        }
        if self.message_type == "text":
            return f"/sessions/{code}/send", {**common, "message": self.body or ""}
        return f"/sessions/{code}/send-media", {
            **common,
            "caption": self.body or "",
            "filename": self.media_filename or "",
            "mimetype": self.media_mimetype or "",
            "kind": self.message_type,
            "ptt": self.is_voice_note,
            # media_data is already base64 in Odoo; decode/re-encode would be waste.
            "data": (self.media_data or b"").decode(),
        }

    def action_reply(self):
        """Open a new outgoing message addressed back to this one's chat."""
        self.ensure_one()
        if not self.can_reply:
            raise UserError(self.env._(
                "This message cannot be replied to: there is no chat to answer in."
            ))
        is_group = self.chat_type == "group"
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Reply"),
            "res_model": self._name,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {
                "default_session_id": self.session_id.id,
                "default_direction": "out",
                "default_message_type": "text",
                "default_reply_to_id": self.id,
                "default_chat_jid": self.chat_jid or False,
                # A group reply belongs to the group, not to the participant who
                # happened to write: addressing it to them would send a private
                # message instead, and pin the log on the wrong contact.
                "default_phone": "" if is_group else (self.phone or ""),
                "default_partner_id": False if is_group else self.partner_id.id,
            },
        }

    # -- opt-out (PLAN.md §12.3) ----------------------------------------------
    # Unlike the volume caps, this gate is absolute and belongs on the message
    # rather than in the queue: every path that sends — the form button, the
    # template composer, a server action, the Discuss relay — goes through
    # `action_send`, so checking here is the one place no future module can
    # route around.
    def _optout_partner(self):
        """The contact whose opt-out flag governs this message, if any.

        A group has no single recipient, so an individual's opt-out cannot
        speak for it; only private chats are gated. When the message carries a
        bare number the partner is resolved the same way the inbound path
        resolves one, so opting out from WhatsApp also blocks a send composed
        against a hand-typed number.
        """
        self.ensure_one()
        if self.direction != "out" or self.chat_type == "group":
            return self.env["res.partner"]
        if self.partner_id:
            return self.partner_id
        return self._find_partner(self.phone) if self.phone else \
            self.env["res.partner"]

    def _optout_block_reason(self):
        """Why this message must not be sent, or False when it may be.

        Two different gates, both about who we are messaging: a contact who
        asked us to stop, and a number that is not on WhatsApp at all. The
        second is a data-quality guard rather than a wish, so it expires — an
        answer older than `REGISTRATION_TTL_DAYS` is treated as the open
        question it has become, not as a permanent no.
        """
        self.ensure_one()
        partner = self._optout_partner().sudo()
        if not partner:
            return False
        if partner.whatsmeow_optout:
            return self.env._(
                "%s has opted out of WhatsApp messages.", partner.display_name)
        if partner.whatsmeow_registered == "no" and partner.whatsmeow_registered_date \
                and partner.whatsmeow_registered_date >= fields.Datetime.now() - \
                timedelta(days=REGISTRATION_TTL_DAYS):
            return self.env._(
                "%(name)s's number is not on WhatsApp (checked %(date)s).",
                name=partner.display_name,
                date=fields.Date.to_string(partner.whatsmeow_registered_date.date()),
            )
        return False

    def action_send(self):
        sendable = self.filtered(
            lambda r: r.direction == "out" and r.state in ("outgoing", "error")
        )
        # Check every recipient before sending any of them, so a raise cannot
        # roll back a message that already left for WhatsApp.
        blocked = self.env[self._name]
        reasons = []
        for rec in sendable:
            reason = rec._optout_block_reason()
            if reason:
                blocked |= rec
                reasons.append(reason)
                rec.write({"state": "error", "error_message": reason})
        if blocked and not self.env.context.get("whatsmeow_queued"):
            # An interactive sender is told to their face; the queue only marks
            # the row, because there is nobody there to tell.
            raise UserError("\n".join(dict.fromkeys(reasons)))

        for rec in sendable - blocked:
            # Building the payload is our own work: a failure here is a bad
            # row, and must not count against the number's health.
            try:
                path, payload = rec._send_payload()
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the queue
                rec.write({"state": "error", "error_message": str(exc)})
                _logger.warning("whatsmeow.message %s could not be built: %s",
                                rec.id, exc)
                continue
            try:
                # Uploading a file takes longer than posting a line of text.
                timeout = None if rec.message_type == "text" else MEDIA_TIMEOUT
                data = rec.session_id._gw("POST", path, payload, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - one bad send must not stop the queue
                rec.write({"state": "error", "error_message": str(exc)})
                # A refusal from the gateway is the only signal Odoo gets that
                # WhatsApp may be pushing back on this number; a run of them
                # pauses its queue.
                rec.session_id.sudo()._note_send_failure(str(exc))
                _logger.warning("whatsmeow.message %s send failed: %s", rec.id, exc)
                continue
            rec.write({
                "state": "sent",
                "sent_date": fields.Datetime.now(),
                "wa_message_id": data.get("wa_message_id"),
                "error_message": False,
            })
            rec.session_id.sudo()._note_send()

    @api.model
    def cron_process_outgoing(self):
        """Drain the outgoing queue, pacing each session's sends apart.

        Firing a batch back to back is what gets a number banned, so each
        session may only send once its own window has opened (see
        `whatsmeow.session._schedule_next_send`). Sessions take turns, so a
        backlog on one number does not stall the others, and the run gives the
        cron worker back within its interval — whatever is left waits for the
        next run, which is exactly the throttle doing its job.
        """
        deadline = time.monotonic() + CRON_BUDGET_SECONDS
        messages = self.search(
            [("direction", "=", "out"), ("state", "=", "outgoing")],
            limit=CRON_MAX_MESSAGES, order="create_date asc",
        )
        # Oldest first within each session; sessions round-robin between sends.
        queues = {}
        for msg in messages:
            queues.setdefault(msg.session_id, []).append(msg)

        while queues and time.monotonic() < deadline:
            # `None` means "not today": this number has spent its warm-up
            # allowance, so drop it from the run rather than sleeping on a
            # window that will not open before midnight. Its messages stay
            # `outgoing` and tomorrow's cron drains them.
            waits = {}
            for session in list(queues):
                wait = session._seconds_until_sendable()
                if wait is None:
                    del queues[session]
                else:
                    waits[session] = wait
            if not queues:
                break
            due = [s for s, wait in waits.items() if not wait]
            if not due:
                wait = min(waits.values())
                if time.monotonic() + wait >= deadline:
                    break  # the next window opens after this run; leave it queued
                time.sleep(wait)
                continue
            for session in due:
                msg = queues[session].pop(0)
                if not queues[session]:
                    del queues[session]
                try:
                    # action_send() records its own failures on the record, so
                    # the savepoint is for anything else unforeseen: one bad
                    # send must not poison the whole run's transaction.
                    with self.env.cr.savepoint():
                        # Queued: a blocked recipient marks the row and the run
                        # carries on; nobody is watching to be told.
                        msg.with_context(whatsmeow_queued=True).action_send()
                        session._schedule_next_send()
                except Exception as exc:  # noqa: BLE001 - one bad send must not stop the queue
                    _logger.warning(
                        "whatsmeow.message %s queue send failed: %s", msg.id, exc)
                    continue
                self._commit_progress()

    def _commit_progress(self):
        """Make each send durable before pacing on to the next.

        A paced run lasts most of a minute, and its sends are independent: a
        crash near the end must not roll back messages that have already left
        for WhatsApp, which would send them twice. Tests run inside one
        rolled-back transaction and forbid a real commit.
        """
        if not modules.module.current_test:
            self.env.cr.commit()

    @api.model
    def cron_check_numbers(self):
        """Validate the numbers a queued batch is about to be sent to.

        Lazy and ahead of the queue, never in the webhook path: the point is to
        find the dead numbers in a large batch *before* sending into them, and
        an inbound message is not the moment to be making outbound queries.

        Only numbers that resolve to a contact are checked, because the answer
        has nowhere else to live — a one-off message typed against a bare
        number is a human act, and a human act does not need a list validated.
        """
        messages = self.search(
            [("direction", "=", "out"), ("state", "=", "outgoing")],
            limit=CRON_MAX_MESSAGES, order="create_date asc",
        )
        stale = fields.Datetime.now() - timedelta(days=REGISTRATION_TTL_DAYS)
        by_session = {}
        for msg in messages:
            partner = msg._optout_partner().sudo()
            if not partner or partner.whatsmeow_optout:
                continue
            if partner.whatsmeow_registered and (
                    not partner.whatsmeow_registered_date
                    or partner.whatsmeow_registered_date >= stale):
                continue  # a recent answer; nothing to ask
            phone = (msg.phone or "").strip() or (partner.phone or "")
            digits = DIGITS.sub("", phone)
            if digits:
                by_session.setdefault(msg.session_id, {}).setdefault(digits, partner)

        for session, wanted in by_session.items():
            try:
                answers = session._check_numbers(list(wanted)[:CHECK_CRON_LIMIT])
            except Exception as exc:  # noqa: BLE001 - a check is best-effort
                _logger.warning("whatsmeow: number check on session %s failed: %s",
                                session.code, exc)
                continue
            now = fields.Datetime.now()
            for digits, registered in answers.items():
                partner = wanted.get(digits)
                if not partner:
                    continue
                partner.sudo().write({
                    "whatsmeow_registered": "yes" if registered else "no",
                    "whatsmeow_registered_date": now,
                })
            self._commit_progress()

    # -- inbound media --------------------------------------------------------
    def action_fetch_media(self):
        """Pull the bytes the gateway downloaded for us, then let it drop them.

        Deliberately not done inline in the webhook: a large file would hold the
        webhook (and its transaction) open past the gateway's timeout, which
        would just make it retry.
        """
        for rec in self.filtered(lambda r: r.media_state == "pending"):
            code = rec.session_id.code
            try:
                content, headers = rec.session_id.connection_id._request_raw(
                    "GET", f"/sessions/{code}/media/{rec.wa_message_id}")
            except Exception as exc:  # noqa: BLE001 - keep the batch moving
                rec.write({"media_state": "error", "error_message": str(exc)})
                _logger.warning("whatsmeow.message %s media fetch failed: %s", rec.id, exc)
                continue

            rec.write({
                "media_data": base64.b64encode(content),
                "media_size": len(content),
                "media_state": "fetched",
                "error_message": False,
            })
            rec._deliver_inbound()
            # Best-effort: the gateway garbage-collects anything we miss.
            try:
                rec.session_id._gw("DELETE", f"/sessions/{code}/media/{rec.wa_message_id}")
            except Exception as exc:  # noqa: BLE001
                _logger.info("whatsmeow: could not release media %s: %s", rec.wa_message_id, exc)

    def _apply_reaction(self, emoji, partner):
        """Surface an inbound reaction on this message. A seam, like
        `_deliver_inbound`: core has no live surface for a reaction (a chatter
        post is not an updatable bubble), so it does nothing; `whatsmeow_discuss`
        overrides this to react on the message's Discuss bubble. `emoji` is empty
        when the sender removed their reaction."""
        self.ensure_one()
        return

    def _deliver_inbound(self):
        """Deliver an accepted inbound message to wherever it should land.

        A seam, deliberately overridable: core posts to the correspondent's
        chatter (`_post_to_chatter`). The `whatsmeow_discuss` bridge overrides
        this to route the message into a `discuss.channel` when its session opts
        in, and falls back to `super()` otherwise — so no bridge symbol is ever
        referenced from core; installing the bridge is what changes behaviour.
        """
        self.ensure_one()
        self._post_to_chatter()

    def _post_to_chatter(self):
        """Post an inbound message onto the partner's chatter, with the media
        attached when there is any."""
        self.ensure_one()
        if not self.partner_id:
            return
        attachments = self.env["ir.attachment"]
        if self.media_data:
            attachments = self.env["ir.attachment"].create({
                "name": self.media_filename or "whatsapp-media",
                "datas": self.media_data,
                "mimetype": self.media_mimetype or "application/octet-stream",
                "res_model": self._name,
                "res_id": self.id,
            })
            if self.is_voice_note:
                # Flag a voice note so it plays inline (Discuss/chatter render a
                # player) instead of showing as a download-only file. The metadata
                # record is what Odoo's own voice recorder sets.
                attachments._set_voice_metadata()
        # Name the group: posted on a partner's chatter, a group message would
        # otherwise read as though they had messaged us privately.
        if self.chat_type == "group":
            label = self.env._(
                "WhatsApp group %(group)s (%(session)s)",
                group=self.chat_name or self.chat_jid,
                session=self.session_id.name,
            )
        else:
            label = self.env._("WhatsApp (%s)", self.session_id.name)
        body = self.body or ""
        if not body and self.message_type != "text":
            body = self.env._("sent %s", self.message_type)
        self.partner_id.message_post(
            # Markup(...) % args escapes the args: inbound text is untrusted.
            # The body arrives as Markup from `render_markup`, which escaped it
            # itself before turning WhatsApp's markers into real formatting —
            # so the reader sees the message as the sender's phone drew it
            # rather than a line of `*asterisks*`.
            body=Markup("<p><b>%s</b><br/>%s</p>") % (label, render_markup(body)),
            # Typed, not just labelled: the web client badges a 'whatsmeow'
            # message with the WhatsApp mark and tints its bubble, so an
            # operator scanning a busy chatter can tell at a glance which
            # entries came over WhatsApp. The subtype is still mt_comment, so
            # followers are notified exactly as before.
            message_type="whatsmeow",
            subtype_xmlid="mail.mt_comment",
            attachment_ids=attachments.ids,
        )

    @api.model
    def cron_fetch_media(self):
        for rec in self.search([("media_state", "=", "pending")],
                               limit=20, order="create_date asc"):
            try:
                rec.action_fetch_media()
                rec.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the cron
                rec.env.cr.rollback()
                _logger.warning("whatsmeow.message %s media cron failed: %s", rec.id, exc)
