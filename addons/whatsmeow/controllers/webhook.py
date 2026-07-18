import json
import logging

from psycopg2 import IntegrityError

from odoo import http
from odoo.http import request

from ..models.whatsmeow_message import chat_type_for_jid
from ..models.whatsmeow_match_mixin import _phone_tail

_logger = logging.getLogger(__name__)

SESSION_EVENT_STATUS = {
    "session.paired": "connected",
    "session.connected": "connected",
    "session.disconnected": "disconnected",
    "session.logged_out": "logged_out",
}


class WhatsmeowWebhook(http.Controller):
    """Receives gateway events. Routing is by webhook secret -> connection -> session,
    so several gateways can post to the same Odoo without colliding."""

    @http.route("/whatsmeow/webhook", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def webhook(self, **kwargs):
        env = request.env(su=True)
        secret = request.httprequest.headers.get("X-Webhook-Secret")
        connection = env["whatsmeow.connection"].search(
            [("webhook_secret", "=", secret)], limit=1) if secret else None
        if not connection:
            return request.make_json_response({"error": "unauthorized"}, status=401)

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return request.make_json_response({"error": "bad json"}, status=400)

        session = env["whatsmeow.session"].search(
            [("code", "=", payload.get("session")),
             ("connection_id", "=", connection.id)], limit=1)
        if not session:
            return request.make_json_response({"error": "unknown session"}, status=404)

        event = payload.get("event")
        data = payload.get("data") or {}

        if event == "message.received":
            self._on_message(env, session, data)
        elif event == "message.reaction":
            self._on_reaction(env, session, data)
        elif event == "message.receipt":
            self._on_receipt(env, data)
        elif event in SESSION_EVENT_STATUS:
            vals = {"status": SESSION_EVENT_STATUS[event], "qr_image": False}
            if event == "session.logged_out":
                vals["last_error"] = data.get("reason") or "logged out"
            session.write(vals)
        else:
            _logger.info("whatsmeow: ignoring unknown event %r", event)

        return request.make_json_response({"status": "ok"})

    def _find_existing(self, env, wa_id):
        if not wa_id:
            return env["whatsmeow.message"]
        return env["whatsmeow.message"].search(
            [("wa_message_id", "=", wa_id), ("direction", "=", "in")], limit=1)

    def _find_quoted(self, env, session, quoted_id):
        """The message an inbound reply quotes, if we have it.

        Either direction: a contact commonly replies to *our* outgoing message.
        Scoped to the session, since a WhatsApp id is only unique within one.
        Missing is normal — the quoted message may predate this install, or
        have been rejected by the inbound filter — so this returns an empty
        recordset rather than complaining.
        """
        if not quoted_id:
            return env["whatsmeow.message"]
        return env["whatsmeow.message"].search([
            ("session_id", "=", session.id),
            ("wa_message_id", "=", quoted_id),
        ], limit=1)

    def _media_vals(self, media):
        """The bytes stay on the gateway; a cron collects them so a big file
        can't hold this webhook (and its transaction) open."""
        return {
            "message_type": media.get("kind") or "document",
            "media_filename": media.get("filename") or False,
            "media_mimetype": media.get("mimetype") or False,
            "media_size": media.get("size") or 0,
            "media_duration": media.get("seconds") or 0,
            "is_voice_note": bool(media.get("ptt")),
            "media_state": "pending",
        }

    def _on_message(self, env, session, data):
        wa_id = data.get("wa_message_id")
        existing = self._find_existing(env, wa_id)
        if existing:
            # Webhook retries and WhatsApp's own double-delivery both land here.
            return self._merge_duplicate(existing, data)
        # sender_phone is empty when WhatsApp only gave us a LID; don't try to
        # match a partner on it, and never store the LID as if it were a phone.
        phone = (data.get("sender_phone") or "").strip()
        partner = env["whatsmeow.message"]._find_partner(phone) if phone else \
            env["res.partner"]
        body = data.get("body") or ""
        media = data.get("media") or {}

        # Per-session inbound filter (§9). Runs after the partner is resolved
        # and before create, so a rejected message stores nothing: no row to
        # collide on, no media to fetch, no chatter post. Dropping a fresh
        # message (never an already-stored duplicate) also means a rejected
        # placeholder simply never exists, so the real copy that follows is
        # judged on its own body rather than merged into a stub.
        facts = {
            "chat_type": chat_type_for_jid(data.get("chat_jid")),
            "message_type": (media.get("kind") or "document") if media else "text",
            "partner_id": partner.id or False,
            "sender_state": "existing" if partner.id else "new",
            "chat_jid": data.get("chat_jid") or "",
            "phone_tail": _phone_tail(phone),
            "sender_lid": data.get("sender_lid") or "",
            "body": body.lower(),
            "is_placeholder": bool(data.get("placeholder")),
        }
        if session._inbound_decision(facts) == "reject":
            _logger.info("whatsmeow: session %s dropped inbound %s by filter",
                         session.code, wa_id)
            return

        vals = {
            "session_id": session.id,
            "partner_id": partner.id or False,
            "phone": phone,
            "sender_lid": data.get("sender_lid") or False,
            # sender_jid is the participant, chat_jid the conversation; they only
            # differ in a group, and a reply has to be addressed to the chat.
            "sender_jid": data.get("sender_jid") or False,
            "chat_jid": data.get("chat_jid") or False,
            "chat_name": data.get("chat_name") or False,
            "push_name": data.get("push_name") or False,
            "direction": "in",
            "state": "received",
            "body": body,
            "wa_message_id": wa_id,
            "is_placeholder": bool(data.get("placeholder")),
        }
        quoted = self._find_quoted(env, session, data.get("quoted_id"))
        if quoted:
            vals["reply_to_id"] = quoted.id
        if media:
            vals.update(self._media_vals(media))
        try:
            # Two retries can arrive at the same instant: both search, both find
            # nothing, both insert. Only the unique index can settle that, so
            # let it, and treat the loser as the duplicate it is.
            with env.cr.savepoint():
                msg = env["whatsmeow.message"].create(vals)
                msg.flush_recordset()
        except IntegrityError:
            existing = self._find_existing(env, wa_id)
            _logger.info("whatsmeow: concurrent duplicate of %s settled by the "
                         "database", wa_id)
            return self._merge_duplicate(existing, data) if existing else None

        # Marked here, on the create path only, so the receipt goes out once per
        # message: a retry or WhatsApp's second copy lands in _merge_duplicate
        # and must not tick again. No-op unless the session opted in.
        msg._mark_read()

        if media:
            return  # delivery runs once the media has been fetched
        msg._deliver_inbound()

    def _merge_duplicate(self, existing, data):
        """Reconcile a second copy of a message we already have.

        Normally there is nothing to do — the gateway retried and we are
        idempotent. But WhatsApp also delivers some messages twice, the first
        copy empty, and the gateway flags that one as a placeholder. Keeping
        whichever landed first would mean keeping the empty one and losing the
        real text, so let the real copy replace the stand-in.
        """
        if not existing.is_placeholder or data.get("placeholder"):
            return  # a plain retry, or another empty copy: nothing to gain
        media = data.get("media") or {}
        vals = {"body": data.get("body") or "", "is_placeholder": False}
        # The empty first copy carries no ContextInfo, so the quote only shows
        # up on the real one.
        quoted = self._find_quoted(
            existing.env, existing.session_id, data.get("quoted_id"))
        if quoted:
            vals["reply_to_id"] = quoted.id
        if media:
            vals.update(self._media_vals(media))
        existing.write(vals)
        _logger.info("whatsmeow: real copy of %s replaced its placeholder",
                     existing.wa_message_id)
        if not media:
            # The placeholder never reached the chatter/channel with anything
            # useful; deliver the real text now.
            existing._deliver_inbound()

    def _on_reaction(self, env, session, data):
        """Apply an inbound WhatsApp reaction to the message it targets.

        A reaction is not a message — it annotates one — so it stores no row of
        its own: it finds the target and hands off to `_apply_reaction`. Core
        has no live place to show a reaction, so that seam does nothing until
        the `whatsmeow_discuss` bridge overrides it to react on the channel
        bubble. An empty emoji means the sender removed their reaction.
        """
        target_id = data.get("target_id")
        if not target_id:
            return
        target = env["whatsmeow.message"].search([
            ("session_id", "=", session.id),
            ("wa_message_id", "=", target_id),
        ], limit=1)
        if not target:
            return  # a reaction to a message we never stored (or already gone)
        phone = (data.get("sender_phone") or "").strip()
        partner = env["whatsmeow.message"]._find_partner(phone) if phone else \
            env["res.partner"]
        target._apply_reaction(data.get("emoji") or "", partner)

    def _on_receipt(self, env, data):
        state = "read" if data.get("receipt_type") == "read" else "delivered"
        wa_ids = data.get("wa_message_ids") or []
        if not wa_ids:
            return
        env["whatsmeow.message"].search([
            ("wa_message_id", "in", wa_ids),
            ("direction", "=", "out"),
        ]).write({"state": state})
