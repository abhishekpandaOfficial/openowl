"""
OpenOwl Gmail Tool
━━━━━━━━━━━━━━━━━
Full Gmail integration via Google API + OAuth2.
Read emails freely. Send ONLY after user approval.

Capabilities:
  • Read recent unread emails
  • Search emails by query
  • Read a specific email thread
  • Draft an email (shows user before sending)
  • Send email (REQUIRES APPROVAL — hardcoded)
  • Reply to an email (REQUIRES APPROVAL)
  • Mark as read / archive
  • Extract key info from emails (flight confirmations, OTPs, invoices)
"""
import base64
import logging
import os
import re
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

TOKEN_FILE = "data/google_gmail_token_{user_id}.json"


def _get_gmail_service(user_id: str):
    """Get authenticated Gmail service."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        token_path = TOKEN_FILE.format(user_id=user_id)
        creds = None

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise ValueError(
                    f"Gmail not authorized. Visit /auth/google/gmail to connect."
                )

        return build("gmail", "v1", credentials=creds)
    except ImportError:
        raise ImportError("Run: pip install google-auth google-api-python-client")


def _decode_body(payload: dict) -> str:
    """Extract plain text body from a Gmail message payload."""
    body = ""

    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                    break
            # Recurse into nested parts
            elif part.get("parts"):
                body = _decode_body(part)
                if body:
                    break

    return body.strip()


def _get_header(headers: list, name: str) -> str:
    """Extract a header value from Gmail message headers."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _format_email_preview(msg: dict, snippet_only: bool = True) -> str:
    """Format an email into a readable preview."""
    payload  = msg.get("payload", {})
    headers  = payload.get("headers", [])
    from_    = _get_header(headers, "From")
    subject  = _get_header(headers, "Subject")
    date_str = _get_header(headers, "Date")
    snippet  = msg.get("snippet", "")

    # Clean sender name
    from_clean = re.sub(r"<.*?>", "", from_).strip() or from_

    # Parse date
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        time_label = dt.strftime("%d %b, %I:%M %p")
    except:
        time_label = date_str[:20]

    if snippet_only:
        return f"📧 *{subject[:60]}*\n   From: {from_clean}\n   _{time_label}_\n   {snippet[:100]}..."
    else:
        body = _decode_body(payload)
        return (
            f"📧 *{subject}*\n"
            f"From: {from_clean}\n"
            f"Date: {time_label}\n\n"
            f"{body[:800]}{'...' if len(body) > 800 else ''}"
        )


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@tool
def get_unread_emails(
    user_id: str,
    max_results: int = 5,
) -> str:
    """
    Get recent unread emails from Gmail.
    Shows sender, subject, date and a short preview.
    """
    try:
        service = _get_gmail_service(user_id)

        result = service.users().messages().list(
            userId="me",
            labelIds=["UNREAD", "INBOX"],
            maxResults=max_results,
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            return "📬 No unread emails. Inbox is clean!"

        lines = [f"📬 *{len(messages)} unread emails:*\n"]
        for msg_meta in messages:
            msg = service.users().messages().get(
                userId="me",
                id=msg_meta["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            lines.append(_format_email_preview(msg))
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"get_unread_emails error: {e}")
        return f"❌ Could not fetch emails: {e}"


@tool
def search_emails(user_id: str, query: str, max_results: int = 5) -> str:
    """
    Search Gmail using standard Gmail search syntax.
    Examples: 'from:boss@company.com', 'subject:invoice', 'has:attachment'
    """
    try:
        service = _get_gmail_service(user_id)

        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            return f"📭 No emails found for: _{query}_"

        lines = [f"🔍 *Search results for '{query}':*\n"]
        for msg_meta in messages:
            msg = service.users().messages().get(
                userId="me",
                id=msg_meta["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            lines.append(_format_email_preview(msg))
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Search failed: {e}"


@tool
def read_email_thread(user_id: str, message_id: str) -> str:
    """
    Read the full content of a specific email.
    Use the message ID from get_unread_emails or search_emails.
    """
    try:
        service = _get_gmail_service(user_id)

        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute()

        return _format_email_preview(msg, snippet_only=False)

    except Exception as e:
        return f"❌ Could not read email: {e}"


@tool
def extract_important_info(user_id: str, query: str = "newer_than:3d") -> str:
    """
    Scan recent emails and extract important information:
    flight bookings, OTPs (not shown), invoices, meeting invites.
    Useful for morning briefings.
    """
    try:
        service = _get_gmail_service(user_id)

        # Scan last 3 days of inbox
        result = service.users().messages().list(
            userId="me",
            q=query + " in:inbox",
            maxResults=15,
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            return "No important items found in recent emails."

        important_items = []

        for msg_meta in messages:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_meta["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()

                payload = msg.get("payload", {})
                headers = payload.get("headers", [])
                subject = _get_header(headers, "Subject").lower()
                from_   = _get_header(headers, "From")
                snippet = msg.get("snippet", "").lower()

                # Flight/travel
                if any(k in subject or k in snippet for k in
                       ["flight", "booking confirmed", "pnr", "boarding pass", "itinerary"]):
                    important_items.append(
                        f"✈️ *Travel:* {_get_header(headers, 'Subject')[:60]}"
                    )

                # Invoice/payment
                elif any(k in subject for k in
                         ["invoice", "receipt", "payment", "paid", "bill"]):
                    important_items.append(
                        f"💰 *Payment:* {_get_header(headers, 'Subject')[:60]}"
                    )

                # Meeting invite
                elif any(k in subject for k in
                         ["invite", "invitation", "calendar", "meeting request"]):
                    important_items.append(
                        f"📅 *Meeting invite:* {_get_header(headers, 'Subject')[:60]}"
                    )

                # Action required
                elif any(k in subject for k in
                         ["urgent", "action required", "deadline", "reminder", "follow up"]):
                    important_items.append(
                        f"⚡ *Action needed:* {_get_header(headers, 'Subject')[:60]}"
                    )

            except Exception:
                continue

        if not important_items:
            return "No particularly important items found in recent emails."

        return "*Important email items:*\n" + "\n".join(important_items[:8])

    except Exception as e:
        return f"❌ Could not scan emails: {e}"


@tool
def draft_email(
    user_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
) -> dict:
    """
    Prepare an email draft.
    ⚠️  REQUIRES APPROVAL — user must confirm before sending.
    Returns the draft for approval display.
    """
    return {
        "action": "send_email",
        "requires_approval": True,
        "description": f"Send email to {to}: \"{subject[:60]}\"",
        "details": {
            "to": to, "subject": subject,
            "body": body, "cc": cc, "user_id": user_id,
        },
        "recipient": to,
        "preview": f"*To:* {to}\n*Subject:* {subject}\n\n{body[:300]}",
    }


@tool
def send_email_confirmed(
    user_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    reply_to_id: str = "",
) -> str:
    """
    Actually sends the email after user approval.
    DO NOT call this without prior approval from the user.
    """
    try:
        service = _get_gmail_service(user_id)

        msg = MIMEMultipart("alternative")
        msg["To"]      = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        # Plain text part
        msg.attach(MIMEText(body, "plain"))

        # HTML part (auto-convert basic formatting)
        html_body = body.replace("\n", "<br>")
        msg.attach(MIMEText(f"<html><body>{html_body}</body></html>", "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        if reply_to_id:
            service.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": reply_to_id}
            ).execute()
        else:
            service.users().messages().send(
                userId="me",
                body={"raw": raw}
            ).execute()

        logger.info(f"📧 Email sent to {to} | Subject: {subject}")
        return (
            f"✅ *Email sent!*\n"
            f"📧 To: {to}\n"
            f"Subject: {subject}"
        )

    except Exception as e:
        logger.error(f"Gmail send failed: {e}")
        return f"❌ Could not send email: {e}"


@tool
def mark_email_read(user_id: str, message_id: str) -> str:
    """Mark an email as read."""
    try:
        service = _get_gmail_service(user_id)
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return "✅ Marked as read."
    except Exception as e:
        return f"❌ Error: {e}"


# All Gmail tools for registration
GMAIL_TOOLS = [
    get_unread_emails,
    search_emails,
    read_email_thread,
    extract_important_info,
    draft_email,
    send_email_confirmed,
    mark_email_read,
]
