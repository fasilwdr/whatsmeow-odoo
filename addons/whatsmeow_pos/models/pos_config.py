import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# A session in one of these will never send until someone pairs it again, so a
# point of sale holding one is as good as holding none: it gets a working
# number in its place. `disconnected` is not here on purpose — a phone that has
# lost signal comes back by itself, and swapping numbers under it would leave
# the shop's customers hearing from two different senders.
UNUSABLE_STATUSES = ("draft", "logged_out", "error")

# Pairing from the register creates a session with this key; reusing it on the
# next attempt keeps a cashier who closes the dialog twice from leaving a trail
# of half-paired sessions behind.
PAIRING_CODE = "pos_%s"


class PosConfig(models.Model):
    _inherit = "pos.config"

    whatsmeow_enabled = fields.Boolean(
        string="WhatsApp Receipts", default=True,
        help="Offer WhatsApp beside email in the Send Receipt dialog.",
    )
    whatsmeow_session_id = fields.Many2one(
        "whatsmeow.session", string="Send From", ondelete="set null",
        default=lambda self: self._whatsmeow_default_session(),
        help="The WhatsApp number receipts leave from. Filled in with a "
             "connected number when empty, and replaced by one when this "
             "number is logged out.",
    )
    whatsmeow_receipt_template_id = fields.Many2one(
        "whatsmeow.template", string="Receipt Message",
        domain=[("model", "=", "pos.order")],
        help="Text sent with the receipt image. Empty uses the default 'POS "
             "Receipt' template.",
    )
    whatsmeow_invoice_template_id = fields.Many2one(
        "whatsmeow.template", string="Invoice Message",
        domain=[("model", "=", "account.move")],
        help="Text and report sent for an invoiced order. Empty uses the "
             "default 'POS Invoice' template, which attaches the invoice PDF.",
    )

    @api.model
    def _whatsmeow_default_session(self):
        # sudo: whoever creates a point of sale need not be a WhatsApp user.
        return self.env["whatsmeow.session"].sudo().search(
            [("status", "=", "connected")], limit=1)

    # -- which number sends ---------------------------------------------------
    def _whatsmeow_pos_session(self):
        """The session this point of sale sends from, assigning one if needed.

        The assigned number when it is connected; otherwise the first connected
        number, which becomes this point of sale's own when it had none or held
        one that can no longer send. A number that is merely disconnected keeps
        its place and lends the send to another, because it will be back.

        May return a session that is not connected, or none: the caller decides
        what to tell the cashier.
        """
        self.ensure_one()
        assigned = self.sudo().whatsmeow_session_id
        if assigned.status == "connected":
            return assigned
        available = self._whatsmeow_default_session()
        if not available:
            return assigned
        if not assigned or assigned.status in UNUSABLE_STATUSES:
            self.sudo().whatsmeow_session_id = available
            _logger.info("whatsmeow_pos: %s now sends from session %s",
                         self.name, available.code)
        return available

    def _whatsmeow_refresh(self, session):
        """Ask the gateway how `session` is doing, without ever raising.

        The stored status is only as fresh as the refresh cron, and a phone that
        reconnected a minute ago should not send the cashier off to pair it.
        """
        try:
            session.action_refresh()
        except UserError as exc:
            _logger.info("whatsmeow_pos: could not refresh session %s: %s",
                         session.code, exc)

    def whatsmeow_pos_state(self):
        """What the register needs to know before offering WhatsApp."""
        self.ensure_one()
        session = self._whatsmeow_pos_session()
        if session and session.status != "connected":
            self._whatsmeow_refresh(session)
        return {
            "ready": session.status == "connected",
            "session": session.name or "",
            "can_pair": self._whatsmeow_can_pair(),
        }

    # -- pairing from the register --------------------------------------------
    def _whatsmeow_pairing_connection(self):
        return self.env["whatsmeow.connection"].sudo().search([], limit=1)

    def _whatsmeow_can_pair(self):
        """Whether this user may pair a number from the register.

        Pairing links a phone as the shop's sender — a manager's decision, not
        a cashier's — and it needs a gateway to pair against.
        """
        user = self.env.user
        allowed = user.has_group("point_of_sale.group_pos_manager") \
            or user.has_group("whatsmeow.group_whatsmeow_manager")
        return bool(allowed and self._whatsmeow_pairing_connection())

    def _whatsmeow_check_can_pair(self):
        if not self._whatsmeow_pairing_connection():
            raise UserError(self.env._(
                "There is no WhatsApp gateway to pair a number with. Add one in "
                "WhatsApp > Configuration > Gateways."))
        if not self._whatsmeow_can_pair():
            raise AccessError(self.env._(
                "Only a Point of Sale manager can connect a WhatsApp number."))

    def _whatsmeow_pairing_session(self):
        """The session to pair: this point of sale's own if it has one on an
        active gateway, else the one pairing created before, else a new one."""
        self.ensure_one()
        Session = self.env["whatsmeow.session"].sudo()
        assigned = self.sudo().whatsmeow_session_id
        if assigned and assigned.connection_id.active:
            return assigned
        connection = self._whatsmeow_pairing_connection()
        code = PAIRING_CODE % self.id
        session = Session.search(
            [("connection_id", "=", connection.id), ("code", "=", code)], limit=1)
        return session or Session.create({
            "name": self.env._("%s (Point of Sale)", self.name),
            "code": code,
            "connection_id": connection.id,
        })

    def _whatsmeow_pairing_payload(self, session):
        if session.status == "connected" and self.sudo().whatsmeow_session_id != session:
            self.sudo().whatsmeow_session_id = session
        return {
            "session_id": session.id,
            "session": session.name,
            "status": session.status,
            "qr": session.qr_image.to_base64() if session.qr_image else False,
            "error": session.last_error or False,
        }

    def whatsmeow_pos_pair_start(self):
        """Start pairing a number and return the QR code to scan."""
        self.ensure_one()
        self._whatsmeow_check_can_pair()
        session = self._whatsmeow_pairing_session()
        session.action_start()
        if session.status == "qr" and not session.qr_image:
            # The start reply announces the QR state before the code itself
            # is ready; the next status call carries it.
            session.action_refresh()
        return self._whatsmeow_pairing_payload(session)

    def whatsmeow_pos_pair_poll(self, session_id):
        """Report pairing progress: a fresh QR while waiting, the session once
        the phone has scanned it."""
        self.ensure_one()
        self._whatsmeow_check_can_pair()
        session = self.env["whatsmeow.session"].sudo().browse(session_id).exists()
        if not session:
            raise UserError(self.env._("That WhatsApp session no longer exists."))
        session.action_refresh()
        return self._whatsmeow_pairing_payload(session)
