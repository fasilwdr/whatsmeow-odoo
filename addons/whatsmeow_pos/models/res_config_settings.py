from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pos_whatsmeow_enabled = fields.Boolean(
        related="pos_config_id.whatsmeow_enabled", readonly=False)
    pos_whatsmeow_session_id = fields.Many2one(
        related="pos_config_id.whatsmeow_session_id", readonly=False)
    pos_whatsmeow_session_status = fields.Selection(
        related="pos_config_id.whatsmeow_session_id.status")
    pos_whatsmeow_receipt_template_id = fields.Many2one(
        related="pos_config_id.whatsmeow_receipt_template_id", readonly=False)
    pos_whatsmeow_invoice_template_id = fields.Many2one(
        related="pos_config_id.whatsmeow_invoice_template_id", readonly=False)
