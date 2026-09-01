import json

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class IrActionsServer(models.Model):
    """A server action that WhatsApps a template to the records it runs on.

    This is what lets an automation rule ("on SO confirmed, WhatsApp the
    customer") fire a template with no code — the same hook Enterprise's
    whatsapp module hangs off `ir.actions.server`.
    """
    _inherit = "ir.actions.server"

    state = fields.Selection(
        selection_add=[("whatsmeow_send", "Send WhatsApp")],
        ondelete={"whatsmeow_send": "cascade"},
    )
    whatsmeow_template_id = fields.Many2one(
        "whatsmeow.template", string="WhatsApp Template",
        domain="[('model_id', '=', model_id)]", ondelete="cascade",
    )

    @api.constrains("state", "model_id", "whatsmeow_template_id")
    def _check_whatsmeow_template(self):
        for action in self.filtered(lambda a: a.state == "whatsmeow_send"):
            if not action.whatsmeow_template_id:
                raise ValidationError(_(
                    "A 'Send WhatsApp' server action needs a template."
                ))
            if action.whatsmeow_template_id.model_id != action.model_id:
                raise ValidationError(_(
                    "Template '%(template)s' applies to %(template_model)s, but "
                    "this action runs on %(model)s.",
                    template=action.whatsmeow_template_id.name,
                    template_model=action.whatsmeow_template_id.model,
                    model=action.model_id.model,
                ))

    def _run_action_whatsmeow_send_multi(self, eval_context=None):
        """Compose and queue for every record the action was triggered on.

        Goes through the composer rather than creating messages directly, so
        an automated send renders, resolves and skips numberless records by
        exactly the same rules as a hand-launched one.
        """
        eval_context = eval_context or {}
        records = eval_context.get("records") or eval_context.get("record") \
            or self.env[self.model_name].browse(self.env.context.get("active_ids", []))
        if not records:
            return False
        template = self.whatsmeow_template_id
        vals = {
            "res_model": records._name,
            "res_ids": json.dumps(records.ids),
            "template_id": template.id,
        }
        # Leave session_id out when the template names none, so the composer's
        # compute can fall back instead of being handed a blank required value.
        if template.session_id:
            vals["session_id"] = template.session_id.id
        self.env["whatsmeow.composer"].create(vals).action_send()
        return False
