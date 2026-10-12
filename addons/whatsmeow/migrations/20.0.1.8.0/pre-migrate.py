"""Decode the pairing QR codes stored by Odoo 19.

Up to Odoo 19 a Binary field kept in a table column (`attachment=False`) held
the *base64 text* of the file; Odoo 20 keeps the raw bytes and hands them out
as they are. `whatsmeow.session.qr_image` is the one such field here, so a
session that was waiting to be paired during the upgrade would show a broken
image until its next refresh.

Media (`whatsmeow.message.media_data`) is stored as attachments, which already
held raw bytes, and needs nothing.
"""
import logging

_logger = logging.getLogger(__name__)

# First byte of a PNG. A row that already starts with it was written by Odoo 20
# (or converted by an earlier run), and must not be decoded a second time.
PNG_MAGIC = 0x89


def migrate(cr, version):
    cr.execute("""
        UPDATE whatsmeow_session
           SET qr_image = decode(convert_from(qr_image, 'LATIN1'), 'base64')
         WHERE qr_image IS NOT NULL
           AND length(qr_image) > 0
           AND get_byte(qr_image, 0) != %s
           AND convert_from(qr_image, 'LATIN1') ~ '^[A-Za-z0-9+/=\\s]+$'
    """, [PNG_MAGIC])
    if cr.rowcount:
        _logger.info("whatsmeow: decoded the stored pairing QR of %s session(s)",
                     cr.rowcount)
