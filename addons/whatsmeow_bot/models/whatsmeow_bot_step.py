import logging
import re
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .whatsmeow_bot import split_keywords

_logger = logging.getLogger(__name__)

# A store key becomes `{{ answer.<key> }}` in later messages and a dict key in
# the server-action context, so it is restricted to what reads unambiguously in
# both places.
STORE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
DIGITS = re.compile(r"\D")

# The kinds of step a workflow is built from. Deliberately few: every extra
# type is a branch in the engine and a card the person drawing the flow has to
# understand, and these six cover "say something, ask something, do something,
# fetch a human, stop".
STEP_TYPES = [
    ("message", "Send a message"),
    ("question", "Ask for an answer"),
    ("choice", "Ask with options"),
    ("action", "Run a server action"),
    ("handover", "Hand over to a human"),
    ("end", "End the conversation"),
]

INPUT_TYPES = [
    ("text", "Any text"),
    ("number", "Number"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("date", "Date"),
]

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y")

# Steps that stop and wait for the contact to write back. Everything else runs
# straight through, which is what lets `_run_from` chain a greeting, a server
# action and a question into one turn.
WAITING_TYPES = ("question", "choice")


class WhatsmeowBotStep(models.Model):
    """One node of a chatbot workflow.

    Order is the flow: a step runs, then the next one by sequence runs, unless
    something says otherwise — an explicit *Then go to*, or, on a choice, the
    option the contact picked. Sequence is what the designer drags; the m2os are
    what it draws as branch arrows.
    """
    _name = "whatsmeow.bot.step"
    _description = "Whatsmeow Chatbot Step"
    _order = "sequence, id"

    bot_id = fields.Many2one(
        "whatsmeow.bot", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string="Label",
        help="What this step is called in the designer and in the branch "
             "dropdowns. Purely for whoever maintains the flow — the contact "
             "never sees it.",
    )
    step_type = fields.Selection(STEP_TYPES, default="message", required=True)
    body = fields.Text(
        string="Message",
        help="What the bot sends. WhatsApp formatting works (*bold*, _italic_, "
             "~strike~, ```mono```), and {{ answer.<key> }} / {{ contact.name }} "
             "are replaced with what the chat has collected so far.",
    )

    # -- question specifics ---------------------------------------------------
    input_type = fields.Selection(
        INPUT_TYPES, default="text", required=True, string="Expecting",
        help="What a valid reply looks like. Anything else is re-asked.",
    )
    store_key = fields.Char(
        string="Store As",
        help="Name the answer is filed under: lowercase letters, digits and "
             "underscores. Reuse it later as {{ answer.your_key }}, and find it "
             "in a server action under the chat's collected values. Empty stores "
             "the answer against the step itself.",
    )
    retry_message = fields.Text(
        string="If Not Understood",
        help="Overrides the bot's own wording for this question only.",
    )
    answer_ids = fields.One2many(
        "whatsmeow.bot.answer", "step_id", string="Options", copy=True,
    )

    # -- links ----------------------------------------------------------------
    next_step_id = fields.Many2one(
        "whatsmeow.bot.step", string="Then Go To", ondelete="set null",
        help="Where the flow continues. Empty follows the designer's order — "
             "the step drawn underneath this one.",
    )
    server_action_ids = fields.Many2many(
        "ir.actions.server", string="Run Actions",
        relation="whatsmeow_bot_step_action_rel",
        column1="step_id", column2="action_id",
        help="Fired once this step is done — for a question, once the answer "
             "has been stored, so an action can read it. Each runs on the chat "
             "record, with everything collected so far in its context. They run "
             "in the order shown; when the order between two of them really "
             "matters, one 'Multiple actions' server action states it explicitly.",
    )
    operator_ids = fields.Many2many(
        "res.users", string="Hand Over To",
        relation="whatsmeow_bot_step_user_rel",
        help="Operators for this handover specifically, on top of the session's "
             "routing rules. Empty falls back to the bot's own list.",
    )

    # -- computed helpers -----------------------------------------------------
    answer_count = fields.Integer(compute="_compute_answer_count")
    is_waiting = fields.Boolean(compute="_compute_is_waiting")

    @api.depends("answer_ids")
    def _compute_answer_count(self):
        for rec in self:
            rec.answer_count = len(rec.answer_ids)

    @api.depends("step_type")
    def _compute_is_waiting(self):
        for rec in self:
            rec.is_waiting = rec.step_type in WAITING_TYPES

    @api.constrains("store_key")
    def _check_store_key(self):
        for rec in self:
            if rec.store_key and not STORE_KEY_RE.match(rec.store_key):
                raise ValidationError(_(
                    "'%s' cannot be a store key: start with a lowercase letter "
                    "and use only lowercase letters, digits and underscores.",
                    rec.store_key,
                ))

    @api.constrains("next_step_id", "bot_id")
    def _check_next_step_bot(self):
        for rec in self:
            if rec.next_step_id and rec.next_step_id.bot_id != rec.bot_id:
                raise ValidationError(_(
                    "A step can only continue into a step of the same chatbot."
                ))

    @api.onchange("step_type")
    def _onchange_step_type(self):
        """Keep a step from carrying settings its kind cannot use — a stray
        store key on a plain message is the sort of thing that reads like a bug
        for months before anyone works out it never did anything."""
        if self.step_type not in WAITING_TYPES:
            self.store_key = False
            self.retry_message = False
        if self.step_type != "choice":
            self.answer_ids = [(5, 0, 0)]
        if self.step_type != "handover":
            self.operator_ids = [(5, 0, 0)]

    @api.returns("self", lambda value: value.id)
    def copy(self, default=None):
        """A duplicated step keeps everything except the store key.

        Two steps filing their answers under one key is legal — the second
        simply overwrites the first — but it is never what someone pressing
        Duplicate meant, and it fails silently, which is the worst way for a
        workflow to be wrong.
        """
        self.ensure_one()
        default = dict(default or {})
        default.setdefault("store_key", False)
        if self.name:
            default.setdefault("name", _("%s (copy)", self.name))
        return super().copy(default)

    def name_get(self):
        """Steps are picked from dropdowns while drawing branches, so an
        unnamed one still has to be recognisable. Falls back to the opening
        words of what it says, then to its kind."""
        labels = dict(self._fields["step_type"].selection)
        result = []
        for rec in self:
            name = (rec.name or "").strip()
            if not name:
                body = " ".join((rec.body or "").split())
                name = (body[:37] + "...") if len(body) > 40 else body
            result.append((rec.id, name or labels.get(rec.step_type, _("Step"))))
        return result

    def _sorted_answers(self):
        self.ensure_one()
        return self.answer_ids.sorted(key=lambda a: (a.sequence, a.id))

    # -- flow -----------------------------------------------------------------
    def _following_step(self):
        """The step after this one when nothing branches: the explicit link, or
        the next one down in the designer."""
        self.ensure_one()
        if self.next_step_id:
            return self.next_step_id
        steps = self.bot_id._sorted_steps()
        for index, step in enumerate(steps):
            if step == self:
                return steps[index + 1] if index + 1 < len(steps) else self.browse()
        return self.browse()

    def _prompt(self):
        """The text this step puts on the wire, options and all.

        A choice numbers its options into the message body rather than using
        WhatsApp's interactive list: an unofficial-protocol session cannot send
        those, and a numbered list is understood by every client, forwards
        cleanly, and can be answered with '2' or with the option's own words.

        Placeholders are left alone here — `whatsmeow.bot.chat._say` renders
        every line the bot sends, and rendering twice would let a contact's own
        answer be read as a placeholder on the second pass.
        """
        self.ensure_one()
        lines = []
        body = (self.body or "").strip()
        if body:
            lines.append(body)
        if self.step_type == "choice":
            options = self._sorted_answers()
            if options:
                if lines:
                    lines.append("")
                lines.extend(
                    "%d. %s" % (index, answer.name)
                    for index, answer in enumerate(options, start=1)
                )
        return "\n".join(lines)

    # -- reading a reply ------------------------------------------------------
    def _parse_reply(self, text):
        """Read the contact's reply against what this step asked for.

        Returns `(understood, stored_value, answer)`. `answer` is set only on a
        choice, and is what decides the branch. A reply is never rejected for
        being over-helpful — 'my email is a@b.com' is still an email — but it is
        rejected for being the wrong kind of thing, because storing '???' as a
        phone number and firing a server action on it is worse than asking again.
        """
        self.ensure_one()
        raw = (text or "").strip()
        if not raw:
            return False, None, self.env["whatsmeow.bot.answer"]

        if self.step_type == "choice":
            answer = self._match_answer(raw)
            if not answer:
                return False, None, self.env["whatsmeow.bot.answer"]
            return True, answer.value or answer.name, answer

        empty = self.env["whatsmeow.bot.answer"]
        if self.input_type == "number":
            match = re.search(r"-?\d+(?:[.,]\d+)?", raw)
            if not match:
                return False, None, empty
            return True, match.group(0).replace(",", "."), empty
        if self.input_type == "email":
            for token in raw.split():
                token = token.strip(".,;:<>()[]")
                if EMAIL_RE.match(token):
                    return True, token.lower(), empty
            return False, None, empty
        if self.input_type == "phone":
            digits = DIGITS.sub("", raw)
            # Short enough to be a house number or a typo is not a phone number.
            if len(digits) < 7:
                return False, None, empty
            return True, raw, empty
        if self.input_type == "date":
            parsed = self._parse_date(raw)
            if not parsed:
                return False, None, empty
            return True, parsed, empty
        return True, raw, empty

    def _match_answer(self, raw):
        """Which option the contact picked: its number, its own words, or one of
        its keywords. Checked in that order because the number is what the
        message asked for, and the loosest match must never beat an exact one."""
        self.ensure_one()
        options = self._sorted_answers()
        if not options:
            return self.env["whatsmeow.bot.answer"]
        stripped = raw.strip().strip(".)")
        if stripped.isdigit():
            index = int(stripped)
            if 1 <= index <= len(options):
                return options[index - 1]
        lowered = raw.strip().lower()
        for answer in options:
            if (answer.name or "").strip().lower() == lowered:
                return answer
        for answer in options:
            if lowered in split_keywords(answer.keywords):
                return answer
        return self.env["whatsmeow.bot.answer"]

    @api.model
    def _parse_date(self, raw):
        """A date the contact typed, as an ISO string, or None.

        Tried against a short list of formats rather than a fuzzy parser: a
        fuzzy parser will happily read '5' as a date, and a bot that silently
        stores today's date because someone typed a house number is worse than
        one that asks again.
        """
        token = raw.strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    # -- server actions -------------------------------------------------------
    def _run_action(self, chat):
        """Fire this step's server actions, in order.

        Each is run in its own savepoint by `_run_server_action`, so one that
        raises is recorded on the chat and the rest still run — the alternative
        is a workflow where a broken action quietly cancels the good one next
        to it.
        """
        self.ensure_one()
        for action in self.server_action_ids:
            chat._run_server_action(action, step=self)
