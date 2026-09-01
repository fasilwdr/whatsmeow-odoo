from ast import literal_eval

from odoo import api, fields, models
from odoo.exceptions import ValidationError

# The models a campaign may be aimed at in this version. Kept as a check rather
# than a Selection so the field stays a real ir.model reference: widening it is
# then a matter of teaching the recipient resolver about a new model, not of
# rewriting the schema (PLAN_WHATSAPP_MARKETING.md §4.2).
SUPPORTED_MODELS = ("res.partner", "whatsmeow.broadcast.contact")


class WhatsmeowMarketingFilter(models.Model):
    """A saved domain — a "Dynamic List".

    Quite like `ir.filters`, and deliberately not it: an `ir.filters` record is
    per-user and bound to an action, so it cannot be the shared, reusable
    audience definition a campaign needs. `mailing.filter` exists for the same
    reason, and this is its shape.
    """
    _name = "whatsmeow.marketing.filter"
    _description = "WhatsApp Dynamic List"
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True)
    create_uid = fields.Many2one(
        "res.users", string="Saved by", index=True, readonly=True,
        default=lambda self: self.env.user,
    )
    model_id = fields.Many2one(
        "ir.model", string="Recipients Model", required=True, ondelete="cascade",
        default=lambda self: self.env["ir.model"]._get("res.partner"),
        domain=[("model", "in", list(SUPPORTED_MODELS))],
    )
    model = fields.Char(related="model_id.model", string="Model Name", store=True)
    mailing_domain = fields.Char(string="Filter", required=True, default="[]")
    recipient_count = fields.Integer(compute="_compute_recipient_count")

    _name_model_uniq = models.Constraint(
        "UNIQUE (name, model_id)",
        "A dynamic list with this name already exists for that model.",
    )

    @api.constrains("model_id", "mailing_domain")
    def _check_domain(self):
        """Run the domain now, so a broken filter surfaces while editing rather
        than at 2 a.m. when a campaign silently reaches nobody."""
        for rec in self:
            if rec.model not in SUPPORTED_MODELS:
                raise ValidationError(self.env._(
                    "A dynamic list can only target %s for now.",
                    ", ".join(SUPPORTED_MODELS),
                ))
            try:
                self.env[rec.model].search_count(literal_eval(rec.mailing_domain))
            except Exception as exc:  # noqa: BLE001 - any bad domain is the same answer
                raise ValidationError(self.env._(
                    "This filter is not a valid domain for %(model)s: %(error)s",
                    model=rec.model, error=exc,
                )) from exc

    def _compute_recipient_count(self):
        for rec in self:
            try:
                rec.recipient_count = self.env[rec.model].search_count(
                    literal_eval(rec.mailing_domain or "[]"))
            except Exception:  # noqa: BLE001 - a half-typed domain must not break the form
                rec.recipient_count = 0
