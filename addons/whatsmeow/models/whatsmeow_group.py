import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WhatsmeowGroup(models.Model):
    """A WhatsApp group one of our numbers is a member of.

    A group has no phone number: its JID (`120363…@g.us`) is the only address it
    has, and nobody can be expected to know one. This is the directory that lets
    an operator pick a group by its subject instead — filled from the gateway's
    listing (`Synchronise Groups` on the session) and topped up by every group
    message that arrives.

    Scoped to a session rather than global: the same group reached from two of
    our numbers is two memberships, and a send has to leave from the number
    that is actually in it.
    """
    _name = "whatsmeow.group"
    _description = "Whatsmeow Group Chat"
    _order = "name, id"

    name = fields.Char(
        required=True,
        help="The group's subject in WhatsApp. Falls back to the JID until the "
             "gateway has told us what the group is called.",
    )
    jid = fields.Char(
        string="Chat JID", required=True, readonly=True, index=True,
        help="The group's address — what a message to it is sent to.",
    )
    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
        readonly=True,
    )
    participant_count = fields.Integer(string="Participants", readonly=True)
    is_announce = fields.Boolean(
        string="Admins Only", readonly=True,
        help="Only the group's admins may post. A message sent here is refused "
             "by WhatsApp unless this number is one of them.",
    )
    is_community = fields.Boolean(
        string="Community", readonly=True,
        help="The parent of a community: a container for its groups rather "
             "than a chat. Send to the community's announcement group or to "
             "one of its groups instead.",
    )
    active = fields.Boolean(
        default=True,
        help="Archived when the gateway's listing no longer includes the group "
             "— the number has left it, or was removed.",
    )
    last_sync = fields.Datetime(
        string="Last Synchronised", readonly=True,
        help="When the gateway last listed this group. Empty for a group known "
             "only from a message it sent us.",
    )
    message_count = fields.Integer(compute="_compute_message_count")

    _session_jid_uniq = models.Constraint(
        "UNIQUE (session_id, jid)",
        "This group is already listed for the session.",
    )

    @api.depends("name", "session_id")
    @api.depends_context("whatsmeow_group_show_session")
    def _compute_display_name(self):
        show_session = self.env.context.get("whatsmeow_group_show_session")
        for rec in self:
            name = rec.name or rec.jid or ""
            if show_session and rec.session_id:
                name = f"{name} ({rec.session_id.name})"
            rec.display_name = name

    def _compute_message_count(self):
        # Counted by address, not by `group_id`: messages received before this
        # directory existed belong to the group just as much, and only their
        # chat JID says so.
        Message = self.env["whatsmeow.message"]
        for rec in self:
            rec.message_count = Message.search_count(rec._message_domain())

    def _message_domain(self):
        self.ensure_one()
        return [("session_id", "=", self.session_id.id), ("chat_jid", "=", self.jid)]

    # -- keeping the directory current ----------------------------------------
    @api.model
    def _find(self, session, jid):
        """The group's row, archived or not: a group we were removed from and
        then re-added to must come back as the same record, not a second one."""
        return self.with_context(active_test=False).search(
            [("session_id", "=", session.id), ("jid", "=", jid)], limit=1)

    @api.model
    def _upsert_from_inbound(self, session, jid, name):
        """Note a group that has just sent us a message.

        The listing is only as fresh as the last time someone pressed the
        button; a message is proof, right now, that the group exists and that
        we are in it. Never raises — recording the group must not cost us the
        message that mentioned it.
        """
        jid = (jid or "").strip()
        if not jid:
            return self.browse()
        name = (name or "").strip()
        group = self._find(session, jid)
        if group:
            vals = {}
            if name and name != group.name:
                vals["name"] = name
            if not group.active:
                vals["active"] = True
            if vals:
                group.write(vals)
            return group
        try:
            # Two messages from a new group can arrive together; only the
            # unique constraint can settle who creates the row.
            with self.env.cr.savepoint():
                group = self.create({
                    "session_id": session.id, "jid": jid, "name": name or jid,
                })
                group.flush_recordset()
        except IntegrityError:
            _logger.info("whatsmeow: concurrent group %s settled by the database", jid)
            group = self._find(session, jid)
        return group

    @api.model
    def _sync_from_gateway(self, session, rows):
        """Mirror the gateway's listing of the groups `session` belongs to.

        The listing is the whole truth at the moment it is taken, so a group it
        omits is one this number is no longer in: it is archived rather than
        deleted, because the messages already exchanged with it still point
        here.
        """
        now = fields.Datetime.now()
        known = {
            group.jid: group
            for group in self.with_context(active_test=False).search(
                [("session_id", "=", session.id)])
        }
        seen = set()
        to_create = []
        for row in rows:
            jid = (row.get("jid") or "").strip()
            if not jid or jid in seen:
                continue
            seen.add(jid)
            vals = {
                "name": (row.get("name") or "").strip() or jid,
                "participant_count": row.get("participants") or 0,
                "is_announce": bool(row.get("announce")),
                "is_community": bool(row.get("community")),
                "active": True,
                "last_sync": now,
            }
            if jid in known:
                known[jid].write(vals)
            else:
                to_create.append({**vals, "session_id": session.id, "jid": jid})
        if to_create:
            self.create(to_create)
        gone = self.browse([g.id for jid, g in known.items()
                            if jid not in seen and g.active])
        if gone:
            gone.write({"active": False})
        return len(seen)

    # -- actions --------------------------------------------------------------
    def action_send_message(self):
        """Open a new outgoing message addressed to this group."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Message %s", self.display_name),
            "res_model": "whatsmeow.message",
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {
                "default_session_id": self.session_id.id,
                "default_direction": "out",
                "default_message_type": "text",
                "default_group_id": self.id,
                "default_chat_jid": self.jid,
                "default_chat_name": self.name,
            },
        }

    def action_view_messages(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.display_name,
            "res_model": "whatsmeow.message",
            "view_mode": "list,form",
            "domain": self._message_domain(),
            "context": {
                "default_session_id": self.session_id.id,
                "default_direction": "out",
                "default_group_id": self.id,
            },
        }
