import logging
import re
from datetime import timedelta

from markupsafe import Markup

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# `{{ answer.email }}`, `{{ contact.name }}` — a dotted path into a plain dict,
# nothing more. Deliberately not a template engine: the bodies are written by
# whoever draws the flow, they end up on a stranger's phone, and there is no
# use case here that wants a loop or a filter. An unknown path renders empty.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\}\}")

# A chain of message/action steps can only be so long before it is a loop
# somebody drew by accident. The bot stops rather than sending a hundred
# WhatsApp messages to find out.
MAX_CHAIN = 50

STATES = [
    ("running", "Running"),
    ("done", "Completed"),
    ("handover", "With an operator"),
    ("expired", "Expired"),
    ("cancelled", "Stopped by the contact"),
]


def dig(context, path):
    """Walk a dotted path through nested dicts; missing is ''."""
    node = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return ""
        node = node[part]
    return "" if node is None else node


class WhatsmeowBotChat(models.Model):
    """One conversation a bot held with one contact.

    Every chat is a record from its first line to its last: the transcript, what
    it collected, which step it is on and how it ended. That is what makes a bot
    supportable — when a contact says "it asked me something odd", there is a
    row to open rather than a log to grep.
    """
    _name = "whatsmeow.bot.chat"
    _description = "Whatsmeow Chatbot Chat"
    _order = "last_activity desc, id desc"
    _rec_name = "name"

    bot_id = fields.Many2one(
        "whatsmeow.bot", required=True, ondelete="cascade", index=True,
    )
    session_id = fields.Many2one(
        "whatsmeow.session", string="WhatsApp Number", required=True,
        ondelete="cascade", index=True,
    )
    chat_jid = fields.Char(string="Chat JID", index=True)
    partner_id = fields.Many2one("res.partner", string="Contact", index=True)
    phone = fields.Char()
    push_name = fields.Char(string="WhatsApp Name")
    name = fields.Char(compute="_compute_name", store=True, string="Chat")

    state = fields.Selection(STATES, default="running", required=True, index=True)
    current_step_id = fields.Many2one(
        "whatsmeow.bot.step", string="Waiting At", ondelete="set null",
        help="The question the contact has not answered yet. Empty on a chat "
             "that is no longer running.",
    )
    retry_count = fields.Integer(
        string="Retries Used", default=0,
        help="How many times the current question has been re-asked.",
    )

    start_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    end_date = fields.Datetime(readonly=True)
    last_activity = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)

    line_ids = fields.One2many("whatsmeow.bot.chat.line", "chat_id", string="Transcript")
    value_ids = fields.One2many("whatsmeow.bot.chat.value", "chat_id", string="Collected")
    value_count = fields.Integer(compute="_compute_counts")
    line_count = fields.Integer(compute="_compute_counts")

    channel_id = fields.Many2one(
        "mail.channel", string="Discuss Conversation", ondelete="set null",
        help="Where the conversation went after a handover.",
    )
    last_message_id = fields.Many2one(
        "whatsmeow.message", string="Last Inbound", ondelete="set null",
        help="The message that last drove this chat. It is what a handover uses "
             "to find (or open) the Discuss conversation.",
    )
    summary = fields.Text(
        compute="_compute_summary",
        help="Everything the bot collected, as one readable block.",
    )

    # -- computes -------------------------------------------------------------
    @api.depends("partner_id", "push_name", "phone", "chat_jid", "bot_id")
    def _compute_name(self):
        for rec in self:
            who = (rec.partner_id.display_name or rec.push_name or rec.phone
                   or rec.chat_jid or _("Unknown"))
            rec.name = "%s / %s" % (who, rec.bot_id.name or "")

    @api.depends("value_ids", "line_ids")
    def _compute_counts(self):
        for rec in self:
            rec.value_count = len(rec.value_ids)
            rec.line_count = len(rec.line_ids)

    @api.depends("value_ids.label", "value_ids.value")
    def _compute_summary(self):
        for rec in self:
            rec.summary = "\n".join(
                "%s: %s" % (value.label or value.key, value.value or "")
                for value in rec.value_ids
            )

    # -- lookup ---------------------------------------------------------------
    @api.model
    def _find_running(self, session, chat_jid):
        """The chat this inbound message continues, if the bot is mid-workflow.

        Scoped to (number, conversation) — the same key the Discuss bridge uses
        for its channels, so a contact writing to two of your numbers is two
        conversations here as well.
        """
        chat = self.search([
            ("session_id", "=", session.id),
            ("chat_jid", "=", chat_jid or ""),
            ("state", "=", "running"),
        ], order="id desc", limit=1)
        if chat and chat._is_expired():
            chat._expire()
            return self.browse()
        return chat

    @api.model
    def _open(self, bot, message):
        """Start a chat for this bot on the message's conversation."""
        return self.create({
            "bot_id": bot.id,
            "session_id": message.session_id.id,
            "chat_jid": message.chat_jid or "",
            "partner_id": message.partner_id.id or False,
            "phone": message.phone or False,
            "push_name": message.push_name or False,
            "last_message_id": message.id,
        })

    def _is_expired(self):
        self.ensure_one()
        minutes = self.bot_id.timeout_minutes
        if not minutes or self.state != "running":
            return False
        deadline = (self.last_activity or self.start_date) + timedelta(minutes=minutes)
        return fields.Datetime.now() > deadline

    # -- the two entry points -------------------------------------------------
    def _handle_start(self, message):
        """First message of a conversation the bot picked up."""
        self.ensure_one()
        self._log_inbound(message)
        self._run_from(self.bot_id._first_step())

    def _handle_inbound(self, message):
        """A reply to whatever the bot last asked.

        The stop and restart words are read before the current question is, so
        they work from anywhere in the flow — which is the whole point of having
        them. Everything else is the step's business.
        """
        self.ensure_one()
        self.write({"last_message_id": message.id, "last_activity": fields.Datetime.now()})
        if not self.partner_id and message.partner_id:
            self.partner_id = message.partner_id
        self._log_inbound(message)

        text = (message.body or "").strip()
        bot = self.bot_id
        if bot._keyword_hit(text, "cancel_keywords"):
            self._say(bot.cancel_message)
            return self._finish("cancelled")
        if bot._keyword_hit(text, "restart_keywords"):
            return self._restart()
        if not self.current_step_id:
            # Nothing was asked, yet a reply arrived — the workflow ran off its
            # end without ending the chat, or a step was deleted underneath it.
            # Start again rather than sitting mute.
            return self._restart()
        return self._on_reply(text)

    def _restart(self):
        self.ensure_one()
        self.value_ids.unlink()
        self.write({"retry_count": 0, "current_step_id": False, "state": "running"})
        self._log("note", _("Workflow restarted."))
        self._run_from(self.bot_id._first_step())

    # -- the engine -----------------------------------------------------------
    def _run_from(self, step):
        """Run steps until one waits for the contact, or the flow ends.

        A greeting, a lookup and a question are one turn as far as the contact
        is concerned, so they are one turn here too — the loop only returns when
        there is genuinely nothing more to do without hearing back.
        """
        self.ensure_one()
        guard = 0
        while step and guard < MAX_CHAIN:
            guard += 1
            kind = step.step_type
            if kind in ("question", "choice"):
                return self._ask(step)
            if kind == "message":
                self._say(step.body, step)
                step._run_action(self)
            elif kind == "action":
                step._run_action(self)
            elif kind == "handover":
                return self._handover(step)
            elif kind == "end":
                if step.body:
                    self._say(step.body, step)
                step._run_action(self)
                return self._finish("done")
            step = step._following_step()
        if guard >= MAX_CHAIN:
            _logger.warning(
                "whatsmeow_bot: chat %s stopped after %s steps — the flow of bot "
                "%s probably loops", self.id, MAX_CHAIN, self.bot_id.id)
            self._log("note", _("Stopped: the workflow ran too long without "
                                "waiting for a reply."))
        return self._finish("done")

    def _ask(self, step):
        """Put a question on the wire and wait."""
        self.ensure_one()
        self._say(step._prompt(), step)
        self.write({
            "current_step_id": step.id,
            "retry_count": 0,
            "state": "running",
            "last_activity": fields.Datetime.now(),
        })

    def _on_reply(self, text):
        self.ensure_one()
        step = self.current_step_id
        understood, value, answer = step._parse_reply(text)
        if not understood:
            return self._on_misunderstood(step)

        self._store(step, value, answer)
        step._run_action(self)
        if answer:
            answer._run_action(self)
        # An option's own destination wins over the step's, because it is the
        # more specific statement of intent — that is what branching *is*.
        target = answer.next_step_id if answer and answer.next_step_id \
            else step._following_step()
        self.retry_count = 0
        return self._run_from(target)

    def _on_misunderstood(self, step):
        """A reply the step could not read.

        Re-ask, up to the bot's limit, then do whatever the bot says to do —
        which by default is to fetch a human, because a contact who has failed
        three times is a contact the workflow is failing.
        """
        self.ensure_one()
        bot = self.bot_id
        self.retry_count += 1
        if self.retry_count < bot.max_retries or bot.on_max_retry == "repeat":
            if bot.on_max_retry == "repeat":
                self.retry_count = 0
            self._say(step.retry_message or bot.invalid_message, step)
            # Re-send the options so the contact can see what they may pick,
            # instead of being told "no" with no menu in sight.
            if step.step_type == "choice":
                self._say(step._prompt(), step)
            return
        self._log("note", _("Gave up reading the answer after %s tries.",
                            self.retry_count))
        if bot.on_max_retry == "handover":
            return self._handover()
        if bot.on_max_retry == "skip":
            self._store(step, "", self.env["whatsmeow.bot.answer"])
            self.retry_count = 0
            return self._run_from(step._following_step())
        return self._finish("done")

    def _store(self, step, value, answer):
        """File an answer under the step's store key.

        Re-answering the same key overwrites it rather than piling up a second
        row: a restarted or corrected answer is the answer, and a server action
        reading `{{ answer.email }}` must not have to guess which one is current.
        """
        self.ensure_one()
        key = step.store_key or ("step_%s" % step.id)
        Value = self.env["whatsmeow.bot.chat.value"]
        existing = Value.search([("chat_id", "=", self.id), ("key", "=", key)], limit=1)
        vals = {
            "chat_id": self.id,
            "step_id": step.id,
            "key": key,
            "label": step.name or step.display_name,
            "value": value if value is not None else "",
            "answer_id": answer.id if answer else False,
        }
        if existing:
            existing.write(vals)
        else:
            Value.create(vals)
        # The cache still holds the old One2many, and `_render` for the very
        # next step reads it — so invalidate before anyone quotes the answer back.
        self.invalidate_recordset(["value_ids"])

    # -- talking --------------------------------------------------------------
    def _say(self, text, step=None):
        """Send a line from the bot, and keep a copy in the transcript.

        Sent inline rather than queued, like a Discuss operator's reply and for
        the same reason: this is an answer to someone who just wrote to us,
        which is the safest traffic there is and the traffic a delay makes
        useless. It is therefore not counted against the warm-up allowance.
        """
        self.ensure_one()
        body = self._render(text)
        if not body.strip():
            return self.env["whatsmeow.message"]
        session = self.session_id
        message = self.env["whatsmeow.message"].sudo().create({
            "session_id": session.id,
            "direction": "out",
            "message_type": "text",
            "body": body,
            "chat_jid": self.chat_jid or False,
            "phone": self.phone or False,
            "partner_id": self.partner_id.id or False,
            "bot_chat_id": self.id,
        })
        try:
            with self.env.cr.savepoint():
                # Queued context: a blocked recipient marks the row instead of
                # raising into the webhook that is handling the inbound message.
                message.with_context(whatsmeow_queued=True).action_send()
        except Exception as exc:  # noqa: BLE001 - a failed line must not kill the chat
            _logger.warning("whatsmeow_bot: chat %s could not send: %s", self.id, exc)
            message.write({"state": "error", "error_message": str(exc)})
        self._log("out", body, step=step, message=message)
        self.last_activity = fields.Datetime.now()
        return message

    def _log_inbound(self, message):
        self.ensure_one()
        body = message.body or ""
        if not body and message.message_type != "text":
            body = _("sent %s", message.message_type)
        self._log("in", body, step=self.current_step_id, message=message)

    def _log(self, direction, body, step=None, message=None):
        self.ensure_one()
        return self.env["whatsmeow.bot.chat.line"].sudo().create({
            "chat_id": self.id,
            "direction": direction,
            "body": body or "",
            "step_id": step.id if step else False,
            "message_id": message.id if message else False,
        })

    # -- placeholders ---------------------------------------------------------
    def _render_context(self):
        self.ensure_one()
        partner = self.partner_id
        answers = {value.key: value.value or "" for value in self.value_ids}
        return {
            "answer": answers,
            "answers": answers,
            "contact": {
                "name": partner.name or self.push_name or "",
                "phone": partner.phone or self.phone or "",
                "mobile": partner.mobile or "",
                "email": partner.email or "",
                "company": partner.parent_id.name or "",
            },
            "bot": {"name": self.bot_id.name or ""},
            "session": {"name": self.session_id.name or ""},
        }

    def _render(self, text):
        self.ensure_one()
        if not text:
            return ""
        context = self._render_context()
        return PLACEHOLDER_RE.sub(lambda m: str(dig(context, m.group(1))), text)

    # -- server actions -------------------------------------------------------
    def _run_server_action(self, action, step=None, answer=None):
        """Run a server action off the back of what the bot collected.

        The action is given whatever record it declares it works on — the chat
        itself, or the contact when it is a `res.partner` action, because "make
        an opportunity for this person" is the common case and having to browse
        from the chat to reach them is friction for no gain. Either way the
        collected values ride along in the context, so a code action can read
        them without knowing anything about this model.
        """
        self.ensure_one()
        if not action:
            return
        values = {value.key: value.value or "" for value in self.value_ids}
        model = action.sudo().model_id.model
        if model == "res.partner" and self.partner_id:
            active_model, record = "res.partner", self.partner_id
        else:
            active_model, record = self._name, self
        context = {
            "active_model": active_model,
            "active_id": record.id,
            "active_ids": record.ids,
            "whatsmeow_bot_chat_id": self.id,
            "whatsmeow_bot_values": values,
            "whatsmeow_bot_step_id": step.id if step else False,
            "whatsmeow_bot_answer_id": answer.id if answer else False,
        }
        try:
            with self.env.cr.savepoint():
                # As the superuser, the way `ir.cron` and automated actions run
                # theirs. The trigger is an inbound webhook, whose environment
                # has no real user behind it — so an action carrying a group
                # restriction would otherwise fail on whoever the webhook
                # happened to be, which says nothing about whether it should run.
                action.with_user(SUPERUSER_ID).with_context(**context).run()
            self._log("note", _("Ran server action: %s", action.name))
        except Exception as exc:  # noqa: BLE001 - a bad action must not end the chat
            _logger.exception("whatsmeow_bot: server action %s failed on chat %s",
                              action.id, self.id)
            self._log("note", _("Server action %(name)s failed: %(error)s",
                                name=action.name, error=exc))

    # -- handover -------------------------------------------------------------
    def _handover(self, step=None):
        """Put the conversation in front of a person and stand down.

        The operators come from the session's own Discuss routing rules — the
        ones already configured for "who attends this contact" — with the bot's
        (or the step's) list added on top. Reusing the rules is the point: a
        handover is not a new idea about who should answer, it is the existing
        one, arrived at from a different direction.
        """
        self.ensure_one()
        bot = self.bot_id
        message = self.last_message_id
        channel = self.env["mail.channel"]
        if message:
            try:
                channel = message.sudo()._wa_get_or_create_channel()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("whatsmeow_bot: chat %s could not open a Discuss "
                                "conversation: %s", self.id, exc)
        operators = (step.operator_ids if step else self.env["res.users"]) \
            or bot.handover_user_ids
        if channel and operators:
            extra = set(operators.partner_id.ids) - set(
                channel.channel_member_ids.partner_id.ids)
            if extra:
                channel.sudo().add_members(partner_ids=list(extra),
                                           post_joined_message=False)
        line = (step.body if step and step.body else bot.handover_message)
        if line:
            self._say(line, step)
        if channel and bot.handover_summary:
            self._post_briefing(channel)
        if channel and message:
            # The message that asked for a human has been consumed by the bot,
            # so it is not in the conversation yet. Post it last, so the
            # operator's most recent bubble is the contact's own words.
            try:
                message.sudo()._wa_post_into_channel(channel)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("whatsmeow_bot: could not post message %s into "
                                "channel %s: %s", message.id, channel.id, exc)
        self.channel_id = channel.id or False
        self._log("note", _("Handed over to an operator."))
        return self._finish("handover")

    def _post_briefing(self, channel):
        """Everything the bot collected, as a note in the conversation."""
        self.ensure_one()
        rows = Markup("").join(
            Markup("<li><b>%s:</b> %s</li>") % (value.label or value.key,
                                                value.value or "")
            for value in self.value_ids
        )
        body = Markup("<p><b>%s</b></p>") % _(
            "Handed over by the chatbot %s", self.bot_id.name)
        if rows:
            body += Markup("<ul>%s</ul>") % rows
        else:
            body += Markup("<p>%s</p>") % _("No answers were collected.")
        # A note, not a comment: `mail.channel._whatsmeow_relay` only relays
        # comments, so a briefing meant for the operator can never be sent to
        # the contact by accident.
        channel.sudo().with_context(whatsmeow_skip_send=True).message_post(
            body=body, message_type="notification", subtype_xmlid="mail.mt_note",
            author_id=self.bot_id.bot_partner_id.id or False,
        )

    # -- ending ---------------------------------------------------------------
    def _finish(self, state):
        self.ensure_one()
        self.write({
            "state": state,
            "current_step_id": False,
            "end_date": fields.Datetime.now(),
            "last_activity": fields.Datetime.now(),
        })

    def _expire(self):
        self.ensure_one()
        if self.bot_id.timeout_message:
            self._say(self.bot_id.timeout_message)
        self._log("note", _("Expired with no reply."))
        self._finish("expired")

    @api.model
    def cron_expire(self):
        """Close chats nobody came back to.

        A search on `last_activity` rather than a per-record check: the timeout
        varies by bot, so the query takes the widest bound and `_is_expired`
        settles each one. Cheap either way — a running chat is a rare row.
        """
        running = self.search([("state", "=", "running")], limit=1000)
        for chat in running:
            if not chat._is_expired():
                continue
            try:
                with self.env.cr.savepoint():
                    chat._expire()
            except Exception as exc:  # noqa: BLE001 - one bad chat must not kill the cron
                _logger.warning("whatsmeow_bot: could not expire chat %s: %s",
                                chat.id, exc)

    # -- UI actions -----------------------------------------------------------
    def action_release(self):
        """Give a handed-over conversation back to the bot.

        While a chat sits in *With an operator* the bot stays out of that
        conversation entirely — that hold is what stops it interrupting the
        colleague who took it on. This lifts it.
        """
        for rec in self.filtered(lambda c: c.state == "handover"):
            rec.write({"state": "done"})
            rec._log("note", _("Released — the chatbot may answer again."))

    def action_close(self):
        for rec in self.filtered(lambda c: c.state == "running"):
            rec._log("note", _("Closed by %s.", self.env.user.display_name))
            rec._finish("done")

    def action_open_channel(self):
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_("This chat was never handed over to an operator."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "mail.channel",
            "res_id": self.channel_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }
