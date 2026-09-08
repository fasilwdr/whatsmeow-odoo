import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


def split_keywords(value):
    """Comma/newline-separated Char -> clean lowercase tokens.

    The same reduction `whatsmeow.match.mixin` applies to its own multi-value
    Chars, so a bot's keywords behave exactly like a filter rule's chat JIDs —
    one convention for every "type a few things in one field" in the suite.
    """
    return [tok.strip().lower() for tok in re.split(r"[,\n]", value or "") if tok.strip()]


class WhatsmeowBot(models.Model):
    """One chatbot: when it should speak, who it speaks as, and the workflow it
    walks a contact through.

    The trigger criteria are `whatsmeow.match.mixin` — the very same fields the
    inbound filter (§9) and the Discuss routes (§10) match on — so "when does
    the bot answer" is asked and answered in exactly the language an admin has
    already learnt twice. First matching bot wins, ordered by sequence, which is
    the third use of that same discipline.
    """
    _name = "whatsmeow.bot"
    _inherit = "whatsmeow.match.mixin"
    _description = "Whatsmeow Chatbot"
    _order = "sequence, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10, help="First matching bot wins.")

    session_ids = fields.Many2many(
        "whatsmeow.session", string="WhatsApp Numbers",
        relation="whatsmeow_bot_session_rel", column1="bot_id", column2="session_id",
        help="The numbers this bot answers on. Leave empty and it answers on "
             "every session that has chatbots enabled.",
    )
    bot_user_id = fields.Many2one(
        "res.users", string="Answers as", required=True,
        default=lambda self: self.env.ref("base.user_root", raise_if_not_found=False),
        help="The user the bot's replies are attributed to inside Odoo. The "
             "module ships a dedicated (archived) 'WhatsApp Bot' user; OdooBot "
             "works just as well. It changes nothing on WhatsApp's side — the "
             "contact always sees the number the session is paired to.",
    )
    bot_partner_id = fields.Many2one(
        "res.partner", compute="_compute_bot_partner_id",
        string="Bot Contact", store=False,
    )

    step_ids = fields.One2many("whatsmeow.bot.step", "bot_id", string="Steps", copy=True)
    step_count = fields.Integer(compute="_compute_step_count")
    chat_count = fields.Integer(compute="_compute_chat_count")

    # -- conversation control -------------------------------------------------
    timeout_minutes = fields.Integer(
        string="Give Up After (min)", default=120,
        help="A chat with no reply for this long is closed as expired, so the "
             "contact starts fresh next time instead of answering a question "
             "they have long forgotten. 0 never expires.",
    )
    max_retries = fields.Integer(
        string="Retries", default=3, required=True,
        help="How many times a question is re-asked after an answer it cannot "
             "read, before the bot gives up on it.",
    )
    on_max_retry = fields.Selection(
        [
            ("repeat", "Keep asking"),
            ("skip", "Move on to the next step"),
            ("handover", "Hand over to a human"),
            ("end", "End the conversation"),
        ],
        string="When Retries Run Out", default="handover", required=True,
    )
    invalid_message = fields.Text(
        string="Not Understood",
        default="Sorry, I didn't quite get that. Could you try again?",
        help="Sent when a reply does not fit the question. A step can override "
             "it with its own wording.",
    )

    restart_keywords = fields.Char(
        string="Restart Words", default="menu, restart",
        help="A reply matching one of these starts the workflow over, whatever "
             "step the contact was on.",
    )
    cancel_keywords = fields.Char(
        string="Stop Words", default="cancel, exit, quit",
        help="A reply matching one of these ends the chat. This is not an "
             "opt-out — that lives on the session's inbound rules and blocks "
             "every future send; this just closes the current conversation.",
    )
    cancel_message = fields.Text(
        string="On Stop",
        default="No problem — I have closed this. Message me any time.",
    )
    timeout_message = fields.Text(
        string="On Timeout",
        help="Optional line sent when a chat expires. Empty sends nothing, "
             "which is usually the polite choice.",
    )

    # -- handover -------------------------------------------------------------
    handover_user_ids = fields.Many2many(
        "res.users", string="Hand Over To",
        relation="whatsmeow_bot_handover_user_rel",
        help="Operators added to the Discuss conversation on a handover, on top "
             "of whoever the session's own routing rules pick. Leave empty to "
             "let the routing rules decide alone.",
    )
    handover_message = fields.Text(
        string="Handover Line",
        default="One moment — I am passing you to a colleague.",
    )
    handover_summary = fields.Boolean(
        string="Brief the Operator", default=True,
        help="Post everything the bot collected into the Discuss conversation "
             "as a note, so whoever picks it up starts informed.",
    )

    # -- politeness -----------------------------------------------------------
    skip_if_attended = fields.Boolean(
        string="Never Interrupt a Human", default=True,
        help="Do not start a workflow on a conversation an operator is already "
             "attending in Discuss. A bot talking over a colleague is worse "
             "than a bot that stays quiet.",
    )
    deliver_to_inbox = fields.Boolean(
        string="Also Deliver to Discuss", default=False,
        help="Keep posting the contact's messages to Discuss (or the chatter) "
             "while the bot is answering. Off keeps the noise out of the "
             "operators' inbox until the bot is done — the chat is on file "
             "either way, under WhatsApp / Chatbot Chats.",
    )

    chat_ids = fields.One2many("whatsmeow.bot.chat", "bot_id", string="Chats")

    # -- computes -------------------------------------------------------------
    @api.depends("bot_user_id")
    def _compute_bot_partner_id(self):
        for rec in self:
            rec.bot_partner_id = rec.bot_user_id.sudo().partner_id

    @api.depends("step_ids")
    def _compute_step_count(self):
        for rec in self:
            rec.step_count = len(rec.step_ids)

    def _compute_chat_count(self):
        # Non-stored and no depends: recomputed on read, which is all a smart
        # button needs. A grouped count avoids one query per bot.
        groups = self.env["whatsmeow.bot.chat"].sudo().read_group(
            [("bot_id", "in", self.ids)], fields=["bot_id"], groupby=["bot_id"])
        counts = {g["bot_id"][0]: g["bot_id_count"] for g in groups if g["bot_id"]}
        for rec in self:
            rec.chat_count = counts.get(rec.id, 0)

    @api.constrains("max_retries", "timeout_minutes")
    def _check_limits(self):
        for rec in self:
            if rec.max_retries < 1:
                raise ValidationError(_("A question must be asked at least once."))
            if rec.timeout_minutes < 0:
                raise ValidationError(_("The timeout cannot be negative."))

    # -- flow -----------------------------------------------------------------
    def _sorted_steps(self):
        self.ensure_one()
        return self.step_ids.sorted(key=lambda s: (s.sequence, s.id))

    def _first_step(self):
        """Where a new chat begins: the topmost step in the designer."""
        self.ensure_one()
        steps = self._sorted_steps()
        return steps[0] if steps else self.env["whatsmeow.bot.step"]

    # -- matching -------------------------------------------------------------
    @api.model
    def _match(self, message):
        """The bot that should open a workflow for this inbound message, if any.

        Ordered by sequence, first match wins — the criteria are evaluated in
        pure Python over prefetched fields, exactly as `_inbound_decision` and
        `_route_users` do, so adding bots costs a comparison each and no query.
        """
        session = message.session_id
        if not session.bot_enabled:
            return self.browse()
        facts = message._wa_route_facts()
        bots = self.search([
            "|", ("session_ids", "=", False), ("session_ids", "in", session.ids),
        ])
        for bot in bots.sorted(key=lambda b: (b.sequence, b.id)):
            if not bot.step_ids:
                continue  # a bot with no workflow has nothing to say
            if bot._matches(facts):
                return bot
        return self.browse()

    def _is_blocked(self, message):
        """True when this bot must stay quiet on the message's conversation.

        Two reasons, both about not talking over people: an operator is already
        attending the conversation in Discuss, or a previous chat was handed
        over to a human and nobody has released it back to the bot yet.
        """
        self.ensure_one()
        chat_jid = message.chat_jid or ""
        held = self.env["whatsmeow.bot.chat"].sudo().search_count([
            ("session_id", "=", message.session_id.id),
            ("chat_jid", "=", chat_jid),
            ("state", "=", "handover"),
        ])
        if held:
            return True
        if not self.skip_if_attended:
            return False
        channel = self.env["mail.channel"].sudo().search([
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", message.session_id.id),
            ("whatsmeow_chat_jid", "=", chat_jid),
        ], limit=1)
        # The correspondent is the message *author*, never a member, so any
        # member at all is a human who has been given this conversation.
        return bool(channel and channel.channel_member_ids)

    # -- keywords -------------------------------------------------------------
    def _keyword_hit(self, text, field):
        self.ensure_one()
        body = (text or "").strip().lower()
        return bool(body) and body in split_keywords(self[field])

    # -- UI actions -----------------------------------------------------------
    def action_open_builder(self):
        """Open the drag-and-drop flow designer for this bot."""
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "whatsmeow_bot.flow_builder",
            "name": _("Flow: %s", self.name),
            "params": {"bot_id": self.id},
            "context": {"active_id": self.id, "active_model": self._name},
        }

    def action_view_chats(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Chats"),
            "res_model": "whatsmeow.bot.chat",
            "view_mode": "tree,form",
            "domain": [("bot_id", "=", self.id)],
            "context": {"create": False},
        }

    # -- flow designer ---------------------------------------------------------
    # The designer draws the workflow and moves steps around; it does not edit
    # them. Every field a step has is edited in the ordinary Odoo form, opened
    # in a dialog straight from the canvas — so the designer never has to
    # reimplement a many2one dropdown, a tags widget or a help tooltip, and
    # every rule the model already enforces still applies. What is left here is
    # a read model and a reorder, which is all a map needs.
    @api.model
    def builder_data(self, bot_id):
        """Everything the canvas draws — and everything its inline editor needs.

        Both raw values and rendered ones: the card shows an action's *name*,
        the editor edits its *id*, and sending both saves the editor a second
        round trip every time somebody clicks a step.
        """
        bot = self.browse(int(bot_id))
        bot.check_access_rights("read")
        bot.check_access_rule("read")
        Step = self.env["whatsmeow.bot.step"]
        inputs = dict(Step._fields["input_type"].selection)
        steps = []
        for step in bot._sorted_steps():
            steps.append({
                "id": step.id,
                # Sent so a new step can be appended past the last one, whatever
                # the sequences happen to be: they are only normalised by a drag.
                "sequence": step.sequence,
                "title": step.display_name,
                "name": step.name or "",
                "step_type": step.step_type,
                "body": step.body or "",
                "input_type": step.input_type,
                "input_label": inputs.get(step.input_type, ""),
                "store_key": step.store_key or "",
                "next_step_id": step.next_step_id.id or False,
                "next_step_title": step.next_step_id.display_name or "",
                "server_action_ids": step.server_action_ids.ids,
                "action_names": step.server_action_ids.mapped("name"),
                "operator_ids": step.operator_ids.ids,
                "operator_names": step.operator_ids.mapped("name"),
                "answers": [{
                    "id": answer.id,
                    "name": answer.name or "",
                    "keywords": answer.keywords or "",
                    "value": answer.value or "",
                    "next_step_id": answer.next_step_id.id or False,
                    "next_step_title": answer.next_step_id.display_name or "",
                    "server_action_ids": answer.server_action_ids.ids,
                    "action_names": answer.server_action_ids.mapped("name"),
                } for answer in step._sorted_answers()],
            })
        # Not sudo: `ir.actions.server` and `res.users` are readable by every
        # internal user, and the editor should offer exactly what the person
        # drawing the flow is allowed to see.
        actions = self.env["ir.actions.server"].search_read(
            [("usage", "=", "ir_actions_server")], ["id", "name"],
            order="name", limit=500)
        users = self.env["res.users"].search_read(
            [("share", "=", False)], ["id", "name"], order="name", limit=500)
        return {
            "bot": {
                "id": bot.id,
                "name": bot.name,
                "sessions": bot.session_ids.mapped("name"),
            },
            "steps": steps,
            "options": {
                "step_types": [
                    {"value": key, "label": label}
                    for key, label in Step._fields["step_type"].selection
                ],
                "input_types": [
                    {"value": key, "label": label}
                    for key, label in Step._fields["input_type"].selection
                ],
                "server_actions": actions,
                "users": users,
            },
            "readonly": not self.env["whatsmeow.bot"].check_access_rights(
                "write", raise_exception=False),
        }

    @api.model
    def builder_reorder(self, bot_id, step_ids):
        """Renumber the workflow after a drag.

        Position *is* the flow — a step with no explicit link continues into
        whatever is drawn under it — so this is the one edit the canvas makes
        directly. It writes sequences and nothing else.
        """
        bot = self.browse(int(bot_id))
        bot.check_access_rights("write")
        bot.check_access_rule("write")
        steps = self.env["whatsmeow.bot.step"].browse([int(sid) for sid in step_ids])
        if steps and steps.bot_id != bot:
            raise UserError(_("Those steps do not all belong to this chatbot."))
        for index, step in enumerate(steps):
            step.sequence = (index + 1) * 10
        return self.builder_data(bot.id)
