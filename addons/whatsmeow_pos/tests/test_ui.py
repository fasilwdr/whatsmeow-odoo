from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon


def fake_gateway(session, method, path, payload=None, timeout=None):
    """A gateway whose phone scans the QR code as soon as it is shown."""
    if path.endswith("/start") or path.endswith("/qr"):
        return {"status": "qr", "qr": "2@pairing-code"}
    if path.endswith("/status"):
        return {"status": "connected", "jid": "447700900000@s.whatsapp.net"}
    if path.endswith("/send") or path.endswith("/send-media"):
        return {"wa_message_id": "WA-UI"}
    return {}


@tagged("post_install", "-at_install")
class TestWhatsmeowPosUi(TestPointOfSaleHttpCommon):
    _test_user_groups = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["whatsmeow.connection"].search([]).active = False
        cls.env["whatsmeow.session"].search([]).write({"status": "draft"})
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "POS GW", "base_url": "http://127.0.0.1:8081",
            "api_key": "k", "webhook_secret": "pos-ui-s",
        })
        cls.main_pos_config.whatsmeow_session_id = False

    def _start(self, tour, login):
        self.main_pos_config.with_user(self.pos_user).open_ui()
        with patch.object(type(self.env["pos.order"]), "order_receipt_generate_image",
                          return_value=b"receipt-jpg"), \
                patch.object(type(self.env["whatsmeow.session"]), "_gw",
                             autospec=True, side_effect=fake_gateway):
            self.start_pos_tour(tour, login=login)

    def _sent(self):
        return self.env["whatsmeow.message"].search([
            ("direction", "=", "out"), ("connection_id", "=", self.connection.id),
        ])

    def test_send_receipt_over_whatsapp(self):
        session = self.env["whatsmeow.session"].create({
            "name": "Shop", "code": "shop", "connection_id": self.connection.id,
            "status": "connected",
        })
        self._start("WhatsmeowPosSendTour", login="pos_user")
        message = self._sent()
        self.assertRecordValues(message, [{
            "session_id": session.id, "phone": "447700900123",
            "message_type": "image", "state": "sent",
        }])

    def test_pair_a_number_from_the_register(self):
        self._start("WhatsmeowPosPairTour", login="pos_admin")
        session = self.main_pos_config.whatsmeow_session_id
        self.assertEqual(session.code, "pos_%s" % self.main_pos_config.id)
        self.assertEqual(session.status, "connected")
        self.assertEqual(self._sent().session_id, session)
