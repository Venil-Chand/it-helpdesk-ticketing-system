"""
Turn one queued notification into a plain-text email.

Messages are built only from the fixed sentences below and the reviewed
payload fields (see events.ALLOWED_PAYLOAD_KEYS). Ticket titles,
descriptions and comment text are never included: the reader signs in to
see them, so the helpdesk's own access checks still decide who reads
ticket contents. Header values come only from the configured sender, the
validated recipient address and fixed text, so nothing a user typed can
add a header or a recipient.
"""

import hashlib
import json
from email.message import EmailMessage
from email.utils import format_datetime

from . import events, timeutil

SUBJECT_PREFIX = "[IT Helpdesk]"
SIMULATED_HEADER = "X-Helpdesk-Delivery"
SIMULATED_VALUE = "simulated; written to a local folder, not sent"

SUBJECTS = {
    events.TICKET_CREATED: "Ticket #{ticket_id} created",
    events.TICKET_ASSIGNED: "Ticket #{ticket_id} assigned to you",
    events.TICKET_UNASSIGNED: "Ticket #{ticket_id} no longer assigned to you",
    events.STAFF_COMMENT: "New reply from support on ticket #{ticket_id}",
    events.REQUESTER_COMMENT: "Requester replied on ticket #{ticket_id}",
    events.TICKET_RESOLVED: "Ticket #{ticket_id} resolved",
    events.TICKET_CLOSED: "Ticket #{ticket_id} closed",
    events.PASSWORD_CHANGED: "Your helpdesk password was changed",
    events.PASSWORD_RESET: "Your helpdesk password was reset",
    events.ROLE_CHANGED: "Your helpdesk access level changed",
    events.ACCOUNT_REACTIVATED: "Your helpdesk account was reactivated",
}

SECURITY_FOOTER = (
    "If you did not expect this change, contact IT support immediately "
    "through a channel you trust. The helpdesk will never ask for your "
    "password by email."
)

GENERAL_FOOTER = (
    "This is an automated message from the IT Helpdesk. Do not reply "
    "with passwords or other secrets."
)


def _body_lines(event_type, reason, payload):
    p = payload
    ticket = f"ticket #{p.get('ticket_id')}"
    actor = p.get("actor") or "someone"

    if event_type == events.TICKET_CREATED:
        if reason == events.REQUESTER:
            return [f"Your request was received as {ticket} "
                    f"(priority {p.get('priority')}).",
                    "Support staff will reply on the ticket page."]
        return [f"A new {ticket} was submitted by {actor} "
                f"(priority {p.get('priority')})."]

    if event_type == events.TICKET_ASSIGNED:
        return [f"{actor} assigned {ticket} to you "
                f"(priority {p.get('priority')})."]

    if event_type == events.TICKET_UNASSIGNED:
        return [f"{actor} changed the assignment of {ticket}; it is no "
                "longer assigned to you."]

    if event_type == events.STAFF_COMMENT:
        if reason == events.REQUESTER:
            return [f"Support staff member {actor} replied on your {ticket}."]
        return [f"{actor} added a note to {ticket}, which is assigned "
                "to you."]

    if event_type == events.REQUESTER_COMMENT:
        return [f"The requester, {actor}, replied on {ticket}, which is "
                "assigned to you."]

    if event_type in (events.TICKET_RESOLVED, events.TICKET_CLOSED):
        return [f"{actor} changed the status of your {ticket} from "
                f"{p.get('old_status')} to {p.get('new_status')}."]

    by = ("you" if p.get("changed_by") == events.CHANGED_BY_OWNER
          else "an administrator")

    if event_type == events.PASSWORD_CHANGED:
        return [f"The password of your helpdesk account was changed by {by}.",
                "All other signed-in sessions of your account were ended."]

    if event_type == events.PASSWORD_RESET:
        return ["The password of your helpdesk account was reset by "
                f"{by}.",
                "All of your signed-in sessions were ended. You will be "
                "asked to choose a new password the next time you sign in.",
                "The temporary password is never sent by email; it is "
                "given to you separately."]

    if event_type == events.ROLE_CHANGED:
        return [f"Your helpdesk access level was changed by {by} from "
                f"{p.get('old_role')} to {p.get('new_role')}."]

    if event_type == events.ACCOUNT_REACTIVATED:
        return [f"Your helpdesk account was reactivated by {by}. "
                "You can sign in again."]

    raise ValueError(f"no message for notification event {event_type}")


def message_id(notification_id, dedupe_key, sender):
    """
    The same Message-ID for every attempt of one notification.

    If a retry repeats a message the server had in fact accepted, some
    mail systems drop the copy because the Message-ID is already known.
    This reduces, but cannot remove, the chance of a duplicate.
    """

    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]
    domain = sender.rsplit("@", 1)[1]
    return f"<helpdesk-notification-{notification_id}-{digest}@{domain}>"


def build_message(row, *, to_address, settings, simulated):
    """An EmailMessage for a claimed notification row."""

    payload = json.loads(row["payload"] or "{}")
    event_type = row["event_type"]

    subject = SUBJECTS[event_type].format(
        ticket_id=payload.get("ticket_id", "")
    )

    if simulated:
        subject = "[SIMULATED] " + subject

    lines = [f"Hello {row['recipient_username']},", ""]
    lines += _body_lines(event_type, row["recipient_reason"], payload)
    lines.append("")

    ticket_id = payload.get("ticket_id")

    if event_type in events.TICKET_EVENTS and ticket_id is not None:
        if settings.base_url:
            lines.append(
                f"View the ticket: {settings.base_url}/tickets/{ticket_id}"
            )
        else:
            lines.append(f"Sign in to the IT Helpdesk to view ticket "
                         f"#{ticket_id}.")
        lines += ["", GENERAL_FOOTER]
    else:
        lines.append(SECURITY_FOOTER)

    message = EmailMessage()
    message["Subject"] = f"{SUBJECT_PREFIX} {subject}"
    message["From"] = settings.sender
    message["To"] = to_address
    message["Date"] = format_datetime(timeutil.utc_now())
    message["Message-ID"] = message_id(row["id"], row["dedupe_key"],
                                       settings.sender)
    message["Auto-Submitted"] = "auto-generated"

    if simulated:
        message[SIMULATED_HEADER] = SIMULATED_VALUE

    message.set_content("\n".join(lines) + "\n")
    return message
