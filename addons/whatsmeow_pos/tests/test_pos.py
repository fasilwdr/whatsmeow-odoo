from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged("post_install", "-at_install")
class TestWhatsmeowPos(TestPoSCommon):
    _test_user_groups = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The dev database carries real gateways and numbers from earlier runs;
        # out of the way, so "the first connected number" means one of ours.
        cls.env["whatsmeow.connection"].search([]).active = False
        cls.env["whatsmeow.session"].search([]).write({"status": "draft"})
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "POS GW", "base_url": "http://127.0.0.1:8081",
            "api_key": "k", "webhook_secret": "pos-s",
        })
        cls.shop = cls.env["whatsmeow.session"].create({
            "name": "Shop", "code": "shop", "connection_id": cls.connection.id,
        })
        cls.spare = cls.env["whatsmeow.session"].create({
            "name": "Spare", "code": "spare", "connection_id": cls.connection.id,
        })
        cls.alice = cls.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        cls.cashier = new_test_user(
            cls.env, login="wm_cashier", groups="point_of_sale.group_pos_user")

    def setUp(self):
        super().setUp()
        self.config = self.basic_config
        self.config.whatsmeow_session_id = False
        self.product = self.create_product("Product 1", self.categ_basic, 10.0, 5)

    def _order(self, **kwargs):
        self.open_new_session()
        orders = self._create_orders([{
            "pos_order_lines_ui_args": [(self.product, 1)],
            "customer": self.alice,
            **kwargs,
        }])
        return next(iter(orders.values()))

    def _send(self, order, phone="+44 7700 900123", gw=None):
        gw = gw or {"return_value": {"wa_message_id": "WA1"}}
        Session = type(self.env["whatsmeow.session"])
        with patch.object(type(self.env["pos.order"]), "order_receipt_generate_image",
                          return_value=b"receipt-jpg"), \
                patch.object(type(self.env["ir.actions.report"]), "_render_qweb_pdf",
                             return_value=(b"%PDF-invoice", "pdf")), \
                patch.object(Session, "_gw", **gw):
            return order.action_send_receipt_whatsmeow(phone)

    def _sent(self, order):
        return self.env["whatsmeow.message"].search([
            ("direction", "=", "out"), ("session_id", "in", (self.shop | self.spare).ids),
        ], order="id")

    # -- which number sends ---------------------------------------------------
    def test_assigns_a_connected_number_when_none_is_set(self):
        self.shop.status = "connected"
        self.assertEqual(self.config._whatsmeow_pos_session(), self.shop)
        self.assertEqual(self.config.whatsmeow_session_id, self.shop)

    def test_replaces_a_logged_out_number(self):
        self.config.whatsmeow_session_id = self.spare
        self.spare.status = "logged_out"
        self.shop.status = "connected"
        self.assertEqual(self.config._whatsmeow_pos_session(), self.shop)
        self.assertEqual(self.config.whatsmeow_session_id, self.shop)

    def test_a_disconnected_number_keeps_its_place(self):
        # It will be back; another number covers the send meanwhile.
        self.config.whatsmeow_session_id = self.spare
        self.spare.status = "disconnected"
        self.shop.status = "connected"
        self.assertEqual(self.config._whatsmeow_pos_session(), self.shop)
        self.assertEqual(self.config.whatsmeow_session_id, self.spare)

    def test_new_point_of_sale_defaults_to_a_connected_number(self):
        self.shop.status = "connected"
        config = self.env["pos.config"].create({"name": "Kiosk"})
        self.assertEqual(config.whatsmeow_session_id, self.shop)

    # -- sending --------------------------------------------------------------
    def test_sends_the_receipt_image_to_the_typed_number(self):
        self.shop.status = "connected"
        order = self._order()
        result = self._send(order, phone="+44 7700 900999")
        self.assertEqual(result, {"sent": 1})
        message = self._sent(order)
        self.assertEqual(len(message), 1)
        self.assertRecordValues(message, [{
            "phone": "447700900999",
            "state": "sent",
            "message_type": "image",
            "media_mimetype": "image/jpeg",
            "source_res_model": "pos.order",
            "source_res_id": order.id,
        }])
        self.assertIn(order.pos_reference, message.body)
        self.assertEqual(message.media_data.content, b"receipt-jpg")
        self.assertTrue(message.mail_message_id, "the send is logged on the order")

    def test_invoiced_order_also_sends_the_invoice_pdf(self):
        self.shop.status = "connected"
        order = self._order(is_invoiced=True)
        self.assertTrue(order.account_move)
        self.assertEqual(self._send(order), {"sent": 2})
        receipt, invoice = self._sent(order)
        self.assertEqual(receipt.message_type, "image")
        self.assertRecordValues(invoice, [{
            "message_type": "document",
            "media_mimetype": "application/pdf",
            "source_res_model": "account.move",
            "source_res_id": order.account_move.id,
        }])
        self.assertIn(order.account_move.name, invoice.body)
        self.assertTrue(invoice.media_filename.endswith(".pdf"))

    def test_a_refused_send_is_reported_and_kept(self):
        self.shop.status = "connected"
        order = self._order()
        result = self._send(order, gw={"side_effect": UserError("gateway down")})
        self.assertIn("gateway down", result["error"])
        self.assertEqual(self._sent(order).state, "error")

    def test_cashier_without_whatsapp_rights_can_send(self):
        self.shop.status = "connected"
        order = self._order()
        self.assertEqual(self._send(order.with_user(self.cashier)), {"sent": 1})

    def test_refuses_without_a_connected_number(self):
        order = self._order()
        with self.assertRaises(UserError):
            self._send(order)
        self.assertFalse(self._sent(order))

    def test_refuses_when_turned_off(self):
        self.shop.status = "connected"
        self.config.whatsmeow_enabled = False
        with self.assertRaises(UserError):
            self._send(self._order())

    # -- pairing from the register --------------------------------------------
    def test_state_offers_pairing_to_a_manager_only(self):
        Session = type(self.env["whatsmeow.session"])
        with patch.object(Session, "_gw", return_value={"status": "disconnected"}):
            state = self.config.whatsmeow_pos_state()
            self.assertEqual(state["ready"], False)
            self.assertEqual(state["can_pair"], True)
            cashier_state = self.config.with_user(self.cashier).whatsmeow_pos_state()
        self.assertEqual(cashier_state["can_pair"], False)

    def test_no_pairing_without_a_gateway(self):
        self.connection.active = False
        self.assertFalse(self.config.whatsmeow_pos_state()["can_pair"])
        with self.assertRaises(UserError):
            self.config.whatsmeow_pos_pair_start()

    def test_cashier_cannot_pair(self):
        with self.assertRaises(AccessError):
            self.config.with_user(self.cashier).whatsmeow_pos_pair_start()

    def test_pairing_shows_the_qr_then_assigns_the_number(self):
        Session = type(self.env["whatsmeow.session"])
        with patch.object(Session, "_gw", return_value={"status": "qr", "qr": "2@pairing"}):
            started = self.config.whatsmeow_pos_pair_start()
        session = self.env["whatsmeow.session"].browse(started["session_id"])
        self.assertEqual(session.code, "pos_%s" % self.config.id)
        self.assertEqual(session.connection_id, self.connection)
        self.assertEqual(started["status"], "qr")
        self.assertTrue(started["qr"])
        self.assertFalse(self.config.whatsmeow_session_id, "not assigned until it pairs")

        with patch.object(Session, "_gw", return_value={"status": "qr", "qr": "2@again"}):
            again = self.config.whatsmeow_pos_pair_start()
        self.assertEqual(again["session_id"], session.id, "a retry reuses the session")

        with patch.object(Session, "_gw", return_value={"status": "connected", "jid": "1@s"}):
            polled = self.config.whatsmeow_pos_pair_poll(session.id)
        self.assertEqual(polled["status"], "connected")
        self.assertEqual(self.config.whatsmeow_session_id, session)

    def test_pairing_reuses_the_assigned_number(self):
        self.config.whatsmeow_session_id = self.spare
        self.spare.status = "logged_out"
        Session = type(self.env["whatsmeow.session"])
        with patch.object(Session, "_gw", return_value={"status": "qr", "qr": "2@x"}):
            started = self.config.whatsmeow_pos_pair_start()
        self.assertEqual(started["session_id"], self.spare.id)
