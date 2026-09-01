import re

from odoo import models
from odoo.tools.rendering_tools import parse_inline_template

# WhatsApp's text markers. They are a client-side convention — the recipient's
# phone parses them out of the plain body — which is exactly why a *rendered
# value* carrying one is a problem: {{ object.name }} returning 'SO_2024_07'
# italicises '2024' on the customer's screen, and a '*' in a price or a '~' in
# an address does the same. The values most likely to carry stray markers are
# the ones derived from inbound WhatsApp text, which is already untrusted.
MARKUP_CHARS = "*_~`"
MARKUP_CHAR_RE = re.compile(f"[{re.escape(MARKUP_CHARS)}]")

# WhatsApp has no escape character, so a marker is neutralised by making it
# fail the parser's own adjacency rule instead: a marker only opens a span when
# a non-space follows it, and only closes one when a non-space precedes it. A
# zero-width space on both sides breaks it in both roles.
#
# Chosen over stripping the markers because it is the safer failure: U+200B is
# invisible and copy-pastes away, so the value still *reads* exactly right,
# whereas deleting characters would turn 'SO_2024_07' into 'SO202407' and
# corrupt the order reference the message exists to communicate. If it turns
# out a client treats U+200B as an ordinary character the style survives, which
# is cosmetic; a mangled reference is not.
ZERO_WIDTH_SPACE = "​"

# The name the escape helper is bound to in the rendering context. It is
# prefixed because it shares a namespace with the template author's own
# variables (object, user, company).
MARKUP_ESCAPE_FN = "_whatsmeow_escape_markup"


class WhatsmeowRenderMixin(models.AbstractModel):
    """Render a WhatsApp `body` against a record, escaping WhatsApp's markers
    in every interpolated value.

    Shared by `whatsmeow.template` (§11) and `whatsmeow.marketing.campaign`
    (marketing §5.2): both hold an author-written body with {{ object.field }}
    placeholders, and both must reach the recipient's phone showing the *value*
    rather than the formatting an unlucky value happens to spell. A second copy
    of that rule is a second copy to forget to fix — the same "define once"
    discipline as `whatsmeow.match.mixin` and CHAT_TYPES/MESSAGE_TYPES.

    An inheriting model provides `body` (Text) and a `_compute_render_model`,
    which is what `mail.render.mixin` renders against.
    """
    _name = "whatsmeow.render.mixin"
    _inherit = ["mail.render.mixin"]
    _description = "Whatsmeow Body Rendering"

    # Same call mail.template and sms.template make: writing a template or a
    # campaign is manager-only (see ir.model.access.csv), so the *record* is
    # trusted config and rendering it must not demand
    # `mail.group_mail_template_editor` from the operator who sends it. Without
    # this, a plain user sending a body that says {{ user.name }} gets an
    # AccessError from `_is_restricted`.
    _unrestricted_rendering = True

    def _render_body(self, res_ids):
        """{record id: rendered text}.

        `inline_template` is the engine behind {{ object.field }} — Odoo 19
        offers inline_template/qweb/qweb_view, and it is the one mail.template
        uses for its subject. It is sandboxed, so a template author cannot
        reach beyond the record.

        The mixin's own context already carries `user`; `company` is added
        beside it so a body can sign off with the sender's active company. It
        is the *sender's* company, not the record's: `add_context` is shared by
        the whole batch, and the operator is who the message comes from.
        """
        self.ensure_one()
        return self._render_template(
            self._body_escaping_values(), self.render_model, list(res_ids),
            engine="inline_template",
            add_context={
                "company": self.env.company,
                MARKUP_ESCAPE_FN: self._escape_markup,
            },
        )

    def _body_escaping_values(self):
        """`body` with every {{ expression }} wrapped in the markup escaper.

        The escaping has to happen *inside* the render, per value, because
        afterwards there is no telling which characters came from the author's
        literal text and which came from a field: a `*` the author typed is
        formatting they asked for, and a `*` arriving in a customer name is
        not. Rewriting the expressions is the only seam Odoo's inline_template
        offers between the two.

        This is the outgoing mirror of the `Markup(...) % value` discipline
        core already uses when posting inbound text to the chatter.

        The rewrite is render-time only and never stored: `body` keeps its
        plain `object.field` expressions, so `_has_unsafe_expression` still
        sees a simple template and an author without
        `mail.group_mail_template_editor` can still save it.
        """
        self.ensure_one()
        parts = []
        for literal, expression, default in parse_inline_template(self.body or ""):
            parts.append(literal)
            if expression:
                # `||| default` is the author's own literal, so it is left
                # alone — same trust as the surrounding text. render_inline_
                # template falls back to it when the value is falsy, and an
                # escaped empty string is still empty, so that still holds.
                #
                # The default is re-emitted flush against `}}` because the
                # parser's own `(.*?)\}\}` already captured whatever whitespace
                # preceded them; padding it here would add a space to the
                # rendered output on every round trip.
                call = f"{MARKUP_ESCAPE_FN}({expression})"
                parts.append(f"{{{{ {call} ||| {default}}}}}" if default
                             else f"{{{{ {call} }}}}")
        return "".join(parts)

    def _escape_markup(self, value):
        """Neutralise WhatsApp's text markers in one rendered value.

        Override this to strip the markers instead if a client turns out to
        parse through the zero-width space — see ZERO_WIDTH_SPACE.
        """
        if not value:
            return value
        return MARKUP_CHAR_RE.sub(
            lambda match: f"{ZERO_WIDTH_SPACE}{match.group(0)}{ZERO_WIDTH_SPACE}",
            str(value),
        )

    def _body_is_static(self):
        """True when the body renders byte-identical for every recipient.

        A blast of identical bodies is one of the clearest bulk-sender
        fingerprints (PLAN.md §12.6). Callers warn on it; nothing blocks it,
        because a short identical body is sometimes exactly right.
        """
        self.ensure_one()
        return not any(
            expression
            for _literal, expression, _default in parse_inline_template(self.body or "")
        )
